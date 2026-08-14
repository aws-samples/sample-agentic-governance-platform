"""Service layer for Git org Connections (E19) — persistence + secrets + verify orchestration.

Mirrors the existing real-backend patterns:
  - DynamoDB-or-local persistence à la ``marketplace_service.py`` (the ``_has_ddb`` guard,
    ``boto3.resource("dynamodb")`` + ``.Table(name)``, a local dict + ``threading.Lock``,
    serialize via ``{"pk":..., "sk":..., **json.loads(model.model_dump_json())}`` and
    deserialize via ``Connection.model_validate(clean)``). ``pk="connection"``, ``sk=<id>``.
  - AWS Secrets Manager write/read/delete à la ``langfuse_provisioning.py``
    (``create_secret`` with ``ResourceExistsException → put_secret_value``,
    ``get_secret_value``, ``delete_secret(ForceDeleteWithoutRecovery=True)``).
  - ``httpx`` provider verification à la ``connection_verify.verify_connection`` (injectable).

SECURITY (the whole point — mirror ``agent_credential_service.py``): the credential lives
ONLY in Secrets Manager. The DDB record + every read model carry only ``secret_arn`` /
``has_secret`` / metadata — NEVER the credential. It is never logged, never echoed in an
error, never written to DDB. ``ConnectionError`` carries a SAFE ``.message`` + a ``.kind``
hint the route maps to a fixed HTTP status + fixed detail literal (never ``str(exc)``).

AUTH TYPES & THE SECRET BODY (E20/T9): the secret body is JSON so its shape can vary by
auth type. A ``pat`` connection stores ``{"token": <PAT>}``; a ``github_app`` connection
stores ``{"private_key": <PEM>}`` (the App's ``app_id``/``installation_id`` are NON-secret
and live on the DDB record / read model). ``get_bearer_token`` is the seam every downstream
caller uses: for a PAT it returns the stored token; for a GitHub App it reads the stored
private key + the record's app_id/installation_id and MINTS a fresh, short-lived, org-scoped
installation access token on demand (``github_app_auth.mint_installation_token``, injected).
The private key and every minted token are never logged and never surfaced in an error.

A ``github_app`` secret body may carry a THIRD key, ``client_secret`` — the App's OAuth
client secret (E27B), captured from the manifest conversion or pasted once by an admin
(GitHub exposes it through no API). Its partner ``client_id`` is NON-secret and lives on the
record; the read model says only ``has_oauth_client``. ``get_oauth_client_credentials`` is the
single reader, injected into the per-user link service.

POST ordering & rollback (spec §5): verify FIRST → ``create_secret`` → persist the record.
A failed verify stores NOTHING. If ``create_secret`` succeeds but the subsequent persist
raises, best-effort ``delete_secret`` to avoid an orphaned secret, then re-raise as a
``ConnectionError(kind="secret_error")``.

Determinism: the clock (``now``) and id source (``new_id``) are injectable; tests pass a
fixed clock + a deterministic id iterator. No ``datetime.now()`` sprinkled inline.
"""

from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timezone
from typing import Dict, List, Optional
from uuid import uuid4

import boto3
import httpx
from boto3.dynamodb.conditions import Key
from botocore.exceptions import BotoCoreError, ClientError

from models.connection import AuthType, Connection, ConnectionCreate, ConnStatus, Provider
from services.connection_verify import verify_connection
from services.github_app_auth import mint_installation_token as _mint_installation_token
from services.github_app_manifest import GitHubManifestError
from services.github_app_manifest import convert_manifest_code as _convert_manifest_code
from services.github_app_manifest import fetch_app_client_id as _fetch_app_client_id
from services.github_app_manifest import resolve_installation_id as _resolve_installation_id

logger = logging.getLogger(__name__)

# Every boto3 store fault, BOTH families. ``BotoCoreError`` is NOT a ``ClientError`` subclass
# (they are disjoint siblings in botocore), so a guard naming only ``ClientError`` lets an
# endpoint/DNS/connect-timeout failure propagate RAW — which answered HTTP 500 on
# ``GET /me/github-link`` and ``POST /me/github-link/{id}/verify``, outside this epic's pinned
# {400,404,409,502}. Never widen a store guard to one family only.
_STORE_FAULTS = (ClientError, BotoCoreError)

_PARTITION_KEY = "connection"  # single partition (single-partition list via query(pk))
_STATE_PARTITION_KEY = "conn_state"  # CSRF manifest state — a SEPARATE partition (never in list)
_STATE_TTL_SECONDS = 900  # short-lived CSRF state (15 min)

# SM secret body tag keys (spec §3).
_TAG_MANAGED_BY = "agp"


class ConnectionError(Exception):
    """A connection operation failed. Carries a SAFE message (never a token / secret) and a
    ``.kind`` hint the route maps to a fixed HTTP status + fixed detail literal:
    ``{"verify_failed","not_found","conflict","secret_error","bad_request"}``."""

    def __init__(self, message: str, kind: str) -> None:
        super().__init__(message)
        self.message = message
        self.kind = kind


class ConnectionService:
    def __init__(
        self,
        *,
        table_name: str = "",
        secret_prefix: str = "",
        region: str = "us-east-1",
        verify=verify_connection,
        secrets_client=None,
        new_id=lambda: str(uuid4()),
        now=lambda: datetime.now(timezone.utc),
        mint_installation_token=_mint_installation_token,
        convert_manifest_code=_convert_manifest_code,
        resolve_installation_id=_resolve_installation_id,
        fetch_app_client_id=_fetch_app_client_id,
        ecr_push_role_service=None,
        github_oidc_provider_service=None,
    ) -> None:
        self.table_name = table_name
        self.secret_prefix = secret_prefix
        self.region = region
        self._verify = verify
        self._new_id = new_id
        self._now = now
        # Per-org GitHub-OIDC ECR-push role provisioner (E22 multi-org). None ⇒ role
        # lifecycle is skipped (connections still work; repos fall back to the platform
        # default push role). Injectable for tests.
        self._ecr_push_roles = ecr_push_role_service
        # Account-global GitHub Actions OIDC provider bootstrap. Git-provider integrations
        # are a PLATFORM capability, not a deploy-time one: Terraform ships zero GitHub
        # artifacts, and the FIRST GitHub connection creates the provider (+ the shared
        # fallback push role, whose trust names it) if the account lacks them. None ⇒
        # skipped. Injectable for tests.
        self._github_oidc = github_oidc_provider_service
        # GitHub App installation-token minter (E20/T9) — injectable for tests.
        self._mint_installation_token = mint_installation_token
        # GitHub App manifest converter/resolver (E20/U2) — injectable so tests avoid live GitHub.
        self._convert_manifest_code = convert_manifest_code
        self._resolve_installation_id = resolve_installation_id
        # App OAuth ``client_id`` lookup (E27B) — the verification seam for an admin-pasted
        # pair, and the recovery path for Apps onboarded before AGP captured the pair.
        # Injectable so tests never call live GitHub.
        self._fetch_app_client_id = fetch_app_client_id

        # Secrets Manager client — injectable for tests (moto), lazily built otherwise.
        self._sm = secrets_client or boto3.client("secretsmanager", region_name=region)

        self._ddb = None
        self._table = None
        if table_name:
            try:
                self._ddb = boto3.resource("dynamodb", region_name=region)
                self._table = self._ddb.Table(table_name)
            except Exception:  # pragma: no cover — degrade to local fallback.
                self._table = None

        # Local fallback cache (used when no DDB table is configured).
        self._local: Dict[str, Connection] = {}
        # CSRF manifest state — a SECOND local dict under the SAME lock (never in _local).
        self._state: Dict[str, dict] = {}
        self._local_lock = threading.Lock()

    # -- mode helper --------------------------------------------------------

    @property
    def _has_ddb(self) -> bool:
        return bool(self.table_name) and self._table is not None

    # ===================================================================== #
    # Public API
    # ===================================================================== #

    def list_connections(self) -> List[Connection]:
        return self._load_all()

    def get_connection(self, id: str) -> Connection:
        record = self._get(id)
        if record is None:
            raise ConnectionError("Unknown connection", kind="not_found")
        return record

    def create_connection(self, body: ConnectionCreate, created_by: str) -> Connection:
        """Verify a connection's credential, store the secret, and persist the record.

        Dispatches on ``body.auth_type`` (same verify-FIRST / rollback-on-persist-failure
        ordering for both paths — a failed verify stores NOTHING):

        - ``pat``: verify the PAT directly, store ``{"token": <PAT>}``.
        - ``github_app``: MINT a fresh installation token from the App private key +
          app_id/installation_id, verify org-visibility with that minted token (the SAME
          ``verify_connection`` probe — App-ness never reaches the probe), then store
          ``{"private_key": <PEM>}`` and persist app_id/installation_id (NON-secret) on the
          record. The private key lives ONLY in Secrets Manager; the minted token is
          transient and never stored/logged.
        """
        conn_id = self._new_id()

        # 1) Verify FIRST — a failed verify stores NOTHING (no secret, no record).
        #    For a GitHub App we must first mint an installation token to verify with.
        if body.auth_type == AuthType.GITHUB_APP:
            verify_token = self._mint_for_app(
                body.app_id, body.installation_id, body.private_key, body.base_url
            )
            secret_body = {"private_key": body.private_key}
        else:
            verify_token = body.token
            secret_body = {"token": body.token}

        result = self._run_verify(
            body.provider, body.org, body.base_url, verify_token,
            is_app=(body.auth_type == AuthType.GITHUB_APP),
        )
        if not result.ok:
            raise ConnectionError(result.reason or "verification failed", kind="verify_failed")

        # 2) Create the SM secret (capture the ARN). The credential lives ONLY here.
        secret_arn = self._create_secret(conn_id, secret_body)

        # 2b) Ensure this org's per-org GitHub-OIDC ECR-push role exists (E22 multi-org),
        #     bootstrapping the account's OIDC provider + shared fallback role first for a
        #     GitHub connection. Idempotent; None when the provisioner is inert/absent.
        ecr_push_role_arn = self._ensure_ecr_push_role(body.provider, body.org)

        # 3) Persist the non-secret record. If this raises AFTER the secret was created,
        #    best-effort delete the secret to avoid an orphan, then surface secret_error.
        ts = self._now().isoformat()
        record = Connection(
            id=conn_id,
            provider=body.provider,
            org=body.org,
            base_url=body.base_url,
            auth_type=body.auth_type,
            app_id=body.app_id,
            installation_id=body.installation_id,
            status=ConnStatus.CONNECTED,
            status_detail=None,
            account_login=result.account_login,
            secret_arn=secret_arn,
            has_secret=True,
            ecr_push_role_arn=ecr_push_role_arn,
            last_verified_at=ts,
            created_by=created_by,
            created_at=ts,
            updated_at=ts,
        )
        try:
            self._save(record)
        except Exception:
            logger.exception("[connections] persist failed for %s; rolling back secret", conn_id)
            self._delete_secret_best_effort(conn_id)
            raise ConnectionError("Failed to persist connection record", kind="secret_error") from None
        return record

    # ------------------------------------------------------------------ #
    # App-via-Manifest lifecycle (E20/U2)
    # ------------------------------------------------------------------ #

    def create_manifest_state(
        self, org: str, base_url: Optional[str], created_by: str
    ) -> str:
        """Issue a single-use, short-lived CSRF ``state`` for the manifest flow.

        Persists a state record (a SEPARATE partition from connections) carrying the org,
        base_url, issuer, and an absolute expiry (``exp`` = now + 15 min). Returns the state
        token to embed in the GitHub registration URL. ``consume_manifest_state`` reads it
        back exactly once."""
        state = self._new_id()
        record = {
            "org": org,
            "base_url": base_url,
            "created_by": created_by,
            "exp": int(self._now().timestamp()) + _STATE_TTL_SECONDS,
        }
        self._save_state(state, record)
        return state

    def consume_manifest_state(self, state: str) -> dict:
        """Load + delete a manifest state (single-use). An unknown or expired state raises
        ``bad_request`` (the fixed detail is safe — never echoes the state). Returns
        ``{org, base_url, created_by}``."""
        record = self._get_state(state)
        self._delete_state(state)  # single-use regardless of validity
        if record is None or record.get("exp", 0) < int(self._now().timestamp()):
            raise ConnectionError("manifest state expired or unknown", kind="bad_request")
        return {
            "org": record.get("org"),
            "base_url": record.get("base_url"),
            "created_by": record.get("created_by"),
        }

    def complete_manifest(
        self, code: str, state: str
    ) -> "tuple[Connection, bool, Optional[str]]":
        """Finish the manifest handshake: consume the CSRF state, convert the one-time
        ``code`` into the App's credentials, persist a PENDING connection, then try to
        resolve the installation.

        Returns ``(connection, needs_install, install_url)``:
          - resolved → finalize + return ``(connected_conn, False, None)``.
          - not installed yet → return ``(pending_conn, True, install_url)`` where
            ``install_url`` points at the App's org-install page. A pending record that
            simply is not installed yet is NOT an error.

        A convert failure surfaces as ``verify_failed`` (SAFE message); no partial record is
        persisted before the convert succeeds.

        The conversion also yields the App's OAuth ``client_id``/``client_secret`` (E27B),
        forwarded to ``create_pending_app_connection``. Both are read defensively — GitHub's
        documentation contradicts its own schema on whether they are returned — so an absent
        pair degrades to "per-user linking unavailable for this connection" (recoverable via
        ``set_oauth_client``) and never blocks onboarding."""
        ctx = self.consume_manifest_state(state)
        org = ctx["org"]
        base_url = ctx["base_url"]
        created_by = ctx["created_by"]

        client = httpx.Client()
        try:
            converted = self._convert_manifest_code(code, client=client, base_url=base_url)
        except Exception as exc:
            # github_app_manifest raises a SAFE message; surface as a verify failure.
            raise ConnectionError(str(exc), kind="verify_failed") from None
        finally:
            client.close()

        conn = self.create_pending_app_connection(
            org=org,
            base_url=base_url,
            app_id=converted["app_id"],
            private_key=converted["pem"],
            webhook_secret=converted.get("webhook_secret"),
            created_by=created_by,
            slug=converted.get("slug"),
            client_id=converted.get("client_id"),
            client_secret=converted.get("client_secret"),
        )

        installation_id = self._resolve_for_record(conn, converted["pem"])
        if installation_id is not None:
            connected = self.finalize_app_connection(conn.id, installation_id)
            return connected, False, None

        slug = converted.get("slug")
        install_url = f"https://github.com/apps/{slug}/installations/new"
        return conn, True, install_url

    def create_pending_app_connection(
        self,
        org: str,
        base_url: Optional[str],
        app_id: str,
        private_key: str,
        webhook_secret: Optional[str],
        created_by: str,
        *,
        slug: Optional[str] = None,
        client_id: Optional[str] = None,
        client_secret: Optional[str] = None,
    ) -> Connection:
        """Persist a PENDING GitHub App connection (create_connection's App branch MINUS
        verify). Stores ``{"private_key": pem, "webhook_secret": ws}`` in Secrets Manager,
        then persists a record with ``status=PENDING`` and a null installation_id (the App
        is not installed/verified yet). Same secret-then-persist rollback: a persist failure
        best-effort deletes the secret and surfaces ``secret_error``.

        ``client_id``/``client_secret`` are the App's OAuth pair (E27B), captured from the
        manifest conversion when GitHub returns them. Both are OPTIONAL: a conversion that
        omits them must still onboard the App — it only means per-user linking is unavailable
        for this connection until an admin pastes the secret (``set_oauth_client``). The
        ``client_id`` is NON-secret and goes on the record; the secret becomes a THIRD key in
        the secret body, written only when present (never a null placeholder)."""
        conn_id = self._new_id()

        secret_body = {"private_key": private_key, "webhook_secret": webhook_secret}
        if client_secret:
            secret_body["client_secret"] = client_secret
        secret_arn = self._create_secret(conn_id, secret_body)

        ts = self._now().isoformat()
        record = Connection(
            id=conn_id,
            provider=Provider.GITHUB,
            org=org,
            base_url=base_url,
            auth_type=AuthType.GITHUB_APP,
            app_id=app_id,
            installation_id=None,
            client_id=client_id,
            has_oauth_client=bool(client_secret),
            status=ConnStatus.PENDING,
            status_detail=None,
            account_login=None,
            secret_arn=secret_arn,
            has_secret=True,
            last_verified_at=None,
            created_by=created_by,
            created_at=ts,
            updated_at=ts,
        )
        try:
            self._save(record)
        except Exception:
            logger.exception("[connections] persist failed for %s; rolling back secret", conn_id)
            self._delete_secret_best_effort(conn_id)
            raise ConnectionError("Failed to persist connection record", kind="secret_error") from None
        return record

    def finalize_app_connection(
        self, id: str, installation_id: Optional[str] = None
    ) -> Connection:
        """Finalize a PENDING GitHub App connection: resolve the installation (if not given),
        mint an installation token, run the org-visibility probe, and on success flip the
        record to CONNECTED.

        Not a GitHub App → ``verify_failed``. If ``installation_id`` is None it is resolved
        from the org's installations (None → ``verify_failed`` "not installed yet"). A verify
        failure leaves the record PENDING and does NOT persist the installation_id."""
        record = self.get_connection(id)
        if record.auth_type != AuthType.GITHUB_APP:
            raise ConnectionError(
                "connection is not a GitHub App connection", kind="verify_failed"
            )

        pem = self._get_secret_body(id).get("private_key", "")

        if installation_id is None:
            installation_id = self._resolve_for_record(record, pem)
            if installation_id is None:
                raise ConnectionError(
                    "App is not installed on the org yet", kind="verify_failed"
                )

        verify_token = self._mint_for_app(record.app_id, installation_id, pem, record.base_url)
        result = self._run_verify(
            record.provider, record.org, record.base_url, verify_token, is_app=True
        )
        if not result.ok:
            # Record stays PENDING; installation_id is NOT persisted.
            raise ConnectionError(result.reason or "verification failed", kind="verify_failed")

        # The App connection becomes usable now (CONNECTED) — ensure its per-org ECR-push
        # role here (E22 multi-org), not at pending-create. Idempotent; None when inert.
        ecr_push_role_arn = self._ensure_ecr_push_role(record.provider, record.org)

        ts = self._now().isoformat()
        record.installation_id = installation_id
        record.status = ConnStatus.CONNECTED
        record.status_detail = None
        if result.account_login is not None:
            record.account_login = result.account_login
        record.ecr_push_role_arn = ecr_push_role_arn
        record.last_verified_at = ts
        record.updated_at = ts
        self._save(record)
        return record

    def _resolve_for_record(self, record: Connection, pem: str) -> Optional[str]:
        """Resolve the org's installation id via the injected resolver over a fresh
        per-call ``httpx.Client``. ``now_epoch`` comes from the injected clock. A resolver
        failure surfaces as ``verify_failed`` (SAFE message). The client is always closed."""
        now_epoch = int(self._now().timestamp())
        client = httpx.Client()
        try:
            return self._resolve_installation_id(
                record.app_id, pem, record.org,
                client=client, base_url=record.base_url, now_epoch=now_epoch,
            )
        except Exception as exc:
            raise ConnectionError(str(exc), kind="verify_failed") from None
        finally:
            client.close()

    def test_connection(self, id: str) -> Connection:
        record = self.get_connection(id)
        # Re-verify with a fresh bearer token — a PAT is returned as-is; a GitHub App
        # mints a new installation token (the auth-type seam, so App re-test works too).
        token = self.get_bearer_token(id)
        result = self._run_verify(
            record.provider, record.org, record.base_url, token,
            is_app=(record.auth_type == AuthType.GITHUB_APP),
        )

        ts = self._now().isoformat()
        record.status = ConnStatus.CONNECTED if result.ok else ConnStatus.ERROR
        record.status_detail = None if result.ok else result.reason
        if result.account_login is not None:
            record.account_login = result.account_login
        if result.ok:
            record.last_verified_at = ts
        record.updated_at = ts
        self._save(record)
        return record

    def replace_token(self, id: str, new_token: str) -> Connection:
        record = self.get_connection(id)
        # Verify the NEW token BEFORE touching the stored secret.
        result = self._run_verify(record.provider, record.org, record.base_url, new_token)
        if not result.ok:
            # Secret unchanged on failure.
            raise ConnectionError(result.reason or "verification failed", kind="verify_failed")

        self._put_secret_token(id, new_token)

        ts = self._now().isoformat()
        record.status = ConnStatus.CONNECTED
        record.status_detail = None
        if result.account_login is not None:
            record.account_login = result.account_login
        record.last_verified_at = ts
        record.updated_at = ts
        self._save(record)
        return record

    def replace_key(self, id: str, new_private_key: str) -> Connection:
        """Rotate a GitHub App connection's private key. Verifies the NEW key (mint an
        installation token, then run the org-visibility probe) BEFORE overwriting the
        stored secret — a failed verify leaves the stored key unchanged. 400 (via
        ``verify_failed``) if the connection is not a GitHub App connection."""
        record = self.get_connection(id)
        if record.auth_type != AuthType.GITHUB_APP:
            raise ConnectionError(
                "connection is not a GitHub App connection", kind="verify_failed"
            )
        verify_token = self._mint_for_app(
            record.app_id, record.installation_id, new_private_key, record.base_url
        )
        result = self._run_verify(
            record.provider, record.org, record.base_url, verify_token, is_app=True
        )
        if not result.ok:
            # Secret unchanged on failure.
            raise ConnectionError(result.reason or "verification failed", kind="verify_failed")

        # MERGE, never a full-body replace: since E27B the body also carries ``client_secret``,
        # which IS read back (``get_oauth_client_credentials``). Clobbering it here would break
        # every per-user link on this org while ``has_oauth_client`` still said True.
        body = self._get_secret_body(id)
        body["private_key"] = new_private_key
        self._put_secret_body(id, body)

        ts = self._now().isoformat()
        record.status = ConnStatus.CONNECTED
        record.status_detail = None
        if result.account_login is not None:
            record.account_login = result.account_login
        record.last_verified_at = ts
        record.updated_at = ts
        self._save(record)
        return record

    # ------------------------------------------------------------------ #
    # App OAuth client — the per-user link seam (E27B)
    # ------------------------------------------------------------------ #

    def get_oauth_client_credentials(self, connection_id: str) -> "tuple[str, str]":
        """Return ``(client_id, client_secret)`` for a GitHub App connection's OAuth client.

        This is the loader the per-user link service is injected with — it is the ONLY reader
        of the ``client_secret`` half, which lives beside ``private_key`` in the connection's
        Secrets Manager body and never appears on a read model.

        Raises ``ConnectionError(kind="bad_request")`` when either half is absent (the org
        still needs the one-time admin paste — ``set_oauth_client``), or when
        ``record.base_url`` is set: the OAuth authorize/token legs are github.com-only, and a
        web base cannot be derived from an API base (design §3), so a GitHub Enterprise
        connection is refused outright rather than sent to the wrong host.

        The secret is never logged and never included in an error message."""
        record = self.get_connection(connection_id)
        if record.base_url:
            raise ConnectionError(
                "per-user GitHub linking is available for github.com connections only",
                kind="bad_request",
            )
        client_id = record.client_id
        client_secret = self._get_secret_body(connection_id).get("client_secret")
        if not client_id or not client_secret:
            raise ConnectionError(
                "connection has no GitHub OAuth client", kind="bad_request"
            )
        return client_id, client_secret

    def set_oauth_client(self, id: str, client_id: str, client_secret: str) -> Connection:
        """Store an admin-supplied OAuth client on a GitHub App connection.

        GitHub exposes a client secret through NO API after the App is created, so for an App
        onboarded before AGP captured the pair at manifest conversion this paste is the only
        way to enable per-user linking.

        Mirrors ``replace_key``'s verify-then-write discipline: the supplied ``client_id`` is
        checked against ``GET /app`` (authenticated with the App's own stored private key)
        BEFORE anything is written, so a pasted pair belonging to a different App is refused
        with ``verify_failed`` and leaves the stored secret UNCHANGED. On success the secret is
        MERGED into the existing body so ``private_key``/``webhook_secret`` survive.

        Not a GitHub App connection → ``verify_failed``. The pasted secret never reaches a log
        line or an error message (``connections.py`` surfaces ``verify_failed`` messages to the
        HTTP client)."""
        record = self.get_connection(id)
        if record.auth_type != AuthType.GITHUB_APP:
            raise ConnectionError(
                "connection is not a GitHub App connection", kind="verify_failed"
            )

        private_key = self._get_secret_body(id).get("private_key", "")

        now_epoch = int(self._now().timestamp())
        client = httpx.Client()
        try:
            actual = self._fetch_app_client_id(
                record.app_id, private_key,
                client=client, base_url=record.base_url, now_epoch=now_epoch,
            )
        except GitHubManifestError as exc:
            # ``github_app_manifest`` raises a SAFE message (fixed string + status code).
            raise ConnectionError(str(exc), kind="verify_failed") from None
        finally:
            client.close()

        if actual != client_id:
            # Secret unchanged on failure. The message names neither value.
            raise ConnectionError(
                "client_id does not match the App", kind="verify_failed"
            )

        # MERGE — never overwrite private_key / webhook_secret.
        body = self._get_secret_body(id)
        body["client_secret"] = client_secret
        self._put_secret_body(id, body)

        record.client_id = client_id
        record.has_oauth_client = True
        record.updated_at = self._now().isoformat()
        self._save(record)
        return record

    def get_bearer_token(self, connection_id: str) -> str:
        """The auth-type seam every downstream caller uses to get a ``Bearer`` token.

        - ``pat`` connection → the stored PAT (``{"token": ...}``).
        - ``github_app`` connection → read the stored private key + the record's
          app_id/installation_id and MINT a fresh, short-lived, org-scoped installation
          access token (``github_app_auth.mint_installation_token``). Mint-per-call (KISS —
          no caching); the minted token is transient and never stored or logged.

        The returned token is used as ``Authorization: Bearer {token}`` by the verify probe
        and the GitHub write client unchanged — App-ness is resolved entirely here."""
        record = self.get_connection(connection_id)
        if record.auth_type == AuthType.GITHUB_APP:
            private_key = self._get_secret_body(connection_id).get("private_key", "")
            return self._mint_for_app(
                record.app_id, record.installation_id, private_key, record.base_url
            )
        return self._get_secret_token(connection_id)

    def delete_connection(self, id: str) -> None:
        record = self.get_connection(id)
        self._delete(record.id)
        self._delete_secret_best_effort(record.id)
        # Tear down this org's per-org ECR-push role (E22 multi-org) — but ONLY if no OTHER
        # connection still uses the same org (two connections to one org share one role).
        self._delete_ecr_push_role_if_orphaned(record.org)

    # -- per-org ECR-push role lifecycle (E22 multi-org) --------------------

    def _ensure_github_oidc_bootstrap(self, provider: Provider, org: str) -> None:
        """Bootstrap the account's GitHub-OIDC objects on the FIRST GitHub connection.

        Git-provider integrations are a platform capability: Terraform deploys with zero
        GitHub artifacts, so a customer who never connects GitHub never carries a GitHub
        dependency, and a future GitLab integration bootstraps its own the same way. That
        promise is what the ``provider`` gate enforces — a GitLab-only account gets no
        ``token.actions.githubusercontent.com`` provider from this path, ever.

        Two objects, in this ORDER, because IAM validates a role's ``Federated`` principal
        at create time — the provider must exist before any role that trusts it:

          1. the account-global ``token.actions.githubusercontent.com`` OIDC provider
          2. the shared platform-default ECR-push role (was ``modules/agent_ecr``'s)

        Both are idempotent get-or-create, so every connection after the first is two
        no-op reads. Best-effort, matching ``_ensure_ecr_push_role``: a failure here is
        logged, never raised — a connection is not blocked by an IAM bootstrap, and the
        per-org ensure that follows reports its own outcome."""
        if provider != Provider.GITHUB or self._github_oidc is None:
            return
        try:
            if self._github_oidc.ensure_provider() is None:
                return
        except Exception:
            logger.exception("[connections] GitHub OIDC provider ensure failed (non-fatal)")
            return
        if self._ecr_push_roles is None:
            return
        try:
            self._ecr_push_roles.ensure_shared_role(org)
        except Exception:
            logger.exception("[connections] shared ECR-push role ensure failed (non-fatal)")

    def _ensure_ecr_push_role(self, provider: Provider, org: str) -> Optional[str]:
        """Ensure this org's ECR-push role and return its ARN. No-op → None when the
        provisioner is absent/inert. A provisioning failure must NOT block the connection
        (repos fall back to the platform-default push role), so it is logged, not raised.

        Bootstraps the account's OIDC provider + shared fallback role FIRST — the per-org
        role's trust names the provider, and IAM rejects a role whose ``Federated``
        principal does not exist yet. Ordering the two here rather than at the call sites
        is what makes it impossible to add a third connect path that provisions a role
        against a provider that was never created."""
        self._ensure_github_oidc_bootstrap(provider, org)
        if self._ecr_push_roles is None:
            return None
        try:
            return self._ecr_push_roles.ensure_role(org)
        except Exception:
            logger.exception("[connections] ECR-push role ensure failed for org (non-fatal)")
            return None

    def _delete_ecr_push_role_if_orphaned(self, org: str) -> None:
        """Delete the org's ECR-push role unless another connection still uses that org.
        Best-effort — a teardown failure is logged, never raised (the connection is already
        gone; an orphaned role is a cleanup concern, not a caller-facing error)."""
        if self._ecr_push_roles is None:
            return
        still_used = any(c.org == org for c in self._load_all())
        if still_used:
            logger.info("[connections] org still has another connection; keeping ECR-push role")
            return
        try:
            self._ecr_push_roles.delete_role(org)
        except Exception:
            logger.exception("[connections] ECR-push role delete failed for org (non-fatal)")

    # ===================================================================== #
    # Verify orchestration
    # ===================================================================== #

    def _mint_for_app(
        self, app_id: Optional[str], installation_id: Optional[str],
        private_key: Optional[str], base_url: Optional[str],
    ) -> str:
        """Mint a GitHub App installation access token via the injected minter, over a
        fresh per-call ``httpx.Client``. ``now_epoch`` is derived from the injected clock
        (no inline wall-clock read). A mint failure surfaces as ``verify_failed`` (the
        cause message is SAFE — never the key/token/body). The client is always closed."""
        now_epoch = int(self._now().timestamp())
        client = httpx.Client()
        try:
            return self._mint_installation_token(
                app_id, installation_id, private_key,
                client=client, base_url=base_url, now_epoch=now_epoch,
            )
        except Exception as exc:
            # ``github_app_auth`` raises a SAFE message; surface it as a verify failure.
            raise ConnectionError(str(exc), kind="verify_failed") from None
        finally:
            client.close()

    def _run_verify(
        self, provider: Provider, org: str, base_url: Optional[str], token: str,
        *, is_app: bool = False,
    ):
        """Run the injected verify with a fresh per-call no-auth ``httpx.Client``.

        ``verify_connection`` sets ``Authorization: Bearer {token}`` on each request itself,
        so the client carries NO pre-set auth header. ``is_app`` selects the GitHub App
        installation-token probe (``/installation/repositories``) instead of the PAT ``/user``
        probe — an installation token has no user identity. The client is closed after."""
        client = httpx.Client()
        try:
            return self._verify(provider, org, base_url, token, client=client, is_app=is_app)
        finally:
            client.close()

    # ===================================================================== #
    # Secrets Manager (mirror langfuse_provisioning.py)
    # ===================================================================== #

    def _secret_name(self, id: str) -> str:
        return f"{self.secret_prefix}{id}"

    def _create_secret(self, id: str, body: dict) -> str:
        """Create the per-connection secret and return its ARN. ``body`` is the JSON secret
        payload — ``{"token": <PAT>}`` for a PAT connection or ``{"private_key": <PEM>}`` for
        a GitHub App connection. On a pre-existing secret name, overwrite via
        ``put_secret_value`` and resolve the ARN. SM fault (BOTH boto3 families) →
        ``secret_error`` (the credential / exception value are NEVER logged — traceback only)."""
        name = self._secret_name(id)
        secret_string = json.dumps(body)
        try:
            resp = self._sm.create_secret(
                Name=name,
                SecretString=secret_string,
                Tags=[
                    {"Key": "managed_by", "Value": _TAG_MANAGED_BY},
                    {"Key": "connection_id", "Value": id},
                ],
            )
            return resp["ARN"]
        except self._sm.exceptions.ResourceExistsException:
            resp = self._sm.put_secret_value(SecretId=name, SecretString=secret_string)
            return resp["ARN"]
        except _STORE_FAULTS:
            logger.exception("[connections] create_secret failed for %s", id)
            raise ConnectionError("Failed to store connection secret", kind="secret_error") from None

    def _get_secret_body(self, id: str) -> dict:
        """Read + parse the JSON secret body (``{"token": ...}`` or ``{"private_key": ...}``).
        SM fault (BOTH boto3 families) → ``secret_error`` (the credential/exception value are
        NEVER logged — traceback only)."""
        try:
            resp = self._sm.get_secret_value(SecretId=self._secret_name(id))
            return json.loads(resp["SecretString"])
        except _STORE_FAULTS:
            logger.exception("[connections] get_secret_value failed for %s", id)
            raise ConnectionError("Failed to read connection secret", kind="secret_error") from None

    def _get_secret_token(self, id: str) -> str:
        """Return the stored PAT (``{"token": ...}`` body). PAT connections only."""
        return self._get_secret_body(id).get("token", "")

    def _put_secret_body(self, id: str, body: dict) -> None:
        """Overwrite the secret with a new JSON body (``{"token"}`` or ``{"private_key"}``).
        SM fault (BOTH boto3 families) → ``secret_error`` (credential/exception NEVER logged)."""
        try:
            self._sm.put_secret_value(
                SecretId=self._secret_name(id),
                SecretString=json.dumps(body),
            )
        except _STORE_FAULTS:
            logger.exception("[connections] put_secret_value failed for %s", id)
            raise ConnectionError("Failed to rotate connection secret", kind="secret_error") from None

    def _put_secret_token(self, id: str, token: str) -> None:
        """Rotate a PAT connection's stored token."""
        self._put_secret_body(id, {"token": token})

    def _delete_secret_best_effort(self, id: str) -> None:
        """Delete the secret; a missing secret is treated as success (spec §5)."""
        try:
            self._sm.delete_secret(
                SecretId=self._secret_name(id), ForceDeleteWithoutRecovery=True
            )
        except self._sm.exceptions.ResourceNotFoundException:
            pass
        except _STORE_FAULTS:
            logger.exception("[connections] delete_secret failed for %s", id)

    # ===================================================================== #
    # Persistence (DDB-or-local, mirror marketplace_service.py)
    # ===================================================================== #

    def _get(self, id: str) -> Optional[Connection]:
        if self._has_ddb:
            try:
                resp = self._table.get_item(Key={"pk": _PARTITION_KEY, "sk": id})
                item = resp.get("Item")
                return self._from_item(item) if item else None
            except _STORE_FAULTS:
                # BOTH families swallow IDENTICALLY. The swallow itself is legacy and pinned
                # (a read fault reads as "no such connection" → ``not_found``); the fix here is
                # only that a ``BotoCoreError`` no longer escapes to a 500.
                logger.exception("Failed to fetch connection %s from DDB", id)
                return None
        with self._local_lock:
            record = self._local.get(id)
            return record.model_copy(deep=True) if record else None

    def _load_all(self) -> List[Connection]:
        if self._has_ddb:
            try:
                items = self._scan_partition()
                return [self._from_item(i) for i in items]
            except _STORE_FAULTS:
                # BOTH families swallow IDENTICALLY to ``[]``. The empty-list-on-fault is legacy
                # and shipped (six ``get_bearer_token``-era callers plus
                # ``_delete_ecr_push_role_if_orphaned`` depend on it not raising), so this widens
                # only the exception TYPE: a ``BotoCoreError`` used to escape here and answer 500.
                logger.exception("Failed to load connections from DDB")
                return []
        with self._local_lock:
            return [c.model_copy(deep=True) for c in self._local.values()]

    def _save(self, record: Connection) -> None:
        """Persist the non-secret record. A store fault (BOTH boto3 families) becomes a
        RETRYABLE ``secret_error`` (502) rather than escaping raw as a 500. ``create_connection``
        / ``create_pending_app_connection`` wrap this in ``except Exception`` for the
        secret-rollback, so their behaviour is unchanged; the win is the five bare callers
        (``test_connection``, ``replace_token``, ``replace_key``, ``set_oauth_client``,
        ``finalize_app_connection``), which used to answer 500. The message names no store
        detail."""
        if self._has_ddb:
            try:
                self._table.put_item(Item=self._to_item(record))
            except _STORE_FAULTS:
                logger.exception("[connections] record write failed for %s", record.id)
                raise ConnectionError(
                    "Failed to persist connection record", kind="secret_error"
                ) from None
            return
        with self._local_lock:
            self._local[record.id] = record.model_copy(deep=True)

    def _delete(self, id: str) -> None:
        """Delete the record. Store fault (BOTH families) → retryable ``secret_error``, so a
        DDB blip on ``delete_connection`` is a 502 the caller can retry, not a 500."""
        if self._has_ddb:
            try:
                self._table.delete_item(Key={"pk": _PARTITION_KEY, "sk": id})
            except _STORE_FAULTS:
                logger.exception("[connections] record delete failed for %s", id)
                raise ConnectionError(
                    "Failed to delete connection record", kind="secret_error"
                ) from None
            return
        with self._local_lock:
            self._local.pop(id, None)

    # -- CSRF manifest state (SEPARATE partition; never surfaced by list/scan) ------

    def _save_state(self, state: str, record: dict) -> None:
        """Persist the single-use CSRF state. Store fault (BOTH families) → retryable
        ``secret_error``: the admin is told to retry rather than sent to GitHub with a state
        that was never stored (which would fail the callback instead). The log line names NO
        store detail — ``record`` and ``state`` are the CSRF material itself."""
        if self._has_ddb:
            try:
                self._table.put_item(Item={"pk": _STATE_PARTITION_KEY, "sk": state, **record})
            except _STORE_FAULTS:
                logger.exception("[connections] manifest state write failed")
                raise ConnectionError(
                    "Failed to start the GitHub App registration", kind="secret_error"
                ) from None
            return
        with self._local_lock:
            self._state[state] = dict(record)

    def _get_state(self, state: str) -> Optional[dict]:
        if self._has_ddb:
            try:
                resp = self._table.get_item(Key={"pk": _STATE_PARTITION_KEY, "sk": state})
                item = resp.get("Item")
                return {k: v for k, v in item.items() if k not in ("pk", "sk")} if item else None
            except _STORE_FAULTS:
                # BOTH families swallow IDENTICALLY to ``None`` — which ``consume_manifest_state``
                # turns into ``bad_request`` (fail-CLOSED: an unreadable CSRF state is refused, it
                # is never treated as valid). Legacy semantics kept; only the TYPE is widened.
                logger.exception("Failed to fetch manifest state from DDB")
                return None
        with self._local_lock:
            record = self._state.get(state)
            return dict(record) if record else None

    def _delete_state(self, state: str) -> None:
        """Consume the CSRF state. Store fault (BOTH families) → retryable ``secret_error``.
        The single-use invariant is UNCHANGED: a failed delete leaves the state unconsumed
        exactly as a raw escape did, so this only replaces a 500 with a 502. Nothing is
        surfaced that could echo the state."""
        if self._has_ddb:
            try:
                self._table.delete_item(Key={"pk": _STATE_PARTITION_KEY, "sk": state})
            except _STORE_FAULTS:
                logger.exception("[connections] manifest state delete failed")
                raise ConnectionError(
                    "Failed to complete the GitHub App registration", kind="secret_error"
                ) from None
            return
        with self._local_lock:
            self._state.pop(state, None)

    def _scan_partition(self) -> List[dict]:
        """Page the connection partition. Deliberately UNGUARDED: its sole caller is
        ``_load_all``, whose ``except _STORE_FAULTS`` covers every page of this loop for BOTH
        boto3 families. A guard here would have to invent a return value for a partial page."""
        items: List[dict] = []
        kwargs = {"KeyConditionExpression": Key("pk").eq(_PARTITION_KEY)}
        while True:
            resp = self._table.query(**kwargs)
            items.extend(resp.get("Items", []))
            lek = resp.get("LastEvaluatedKey")
            if not lek:
                break
            kwargs["ExclusiveStartKey"] = lek
        return items

    def _to_item(self, record: Connection) -> dict:
        return {
            "pk": _PARTITION_KEY,
            "sk": record.id,
            **json.loads(record.model_dump_json()),
        }

    def _from_item(self, item: dict) -> Connection:
        clean = {k: v for k, v in item.items() if k not in ("pk", "sk")}
        return Connection.model_validate(clean)
