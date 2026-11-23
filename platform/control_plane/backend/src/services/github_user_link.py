"""Per-user GitHub account links: an Entra human authorizes AGP to act on GitHub as them.

Composes the OAuth transport in :mod:`services.github_user_oauth` with storage and owns the token's
whole lifecycle (mint, refresh, verify, revoke). Every seam is injected, so this module holds no
``httpx`` response parsing, no FastAPI and no wall-clock read. :meth:`get_user_bearer_token` is a
separate ``(principal_oid, connection_id)`` seam rather than a third ``ConnectionAuthType`` on the
connection service, which resolves per-org records and never sees a principal.

Storage: one :class:`core.state_store.StateStore` over the connections table; link metadata is a
partition (``pk="github_user_link"``, ``sk="<principal_oid>#<connection_id>"``) and the short-lived
CSRF/PKCE state is a second partition, written once and consumed by one conditional delete, expired
by the application. The ``ghu_``/``ghr_`` pair lives only in Secrets Manager under
``<secret_prefix><link_id>`` with body ``{"access_token", "refresh_token"}``; the row and every read
model carry ``secret_arn``, a pointer. No token, ``client_secret``, authorization ``code`` or provider
body ever reaches a log line, a :class:`GitHubLinkError` message or an HTTP body, and every store
fault (``core.state_store.STORE_FAULTS``) is translated at its call site into this service's own
retryable kind.

Refresh is claim-then-rotate. GitHub's refresh is destructive and single-use, and AGP runs two ECS
tasks, so a rotation is claimed with the store's ``put_if_version`` on ``token_version`` before
GitHub is ever called, which exactly one caller wins. ``token_version`` is the row's
optimistic-concurrency token: a re-link is conditional on the version it read and writes the next
one, and every other whole-row writer goes through :meth:`GitHubUserLinkService._save_guarded`,
which refuses a row that moved or is gone (a link deleted mid-rotation is never recreated by a late
release). A first link is ``put_new``, conditional on the row's absence, and ``verify_link`` writes
its label with the store's ``update_existing``, which never mentions ``status`` or
``token_version``. A loser re-reads once and trusts the published expiry, not the version bump. A
claim left ``REFRESHING`` past ``_CLAIM_TIMEOUT_SECONDS`` is abandoned only after a ``GET /user``
read proves the stored pair dead. Rotations are never retried.

Revocation is discovered on use (a 401 persists ``UNLINKED``), and :meth:`unlink` revokes at GitHub
before purging anything locally, raising rather than reporting success when the revoke failed. The
service never loads a ``Connection``: it takes two callables. ``client_credentials_loader``'s
deterministic failures (``bad_request``, ``not_found``) are ``oauth_client_missing`` and its
transient ones the retryable ``secret_error``, because ``oauth_client_missing`` is the one thing
``unlink`` treats as licence to purge without revoking. ``connection_org_loader`` answers the org a
link is on; a connection that is gone is ``connection_missing``, which renders as the link not found.
"""

from __future__ import annotations

import json
import logging
import secrets
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from typing import Callable, ClassVar, List, Optional
from urllib.parse import urlparse
from uuid import uuid4

import httpx

from core import aws_clients
from core.errors import AppError, Outcome
from core.state_store import STORE_FAULTS, StateStore, VersionMismatch
from models.github_link import GitHubUserLink, LinkStatus
from services.github_user_oauth import (
    GitHubOAuthError,
    build_authorize_url,
    build_pkce_challenge,
)
from services.github_user_oauth import exchange_code as _exchange_code
from services.github_user_oauth import fetch_user_identity as _fetch_user_identity
from services.github_user_oauth import refresh_user_token as _refresh_user_token
from services.github_user_oauth import revoke_grant as _revoke_grant

logger = logging.getLogger(__name__)

LINK_CALLBACK_PATH = "/ops/github-link/callback"

_PARTITION_KEY = "github_user_link"
_STATE_PARTITION_KEY = "github_link_state"
_SK_SEPARATOR = "#"

_STATE_TTL_SECONDS = 900
_REFRESH_SKEW_SECONDS = 300
_CLAIM_TIMEOUT_SECONDS = 60

_SECRET_TAGS = [
    {"Key": "managed_by", "Value": "agp"},
    {"Key": "purpose", "Value": "github-user-link"},
]

_OAUTH_KIND_MAP = {"bad_grant": "bad_request", "revoked": "link_revoked"}
_PERMANENT_LOADER_KINDS = frozenset({"bad_request", "not_found"})


class GitHubLinkError(AppError):
    """A GitHub user-link operation failed. The message is fixed text (never a token, the
    ``client_secret``, the authorization ``code``, the PKCE verifier or a provider body) and the
    client sees the fixed detail of the kind. No kind is a 401: the SPA's interceptor turns a 401
    into a sign-out. ``refresh_in_progress`` (ordinary two-task contention on the claim) and
    ``secret_error`` (a transient store fault) are retryable, not terminal. ``connection_missing``
    (the link's connection is gone) shares ``not_found``'s wire: a link cannot exist on a connection
    that does not, and the console keys on that detail. ``not_linked`` is the pull-request routes'
    refusal of a caller with no live link on the repository's connection, a remedy the UI offers;
    this service answers ``not_found`` for the same absence. ``changed`` is a link write whose row
    moved or was deleted after its read."""

    domain: ClassVar[str] = "github_link"
    kinds: ClassVar[Mapping[str, Outcome]] = {
        "not_found": Outcome(404, "GitHub link not found"),
        "connection_missing": Outcome(404, "GitHub link not found"),
        "not_linked": Outcome(409, "connect your GitHub account to act on pull requests"),
        "bad_request": Outcome(400, "Invalid request"),
        "conflict": Outcome(409, "That GitHub account is already linked to another user"),
        "oauth_client_missing": Outcome(
            409, "This org connection has no GitHub OAuth client — ask an admin to add one"
        ),
        "refresh_in_progress": Outcome(409, "GitHub token refresh in progress — retry"),
        "link_revoked": Outcome(409, "Your GitHub authorization was revoked — reconnect your account"),
        "provider_error": Outcome(502, "GitHub request failed"),
        "secret_error": Outcome(502, "Secret store operation failed"),
        "changed": Outcome(409, "GitHub link changed while this request ran; reload and retry"),
    }

    def __init__(self, message: str, kind: str) -> None:
        super().__init__(message, kind)


class _ClaimLost(Exception):
    """Internal: another task won the refresh claim, or the row is gone (the store's version guard
    refused the write). Never escapes :meth:`GitHubUserLinkService.get_user_bearer_token`."""


class _LinkLost(Exception):
    """Internal: a link write found its row taken, moved or gone (the store's guard refused the
    write). Never escapes :meth:`GitHubUserLinkService.complete_link`."""


class GitHubUserLinkService:
    def __init__(
        self,
        *,
        table_name: str = "",
        secret_prefix: str = "",
        region: str | None = None,
        allowed_origins: tuple[str, ...] = (),
        client_credentials_loader: Callable[[str], tuple[str, str]],
        connection_org_loader: Callable[[str], str],
        secrets_client=None,
        new_id=lambda: str(uuid4()),
        new_verifier=lambda: secrets.token_urlsafe(32),
        now=lambda: datetime.now(timezone.utc),
        exchange_code=_exchange_code,
        refresh_user_token=_refresh_user_token,
        fetch_user_identity=_fetch_user_identity,
        revoke_grant=_revoke_grant,
    ) -> None:
        self.table_name = table_name
        self.secret_prefix = secret_prefix
        self.region = region
        self._allowed_origins = tuple(o.lower().rstrip("/") for o in allowed_origins)
        if not self._allowed_origins:
            logger.warning(
                "[github-link] constructed with NO allowed_origins: a redirect_uri's ORIGIN will "
                "not be checked, only its path and scheme. Wire allowed_origins (the route passes "
                "CORS_ORIGINS)"
            )
        self._client_credentials_loader = client_credentials_loader
        self._connection_org_loader = connection_org_loader
        self._new_id = new_id
        self._new_verifier = new_verifier
        self._now = now
        self._exchange_code = exchange_code
        self._refresh_user_token = refresh_user_token
        self._fetch_user_identity = fetch_user_identity
        self._revoke_grant = revoke_grant

        self._sm = secrets_client or aws_clients.client("secretsmanager", region=region)
        self._store = StateStore(table_name, region=region)

    def begin_link(self, principal_oid: str, connection_id: str, redirect_uri: str) -> tuple[str, str]:
        """Start the web flow: mint PKCE and CSRF state and return ``(authorize_url, state)``.

        A falsy ``principal_oid`` is refused: a link with no Entra subject is unattributable. The
        connection's OAuth client is resolved first, so an org that still needs the one-time admin
        paste fails before any state is persisted."""
        self._validate_ids(principal_oid, connection_id)
        client_id, _client_secret = self._load_credentials(connection_id)
        self._validate_redirect_uri(redirect_uri)

        verifier = self._new_verifier()
        state = self._new_id()
        self._save_state(
            state,
            {
                "principal_oid": principal_oid,
                "connection_id": connection_id,
                "redirect_uri": redirect_uri,
                "code_verifier": verifier,
                "exp": int(self._now().timestamp()) + _STATE_TTL_SECONDS,
            },
        )
        url = build_authorize_url(
            client_id=client_id,
            redirect_uri=redirect_uri,
            state=state,
            code_challenge=build_pkce_challenge(verifier),
        )
        return url, state

    def complete_link(self, principal_oid: str, code: str, state: str) -> GitHubUserLink:
        """Finish the web flow: consume the state, exchange the code, prove the identity and
        persist the link.

        The state is consumed in one conditional delete before it is validated, so a replayed or
        expired state is never spent twice, and the session that finishes must be the session that
        started. The exchange uses the state's ``redirect_uri`` and ``code_verifier``, never
        anything from this request. The secret is created before the row; a row write that faulted
        revokes at GitHub and deletes the secret rather than orphaning a live pair no row points
        at; a lost write deletes the secret and is ``changed``. A re-link writes one
        ``token_version`` past the row it read, so a guarded write still expecting the replaced
        row's version cannot land on the new one."""
        record = self._delete_state(state)

        if record is None or int(record.get("exp") or 0) < int(self._now().timestamp()):
            raise GitHubLinkError("The link request expired or is unknown", kind="bad_request")
        if not principal_oid or record.get("principal_oid") != principal_oid:
            raise GitHubLinkError("The link request does not belong to this user", kind="bad_request")

        connection_id = str(record.get("connection_id") or "")
        client_id, client_secret = self._load_credentials(connection_id)

        with _http_client() as client:
            try:
                tokens = self._exchange_code(
                    client_id=client_id,
                    client_secret=client_secret,
                    code=code,
                    redirect_uri=record["redirect_uri"],
                    code_verifier=record["code_verifier"],
                    client=client,
                )
            except GitHubOAuthError as exc:
                raise self._from_oauth_error(exc, "Could not complete the GitHub authorization") from None
            try:
                identity = self._fetch_user_identity(tokens["access_token"], client=client)
            except GitHubOAuthError as exc:
                raise self._from_oauth_error(exc, "Could not read the GitHub identity") from None

        github_id = identity["github_id"]
        existing = self._refuse_foreign_binding(principal_oid, connection_id, github_id)

        ts = self._now()
        link_id = self._new_id()
        secret_arn = self._create_secret(
            link_id,
            {"access_token": tokens["access_token"], "refresh_token": tokens.get("refresh_token")},
        )
        record_out = GitHubUserLink(
            id=link_id,
            principal_oid=principal_oid,
            connection_id=connection_id,
            github_id=github_id,
            github_login=identity["github_login"],
            status=LinkStatus.LINKED,
            secret_arn=secret_arn,
            token_version=0 if existing is None else existing.token_version + 1,
            access_token_expires_at=self._deadline(ts, tokens.get("expires_in")),
            refresh_token_expires_at=self._deadline(ts, tokens.get("refresh_token_expires_in")),
            refresh_claimed_at=None,
            last_verified_at=ts.isoformat(),
            created_at=ts.isoformat(),
            updated_at=ts.isoformat(),
        )
        try:
            self._save(record_out, existing=existing)
        except _LinkLost:
            logger.warning("[github-link] link %s lost to a concurrent write; deleting its secret", link_id)
            self._delete_secret_best_effort(link_id)
            raise GitHubLinkError("GitHub link changed", kind="changed") from None
        except (GitHubLinkError, *STORE_FAULTS):
            logger.exception("[github-link] persist failed for link %s; rolling back the credential", link_id)
            self._revoke_best_effort(client_id, client_secret, tokens["access_token"], link_id)
            self._delete_secret_best_effort(link_id)
            raise GitHubLinkError("Failed to store the GitHub link", kind="secret_error") from None
        if existing is not None and existing.id != link_id:
            self._delete_secret_best_effort(existing.id)
        return record_out

    def list_for_principal(self, principal_oid: str) -> List[GitHubUserLink]:
        """Every link this human holds: a ``begins_with`` range read that fails closed on a store
        fault, since the console turns an empty list into "not linked"."""
        self._validate_ids(principal_oid)
        try:
            return self._load_for_principal_strict(principal_oid)
        except STORE_FAULTS:
            logger.exception("[github-link] could not list links for a principal")
            raise GitHubLinkError("Failed to read the GitHub links", kind="secret_error") from None

    def get_user_bearer_token(self, principal_oid: str, connection_id: str) -> str:
        """A live user access token for this human on this org: refresh-if-expiring behind the
        claim-then-rotate guard. The stored token is returned untouched when it is fresh or when
        the grant does not expire at all."""
        row = self._require_row(principal_oid, connection_id)

        if row.status == LinkStatus.UNLINKED:
            raise GitHubLinkError("The GitHub authorization is no longer valid", kind="link_revoked")
        if row.status == LinkStatus.REFRESHING:
            return self._handle_inflight_claim(row)

        if row.access_token_expires_at is None:
            return self._stored_access_token(row)
        remaining = self._seconds_until(row.access_token_expires_at)
        if remaining > _REFRESH_SKEW_SECONDS:
            return self._stored_access_token(row)

        return self._rotate(row)

    def verify_link(self, principal_oid: str, connection_id: str) -> GitHubUserLink:
        """Probe ``GET /user`` and refresh the denormalized login; a 401 persists ``UNLINKED``.
        Only the label moves: ``github_id`` is the join key and is never rewritten from a probe,
        and the write is :meth:`_save_label`'s targeted update."""
        token = self.get_user_bearer_token(principal_oid, connection_id)
        row = self._require_row(principal_oid, connection_id)

        with _http_client() as client:
            try:
                identity = self._fetch_user_identity(token, client=client)
            except GitHubOAuthError as exc:
                if exc.kind == "revoked":
                    self._publish_unlinked(row)
                    raise GitHubLinkError(
                        "The GitHub authorization is no longer valid", kind="link_revoked"
                    ) from None
                raise self._from_oauth_error(exc, "Could not read the GitHub identity") from None

        ts = self._now()
        return self._save_label(row, github_login=identity["github_login"], ts=ts.isoformat())

    def unlink(self, principal_oid: str, connection_id: str) -> None:
        """Revoke at GitHub first, then purge locally once the grant is known to be gone.

        A failed revoke raises ``provider_error`` with the token and the row kept (the token is
        the only means of revoking and the row the only way to find it again) and the link marked
        ``UNLINKED``, so the human can retry. The one case that purges without a revoke is an
        unresolvable OAuth client, when AGP could never call the revoke endpoint again."""
        row = self._require_row(principal_oid, connection_id)

        client_id, client_secret = self._load_credentials_for_unlink(row)
        if client_id is not None:
            access_token = self._get_secret_body(row.id).get("access_token")
            if access_token and not self._revoke_best_effort(client_id, client_secret, access_token, row.id):
                self._publish_unlinked(row)
                raise GitHubLinkError(
                    "Could not revoke the GitHub authorization — retry, or remove it in your GitHub settings",
                    kind="provider_error",
                )

        self._delete_secret_best_effort(row.id)
        self._delete(principal_oid, connection_id)

    def connection_org(self, connection_id: str) -> str:
        """The org of the connection a link is on, through the injected loader; the one connection
        fact a link view carries.

        A connection that is gone is ``connection_missing`` (404 "GitHub link not found": a link
        cannot exist on a connection that does not), a store fault is the retryable
        ``secret_error``, and anything else is ``provider_error``. Duck-typed on ``.kind`` because
        this module does not import the connection service; the loader's message is discarded."""
        try:
            return self._connection_org_loader(connection_id)
        except Exception as exc:
            kind = getattr(exc, "kind", None)
            if kind == "not_found":
                raise GitHubLinkError(
                    "The link's connection no longer exists", kind="connection_missing"
                ) from None
            if kind == "secret_error":
                logger.exception("[github-link] could not read the connection %s for its org", connection_id)
                raise GitHubLinkError("Could not read the link's connection", kind="secret_error") from None
            logger.exception("[github-link] could not resolve the connection %s for its org", connection_id)
            raise GitHubLinkError("Could not resolve the link's connection", kind="provider_error") from None

    def _load_credentials_for_unlink(self, row: GitHubUserLink) -> tuple[Optional[str], str]:
        """``(client_id, client_secret)``, or ``(None, "")`` when the org's OAuth client is gone:
        the one condition under which :meth:`unlink` purges without revoking. Every other failure
        is re-raised, so a transient store fault cannot purge the only token that can revoke a
        grant still live at GitHub."""
        try:
            return self._load_credentials(row.connection_id)
        except GitHubLinkError as exc:
            if exc.kind != "oauth_client_missing":
                raise
            logger.exception(
                "[github-link] no OAuth client to revoke link %s with; the GitHub grant will "
                "survive and only the human's own GitHub settings can clear it",
                row.id,
            )
            return None, ""

    def _revoke_best_effort(
        self, client_id: str, client_secret: str, access_token: str, link_id: str
    ) -> bool:
        """``DELETE /applications/{client_id}/grant``; returns whether the grant is known to be
        gone. Only the traceback is logged."""
        try:
            with _http_client() as client:
                self._revoke_grant(
                    client_id=client_id,
                    client_secret=client_secret,
                    access_token=access_token,
                    client=client,
                )
            return True
        except (GitHubOAuthError, GitHubLinkError):
            logger.exception(
                "[github-link] could not revoke the GitHub grant for link %s; it may still be live",
                link_id,
            )
            return False

    def _handle_inflight_claim(self, row: GitHubUserLink) -> str:
        """A row found ``REFRESHING``: within the timeout another task holds the rotation, so the
        answer is "retry"; past it the claimer is gone, and the link is recovered when the stored
        pair still works or marked ``UNLINKED`` when it does not."""
        claimed_at = row.refresh_claimed_at
        stale = claimed_at is None or self._seconds_since(claimed_at) > _CLAIM_TIMEOUT_SECONDS
        if stale:
            recovered = self._recover_unpublished_rotation(row)
            if recovered is not None:
                return recovered
            logger.warning(
                "[github-link] abandoning a stale refresh claim on link %s; marking it unlinked",
                row.id,
            )
            self._publish_unlinked(row)
            raise GitHubLinkError("The GitHub authorization is no longer valid", kind="link_revoked")
        raise GitHubLinkError("A GitHub token refresh is already in progress", kind="refresh_in_progress")

    def _recover_unpublished_rotation(self, row: GitHubUserLink) -> Optional[str]:
        """A stale claim whose stored pair turns out to be alive: re-publish ``LINKED`` and return
        that token; ``None`` when there is nothing to recover. The row cannot tell the two apart,
        so ``GET /user`` (a read, never a rotation) decides: a success recovers, a 401 abandons,
        and a transient failure raises rather than destroy anything on a blip."""
        try:
            token = self._stored_access_token(row)
        except GitHubLinkError:
            return None
        with _http_client() as client:
            try:
                self._fetch_user_identity(token, client=client)
            except GitHubOAuthError as exc:
                if exc.kind == "revoked":
                    return None
                raise self._from_oauth_error(exc, "Could not check the GitHub authorization") from None

        ts = self._now()
        logger.warning(
            "[github-link] recovering link %s: its refresh claim was abandoned but the stored "
            "token is still live, so re-publishing rather than unlinking a working credential",
            row.id,
        )
        self._save_lossy(
            row.model_copy(
                update={
                    "status": LinkStatus.LINKED,
                    "refresh_claimed_at": None,
                    "updated_at": ts.isoformat(),
                }
            ),
            expected_version=row.token_version,
        )
        return token

    def _rotate(self, row: GitHubUserLink) -> str:
        """Claim the rotation, spend the refresh token exactly once, then publish the result.

        A transient provider failure releases the claim (keeping the version bump, so a caller
        that already lost it still loses it) and is never retried; a spent or revoked grant
        publishes ``UNLINKED``; a lost secret write after rotation also publishes ``UNLINKED``,
        because the old pair is already dead; a store fault on the publish is ``secret_error``
        with the good pair still in the secret."""
        body = self._get_secret_body(row.id)
        refresh_token = body.get("refresh_token")
        if not refresh_token:
            self._publish_unlinked(row)
            raise GitHubLinkError("The GitHub authorization is no longer valid", kind="link_revoked")

        claimed_at = self._now()
        claimed = row.model_copy(
            update={
                "status": LinkStatus.REFRESHING,
                "token_version": row.token_version + 1,
                "refresh_claimed_at": claimed_at.isoformat(),
                "updated_at": claimed_at.isoformat(),
            }
        )
        try:
            self._save_guarded(claimed, expected_version=row.token_version)
        except _ClaimLost:
            return self._read_after_lost_claim(row)

        client_id, client_secret = self._load_credentials(row.connection_id)
        with _http_client() as client:
            try:
                rotated = self._refresh_user_token(
                    client_id=client_id,
                    client_secret=client_secret,
                    refresh_token=refresh_token,
                    client=client,
                )
            except GitHubOAuthError as exc:
                if exc.kind in ("bad_grant", "revoked"):
                    self._publish_unlinked(claimed)
                    raise GitHubLinkError(
                        "The GitHub authorization is no longer valid", kind="link_revoked"
                    ) from None
                self._release_claim(claimed)
                raise self._from_oauth_error(exc, "Could not refresh the GitHub user token") from None

        new_body = {
            "access_token": rotated["access_token"],
            "refresh_token": rotated.get("refresh_token"),
        }
        try:
            self._put_secret_body(row.id, new_body)
        except GitHubLinkError:
            logger.exception(
                "[github-link] lost the rotated token pair for link %s; marking it unlinked", row.id
            )
            self._publish_unlinked(claimed)
            raise GitHubLinkError(
                "The GitHub authorization is no longer valid", kind="link_revoked"
            ) from None

        ts = self._now()
        published = claimed.model_copy(
            update={
                "status": LinkStatus.LINKED,
                "refresh_claimed_at": None,
                "access_token_expires_at": self._deadline(ts, rotated.get("expires_in")),
                "refresh_token_expires_at": self._deadline(ts, rotated.get("refresh_token_expires_in")),
                "updated_at": ts.isoformat(),
            }
        )
        try:
            self._save_guarded(published, expected_version=claimed.token_version)
        except _ClaimLost:
            logger.warning(
                "[github-link] link %s moved during a rotation publish; returning the live token",
                row.id,
            )
        return rotated["access_token"]

    def _read_after_lost_claim(self, row: GitHubUserLink) -> str:
        """Re-read once after losing the claim: the winner either published (return its fresh
        token) or is still rotating (retry). Freshness comes from the published expiry, not the
        version bump, which :meth:`_release_claim` leaves behind with the old pair still stored."""
        current = self._require_row(row.principal_oid, row.connection_id)
        if current.status == LinkStatus.LINKED and current.token_version > row.token_version:
            if current.access_token_expires_at is None or (
                self._seconds_until(current.access_token_expires_at) > _REFRESH_SKEW_SECONDS
            ):
                return self._stored_access_token(current)
            raise GitHubLinkError("A GitHub token refresh is already in progress", kind="refresh_in_progress")
        if current.status == LinkStatus.UNLINKED:
            raise GitHubLinkError("The GitHub authorization is no longer valid", kind="link_revoked")
        raise GitHubLinkError("A GitHub token refresh is already in progress", kind="refresh_in_progress")

    def _publish_unlinked(self, row: GitHubUserLink) -> None:
        """Persist ``status=UNLINKED`` so the console can prompt a re-link; the row and secret are
        kept. Guarded on the version ``row`` was read at, so a concurrent claim is never
        clobbered; losing the guard is benign because the caller raises regardless."""
        ts = self._now()
        self._save_lossy(
            row.model_copy(
                update={
                    "status": LinkStatus.UNLINKED,
                    "refresh_claimed_at": None,
                    "updated_at": ts.isoformat(),
                }
            ),
            expected_version=row.token_version,
        )

    def _release_claim(self, row: GitHubUserLink) -> None:
        """Undo a claim whose rotation never reached GitHub, keeping the version bump so a caller
        that already lost this claim still loses it."""
        ts = self._now()
        self._save_lossy(
            row.model_copy(
                update={
                    "status": LinkStatus.LINKED,
                    "refresh_claimed_at": None,
                    "updated_at": ts.isoformat(),
                }
            ),
            expected_version=row.token_version,
        )

    def _save_lossy(self, record: GitHubUserLink, *, expected_version: int) -> None:
        """A guarded write whose loss is an acceptable outcome, for the state-publishing paths
        whose caller raises regardless."""
        try:
            self._save_guarded(record, expected_version=expected_version)
        except _ClaimLost:
            logger.warning(
                "[github-link] link %s moved under a state publish; leaving the newer row alone",
                record.id,
            )

    def _save_guarded(self, record: GitHubUserLink, *, expected_version: int) -> None:
        """Write a whole row only while it still sits at the ``token_version`` the caller read;
        raises :class:`_ClaimLost` when it moved or is gone, so a late release never recreates a
        link that was unlinked meanwhile. A store fault never reached the condition, so it is
        never a lost claim and always the retryable fault."""
        try:
            self._store.put_if_version(self._to_item(record), "token_version", expected_version)
        except VersionMismatch:
            raise _ClaimLost() from None
        except STORE_FAULTS:
            logger.exception("[github-link] could not write link %s", record.id)
            raise GitHubLinkError("Failed to update the GitHub link", kind="secret_error") from None

    def _save_label(self, row: GitHubUserLink, *, github_login: str, ts: str) -> GitHubUserLink:
        """Write only the three label fields :meth:`verify_link` owns and return the row as it
        now stands. A targeted update, never a whole-row put, so a stale ``token_version`` cannot
        erase a concurrent claim; the store's ``update_existing`` refuses an absent row, so a link
        deleted during the probe is ``not_found`` rather than an unparseable partial row. A store
        fault proves nothing about existence and is the retryable fault, never ``not_found``."""
        key = (_PARTITION_KEY, _sort_key(row.principal_oid, row.connection_id))
        try:
            merged = self._store.update_existing(
                key, {"github_login": github_login, "last_verified_at": ts, "updated_at": ts}
            )
        except STORE_FAULTS:
            logger.exception("[github-link] could not update the label for link %s", row.id)
            raise GitHubLinkError("Failed to update the GitHub link", kind="secret_error") from None
        if merged is None:
            raise GitHubLinkError("Unknown GitHub link", kind="not_found")
        return self._from_item(merged)

    @staticmethod
    def _validate_ids(principal_oid: str, connection_id: Optional[str] = None) -> None:
        """Refuse a falsy or separator-bearing key half: a falsy ``principal_oid`` is the dev-auth
        path, and a ``#`` would make the composite sk non-injective."""
        if not principal_oid:
            raise GitHubLinkError("A signed-in Entra user is required", kind="bad_request")
        if _SK_SEPARATOR in principal_oid:
            raise GitHubLinkError("Invalid user identifier", kind="bad_request")
        if connection_id is not None:
            if not connection_id:
                raise GitHubLinkError("connection_id is required", kind="bad_request")
            if _SK_SEPARATOR in connection_id:
                raise GitHubLinkError("Invalid connection identifier", kind="bad_request")

    def _validate_redirect_uri(self, redirect_uri: str) -> None:
        """The ``redirect_uri`` must be our callback, on our origin, with no extras.

        The path is compared to :data:`LINK_CALLBACK_PATH` exactly; a query, fragment or userinfo
        is refused; ``https`` is required except on ``localhost``. The whole origin (scheme, host,
        port) is compared against ``allowed_origins`` by exact match, since a substring or suffix
        test would accept ``console.example.com.evil.io``. An empty ``allowed_origins`` keeps the
        https/localhost floor and is warned about once at construction."""
        parsed = urlparse(redirect_uri or "")
        host = (parsed.hostname or "").lower()
        if not parsed.scheme or not host:
            raise GitHubLinkError("Invalid redirect_uri", kind="bad_request")
        if parsed.username or parsed.password:
            raise GitHubLinkError("Invalid redirect_uri", kind="bad_request")
        if parsed.path != LINK_CALLBACK_PATH:
            raise GitHubLinkError("Invalid redirect_uri", kind="bad_request")
        if parsed.query or parsed.fragment or parsed.params:
            raise GitHubLinkError("Invalid redirect_uri", kind="bad_request")
        local = host == "localhost" or host == "127.0.0.1" or host.endswith(".localhost")
        if parsed.scheme != "https" and not local:
            raise GitHubLinkError("Invalid redirect_uri", kind="bad_request")
        if not self._allowed_origins:
            return
        origin = f"{parsed.scheme}://{parsed.netloc}".lower()
        if origin not in self._allowed_origins:
            raise GitHubLinkError("Invalid redirect_uri", kind="bad_request")

    def _refuse_foreign_binding(
        self, principal_oid: str, connection_id: str, github_id: int
    ) -> Optional[GitHubUserLink]:
        """Refuse a GitHub account already bound to a different Entra human, and return this
        principal's existing row for this connection (if any) so its secret can be purged. One
        strict whole-partition read answers both and fails closed: an unreadable partition is a
        ``conflict``, never "no conflict"."""
        try:
            rows = self._load_all_strict(consistent=True)
        except STORE_FAULTS:
            logger.exception(
                "[github-link] could not read the link partition, so a foreign GitHub binding "
                "cannot be ruled out"
            )
            raise GitHubLinkError("Could not verify the GitHub account binding", kind="conflict") from None
        for row in rows:
            if row.github_id == github_id and row.principal_oid != principal_oid:
                raise GitHubLinkError("That GitHub account is linked to another user", kind="conflict")
        return next(
            (r for r in rows if r.principal_oid == principal_oid and r.connection_id == connection_id),
            None,
        )

    def _load_credentials(self, connection_id: str) -> tuple[str, str]:
        """Resolve ``(client_id, client_secret)`` through the injected loader.

        A deterministic loader failure (no OAuth client pasted, an unknown connection, a GitHub
        Enterprise connection) carries a kind in :data:`_PERMANENT_LOADER_KINDS` and is
        ``oauth_client_missing``; everything else is the retryable ``secret_error``, because
        :meth:`unlink` treats an unresolvable client as licence to purge without revoking. The
        loader's message is discarded either way. Duck-typed on ``.kind`` because this module does
        not import the connection service."""
        try:
            client_id, client_secret = self._client_credentials_loader(connection_id)
        except Exception as exc:
            if getattr(exc, "kind", None) in _PERMANENT_LOADER_KINDS:
                logger.warning("[github-link] no usable OAuth client for connection %s", connection_id)
                raise GitHubLinkError(
                    "This org connection has no usable GitHub OAuth client",
                    kind="oauth_client_missing",
                ) from None
            logger.exception(
                "[github-link] could not resolve the OAuth client for connection %s; treating it "
                "as retryable rather than as an absent client",
                connection_id,
            )
            raise GitHubLinkError(
                "Could not read the GitHub OAuth client for this org connection",
                kind="secret_error",
            ) from None
        if not client_id or not client_secret:
            raise GitHubLinkError(
                "This org connection has no usable GitHub OAuth client", kind="oauth_client_missing"
            )
        return client_id, client_secret

    @staticmethod
    def _from_oauth_error(exc: GitHubOAuthError, message: str) -> GitHubLinkError:
        """Translate a transport failure into this service's vocabulary; only ``.kind`` crosses,
        the transport's message is dropped."""
        return GitHubLinkError(message, kind=_OAUTH_KIND_MAP.get(exc.kind, "provider_error"))

    @staticmethod
    def _deadline(ts: datetime, seconds: Optional[int]) -> Optional[str]:
        """Absolute ISO-8601 expiry from a provider lifetime; ``None`` means non-expiring."""
        if not seconds:
            return None
        return (ts + timedelta(seconds=int(seconds))).isoformat()

    def _seconds_until(self, iso_ts: str) -> float:
        """Seconds from the injected clock to ``iso_ts``; an unparseable value reads as already
        expired, since treating a corrupt own field as fresh would hand out a dead token."""
        parsed = _parse_ts(iso_ts)
        if parsed is None:
            return -1.0
        return (parsed - self._now()).total_seconds()

    def _seconds_since(self, iso_ts: str) -> float:
        """Seconds from ``iso_ts`` to the injected clock; unparseable reads as infinitely old, so a
        corrupt claim timestamp is abandoned rather than blocking forever."""
        parsed = _parse_ts(iso_ts)
        if parsed is None:
            return float("inf")
        return (self._now() - parsed).total_seconds()

    def _secret_name(self, link_id: str) -> str:
        return f"{self.secret_prefix}{link_id}"

    def _create_secret(self, link_id: str, body: dict) -> str:
        """Create the ``{"access_token","refresh_token"}`` secret, overwriting a name a previous
        link already took. The ``ResourceExistsException`` fallback is nested so its
        ``put_secret_value`` is translated too."""
        name = self._secret_name(link_id)
        secret_string = json.dumps(body)
        try:
            try:
                resp = self._sm.create_secret(Name=name, SecretString=secret_string, Tags=list(_SECRET_TAGS))
            except self._sm.exceptions.ResourceExistsException:
                resp = self._sm.put_secret_value(SecretId=name, SecretString=secret_string)
            return resp["ARN"]
        except STORE_FAULTS:
            logger.exception("[github-link] create_secret failed for link %s", link_id)
            raise GitHubLinkError("Failed to store the GitHub token", kind="secret_error") from None

    def _get_secret_body(self, link_id: str) -> dict:
        """Read and parse ``{"access_token", "refresh_token"}``; the value is never logged."""
        try:
            resp = self._sm.get_secret_value(SecretId=self._secret_name(link_id))
            body = json.loads(resp["SecretString"])
        except STORE_FAULTS:
            logger.exception("[github-link] get_secret_value failed for link %s", link_id)
            raise GitHubLinkError("Failed to read the GitHub token", kind="secret_error") from None
        except ValueError:
            logger.exception("[github-link] secret body for link %s is not JSON", link_id)
            raise GitHubLinkError("Failed to read the GitHub token", kind="secret_error") from None
        return body if isinstance(body, dict) else {}

    def _put_secret_body(self, link_id: str, body: dict) -> None:
        try:
            self._sm.put_secret_value(SecretId=self._secret_name(link_id), SecretString=json.dumps(body))
        except STORE_FAULTS:
            logger.exception("[github-link] put_secret_value failed for link %s", link_id)
            raise GitHubLinkError("Failed to rotate the GitHub token", kind="secret_error") from None

    def _delete_secret_best_effort(self, link_id: str) -> None:
        """Delete the secret; a missing secret is success (unlink must be idempotent)."""
        try:
            self._sm.delete_secret(SecretId=self._secret_name(link_id), ForceDeleteWithoutRecovery=True)
        except self._sm.exceptions.ResourceNotFoundException:
            pass
        except STORE_FAULTS:
            logger.exception("[github-link] delete_secret failed for link %s", link_id)

    def _stored_access_token(self, row: GitHubUserLink) -> str:
        token = self._get_secret_body(row.id).get("access_token")
        if not token or not isinstance(token, str):
            logger.error("[github-link] link %s has no usable stored access token", row.id)
            raise GitHubLinkError("Failed to read the GitHub token", kind="secret_error")
        return token

    def _require_row(self, principal_oid: str, connection_id: str) -> GitHubUserLink:
        """Load one link or raise. A store fault is ``secret_error``, never ``not_found``: telling
        a linked human "no link" would send them through the whole web flow again."""
        self._validate_ids(principal_oid, connection_id)
        try:
            row = self._get(principal_oid, connection_id)
        except STORE_FAULTS:
            logger.exception("[github-link] could not read a link row")
            raise GitHubLinkError("Failed to read the GitHub link", kind="secret_error") from None
        if row is None:
            raise GitHubLinkError("Unknown GitHub link", kind="not_found")
        return row

    def _get(self, principal_oid: str, connection_id: str) -> Optional[GitHubUserLink]:
        """Strict read: a store fault propagates to :meth:`_require_row`."""
        item = self._store.get((_PARTITION_KEY, _sort_key(principal_oid, connection_id)))
        return self._from_item(item) if item else None

    def _load_for_principal_strict(self, principal_oid: str) -> List[GitHubUserLink]:
        prefix = f"{principal_oid}{_SK_SEPARATOR}"
        return [self._from_item(i) for i in self._scan_partition(sk_prefix=prefix)]

    def _load_all_strict(self, *, consistent: bool = False) -> List[GitHubUserLink]:
        """The whole link partition, the uniqueness guard's input; there is no GSI on ``github_id``."""
        return [self._from_item(i) for i in self._scan_partition(consistent=consistent)]

    def _save(self, record: GitHubUserLink, *, existing: Optional[GitHubUserLink]) -> None:
        item = self._to_item(record)
        try:
            if existing is None:
                if not self._store.put_new(item):
                    raise _LinkLost()
            else:
                self._store.put_if_version(item, "token_version", existing.token_version)
        except VersionMismatch:
            raise _LinkLost() from None
        except STORE_FAULTS:
            logger.exception("[github-link] could not write link %s", record.id)
            raise GitHubLinkError("Failed to store the GitHub link", kind="secret_error") from None

    def _delete(self, principal_oid: str, connection_id: str) -> None:
        """Remove the link row (``unlink``'s last step); by then the grant is revoked and the secret
        deleted, so a store fault is the retryable ``secret_error``."""
        try:
            self._store.delete((_PARTITION_KEY, _sort_key(principal_oid, connection_id)))
        except STORE_FAULTS:
            logger.exception("[github-link] could not delete a link row")
            raise GitHubLinkError("Failed to remove the GitHub link", kind="secret_error") from None

    def _save_state(self, state: str, record: dict) -> None:
        """Persist the CSRF/PKCE state. A store fault aborts ``begin_link``: without a stored state
        the callback could never be validated. ``record`` holds the verifier, so nothing from it
        reaches a log or a body."""
        try:
            created = self._store.put_new({"pk": _STATE_PARTITION_KEY, "sk": state, **record})
        except STORE_FAULTS:
            logger.exception("[github-link] could not store a link state")
            raise GitHubLinkError("Failed to start the GitHub authorization", kind="secret_error") from None
        if not created:
            raise RuntimeError("a link state with this id was already issued")

    def _delete_state(self, state: str) -> Optional[dict]:
        """Consume the state: one conditional delete that returns the record, or ``None`` when it
        was never issued or is already spent. A store fault raises rather than continuing, because
        this delete is what makes the state single-use and no code has been redeemed yet."""
        try:
            item = self._store.delete((_STATE_PARTITION_KEY, state))
        except STORE_FAULTS:
            logger.exception("[github-link] could not consume a link state")
            raise GitHubLinkError(
                "Failed to complete the GitHub authorization", kind="secret_error"
            ) from None
        return {k: v for k, v in item.items() if k not in ("pk", "sk")} if item else None

    def _scan_partition(self, sk_prefix: str = "", *, consistent: bool = False) -> List[dict]:
        return self._store.query(_PARTITION_KEY, sk_prefix=sk_prefix, consistent=consistent)

    def _to_item(self, record: GitHubUserLink) -> dict:
        return {
            "pk": _PARTITION_KEY,
            "sk": _sort_key(record.principal_oid, record.connection_id),
            **json.loads(record.model_dump_json()),
        }

    def _from_item(self, item: dict) -> GitHubUserLink:
        clean = {k: v for k, v in item.items() if k not in ("pk", "sk")}
        return GitHubUserLink.model_validate(clean)


def _sort_key(principal_oid: str, connection_id: str) -> str:
    """The composite sk, ``<principal_oid>#<connection_id>``: derived, not generated, which is what
    makes a re-link an upsert over the same row."""
    return f"{principal_oid}{_SK_SEPARATOR}{connection_id}"


def _parse_ts(iso_ts: Optional[str]) -> Optional[datetime]:
    """Parse one of this service's own ISO-8601 stamps into an aware UTC datetime."""
    if not iso_ts or not isinstance(iso_ts, str):
        return None
    try:
        parsed = datetime.fromisoformat(iso_ts)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _http_client() -> httpx.Client:
    """One short-lived ``httpx.Client`` per operation, used as a context manager so it is always
    closed. Every GitHub call is injected, so tests never construct one."""
    return httpx.Client(timeout=15.0)
