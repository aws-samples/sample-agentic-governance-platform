"""Git org connections: persistence, secrets and verify orchestration.

Persistence is one :class:`core.state_store.StateStore` over the connections table
(``pk="connection"``, ``sk=<id>``; the CSRF manifest state is a separate partition that never
appears in a listing, written once and consumed by one conditional delete, so two callbacks
racing on one state spend it once). A record row carries a storage-only ``row_version``: a create
writes 1 through ``put_new`` and an update saves under the version it read, so an update that
raced another save or a delete is ``changed`` and never writes a deleted connection back. The
credential lives only in Secrets Manager: the record and every read model carry ``secret_arn`` /
``has_secret`` and metadata, and the credential is never logged, never echoed in an error, never
written to the table.

The secret body is JSON so its shape can vary by auth type: a ``pat`` connection stores
``{"token": <PAT>}``; a ``github_app`` connection stores ``{"private_key": <PEM>}`` plus an
optional ``webhook_secret`` and, once captured or pasted, the App's OAuth ``client_secret``
(its ``client_id`` is non-secret and lives on the record). ``get_bearer_token`` is the seam
every downstream caller uses: a PAT is returned as stored; a GitHub App mints a fresh,
short-lived installation token on demand. ``get_oauth_client_credentials`` is the single reader
of the OAuth pair.

Writes verify first, then create the secret, then persist the record; a failed verify stores
nothing, and a persist failure after the secret was written deletes the secret best-effort and
raises ``secret_error``. The clock (``now``) and id source (``new_id``) are injectable.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import ClassVar, List, Optional
from uuid import uuid4

import httpx

from core import aws_clients
from core.errors import AppError, Outcome
from core.state_store import STORE_FAULTS, StateStore, VersionMismatch
from models.connection import Connection, ConnectionAuthType, ConnectionCreate, ConnStatus, Provider
from services.connection_verify import verify_connection
from services.github_app_auth import GitHubAppAuthError
from services.github_app_auth import mint_installation_token as _mint_installation_token
from services.github_app_manifest import GitHubManifestError
from services.github_app_manifest import convert_manifest_code as _convert_manifest_code
from services.github_app_manifest import fetch_app_client_id as _fetch_app_client_id
from services.github_app_manifest import resolve_installation_id as _resolve_installation_id

logger = logging.getLogger(__name__)

_PARTITION_KEY = "connection"
_STATE_PARTITION_KEY = "conn_state"
_STATE_TTL_SECONDS = 900

_TAG_MANAGED_BY = "agp"


class ConnectionError(AppError):
    """A connection operation was refused or failed.

    ``verify_failed`` sends the message to the client verbatim, so that message is client copy:
    a ``VerifyResult.reason`` (fixed text plus the org or provider name, never a response body),
    a ``GitHubManifestError`` or ``GitHubAppAuthError`` message (fixed text plus a status code),
    or a literal from this module. Every other kind has a fixed detail; its message names the
    operation for the log, never a token, a secret or a store message. ``has_projects`` is the
    delete route's refusal while a project still names the connection. ``changed`` is an update
    whose row moved or was deleted after its read.
    """

    domain: ClassVar[str] = "connection"
    kinds: ClassVar[Mapping[str, Outcome]] = {
        "verify_failed": Outcome(400, None),
        "not_found": Outcome(404, "Connection not found"),
        "secret_error": Outcome(502, "Secret store operation failed"),
        "bad_request": Outcome(400, "Invalid request"),
        "has_projects": Outcome(409, "Connection has projects; delete them first"),
        "changed": Outcome(409, "Connection changed while this request ran; reload and retry"),
    }

    def __init__(self, message: str, kind: str) -> None:
        super().__init__(message, kind)


class ConnectionService:
    def __init__(
        self,
        *,
        table_name: str = "",
        secret_prefix: str = "",
        region: str | None = None,
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
        self._ecr_push_roles = ecr_push_role_service
        self._github_oidc = github_oidc_provider_service
        self._mint_installation_token = mint_installation_token
        self._convert_manifest_code = convert_manifest_code
        self._resolve_installation_id = resolve_installation_id
        self._fetch_app_client_id = fetch_app_client_id

        self._sm = secrets_client or aws_clients.client("secretsmanager", region=region)
        self._store = StateStore(table_name, region=region)

    def list_connections(self) -> List[Connection]:
        return self._load_all()

    def get_connection(self, id: str, *, consistent: bool = False) -> Connection:
        record = self._get(id, consistent=consistent)
        if record is None:
            raise ConnectionError("Unknown connection", kind="not_found")
        return record

    def create_connection(self, body: ConnectionCreate, created_by: str) -> Connection:
        """Verify the credential, store the secret, ensure the org's ECR-push role, persist.

        A ``pat`` body is verified with the token itself; a ``github_app`` body first mints an
        installation token from the private key and verifies with that, then stores the private
        key. A failed verify stores nothing.
        """
        conn_id = self._new_id()

        if body.auth_type == ConnectionAuthType.GITHUB_APP:
            verify_token = self._mint_for_app(
                body.app_id, body.installation_id, body.private_key, body.base_url
            )
            secret_body = {"private_key": body.private_key}
        else:
            verify_token = body.token or ""
            secret_body = {"token": body.token}

        result = self._run_verify(
            body.provider,
            body.org,
            body.base_url,
            verify_token,
            is_app=(body.auth_type == ConnectionAuthType.GITHUB_APP),
        )
        if not result.ok:
            raise ConnectionError(result.reason or "verification failed", kind="verify_failed")

        secret_arn = self._create_secret(conn_id, secret_body)
        ecr_push_role_arn = self._ensure_ecr_push_role(body.provider, body.org)

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
            self._create(record)
        except Exception:
            logger.exception("[connections] persist failed for %s; rolling back secret", conn_id)
            self._delete_secret_best_effort(conn_id)
            raise ConnectionError("Failed to persist connection record", kind="secret_error") from None
        return record

    def create_manifest_state(self, org: str, base_url: Optional[str], created_by: str) -> str:
        """Issue a single-use CSRF ``state`` for the manifest flow, stored with the org, base URL,
        issuer and an absolute expiry; ``consume_manifest_state`` reads it back once."""
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
        """Consume a manifest state in one conditional delete. An unknown, already consumed or
        expired state is ``bad_request``."""
        record = self._delete_state(state)
        if record is None or record.get("exp", 0) < int(self._now().timestamp()):
            raise ConnectionError("manifest state expired or unknown", kind="bad_request")
        return {
            "org": record.get("org"),
            "base_url": record.get("base_url"),
            "created_by": record.get("created_by"),
        }

    def complete_manifest(self, code: str, state: str) -> "tuple[Connection, bool, Optional[str]]":
        """Finish the manifest handshake: consume the state, convert the one-time ``code`` into
        the App's credentials, persist a PENDING connection, then try to resolve the installation.

        Returns ``(connection, needs_install, install_url)``: a resolved installation is finalized
        and returned as ``(connected, False, None)``; an App not installed yet returns the pending
        record with the org-install URL. A convert failure is ``verify_failed`` and persists
        nothing. The OAuth ``client_id``/``client_secret`` the conversion may return are forwarded
        when present.
        """
        ctx = self.consume_manifest_state(state)
        org = ctx["org"]
        base_url = ctx["base_url"]
        created_by = ctx["created_by"]

        client = httpx.Client()
        try:
            converted = self._convert_manifest_code(code, client=client, base_url=base_url)
        except GitHubManifestError as exc:
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
        """Persist a PENDING GitHub App connection without verifying: the secret body holds the
        private key, the webhook secret and, when present, the OAuth client secret; the record
        carries ``client_id`` and ``has_oauth_client``."""
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
            auth_type=ConnectionAuthType.GITHUB_APP,
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
            self._create(record)
        except Exception:
            logger.exception("[connections] persist failed for %s; rolling back secret", conn_id)
            self._delete_secret_best_effort(conn_id)
            raise ConnectionError("Failed to persist connection record", kind="secret_error") from None
        return record

    def finalize_app_connection(self, id: str, installation_id: Optional[str] = None) -> Connection:
        """Finalize a PENDING GitHub App connection: resolve the installation when not given, mint
        a token, run the org-visibility probe, and on success flip the record to CONNECTED. A
        verify failure leaves the record PENDING and persists no installation id."""
        record = self.get_connection(id, consistent=True)
        if record.auth_type != ConnectionAuthType.GITHUB_APP:
            raise ConnectionError("connection is not a GitHub App connection", kind="verify_failed")

        pem = self._get_secret_body(id).get("private_key", "")

        if installation_id is None:
            installation_id = self._resolve_for_record(record, pem)
            if installation_id is None:
                raise ConnectionError("App is not installed on the org yet", kind="verify_failed")

        verify_token = self._mint_for_app(record.app_id, installation_id, pem, record.base_url)
        result = self._run_verify(record.provider, record.org, record.base_url, verify_token, is_app=True)
        if not result.ok:
            raise ConnectionError(result.reason or "verification failed", kind="verify_failed")

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
        now_epoch = int(self._now().timestamp())
        client = httpx.Client()
        try:
            return self._resolve_installation_id(
                record.app_id,
                pem,
                record.org,
                client=client,
                base_url=record.base_url,
                now_epoch=now_epoch,
            )
        except GitHubManifestError as exc:
            raise ConnectionError(str(exc), kind="verify_failed") from None
        finally:
            client.close()

    def test_connection(self, id: str) -> Connection:
        record = self.get_connection(id, consistent=True)
        token = self.get_bearer_token(id)
        result = self._run_verify(
            record.provider,
            record.org,
            record.base_url,
            token,
            is_app=(record.auth_type == ConnectionAuthType.GITHUB_APP),
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
        """Verify the new token, then rotate the stored one; a failed verify leaves it unchanged."""
        record = self.get_connection(id, consistent=True)
        result = self._run_verify(record.provider, record.org, record.base_url, new_token)
        if not result.ok:
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
        """Verify the new private key (mint, then probe), then merge it into the stored secret
        body so the OAuth client secret survives; a failed verify leaves the stored key unchanged.
        Not a GitHub App connection is ``verify_failed``."""
        record = self.get_connection(id, consistent=True)
        if record.auth_type != ConnectionAuthType.GITHUB_APP:
            raise ConnectionError("connection is not a GitHub App connection", kind="verify_failed")
        verify_token = self._mint_for_app(
            record.app_id, record.installation_id, new_private_key, record.base_url
        )
        result = self._run_verify(record.provider, record.org, record.base_url, verify_token, is_app=True)
        if not result.ok:
            raise ConnectionError(result.reason or "verification failed", kind="verify_failed")

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

    def get_oauth_client_credentials(self, connection_id: str) -> "tuple[str, str]":
        """``(client_id, client_secret)`` for a GitHub App connection's OAuth client, the only
        reader of the secret half. ``bad_request`` when either half is absent or when the
        connection has a ``base_url``: the OAuth legs are github.com-only."""
        record = self.get_connection(connection_id)
        if record.base_url:
            raise ConnectionError(
                "per-user GitHub linking is available for github.com connections only",
                kind="bad_request",
            )
        client_id = record.client_id
        client_secret = self._get_secret_body(connection_id).get("client_secret")
        if not client_id or not client_secret:
            raise ConnectionError("connection has no GitHub OAuth client", kind="bad_request")
        return client_id, client_secret

    def set_oauth_client(self, id: str, client_id: str, client_secret: str) -> Connection:
        """Store an admin-supplied OAuth client pair on a GitHub App connection after checking
        ``client_id`` against ``GET /app`` with the App's own stored key; a mismatch or a probe
        failure is ``verify_failed`` and leaves the stored secret unchanged. The secret is merged
        into the existing body."""
        record = self.get_connection(id, consistent=True)
        if record.auth_type != ConnectionAuthType.GITHUB_APP:
            raise ConnectionError("connection is not a GitHub App connection", kind="verify_failed")

        private_key = self._get_secret_body(id).get("private_key", "")

        now_epoch = int(self._now().timestamp())
        client = httpx.Client()
        try:
            actual = self._fetch_app_client_id(
                record.app_id,
                private_key,
                client=client,
                base_url=record.base_url,
                now_epoch=now_epoch,
            )
        except GitHubManifestError as exc:
            raise ConnectionError(str(exc), kind="verify_failed") from None
        finally:
            client.close()

        if actual != client_id:
            raise ConnectionError("client_id does not match the App", kind="verify_failed")

        body = self._get_secret_body(id)
        body["client_secret"] = client_secret
        self._put_secret_body(id, body)

        record.client_id = client_id
        record.has_oauth_client = True
        record.updated_at = self._now().isoformat()
        self._save(record)
        return record

    def get_bearer_token(self, connection_id: str) -> str:
        """The bearer token for a connection: the stored PAT, or for a GitHub App a freshly
        minted installation token."""
        record = self.get_connection(connection_id)
        if record.auth_type == ConnectionAuthType.GITHUB_APP:
            private_key = self._get_secret_body(connection_id).get("private_key", "")
            return self._mint_for_app(record.app_id, record.installation_id, private_key, record.base_url)
        return self._get_secret_token(connection_id)

    def delete_connection(self, id: str) -> None:
        record = self.get_connection(id)
        self._delete(record.id)
        self._delete_secret_best_effort(record.id)
        self._delete_ecr_push_role_if_orphaned(record.org)

    def _ensure_github_oidc_bootstrap(self, provider: Provider, org: str) -> None:
        """On a GitHub connection, ensure the account-global GitHub OIDC provider and then the
        shared platform-default ECR-push role, in that order because IAM validates a role's
        ``Federated`` principal at create time. Best-effort: a failure is logged, never raised."""
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
        """Ensure this org's ECR-push role after the OIDC bootstrap and return its ARN; ``None``
        when the provisioner is absent or the ensure failed (logged, never raised)."""
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
        Best-effort."""
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

    def _mint_for_app(
        self,
        app_id: Optional[str],
        installation_id: Optional[str],
        private_key: Optional[str],
        base_url: Optional[str],
    ) -> str:
        """Mint a GitHub App installation token over a fresh client; a mint failure, including a
        private key PyJWT cannot parse, is ``verify_failed`` with the minter's fixed message."""
        now_epoch = int(self._now().timestamp())
        client = httpx.Client()
        try:
            return self._mint_installation_token(
                app_id,
                installation_id,
                private_key,
                client=client,
                base_url=base_url,
                now_epoch=now_epoch,
            )
        except GitHubAppAuthError as exc:
            raise ConnectionError(str(exc), kind="verify_failed") from None
        finally:
            client.close()

    def _run_verify(
        self,
        provider: Provider,
        org: str,
        base_url: Optional[str],
        token: str,
        *,
        is_app: bool = False,
    ):
        client = httpx.Client()
        try:
            return self._verify(provider, org, base_url, token, client=client, is_app=is_app)
        finally:
            client.close()

    def _secret_name(self, id: str) -> str:
        return f"{self.secret_prefix}{id}"

    def _create_secret(self, id: str, body: dict) -> str:
        """Create the per-connection secret and return its ARN; an existing name is overwritten.
        A store fault is ``secret_error``; the value and the exception text are never logged."""
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
        except STORE_FAULTS:
            logger.exception("[connections] create_secret failed for %s", id)
            raise ConnectionError("Failed to store connection secret", kind="secret_error") from None

    def _get_secret_body(self, id: str) -> dict:
        """The stored JSON object for a connection. A store fault, a binary secret, a body that is
        not JSON or not an object are all ``secret_error``; the body is never logged."""
        try:
            resp = self._sm.get_secret_value(SecretId=self._secret_name(id))
        except STORE_FAULTS:
            logger.exception("[connections] get_secret_value failed for %s", id)
            raise ConnectionError("Failed to read connection secret", kind="secret_error") from None
        try:
            body = json.loads(resp["SecretString"])
        except (KeyError, ValueError):
            logger.exception("[connections] the secret for %s is not the JSON object this service writes", id)
            raise ConnectionError("Failed to read connection secret", kind="secret_error") from None
        if not isinstance(body, dict):
            logger.error(
                "[connections] the secret for %s decodes to %s, not an object", id, type(body).__name__
            )
            raise ConnectionError("Failed to read connection secret", kind="secret_error")
        return body

    def _get_secret_token(self, id: str) -> str:
        return self._get_secret_body(id).get("token", "")

    def _put_secret_body(self, id: str, body: dict) -> None:
        try:
            self._sm.put_secret_value(
                SecretId=self._secret_name(id),
                SecretString=json.dumps(body),
            )
        except STORE_FAULTS:
            logger.exception("[connections] put_secret_value failed for %s", id)
            raise ConnectionError("Failed to rotate connection secret", kind="secret_error") from None

    def _put_secret_token(self, id: str, token: str) -> None:
        self._put_secret_body(id, {"token": token})

    def _delete_secret_best_effort(self, id: str) -> None:
        try:
            self._sm.delete_secret(SecretId=self._secret_name(id), ForceDeleteWithoutRecovery=True)
        except self._sm.exceptions.ResourceNotFoundException:
            pass
        except STORE_FAULTS:
            logger.exception("[connections] delete_secret failed for %s", id)

    def _get(self, id: str, *, consistent: bool = False) -> Optional[Connection]:
        try:
            item = self._store.get((_PARTITION_KEY, id), consistent=consistent)
        except STORE_FAULTS:
            logger.exception("Failed to fetch connection %s from the store", id)
            return None
        return self._from_item(item) if item else None

    def _load_all(self) -> List[Connection]:
        try:
            return [self._from_item(i) for i in self._scan_partition()]
        except STORE_FAULTS:
            logger.exception("Failed to load connections from the store")
            return []

    def _create(self, record: Connection) -> None:
        if not self._store.put_new({**self._to_item(record), "row_version": 1}):
            raise RuntimeError("a connection with this id already exists")
        record._row_version = 1

    def _save(self, record: Connection) -> None:
        row_version = (record._row_version or 0) + 1
        try:
            self._store.put_if_version(
                {**self._to_item(record), "row_version": row_version}, "row_version", record._row_version
            )
        except VersionMismatch:
            raise ConnectionError("Connection changed", kind="changed") from None
        except STORE_FAULTS:
            logger.exception("[connections] record write failed for %s", record.id)
            raise ConnectionError("Failed to persist connection record", kind="secret_error") from None
        record._row_version = row_version

    def _delete(self, id: str) -> None:
        try:
            self._store.delete((_PARTITION_KEY, id))
        except STORE_FAULTS:
            logger.exception("[connections] record delete failed for %s", id)
            raise ConnectionError("Failed to delete connection record", kind="secret_error") from None

    def _save_state(self, state: str, record: dict) -> None:
        try:
            created = self._store.put_new({"pk": _STATE_PARTITION_KEY, "sk": state, **record})
        except STORE_FAULTS:
            logger.exception("[connections] manifest state write failed")
            raise ConnectionError(
                "Failed to start the GitHub App registration", kind="secret_error"
            ) from None
        if not created:
            raise RuntimeError("a manifest state with this id was already issued")

    def _delete_state(self, state: str) -> Optional[dict]:
        """Consume the state: one conditional delete that returns the record, or ``None`` when it
        was never issued or is already spent. A store fault raises, because this delete is what
        makes the state single-use."""
        try:
            item = self._store.delete((_STATE_PARTITION_KEY, state))
        except STORE_FAULTS:
            logger.exception("[connections] manifest state delete failed")
            raise ConnectionError(
                "Failed to complete the GitHub App registration", kind="secret_error"
            ) from None
        return {k: v for k, v in item.items() if k not in ("pk", "sk")} if item else None

    def _scan_partition(self) -> List[dict]:
        return self._store.query(_PARTITION_KEY)

    def _to_item(self, record: Connection) -> dict:
        return {
            "pk": _PARTITION_KEY,
            "sk": record.id,
            **json.loads(record.model_dump_json()),
        }

    def _from_item(self, item: dict) -> Connection:
        clean = {k: v for k, v in item.items() if k not in ("pk", "sk")}
        record = Connection.model_validate(clean)
        version = item.get("row_version")
        record._row_version = None if version is None else int(version)
        return record
