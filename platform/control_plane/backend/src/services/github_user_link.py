"""Per-user GitHub account links (E27B/T7) — the epic's core service.

Composes the pure OAuth transport in :mod:`services.github_user_oauth` with storage so an
Entra human can authorize AGP to act on GitHub **as them**: AGP holds the Entra ``oid`` on
one side and GitHub's numeric user id on the other, and owns the resulting token's whole
lifecycle (mint → refresh → verify → revoke). Every seam is injected, so this module holds
no ``httpx`` response parsing, no FastAPI, and no wall-clock read.

A NEW SEAM, NOT A THIRD ``AuthType`` (D2). E27A's design pre-committed to hiding user
tokens behind ``ConnectionService.get_bearer_token`` "so no call site changes". That is not
achievable: ``get_bearer_token(connection_id)`` resolves a per-ORG record and
``ConnectionService`` never sees a principal, so overloading it would either grow the
signature (touching all six production callers) or smuggle request state into a service.
Hence :meth:`GitHubUserLinkService.get_user_bearer_token` — a separate
``(principal_oid, connection_id)`` seam. The E27A invariant that still holds is the one that
matters: no code mints or reads a provider credential outside a service seam.

STORAGE (D1). Link METADATA is a new **partition** in the existing connections table
(``pk="github_user_link"``, composite ``sk="<principal_oid>#<connection_id>"``) — a new
partition needs zero terraform, where a new table would need an ARN added to the ECS task's
explicit DDB allowlist. Short-lived CSRF/PKCE state is a second partition
(``pk="github_link_state"``), expired by the APPLICATION (``_STATE_TTL_SECONDS``) because
this table has no DDB TTL. Persistence is the DDB-or-local shape cloned from
``connection_service`` / ``project_role_service``: the ``_has_ddb`` guard, two local dicts
under ONE lock, ``_to_item``/``_from_item`` stripping ``pk``/``sk``. Nothing is written to
Entra — AGP reads identity from the directory and never writes to it.

SECRET SEAM. The ``ghu_``/``ghr_`` pair lives ONLY in Secrets Manager, under
``<secret_prefix><link_id>``, body exactly ``{"access_token", "refresh_token"}``. The DDB row
and every read model carry only ``secret_arn`` — a POINTER — plus non-secret metadata, exactly
as ``Connection`` exposes ``secret_arn``/``has_secret`` and never the credential. No token, no
``client_secret``, no authorization ``code``, and no provider response body ever reaches a log
line, a :class:`GitHubLinkError` message, or (therefore) an HTTP body: errors carry a fixed
safe string plus a ``.kind``, and every transport failure is re-raised ``from None`` because a
chained exception's args can carry URLs with auth material.

CONCURRENCY — claim-then-rotate (D6, the epic's top outage risk). GitHub's refresh is
DESTRUCTIVE and SINGLE-USE: spending a ``ghr_`` token invalidates both the old refresh token
AND the old access token. AGP runs **two ECS tasks**, so two concurrent refreshes of one link
would mutually invalidate — the loser's write would land last and store a pair GitHub has
already killed, locking the human out of their own link with no recovery but a full re-link.
So the rotation is CLAIMED before GitHub is ever called: a conditional write of
``status=REFRESHING, token_version=n+1`` guarded by ``Attr("token_version").eq(n)``, which
exactly one caller can win (the local-fallback branch performs the same compare-and-set under
its lock, so dev behaves like production). The order is load-strict as well as claim-first:
claim → refresh → write secret → publish. Rotations are NEVER retried.

``token_version`` IS THE ROW'S OPTIMISTIC-CONCURRENCY TOKEN, not a refresh counter, so EVERY
whole-row writer goes through :meth:`~GitHubUserLinkService._save_guarded` and names the
version it read. A version-blind ``put_item`` after any network round-trip can erase a
concurrent claim — after which the loser's CAS on the OLD version passes and the same ``ghr_``
token is spent a SECOND time, which is precisely the outage this design exists to prevent. That
is why ``verify_link`` writes its three label fields with a TARGETED ``update_item`` that never
mentions ``status``/``token_version`` (:meth:`~GitHubUserLinkService._save_label`) rather than
putting back the row it read before its probe.

A caller that LOSES the claim re-reads exactly once. ``LINKED`` at a higher version is NOT by
itself proof of a completed rotation — a released claim keeps the bump while the secret still
holds the old pair — so the published EXPIRY is what decides: still-expiring means "retry"
(``refresh_in_progress``), never a token that is already dead.

A claim left ``REFRESHING`` past ``_CLAIM_TIMEOUT_SECONDS`` is ABANDONED, but the link is not
destroyed before the stored pair is PROVEN dead: a ``GET /user`` read (no refresh token spent)
distinguishes "the rotation completed and only the row write failed" — republish ``LINKED``,
keep the working credential — from a genuine 401, which is the case ``UNLINKED`` + "re-link"
was written for. A transient probe failure destroys nothing. Never discard a live credential
because a metadata write failed.

LIVENESS. AGP does not consume the ``github_app_authorization`` webhook, so revocation is
discovered on use: a ``401`` from GitHub on a user-token call raises ``kind="revoked"`` in the
transport, and this service persists ``status=UNLINKED`` so the console can prompt a re-link
instead of retrying forever. :meth:`unlink` calls
``DELETE /applications/{client_id}/grant`` BEFORE deleting anything locally — deleting only
AGP's row would leave a live ``ghu_``/``ghr_`` pair at GitHub after the human believes they
unlinked — and it does NOT report success when that revoke failed: a failed unlink the human
can retry beats telling them their authorization is gone while it is live. NO PATH LEAVES A
LIVE CREDENTIAL AGP CANNOT FIND: ``complete_link`` creates the secret before the row, so a
failed row write revokes at GitHub and deletes the secret rather than orphaning a pair no row
points at.

DETERMINISM. The clock (``now``), the id source (``new_id``, used for both the state token and
the link id) and the PKCE verifier source (``new_verifier``) are all injected; tests pass a
fixed clock and deterministic generators. There is no inline ``datetime.now()`` in logic — the
repo-wide rule.

CONNECTION-UNAWARE BY DESIGN. This service never loads a ``Connection``: it takes a
``client_credentials_loader(connection_id) -> (client_id, client_secret)`` callable (the route
injects ``ConnectionService.get_oauth_client_credentials``). That is also where the GHE refusal
lives — the OAuth legs are ``github.com``-only and a web base cannot be derived from an API
base, so the loader raises for a connection with a ``base_url``. A DETERMINISTIC loader failure
surfaces here as ``kind="oauth_client_missing"``; a transient one surfaces as the retryable
``kind="secret_error"``, because ``oauth_client_missing`` is the one thing :meth:`unlink` treats
as licence to purge a token without revoking it.
"""

from __future__ import annotations

import json
import logging
import secrets
import threading
from datetime import datetime, timedelta, timezone
from typing import Callable, Dict, List, Optional
from urllib.parse import urlparse
from uuid import uuid4

import boto3
import httpx
from boto3.dynamodb.conditions import Attr, Key
from botocore.exceptions import BotoCoreError, ClientError

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

# The ONE literal four tasks depend on byte-for-byte: T5 builds it client-side, T7 (here)
# validates the supplied redirect_uri's path against it, T9 derives the App manifest's
# callback_urls entry from it, T10 registers the SPA route at it. A mismatch anywhere makes
# GitHub reject the redirect_uri outright, so it is declared ONCE and imported.
LINK_CALLBACK_PATH = "/ops/github-link/callback"

_PARTITION_KEY = "github_user_link"  # a new partition in the EXISTING connections table
_STATE_PARTITION_KEY = "github_link_state"  # CSRF + PKCE state, a SEPARATE partition
_SK_SEPARATOR = "#"

_STATE_TTL_SECONDS = 900  # application-enforced (this table has no DDB TTL)
_REFRESH_SKEW_SECONDS = 300  # refresh when the access token expires within this
_CLAIM_TIMEOUT_SECONDS = 60  # a REFRESHING row older than this is ABANDONED → unlinked

# SM secret tags. Deliberately NO principal_oid: tags are readable metadata, and which human a
# secret belongs to belongs in the row, not in a listable tag.
_SECRET_TAGS = [
    {"Key": "managed_by", "Value": "agp"},
    {"Key": "purpose", "Value": "github-user-link"},
]

# GitHubOAuthError.kind → GitHubLinkError.kind. Anything unmapped is a provider fault.
_OAUTH_KIND_MAP = {"bad_grant": "bad_request", "revoked": "link_revoked"}

# The loader failures that genuinely mean "this org has no usable OAuth client", as opposed to
# "the store could not answer right now". The injected loader
# (``ConnectionService.get_oauth_client_credentials``) carries a ``.kind`` and uses exactly these
# two for its DETERMINISTIC answers: ``bad_request`` for an absent client_id/client_secret pair
# and for the GitHub Enterprise refusal, ``not_found`` for an unknown connection. Everything
# else — a raw ``ClientError``, a ``secret_error`` from Secrets Manager, an unexpected fault — is
# TRANSIENT-OR-UNKNOWN and must stay retryable. Duck-typed on the attribute rather than the
# class because this module must not depend on ``ConnectionService`` (module docstring).
_PERMANENT_LOADER_KINDS = frozenset({"bad_request", "not_found"})

# EVERY boto3 fault this module translates, at EVERY store call site. ``ClientError`` is the
# service-answered half (a throttle, an IAM denial, a validation refusal); ``BotoCoreError`` is the
# half that never reached the service at all (``EndpointConnectionError``,
# ``ConnectTimeoutError``, ``ReadTimeoutError``, a credential-resolution failure) and is NOT a
# subclass of ``ClientError``. Both are equally ordinary against DynamoDB and Secrets Manager from
# ECS, and either one escaping raw is an HTTP 500 — outside the {400,404,409,502} set this epic
# pins, and unactionable where a retryable 502 was intended. Named once so no site can guard only
# half of it.
_STORE_FAULTS = (ClientError, BotoCoreError)


class GitHubLinkError(Exception):
    """A GitHub user-link operation failed. Carries a SAFE message — never a token, the
    ``client_secret``, the authorization ``code``, the PKCE verifier, or a provider body — plus
    a ``.kind`` the route maps to a FIXED status and a FIXED detail literal (never
    ``str(exc)``): ``{"not_found","bad_request","conflict","oauth_client_missing",
    "refresh_in_progress","link_revoked","provider_error","secret_error"}``."""

    def __init__(self, message: str, kind: str) -> None:
        super().__init__(message)
        self.message = message
        self.kind = kind


class _ClaimLost(Exception):
    """Internal: another task won the refresh claim (the conditional write's condition
    failed). Never escapes :meth:`GitHubUserLinkService.get_user_bearer_token`."""


class GitHubUserLinkService:
    def __init__(
        self,
        *,
        table_name: str = "",
        secret_prefix: str = "",
        region: str = "us-east-1",
        # The origins a redirect_uri may name (T8 passes ``tuple(settings.CORS_ORIGINS)``, the
        # same list the API already accepts a browser from). Injected like every other seam, and
        # optional so an empty value degrades to the https/localhost floor rather than breaking
        # every link on a wiring miss.
        allowed_origins: "tuple[str, ...]" = (),
        client_credentials_loader: Callable[[str], "tuple[str, str]"],
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
            # The empty default fails OPEN — ``_validate_redirect_uri`` then checks only the path
            # and scheme, never the host. That is deliberate (a T8 wiring miss must not break
            # every link) but it must not be SILENT: with no list, the only thing between a
            # user-supplied host and the authorization code is GitHub's own registered-callback
            # match, a backstop AGP does not own. Logged once at construction rather than per
            # request, since the service is a lazy singleton.
            logger.warning(
                "[github-link] constructed with NO allowed_origins: a redirect_uri's ORIGIN will "
                "not be checked, only its path and scheme. Wire allowed_origins (T8 passes "
                "CORS_ORIGINS)"
            )
        # (client_id, client_secret) for a connection's OAuth client. Injected rather than
        # imported so this module never depends on ConnectionService — and so the GHE refusal
        # can live on the loader side (see the module docstring).
        self._client_credentials_loader = client_credentials_loader
        self._new_id = new_id
        self._new_verifier = new_verifier
        self._now = now
        self._exchange_code = exchange_code
        self._refresh_user_token = refresh_user_token
        self._fetch_user_identity = fetch_user_identity
        self._revoke_grant = revoke_grant

        self._sm = secrets_client or boto3.client("secretsmanager", region_name=region)

        self._ddb = None
        self._table = None
        if table_name:
            try:
                self._ddb = boto3.resource("dynamodb", region_name=region)
                self._table = self._ddb.Table(table_name)
            except Exception:  # pragma: no cover — degrade to local fallback.
                self._table = None

        # Local fallback caches, keyed by the composite sk / the state token. Two dicts under
        # ONE lock, exactly as connection_service holds _local + _state.
        self._local: Dict[str, GitHubUserLink] = {}
        self._state: Dict[str, dict] = {}
        self._local_lock = threading.Lock()

    # -- mode helper --------------------------------------------------------

    @property
    def _has_ddb(self) -> bool:
        return bool(self.table_name) and self._table is not None

    # ===================================================================== #
    # Public API
    # ===================================================================== #

    def begin_link(
        self, principal_oid: str, connection_id: str, redirect_uri: str
    ) -> "tuple[str, str]":
        """Start the web flow: mint PKCE + CSRF state and return ``(authorize_url, state)``.

        Refuses a falsy ``principal_oid`` — the dev-auth path has no ``oid``, and a link with
        no Entra subject is unattributable, which is the one thing this feature exists to
        provide (design §1). The connection's OAuth client is resolved FIRST so an org that
        still needs the one-time admin paste fails before any state is persisted."""
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
        """Finish the web flow: consume the state, exchange the code, prove the identity, and
        persist the link.

        The state is deleted UNCONDITIONALLY before it is validated — single-use regardless of
        validity, the rule ``consume_manifest_state`` already follows, so a replayed or expired
        state can never be spent twice. The exchange uses the STATE's ``redirect_uri`` and
        ``code_verifier``, never anything from this request: those two are what bind the
        callback to the authorize call, and taking them from the caller would make the PKCE
        pairing decorative."""
        record = self._get_state(state)
        self._delete_state(state)  # single-use regardless of validity

        if record is None or int(record.get("exp") or 0) < int(self._now().timestamp()):
            raise GitHubLinkError("The link request expired or is unknown", kind="bad_request")
        if not principal_oid or record.get("principal_oid") != principal_oid:
            # The binding is only trustworthy if the session that FINISHES is the session that
            # started: otherwise one human could complete a flow another human began and end up
            # bound to that person's GitHub account.
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
            token_version=0,
            access_token_expires_at=self._deadline(ts, tokens.get("expires_in")),
            refresh_token_expires_at=self._deadline(ts, tokens.get("refresh_token_expires_in")),
            refresh_claimed_at=None,
            last_verified_at=ts.isoformat(),
            created_at=ts.isoformat(),
            updated_at=ts.isoformat(),
        )
        try:
            # The secret already holds a LIVE ghu_/ghr_ pair for a real human. An unguarded
            # write here would, on a store fault, leave that pair in Secrets Manager with NO row
            # pointing at it: unfindable (no row → no link_id) and unrevocable (unlink needs the
            # row) for the whole 6-month refresh window. So roll the credential back — at GitHub
            # first, because forgetting a live grant is worse than failing the link — then speak
            # this service's own vocabulary rather than letting a raw boto3 error 500. Mirrors
            # ``connection_service.create_connection``'s documented rollback.
            #
            # CATCHES ALL THREE SHAPES, DELIBERATELY. ``_save`` now translates store faults into
            # ``GitHubLinkError`` itself, so that is the live production shape and it MUST be
            # caught — a handler still naming only ``ClientError`` would have silently stopped
            # this rollback firing the moment that translation landed, which is exactly the
            # orphaned-live-pair outcome the block exists to prevent. The two raw classes are kept
            # as belt-and-braces for the same reason the rollback exists at all: this is the one
            # window where a LIVE ``ghu_``/``ghr_`` pair has no row pointing at it, so a store
            # fault reaching it by ANY shape — including a ``BotoCoreError``, which is not a
            # ``ClientError`` and used to escape this handler entirely — must roll back rather
            # than orphan. Cheap, and the failure mode it guards is unrecoverable.
            self._save(record_out)
        except (GitHubLinkError, *_STORE_FAULTS):
            logger.exception(
                "[github-link] persist failed for link %s; rolling back the credential", link_id
            )
            self._revoke_best_effort(client_id, client_secret, tokens["access_token"], link_id)
            self._delete_secret_best_effort(link_id)
            raise GitHubLinkError("Failed to store the GitHub link", kind="secret_error") from None
        if existing is not None and existing.id != link_id:
            # A re-link reuses the composite sk but mints a NEW link id + secret, so the
            # superseded secret would otherwise be orphaned (and still hold a live pair).
            self._delete_secret_best_effort(existing.id)
        return record_out

    def list_for_principal(self, principal_oid: str) -> List[GitHubUserLink]:
        """Every link this human holds — a ``begins_with`` range read, not a whole scan.

        Fails CLOSED on a store fault rather than degrading to ``[]``: the console turns an
        empty list into "not linked" and offers a Connect button, so a swallowed blip would
        push a linked human back through the whole web flow."""
        self._validate_ids(principal_oid)
        try:
            return self._load_for_principal_strict(principal_oid)
        except _STORE_FAULTS:
            logger.exception("[github-link] could not list links for a principal")
            raise GitHubLinkError("Failed to read the GitHub links", kind="secret_error") from None

    def get_user_bearer_token(self, principal_oid: str, connection_id: str) -> str:
        """THE new seam (design §2): a live user access token for this human on this org.

        Refresh-if-expiring behind the claim-then-rotate guard described in the module
        docstring. Returns the stored token untouched when it is fresh, or when the grant does
        not expire at all."""
        row = self._require_row(principal_oid, connection_id)

        if row.status == LinkStatus.UNLINKED:
            raise GitHubLinkError("The GitHub authorization is no longer valid", kind="link_revoked")
        if row.status == LinkStatus.REFRESHING:
            return self._handle_inflight_claim(row)

        if row.access_token_expires_at is None:
            return self._stored_access_token(row)  # a non-expiring grant: nothing to rotate
        remaining = self._seconds_until(row.access_token_expires_at)
        if remaining > _REFRESH_SKEW_SECONDS:
            return self._stored_access_token(row)

        return self._rotate(row)

    def verify_link(self, principal_oid: str, connection_id: str) -> GitHubUserLink:
        """Probe ``GET /user`` and refresh the denormalized login.

        The identity probe is also the liveness probe: a ``401`` means the human revoked the
        authorization, so the link is persisted ``UNLINKED`` and the caller is told to
        re-link. A rename updates the LABEL only — ``github_id`` is the join key and this
        row's binding, so it is never rewritten from a probe.

        The label write is a TARGETED update of three fields, never a put of the row read
        before the probe: ``get_user_bearer_token`` on another task may have claimed a refresh
        in the meantime, and writing back a stale ``token_version`` would erase that claim and
        let one refresh token be spent twice (see :meth:`_save_label`)."""
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
        return self._save_label(
            row, github_login=identity["github_login"], ts=ts.isoformat()
        )

    def unlink(self, principal_oid: str, connection_id: str) -> None:
        """Revoke at GitHub FIRST, and only purge locally once the grant is known to be gone.

        ORDERING, decided deliberately. Revoke-before-delete is not negotiable: AGP's stored
        access token is the only thing ``DELETE /applications/{client_id}/grant`` accepts, so
        deleting first would leave a live ``ghu_``/``ghr_`` pair authorized at GitHub with
        nothing left to revoke it with.

        WHEN THE REVOKE FAILS, THIS RAISES rather than reporting success. Purging anyway would
        (a) tell the human their GitHub authorization is gone while it is LIVE for up to the
        6-month refresh window, and (b) destroy the one credential that could still revoke it —
        making the revoke permanently unretryable and leaving one un-alerted log line as the
        only trace. A failed unlink the human can RETRY is strictly better than a silent live
        grant, so the token and the row are both KEPT and the caller gets a retryable
        ``provider_error``. The link is marked ``UNLINKED`` first, so the console shows it as
        disconnected (and no token is handed out) while the retry is still possible.

        The one case that still purges without a successful revoke is an unresolvable OAuth
        client (``oauth_client_missing`` — the admin rotated or removed it): AGP then has no way
        to ever call the revoke endpoint, so blocking would leave a row the human can never
        delete. That grant is recorded as outstanding and only the human's own GitHub settings
        can clear it."""
        row = self._require_row(principal_oid, connection_id)

        client_id, client_secret = self._load_credentials_for_unlink(row)
        if client_id is not None:
            access_token = self._get_secret_body(row.id).get("access_token")
            if access_token and not self._revoke_best_effort(
                client_id, client_secret, access_token, row.id
            ):
                # Keep the secret AND the row: the token is the only means of revoking, and a
                # row is the only thing that can find it again.
                self._publish_unlinked(row)
                raise GitHubLinkError(
                    "Could not revoke the GitHub authorization — retry, or remove it in your "
                    "GitHub settings",
                    kind="provider_error",
                )

        self._delete_secret_best_effort(row.id)
        self._delete(principal_oid, connection_id)

    def _load_credentials_for_unlink(
        self, row: GitHubUserLink
    ) -> "tuple[Optional[str], str]":
        """``(client_id, client_secret)``, or ``(None, "")`` when the org's OAuth client is
        GENUINELY GONE — the one condition under which :meth:`unlink` purges without revoking.

        THE CARVE-OUT IS DELIBERATE, AND IT IS DELIBERATELY NARROW. ``oauth_client_missing`` means
        the pair AGP would authenticate the revoke with no longer exists (an admin rotated or
        removed it), so ``DELETE /applications/{client_id}/grant`` is not a call AGP can ever make
        again — for that link or any retry of it. Refusing to purge would then leave a row the
        human can never delete, in exchange for a revoke that will never happen, so purging is the
        better of two bad outcomes. The grant is recorded as outstanding and only the human's own
        GitHub settings can clear it.

        EVERY OTHER FAILURE IS RE-RAISED. A transient store fault must not reach this branch: it
        would purge the only token that can revoke a grant still LIVE at GitHub, which is exactly
        the defect the raising :meth:`unlink` closed — reached through a different door. So
        ``_load_credentials`` classifies at the loader boundary (only a deterministic loader
        failure becomes ``oauth_client_missing``) and this method trusts nothing but that kind."""
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
        """``DELETE /applications/{client_id}/grant`` — kill the ghu_/ghr_ pair at GitHub.
        Returns whether the grant is known to be gone.

        ``False`` means the grant may still be LIVE at GitHub, which is an operational event,
        not a swallowed nothing: the caller decides whether that is survivable. Only the
        traceback is logged — both error classes guarantee safe messages, but neither is
        allowed to reach a caller from here."""
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

    # ===================================================================== #
    # Refresh — claim-then-rotate (D6)
    # ===================================================================== #

    def _handle_inflight_claim(self, row: GitHubUserLink) -> str:
        """A row found ``REFRESHING``: retry, or declare the claim abandoned.

        Within the timeout another task holds the rotation, so the honest answer is "retry" —
        never a second rotation, which would destroy the pair the winner is about to store.
        Past the timeout the claimer is gone (its task died between calling GitHub and writing
        the secret), so the stored pair may already be dead at GitHub: the link is marked
        ``UNLINKED`` and the human re-links. Retrying with a possibly-spent refresh token
        would instead hard-kill a link that might still have been recoverable.

        ONE case is NOT abandoned: the rotation succeeded and only the PUBLISH write failed, so
        the secret already holds a good pair. That is visible without asking GitHub — the stored
        access token is still comfortably live, which a pair killed by a completed rotation
        never is. Recovering it means re-publishing ``LINKED``; unlinking instead would force a
        full re-link and throw away a working credential over a metadata write."""
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
        """A stale claim whose stored pair turns out to be ALIVE: re-publish ``LINKED`` and
        return that token. ``None`` when there is nothing to recover, so the caller abandons.

        The row cannot tell these two apart — a claim leaves ``access_token_expires_at`` at its
        pre-rotation value either way — so the only honest discriminator is whether the STORED
        pair still works, which is a question only GitHub can answer. ``GET /user`` answers it
        with a read: it is the same non-destructive probe :meth:`verify_link` uses, it spends no
        refresh token, and it is NOT a retry of the rotation.

        - probe succeeds ⇒ the pair is live. Either the rotation completed and only the row
          write failed (C3), or the claimer died before ever calling GitHub. Both are
          recoverable, and unlinking either would force a full re-link and destroy a working
          credential over a metadata write. Republish ``LINKED``; the token is returned as-is,
          and if it is still inside the skew the next call simply rotates it.
        - probe says ``revoked`` (401) ⇒ the pair really is dead, which is the case the
          abandon-and-unlink rule was written for. Return ``None``.
        - probe fails transiently ⇒ nothing is PROVEN dead, so nothing may be destroyed. Raise
          the provider fault (retryable) rather than unlinking on a blip.

        Reached only on the rare stale-claim path, so it costs one extra read there and nothing
        on the hot path."""
        try:
            token = self._stored_access_token(row)
        except GitHubLinkError:
            return None  # no usable stored pair ⇒ genuinely nothing to recover
        with _http_client() as client:
            try:
                self._fetch_user_identity(token, client=client)
            except GitHubOAuthError as exc:
                if exc.kind == "revoked":
                    return None
                raise self._from_oauth_error(
                    exc, "Could not check the GitHub authorization"
                ) from None

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
        """Claim the rotation, spend the refresh token exactly once, then publish the result."""
        body = self._get_secret_body(row.id)
        refresh_token = body.get("refresh_token")
        if not refresh_token:
            # The access token is expiring and there is nothing to rotate with.
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
                    # The refresh token is spent or the human revoked: nothing to recover.
                    self._publish_unlinked(claimed)
                    raise GitHubLinkError(
                        "The GitHub authorization is no longer valid", kind="link_revoked"
                    ) from None
                # Transient (network/5xx): GitHub never saw a usable request, so the OLD pair
                # is still live. Release the claim so the next request is not wedged for the
                # full claim timeout — but KEEP the version bump, so a caller that already
                # lost this claim still loses it. NEVER retried here: a retry is exactly how
                # one logical refresh becomes two destructive rotations.
                self._release_claim(claimed)
                raise self._from_oauth_error(exc, "Could not refresh the GitHub user token") from None

        new_body = {
            "access_token": rotated["access_token"],
            "refresh_token": rotated.get("refresh_token"),
        }
        try:
            self._put_secret_body(row.id, new_body)
        except GitHubLinkError:
            # The rotated pair is lost AND the old pair is already dead at GitHub (rotation is
            # destructive), so the link really is broken. Saying so beats leaving a row that
            # looks linked and fails on every use.
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
                "refresh_token_expires_at": self._deadline(
                    ts, rotated.get("refresh_token_expires_in")
                ),
                "updated_at": ts.isoformat(),
            }
        )
        try:
            # GUARDED, and inside the contract. A bare put here could erase a concurrent claim,
            # and a raw ClientError would escape the .kind vocabulary as a 500. On a store fault
            # the row stays REFRESHING at the claimed version while the secret holds the GOOD
            # new pair — recoverable, so `secret_error` (502, retryable), NEVER an unlink.
            self._save_guarded(published, expected_version=claimed.token_version)
        except _ClaimLost:
            # Someone moved the row out from under a rotation we already completed. The pair we
            # just stored is live, so hand it back rather than discard a working credential.
            logger.warning(
                "[github-link] link %s moved during a rotation publish; returning the live token",
                row.id,
            )
        return rotated["access_token"]

    def _read_after_lost_claim(self, row: GitHubUserLink) -> str:
        """Re-read ONCE after losing the claim. The winner either published (return its fresh
        token) or is still rotating (retry). Exactly one re-read: looping here would turn a
        contended link into a spin, and re-claiming would double-rotate.

        FRESHNESS COMES FROM THE EXPIRY, NOT THE VERSION. A bumped ``token_version`` on a
        ``LINKED`` row is NOT proof that a rotation happened: :meth:`_release_claim` produces
        exactly that shape after a transient provider failure, keeping the bump while the
        secret still holds the OLD pair. Trusting the bump there would hand this caller a token
        that already expired, which its consumer would 401 on with nothing marking the link
        dead — a silent, self-sustaining failure with a link that reads healthy. So the
        published expiry is re-applied: still-expiring means the rotation did not actually
        happen, which is a retry, not a fresh token."""
        current = self._require_row(row.principal_oid, row.connection_id)
        if current.status == LinkStatus.LINKED and current.token_version > row.token_version:
            if current.access_token_expires_at is None or (
                self._seconds_until(current.access_token_expires_at) > _REFRESH_SKEW_SECONDS
            ):
                return self._stored_access_token(current)
            raise GitHubLinkError(
                "A GitHub token refresh is already in progress", kind="refresh_in_progress"
            )
        if current.status == LinkStatus.UNLINKED:
            raise GitHubLinkError("The GitHub authorization is no longer valid", kind="link_revoked")
        raise GitHubLinkError("A GitHub token refresh is already in progress", kind="refresh_in_progress")

    def _publish_unlinked(self, row: GitHubUserLink) -> None:
        """Persist ``status=UNLINKED`` so the console can prompt a re-link. The row and secret
        are KEPT (unlike :meth:`unlink`) — the human may re-link over the same sk, and the row
        is what tells the UI which connection needs attention.

        GUARDED on the version ``row`` was read at: if another task has since claimed a
        refresh, this write must NOT land, because clobbering a live claim is how one refresh
        token gets spent twice. Losing the guard is benign — the caller still raises
        ``link_revoked``, and whatever moved the row is itself resolving the link's state."""
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
        """Undo a claim whose rotation never reached GitHub, keeping the version bump.

        Guarded on the claim's own version, so a release cannot overwrite a state some other
        writer has since published. The version bump is deliberately KEPT: dropping it would
        wedge a caller that already lost this claim. That the version moved without the token
        rotating is exactly why :meth:`_read_after_lost_claim` re-checks the EXPIRY rather than
        trusting the bump."""
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
        """A guarded write whose LOSS is an acceptable outcome — used by the state-publishing
        paths whose caller raises regardless. Never clobbers a concurrent claim; a store fault
        still surfaces as this service's own ``secret_error``."""
        try:
            self._save_guarded(record, expected_version=expected_version)
        except _ClaimLost:
            logger.warning(
                "[github-link] link %s moved under a state publish; leaving the newer row alone",
                record.id,
            )

    def _save_guarded(self, record: GitHubUserLink, *, expected_version: int) -> None:
        """Write a whole row ONLY while it still sits at the ``token_version`` the caller read.
        Raises :class:`_ClaimLost` when it moved under us.

        ``token_version`` is the row's OPTIMISTIC-CONCURRENCY token, not a refresh-path
        counter, so EVERY whole-row writer goes through here — the claim, its release, an
        unlink publish, and the rotated publish. A version-blind ``put_item`` after any network
        round-trip can erase a concurrent claim, and an erased claim means the same ``ghr_``
        token gets spent twice, which is the one outage D6 exists to prevent.

        DDB mode uses a real ``ConditionExpression`` — the only construct that arbitrates
        across the two ECS tasks. The local fallback performs the SAME compare-and-set under
        its lock so a dev process cannot double-rotate against live GitHub either. Deliberately
        NOT wrapped in the local lock in DDB mode: the condition is the arbiter, and a
        process-local lock there would give a false sense of mutual exclusion."""
        if self._has_ddb:
            try:
                self._table.put_item(
                    Item=self._to_item(record),
                    ConditionExpression=Attr("token_version").eq(expected_version),
                )
            except ClientError as exc:
                if exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
                    raise _ClaimLost() from None
                logger.exception("[github-link] could not write link %s", record.id)
                raise GitHubLinkError("Failed to update the GitHub link", kind="secret_error") from None
            except BotoCoreError:
                # A SEPARATE clause, not folded into ``_STORE_FAULTS`` above: that block reads
                # ``exc.response`` to tell a lost claim from a real fault, an attribute only
                # ``ClientError`` has. A BotoCoreError never reached DynamoDB, so it is never a
                # lost claim — always the retryable store fault.
                logger.exception("[github-link] could not write link %s", record.id)
                raise GitHubLinkError("Failed to update the GitHub link", kind="secret_error") from None
            return
        sk = _sort_key(record.principal_oid, record.connection_id)
        with self._local_lock:
            current = self._local.get(sk)
            if current is None or current.token_version != expected_version:
                raise _ClaimLost()
            self._local[sk] = record.model_copy(deep=True)

    def _save_label(self, row: GitHubUserLink, *, github_login: str, ts: str) -> GitHubUserLink:
        """Write ONLY the three label fields :meth:`verify_link` owns, and return the row as it
        now stands.

        A TARGETED update, not a whole-row put: ``verify_link`` reads its row, spends up to 15s
        on ``GET /user``, and by then any ``token_version``/``status`` it holds may be stale.
        Putting that row back would erase a concurrent refresh claim and let one ``ghr_`` token
        be spent twice. This shape structurally cannot: it never mentions ``status`` or
        ``token_version``, so there is no lost-update window to guard and nothing to retry.

        GUARDED ON EXISTENCE, because ``UpdateItem`` is an UPSERT. The same probe window that
        makes a whole-row put unsafe also lets ``unlink`` DELETE the row, and an unguarded
        ``update_item`` would then CREATE a five-field orphan (the key plus the three label
        fields). That row fails ``GitHubUserLink.model_validate``, and ``_from_item`` is applied
        to EVERY row of the partition by :meth:`_load_all_strict` — which ``complete_link`` calls
        through :meth:`_refuse_foreign_binding` — so one racing verify would stop every human on
        every connection from linking at all, with a raw ``ValidationError`` outside the ``.kind``
        contract and no service path able to delete the orphan again. ``attribute_exists(pk)``
        makes the write land only on a row that already exists, and a refusal is ``not_found`` —
        the same answer the local branch below gives, so the two branches agree exactly."""
        sk = _sort_key(row.principal_oid, row.connection_id)
        if self._has_ddb:
            try:
                resp = self._table.update_item(
                    Key={"pk": _PARTITION_KEY, "sk": sk},
                    UpdateExpression=(
                        "SET github_login = :login, last_verified_at = :ts, updated_at = :ts"
                    ),
                    ExpressionAttributeValues={":login": github_login, ":ts": ts},
                    ConditionExpression=Attr("pk").exists(),
                    ReturnValues="ALL_NEW",
                )
            except ClientError as exc:
                if exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
                    raise GitHubLinkError("Unknown GitHub link", kind="not_found") from None
                logger.exception("[github-link] could not update the label for link %s", row.id)
                raise GitHubLinkError("Failed to update the GitHub link", kind="secret_error") from None
            except BotoCoreError:
                # Separate for the same reason as ``_save_guarded``: the clause above reads
                # ``exc.response`` to recognize the existence guard's refusal, which only a
                # ``ClientError`` carries. A BotoCoreError never reached DynamoDB, so it proves
                # nothing about whether the row exists — it must NOT read as ``not_found``, which
                # the console would show as a vanished link.
                logger.exception("[github-link] could not update the label for link %s", row.id)
                raise GitHubLinkError("Failed to update the GitHub link", kind="secret_error") from None
            return self._from_item(resp["Attributes"])
        with self._local_lock:
            current = self._local.get(sk)
            if current is None:
                raise GitHubLinkError("Unknown GitHub link", kind="not_found")
            merged = current.model_copy(
                update={"github_login": github_login, "last_verified_at": ts, "updated_at": ts}
            )
            self._local[sk] = merged
            return merged.model_copy(deep=True)

    # ===================================================================== #
    # Validation
    # ===================================================================== #

    @staticmethod
    def _validate_ids(principal_oid: str, connection_id: Optional[str] = None) -> None:
        """Refuse a falsy or separator-bearing key half.

        A falsy ``principal_oid`` is the dev-auth path (design §1): a link with no Entra
        subject is unattributable. The ``#`` refusal keeps the composite sk injective —
        ``("a", "b#c")`` and ``("a#b", "c")`` both encode to ``"a#b#c"``, so without it one
        link would silently OVERWRITE another and the survivor would read back the wrong
        connection."""
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
        """The ``redirect_uri`` must be OUR callback, on OUR origin, with no extras.

        The path is compared to :data:`LINK_CALLBACK_PATH` exactly, so a caller cannot aim the
        authorization code at another route (GitHub also requires a byte-for-byte match against
        a registered Callback URL and forbids extra parameters, which is why a query or
        fragment is refused outright). ``https`` is required except on ``localhost``, where the
        dev server has no certificate — an authorization code over plain http on a real network
        is interceptable.

        THE ORIGIN IS CHECKED SERVER-SIDE, not just the path. A path-only check accepts
        ``https://evil.example.com<path>`` and lets an authenticated user have AGP mint state and
        vouch for an authorize URL aimed at an arbitrary host. GitHub's byte-for-byte match
        against its one registered callback is the only thing that would then stop the code
        reaching that host — a backstop AGP does not own, and one that evaporates the moment a
        customer registers a second callback URL. So the whole ORIGIN (scheme + host + port) is
        compared against ``allowed_origins`` by exact match: a substring or suffix test would
        accept ``console.example.com.evil.io``. Userinfo is refused outright — the authority in
        ``https://console.example.com@evil.example.com/…`` reads as ours at a glance and is not.

        ``allowed_origins`` empty (a service constructed without it) keeps the https/localhost
        rule as the floor rather than failing every link, since it is a T8 wiring concern — but it
        fails OPEN, so the miss is LOGGED rather than silent: with no list, the only thing between
        a user-supplied host and the authorization code is GitHub's own registered-callback match,
        the backstop this docstring says AGP does not own."""
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
            return  # fails open to the floor above; warned about once at construction
        origin = f"{parsed.scheme}://{parsed.netloc}".lower()
        if origin not in self._allowed_origins:
            raise GitHubLinkError("Invalid redirect_uri", kind="bad_request")

    def _refuse_foreign_binding(
        self, principal_oid: str, connection_id: str, github_id: int
    ) -> Optional[GitHubUserLink]:
        """Refuse a GitHub account already bound to a DIFFERENT Entra human, and return this
        principal's existing row for this connection (if any) so its secret can be purged.

        One GitHub account is one human, so two Entra oids claiming it would let either of them
        act as the other — the whole attribution claim collapses. The same human linking the
        same GitHub account on SEVERAL org connections is legitimate and passes.

        ONE whole-partition read answers both questions, so they cannot disagree. It is the
        STRICT read and it fails CLOSED — an unreadable partition must never be mistaken for
        "no conflict" (the ``ownership_unverified`` discipline from ``project_role_service``),
        because that is precisely how the binding this guard protects would be handed to the
        wrong person."""
        try:
            rows = self._load_all_strict()
        except _STORE_FAULTS:
            logger.exception(
                "[github-link] could not read the link partition, so a foreign GitHub binding "
                "cannot be ruled out"
            )
            raise GitHubLinkError(
                "Could not verify the GitHub account binding", kind="conflict"
            ) from None
        for row in rows:
            if row.github_id == github_id and row.principal_oid != principal_oid:
                raise GitHubLinkError(
                    "That GitHub account is linked to another user", kind="conflict"
                )
        return next(
            (
                r
                for r in rows
                if r.principal_oid == principal_oid and r.connection_id == connection_id
            ),
            None,
        )

    def _load_credentials(self, connection_id: str) -> "tuple[str, str]":
        """Resolve ``(client_id, client_secret)`` via the injected loader.

        ``oauth_client_missing`` IS A CLAIM ABOUT CONFIGURATION, NOT A CATCH-ALL. A
        DETERMINISTIC loader failure — "the org never pasted an OAuth client", "the connection is
        gone", "this is a GitHub Enterprise Server connection, which the github.com-only OAuth
        legs cannot serve" — is one actionable answer: an admin must fix the connection. Those
        arrive as a loader exception whose ``.kind`` is in :data:`_PERMANENT_LOADER_KINDS`.

        Everything else is TRANSIENT-OR-UNKNOWN and stays retryable (``secret_error``, this
        module's existing store-fault kind). The loader reads DynamoDB and Secrets Manager, so a
        throttle, a timeout or a KMS blip is an ordinary occurrence — and collapsing one into
        "no client configured" is not merely imprecise: :meth:`unlink` treats an unresolvable
        client as licence to purge WITHOUT revoking, so a blip would destroy the only token that
        can revoke a grant that is still live at GitHub. That is the exact defect the raising
        ``unlink`` was written to close, and it must not be reachable through this door.

        The loader's own message is DISCARDED either way, so a store-internal detail cannot reach
        an HTTP body."""
        try:
            client_id, client_secret = self._client_credentials_loader(connection_id)
        except Exception as exc:
            # Duck-typed on ``.kind`` — this module must not import ConnectionService.
            if getattr(exc, "kind", None) in _PERMANENT_LOADER_KINDS:
                logger.warning(
                    "[github-link] no usable OAuth client for connection %s", connection_id
                )
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
        """Translate a transport failure into this service's vocabulary.

        The transport's message is DROPPED, not re-wrapped: it is safe by construction but it
        is also the wrong altitude for a route, and re-using it is how provider text starts
        leaking into HTTP bodies. Only ``.kind`` crosses the boundary."""
        return GitHubLinkError(message, kind=_OAUTH_KIND_MAP.get(exc.kind, "provider_error"))

    # ===================================================================== #
    # Time helpers (injected clock only — no inline wall-clock read)
    # ===================================================================== #

    @staticmethod
    def _deadline(ts: datetime, seconds: Optional[int]) -> Optional[str]:
        """Absolute ISO-8601 expiry from a provider lifetime; ``None`` ⇒ non-expiring."""
        if not seconds:
            return None
        return (ts + timedelta(seconds=int(seconds))).isoformat()

    def _seconds_until(self, iso_ts: str) -> float:
        """Seconds from the injected clock to ``iso_ts``; an unparseable value reads as
        already expired (it is AGP's own field, so this is a bug-or-corruption path, and
        treating it as fresh would hand out a token that fails at GitHub)."""
        parsed = _parse_ts(iso_ts)
        if parsed is None:
            return -1.0
        return (parsed - self._now()).total_seconds()

    def _seconds_since(self, iso_ts: str) -> float:
        """Seconds from ``iso_ts`` to the injected clock; unparseable ⇒ infinitely old, so a
        corrupt claim timestamp is treated as ABANDONED rather than blocking forever."""
        parsed = _parse_ts(iso_ts)
        if parsed is None:
            return float("inf")
        return (self._now() - parsed).total_seconds()

    # ===================================================================== #
    # Secrets Manager (mirror connection_service)
    # ===================================================================== #

    def _secret_name(self, link_id: str) -> str:
        return f"{self.secret_prefix}{link_id}"

    def _create_secret(self, link_id: str, body: dict) -> str:
        """Create the ``{"access_token","refresh_token"}`` secret, overwriting a name a previous
        link already took (a re-link over the same sk, or a retry after a half-failed create).

        The ``ResourceExistsException`` fallback is NESTED rather than a sibling ``except``: a
        handler does not catch what another handler raises, so the ``put_secret_value`` on that
        path used to be able to throw a raw boto3 error straight past the translation below —
        exactly the 500 this guard pass exists to close, on the one call that decides whether a
        live ``ghu_``/``ghr_`` pair becomes findable at all."""
        name = self._secret_name(link_id)
        secret_string = json.dumps(body)
        try:
            try:
                resp = self._sm.create_secret(
                    Name=name, SecretString=secret_string, Tags=list(_SECRET_TAGS)
                )
            except self._sm.exceptions.ResourceExistsException:
                resp = self._sm.put_secret_value(SecretId=name, SecretString=secret_string)
            return resp["ARN"]
        except _STORE_FAULTS:
            logger.exception("[github-link] create_secret failed for link %s", link_id)
            raise GitHubLinkError("Failed to store the GitHub token", kind="secret_error") from None

    def _get_secret_body(self, link_id: str) -> dict:
        """Read + parse ``{"access_token", "refresh_token"}``. The value is NEVER logged —
        traceback only."""
        try:
            resp = self._sm.get_secret_value(SecretId=self._secret_name(link_id))
            body = json.loads(resp["SecretString"])
        except _STORE_FAULTS:
            logger.exception("[github-link] get_secret_value failed for link %s", link_id)
            raise GitHubLinkError("Failed to read the GitHub token", kind="secret_error") from None
        except ValueError:
            logger.exception("[github-link] secret body for link %s is not JSON", link_id)
            raise GitHubLinkError("Failed to read the GitHub token", kind="secret_error") from None
        return body if isinstance(body, dict) else {}

    def _put_secret_body(self, link_id: str, body: dict) -> None:
        try:
            self._sm.put_secret_value(
                SecretId=self._secret_name(link_id), SecretString=json.dumps(body)
            )
        except _STORE_FAULTS:
            logger.exception("[github-link] put_secret_value failed for link %s", link_id)
            raise GitHubLinkError("Failed to rotate the GitHub token", kind="secret_error") from None

    def _delete_secret_best_effort(self, link_id: str) -> None:
        """Delete the secret; a missing secret is success (unlink must be idempotent)."""
        try:
            self._sm.delete_secret(
                SecretId=self._secret_name(link_id), ForceDeleteWithoutRecovery=True
            )
        except self._sm.exceptions.ResourceNotFoundException:
            pass
        except _STORE_FAULTS:
            logger.exception("[github-link] delete_secret failed for link %s", link_id)

    def _stored_access_token(self, row: GitHubUserLink) -> str:
        token = self._get_secret_body(row.id).get("access_token")
        if not token or not isinstance(token, str):
            logger.error("[github-link] link %s has no usable stored access token", row.id)
            raise GitHubLinkError("Failed to read the GitHub token", kind="secret_error")
        return token

    # ===================================================================== #
    # Persistence (DDB-or-local, mirror connection_service / project_role_service)
    # ===================================================================== #

    def _require_row(self, principal_oid: str, connection_id: str) -> GitHubUserLink:
        """Load one link or raise. A store fault is ``secret_error`` (retryable 502), NEVER
        ``not_found``: telling a linked human "no link" would send them through the entire web
        flow again to fix a transient blip."""
        self._validate_ids(principal_oid, connection_id)
        try:
            row = self._get(principal_oid, connection_id)
        except _STORE_FAULTS:
            logger.exception("[github-link] could not read a link row")
            raise GitHubLinkError("Failed to read the GitHub link", kind="secret_error") from None
        if row is None:
            raise GitHubLinkError("Unknown GitHub link", kind="not_found")
        return row

    def _get(self, principal_oid: str, connection_id: str) -> Optional[GitHubUserLink]:
        """Strict read: a DDB ``ClientError`` PROPAGATES (see ``_require_row``)."""
        sk = _sort_key(principal_oid, connection_id)
        if self._has_ddb:
            item = self._table.get_item(Key={"pk": _PARTITION_KEY, "sk": sk}).get("Item")
            return self._from_item(item) if item else None
        with self._local_lock:
            record = self._local.get(sk)
            return record.model_copy(deep=True) if record else None

    def _load_for_principal_strict(self, principal_oid: str) -> List[GitHubUserLink]:
        prefix = f"{principal_oid}{_SK_SEPARATOR}"
        if self._has_ddb:
            return [self._from_item(i) for i in self._scan_partition(sk_prefix=prefix)]
        with self._local_lock:
            return [
                r.model_copy(deep=True) for sk, r in self._local.items() if sk.startswith(prefix)
            ]

    def _load_all_strict(self) -> List[GitHubUserLink]:
        """The whole link partition — the uniqueness guard's input. There is no GSI on
        ``github_id``, so "is this GitHub account bound elsewhere?" needs every row."""
        if self._has_ddb:
            return [self._from_item(i) for i in self._scan_partition()]
        with self._local_lock:
            return [r.model_copy(deep=True) for r in self._local.values()]

    def _save(self, record: GitHubUserLink) -> None:
        """The UNGUARDED whole-row write — ``complete_link``'s FIRST write of a link only.

        Version-blind on purpose and safe only here: a brand-new row has no concurrent claim to
        erase, and a re-link deliberately UPSERTS over the same composite sk. Every LATER writer
        goes through :meth:`_save_guarded` (module docstring). A store fault becomes this
        service's own retryable ``secret_error`` rather than a raw boto3 exception, which the
        route has no mapping for and would answer 500 — and which would ALSO skip
        ``complete_link``'s credential rollback, orphaning a live ``ghu_``/``ghr_`` pair with no
        row pointing at it. That caller therefore catches :class:`GitHubLinkError`."""
        if self._has_ddb:
            try:
                self._table.put_item(Item=self._to_item(record))
            except _STORE_FAULTS:
                logger.exception("[github-link] could not write link %s", record.id)
                raise GitHubLinkError(
                    "Failed to store the GitHub link", kind="secret_error"
                ) from None
            return
        with self._local_lock:
            self._local[_sort_key(record.principal_oid, record.connection_id)] = record.model_copy(
                deep=True
            )

    def _delete(self, principal_oid: str, connection_id: str) -> None:
        """Remove the link row (``unlink``'s last step). A store fault surfaces as the retryable
        ``secret_error`` rather than a raw boto3 error: by this point the grant IS revoked at
        GitHub and the secret IS deleted, so the row is a stale husk and the honest answer is
        "retry", not 500."""
        sk = _sort_key(principal_oid, connection_id)
        if self._has_ddb:
            try:
                self._table.delete_item(Key={"pk": _PARTITION_KEY, "sk": sk})
            except _STORE_FAULTS:
                logger.exception("[github-link] could not delete a link row")
                raise GitHubLinkError(
                    "Failed to remove the GitHub link", kind="secret_error"
                ) from None
            return
        with self._local_lock:
            self._local.pop(sk, None)

    # -- CSRF + PKCE state (SEPARATE partition; never surfaced by a link read) ------

    def _save_state(self, state: str, record: dict) -> None:
        """Persist the CSRF/PKCE state. A store fault ABORTS ``begin_link`` as a retryable
        ``secret_error``: without a stored state the callback could never be validated, so
        returning an authorize URL anyway would send the human to GitHub to earn a code this
        service is guaranteed to refuse. The message is fixed and names no store detail —
        ``record`` holds the PKCE ``code_verifier``, and nothing from it may reach a log or a
        body."""
        if self._has_ddb:
            try:
                self._table.put_item(Item={"pk": _STATE_PARTITION_KEY, "sk": state, **record})
            except _STORE_FAULTS:
                logger.exception("[github-link] could not store a link state")
                raise GitHubLinkError(
                    "Failed to start the GitHub authorization", kind="secret_error"
                ) from None
            return
        with self._local_lock:
            self._state[state] = dict(record)

    def _get_state(self, state: str) -> Optional[dict]:
        if self._has_ddb:
            try:
                item = self._table.get_item(
                    Key={"pk": _STATE_PARTITION_KEY, "sk": state}
                ).get("Item")
            except _STORE_FAULTS:
                logger.exception("[github-link] could not read a link state")
                return None
            return {k: v for k, v in item.items() if k not in ("pk", "sk")} if item else None
        with self._local_lock:
            record = self._state.get(state)
            return dict(record) if record else None

    def _delete_state(self, state: str) -> None:
        """Consume the state. A store fault RAISES rather than continuing, because this delete is
        what makes the state single-use: proceeding on a failed delete would exchange the code
        against a state row still present in the table, i.e. spend a state that is still
        replayable. ``complete_link`` calls this BEFORE the exchange, so aborting here costs
        nothing — no code has been redeemed and the row still expires at ``exp``. Retryable
        (``secret_error``), since a throttle is not the human's error."""
        if self._has_ddb:
            try:
                self._table.delete_item(Key={"pk": _STATE_PARTITION_KEY, "sk": state})
            except _STORE_FAULTS:
                logger.exception("[github-link] could not consume a link state")
                raise GitHubLinkError(
                    "Failed to complete the GitHub authorization", kind="secret_error"
                ) from None
            return
        with self._local_lock:
            self._state.pop(state, None)

    def _scan_partition(self, sk_prefix: str = "") -> List[dict]:
        condition = Key("pk").eq(_PARTITION_KEY)
        if sk_prefix:
            condition = condition & Key("sk").begins_with(sk_prefix)
        items: List[dict] = []
        kwargs = {"KeyConditionExpression": condition}
        while True:
            resp = self._table.query(**kwargs)
            items.extend(resp.get("Items", []))
            lek = resp.get("LastEvaluatedKey")
            if not lek:
                break
            kwargs["ExclusiveStartKey"] = lek
        return items

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
    """The composite sk — ``<principal_oid>#<connection_id>``. Derived, not generated, which
    is what makes a re-link an UPSERT over the same row."""
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
    """One short-lived ``httpx.Client`` per operation, used as a context manager so it is
    always closed. Every GitHub call is injected, so tests never construct one."""
    return httpx.Client(timeout=15.0)
