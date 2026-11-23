"""The one Databricks REST client: token minting, the federated-token exchange, two paginated
listings, the app-permissions surface (read, additive grant, asserting replace), one SCIM create
and the account federation policy's audience list. Plain ``httpx`` over a handful of endpoints;
every request shape is pinned by ``tests/test_databricks_workspace_service.py`` against a fake
that answers 400 to a drifted form.

Credentials are parameters, never fetched here: this service reads no Secrets Manager entry and
holds no state, so an adapter cannot widen its own authority, and ``probe_capabilities`` takes
plain values rather than a ``Tenant``.

A Databricks error body is not safe to propagate: a ``PERMISSION_DENIED`` message names workspace
paths and principal ids, and an OIDC failure can echo the form back. Every non-2xx becomes a
``DatabricksError`` whose ``.message`` is composed here from the method, the path and the status,
and whose ``.kind`` is a code matched against ``_SAFE_KIND``. No secret, JWT, token or upstream
body reaches a message or a log line.

The Apps response field names (``url``, ``oauth2_app_client_id``, the app's service-principal id)
are read defensively: the listings key on ``name`` only, skip a record that lacks it with a
counted log line, and pass every other field through untouched.
"""

from __future__ import annotations

import contextlib
import logging
import re
from typing import Literal, Optional, Sequence
from urllib.parse import quote

import httpx

logger = logging.getLogger(__name__)

# httpx waits without bound on some transports unless every call carries a timeout.
_HTTP_TIMEOUT_SECONDS = 10.0

# The exit condition of a paginated listing is upstream-controlled.
_MAX_PAGES = 100

# Databricks' AWS account console: per-cloud, and not derivable from a workspace URL.
_DEFAULT_ACCOUNT_HOST = "https://accounts.cloud.databricks.com"

_TOKEN_PATH = "/oidc/v1/token"  # noqa: S105 — a URL path, not a credential; expires 2027-10-23; added E49A-50A/T12
_APPS_PATH = "/api/2.0/apps"
_SERVING_ENDPOINTS_PATH = "/api/2.0/serving-endpoints"
_APP_PERMISSIONS_PATH = "/api/2.0/permissions/apps"
_SCIM_SERVICE_PRINCIPALS_PATH = "/api/2.0/preview/scim/v2/ServicePrincipals"

# The workspace host serves the per-SP secret mint under an ``/accounts/`` prefix; the un-prefixed
# path 404s. A template so the SCIM id is quoted into one segment.
_SP_SECRETS_PATH_TEMPLATE = "/api/2.0/accounts/servicePrincipals/{sp_id}/credentials/secrets"

_SCOPE_ALL_APIS = "all-apis"
_GRANT_CLIENT_CREDENTIALS = "client_credentials"
_GRANT_TOKEN_EXCHANGE = "urn:ietf:params:oauth:grant-type:token-exchange"  # noqa: S105 — an OAuth grant-type URN, not a credential; expires 2027-10-23; added E49A-50A/T12
_SUBJECT_TOKEN_TYPE_JWT = "urn:ietf:params:oauth:token-type:jwt"  # noqa: S105 — an OAuth token-type URN, not a credential; expires 2027-10-23; added E49A-50A/T12

_SCIM_SP_SCHEMA = "urn:ietf:params:scim:schemas:core:2.0:ServicePrincipal"
_PERMISSION_CAN_USE = "CAN_USE"
_PERMISSION_CAN_MANAGE = "CAN_MANAGE"

AccessDriftKind = Literal["user", "group", "service_principal"]
_KIND_BY_ACL_KEY: dict[str, AccessDriftKind] = {
    "user_name": "user",
    "group_name": "group",
    "service_principal_name": "service_principal",
}
_ACL_KEY_BY_KIND: dict[str, str] = {kind: key for key, kind in _KIND_BY_ACL_KEY.items()}

# The app ACL vocabulary is closed. Ranked, because a write shape keeps one level per principal
# and the strongest must win.
_APP_LEVEL_RANK = {_PERMISSION_CAN_USE: 1, _PERMISSION_CAN_MANAGE: 2}

_ADMINS_GROUP = "admins"
_ADMINS_ENTRY = {"principal": _ADMINS_GROUP, "kind": "group", "level": _PERMISSION_CAN_MANAGE}

# Entra's issuer form is ``https://login.microsoftonline.com/{tenantId}/v2.0``.
_ENTRA_ISSUER_MARKER = "login.microsoftonline.com"

# Databricks' own error codes are SCREAMING_SNAKE, hence the underscore.
_SAFE_KIND = re.compile(r"^[A-Za-z_]{1,64}$")

# The kind by HTTP status when the body carries no usable code.
_KIND_BY_STATUS = {
    400: "bad_request",
    401: "unauthorized",
    403: "forbidden",
    404: "not_found",
    409: "conflict",
    429: "rate_limited",
}

# A UUID-shaped principal is a service principal's application id; anything else is a group name.
_UUID = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")


def _is_entra_policy(policy: dict) -> bool:
    """Whether a federation policy is Entra-issued. A policy whose ``oidc_policy`` is not a dict is
    not Entra rather than inspected. ``ensure_federation_audience`` and ``probe_capabilities``
    share this one predicate so they can never disagree on which policy they act on."""
    oidc = policy.get("oidc_policy")
    if not isinstance(oidc, dict):
        return False
    return _ENTRA_ISSUER_MARKER in str(oidc.get("issuer") or "")


def _to_write_shape(entries: list[dict]) -> list[dict]:
    """Collapse read-shaped ACL entries into a shape that is safe to PUT.

    One entry per (principal, kind), strongest level wins: a PUT naming the same principal twice
    is undocumented upstream and last-wins would downgrade. Inherited entries are dropped:
    re-PUTting one converts it into a permanent direct grant. A level outside the closed
    vocabulary is dropped with a warning: ``set_app_permissions`` refuses it, and leaving it in
    would make every revoke on that app fail for something the workspace reported. Module level
    because every read-modify-write path must collapse identically."""
    collapsed: dict[tuple[str, str], dict] = {}
    dropped = 0
    for entry in entries:
        if entry.get("inherited"):
            continue
        if str(entry.get("level") or "") not in _APP_LEVEL_RANK:
            dropped += 1
            continue
        key = (str(entry.get("principal") or ""), str(entry.get("kind") or ""))
        kept = collapsed.get(key)
        if kept is None:
            collapsed[key] = dict(entry)
            continue
        if _APP_LEVEL_RANK.get(str(entry.get("level") or ""), 0) > _APP_LEVEL_RANK.get(
            str(kept.get("level") or ""), 0
        ):
            collapsed[key] = dict(entry)
    if dropped:
        logger.warning(
            "[databricks] app ACL write shape: dropped %d entr(y/ies) at a permission level this "
            "client may not transmit; the re-asserted list will not carry them",
            dropped,
        )
    return list(collapsed.values())


def _acl_record(record: object) -> tuple[str, AccessDriftKind, list[tuple[str, bool]]] | None:
    """One ACL record as ``(principal, kind, [(level, inherited), ...])``."""
    if not isinstance(record, dict):
        return None
    principal, kind = "", None
    for key, candidate_kind in _KIND_BY_ACL_KEY.items():
        value = record.get(key)
        if isinstance(value, str) and value:
            principal, kind = value, candidate_kind
            break
    permissions = record.get("all_permissions")
    if kind is None or not isinstance(permissions, list):
        return None
    levels: list[tuple[str, bool]] = []
    for permission in permissions:
        level = permission.get("permission_level") if isinstance(permission, dict) else None
        if isinstance(level, str) and level:
            levels.append((level, bool(permission.get("inherited"))))
    return (principal, kind, levels) if levels else None


class DatabricksError(Exception):
    """A Databricks operation failed. ``message`` is composed by this module and safe to show:
    never a token, a secret, a JWT or an upstream body. ``kind`` is a short code: one of this
    module's fixed kinds, the upstream error code when it matches ``_SAFE_KIND``, or a word for
    the status. A plain exception, not an ``AppError``: the kinds are an open set of upstream
    codes that callers read as a diagnostic, and the service that called Databricks translates
    one into its own kind where the meaning is known."""

    def __init__(self, message: str, kind: str) -> None:
        super().__init__(message)
        self.message = message
        self.kind = kind


def _origin(url: str) -> str:
    """Normalize a caller-supplied host to an origin with no trailing slash."""
    return (url or "").rstrip("/")


def _safe_kind(resp: httpx.Response) -> str:
    """The safest ``.kind`` derivable from a response: the upstream error code when it is a bare
    code, else a fixed per-status word. Never the upstream message, which names paths, principals
    and sometimes the request form. REST APIs use ``error_code``; the OIDC token endpoint uses
    ``error``."""
    code = ""
    with contextlib.suppress(Exception):
        body = resp.json()
        if isinstance(body, dict):
            raw = body.get("error_code") or body.get("error") or ""
            if isinstance(raw, str):
                code = raw.strip().lower()
    if code and _SAFE_KIND.match(code):
        return code
    if resp.status_code in _KIND_BY_STATUS:
        return _KIND_BY_STATUS[resp.status_code]
    if 500 <= resp.status_code < 600:
        return "upstream_error"
    return "request_failed"


class DatabricksWorkspaceService:
    """Token minting, discovery listings, grants, SCIM SPs and federation audiences.

    ``http_client`` is injectable (tests pass a ``MockTransport`` client); when absent, each call
    opens a fresh timed ``httpx.AsyncClient``, which is safe because every route runs on the
    uvicorn loop.
    """

    def __init__(
        self,
        *,
        http_client: Optional[httpx.AsyncClient] = None,
        account_host: str = _DEFAULT_ACCOUNT_HOST,
    ) -> None:
        self._injected_client = http_client
        self._account_host = _origin(account_host)

    @contextlib.asynccontextmanager
    async def _client(self):
        """Yield the injected (caller-owned) client, or a fresh per-call timed one."""
        if self._injected_client is not None:
            yield self._injected_client
        else:
            async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT_SECONDS) as client:
                yield client

    async def _request(self, method: str, url: str, *, what: str, **kwargs) -> dict:
        """One request to its JSON body, or ``DatabricksError``.

        ``what`` is a fixed caller-supplied label (``"list apps"``) used to build the safe
        message; nothing from ``kwargs`` or the response reaches it. A transport failure is
        ``unreachable``; ``httpx.InvalidURL`` and the bare ``ValueError`` httpx raises for an
        unroutable URL, neither an ``HTTPError``, are ``bad_request_url``; unparseable JSON is
        ``bad_response``."""
        try:
            async with self._client() as client:
                resp = await client.request(method, url, timeout=_HTTP_TIMEOUT_SECONDS, **kwargs)
        except httpx.HTTPError:
            logger.warning("[databricks] transport failure on %s", what)
            raise DatabricksError(f"Databricks was unreachable ({what})", kind="unreachable") from None
        except (httpx.InvalidURL, ValueError):
            logger.warning("[databricks] malformed request URL on %s", what)
            raise DatabricksError(
                f"the Databricks URL is not usable ({what})", kind="bad_request_url"
            ) from None

        if resp.status_code >= 400:
            kind = _safe_kind(resp)
            logger.warning("[databricks] %s failed with status %s (%s)", what, resp.status_code, kind)
            raise DatabricksError(
                f"Databricks rejected the request ({what}, status {resp.status_code})",
                kind=kind,
            )

        if not resp.content:
            return {}
        try:
            body = resp.json()
        except ValueError:
            raise DatabricksError(
                f"Databricks returned an unreadable response ({what})", kind="bad_response"
            ) from None
        return body if isinstance(body, dict) else {}

    @staticmethod
    def _access_token(body: dict, what: str) -> str:
        token = body.get("access_token")
        if not isinstance(token, str) or not token:
            raise DatabricksError(f"Databricks returned no access token ({what})", kind="bad_response")
        return token

    async def mint_m2m_token(self, workspace_url: str, client_id: str, client_secret: str) -> str:
        """Mint a workspace token from a service principal's client credentials, sent as HTTP
        Basic so they stay out of any body an intermediary could log."""
        return self._access_token(
            await self._request(
                "POST",
                f"{_origin(workspace_url)}{_TOKEN_PATH}",
                what="mint workspace token",
                data={"grant_type": _GRANT_CLIENT_CREDENTIALS, "scope": _SCOPE_ALL_APIS},
                auth=httpx.BasicAuth(client_id, client_secret),
            ),
            "mint workspace token",
        )

    async def exchange_federated_token(self, workspace_url: str, subject_jwt: str) -> str:
        """RFC 8693 token exchange: an Entra JWT for a Databricks token. Sends no ``client_id``:
        it is required for a service-principal federation policy and prohibited for the
        account-wide policy this path uses, where sending it fails as ``invalid_request``."""
        return self._access_token(
            await self._request(
                "POST",
                f"{_origin(workspace_url)}{_TOKEN_PATH}",
                what="exchange federated token",
                data={
                    "grant_type": _GRANT_TOKEN_EXCHANGE,
                    "subject_token": subject_jwt,
                    "subject_token_type": _SUBJECT_TOKEN_TYPE_JWT,
                    "scope": _SCOPE_ALL_APIS,
                },
            ),
            "exchange federated token",
        )

    async def mint_account_token(
        self,
        account_id: str,
        client_id: str,
        client_secret: str,
        *,
        account_host: Optional[str] = None,
    ) -> str:
        """Mint an account-scoped token; account-level APIs (federation policies, account SPs)
        refuse a workspace token.

        ``account_host`` overrides the instance default for this call: the console host is
        per-cloud and not derivable from the workspace URL, and the caller that knows the
        tenant's cloud passes it. ``account_id`` is a stored tenant-record value, so it is quoted
        into one path segment; unquoted, ``a1/../../accounts/other/...`` retargeted this mint at
        another account."""
        host = _origin(account_host) if account_host else self._account_host
        path = f"/oidc/accounts/{quote(account_id, safe='')}/v1/token"
        return self._access_token(
            await self._request(
                "POST",
                f"{host}{path}",
                what="mint account token",
                data={"grant_type": _GRANT_CLIENT_CREDENTIALS, "scope": _SCOPE_ALL_APIS},
                auth=httpx.BasicAuth(client_id, client_secret),
            ),
            "mint account token",
        )

    async def _list_paginated(self, url: str, token: str, *, key: str, what: str) -> list[dict]:
        """Page a list endpoint via ``next_page_token`` and return the records that carry a
        ``name``; every other field passes through untouched. A repeated page token or more than
        ``_MAX_PAGES`` pages is ``pagination_overflow``, never a truncated inventory presented as
        complete."""
        records: list[dict] = []
        page_token: Optional[str] = None
        seen: set[str] = set()
        pages = 0
        skipped = 0
        while True:
            pages += 1
            if pages > _MAX_PAGES:
                raise DatabricksError(
                    f"Databricks returned too many pages ({what})", kind="pagination_overflow"
                )
            params = {"page_token": page_token} if page_token else None
            body = await self._request(
                "GET",
                url,
                what=what,
                params=params,
                headers={"Authorization": f"Bearer {token}"},
            )
            page = body.get(key)
            if isinstance(page, list):
                for record in page:
                    if isinstance(record, dict) and record.get("name"):
                        records.append(record)
                    else:
                        skipped += 1
            page_token = body.get("next_page_token") or None
            if not page_token:
                break
            page_token = str(page_token)
            if page_token in seen:
                raise DatabricksError(
                    f"Databricks repeated a pagination token ({what})",
                    kind="pagination_overflow",
                )
            seen.add(page_token)
        if skipped:
            logger.info("[databricks] %s: skipped %d record(s) with no name", what, skipped)
        return records

    async def list_apps(self, workspace_url: str, token: str) -> list[dict]:
        """List Databricks Apps. ``apps list`` visibility follows app permissions, so a discovery
        SP with no app grants sees an empty list rather than an error."""
        return await self._list_paginated(
            f"{_origin(workspace_url)}{_APPS_PATH}", token, key="apps", what="list apps"
        )

    async def list_serving_endpoints(self, workspace_url: str, token: str) -> list[dict]:
        """List Model Serving endpoints; ``CAN VIEW`` suffices, so the discovery credential can
        be read-only."""
        return await self._list_paginated(
            f"{_origin(workspace_url)}{_SERVING_ENDPOINTS_PATH}",
            token,
            key="endpoints",
            what="list serving endpoints",
        )

    def _app_permissions_url(self, workspace_url: str, app_name: str) -> str:
        """The app's permissions URL. ``app_name`` arrives from a discovery listing, so it is
        quoted into one path segment: unquoted, ``../../secrets`` resolved to another endpoint
        and ``a?x=1`` injected a query string."""
        return f"{_origin(workspace_url)}{_APP_PERMISSIONS_PATH}/{quote(app_name, safe='')}"

    async def grant_app_can_use(
        self,
        workspace_url: str,
        token: str,
        app_name: str,
        service_principal_or_group: str,
        kind: str = "group",
    ) -> None:
        """Additively grant ``CAN_USE`` on an app (PATCH), the level required to call its
        ``/api/`` routes; ``set_app_permissions`` is the replacing write.

        ``kind`` selects the ACL key. The default ``"group"`` keeps the heuristic the earlier
        callers rely on: a UUID-shaped principal is a service principal's application id,
        anything else a group name. A per-user grant passes ``kind="user"``, since a UPN is not
        UUID-shaped and would otherwise be written as a group name."""
        principal = (service_principal_or_group or "").strip()
        if kind == "group":
            key = "service_principal_name" if _UUID.match(principal) else "group_name"
        else:
            key = _ACL_KEY_BY_KIND.get(kind, "group_name")
        await self._request(
            "PATCH",
            self._app_permissions_url(workspace_url, app_name),
            what="grant app permission",
            json={"access_control_list": [{key: principal, "permission_level": _PERMISSION_CAN_USE}]},
            headers={"Authorization": f"Bearer {token}"},
        )

    async def get_app_permissions(self, workspace_url: str, token: str, app_name: str) -> list[dict]:
        """Read an app's ACL as flat ``{"principal", "kind", "level", "inherited"}`` entries, one
        per (principal, level): Databricks reports one record per principal carrying an
        ``all_permissions[]`` array.

        Inherited permissions are reported and flagged, not filtered: an entry AGP cannot remove
        still grants access, and the write path (``_to_write_shape``) is what drops them. A level
        reported both directly and by inheritance is one entry, flagged direct. An unreadable
        record is skipped with a counted warning, loud because a caller that re-asserts this list
        would write back a list missing those records."""
        body = await self._request(
            "GET",
            self._app_permissions_url(workspace_url, app_name),
            what="read app permissions",
            headers={"Authorization": f"Bearer {token}"},
        )
        raw = body.get("access_control_list")
        entries: list[dict] = []
        seen: dict[tuple[str, str, str], dict] = {}
        skipped = 0
        for record in raw if isinstance(raw, list) else []:
            parsed = _acl_record(record)
            if parsed is None:
                skipped += 1
                continue
            principal, kind, levels = parsed
            for level, inherited in levels:
                key = (principal, kind, level)
                kept = seen.get(key)
                if kept is not None:
                    if not inherited:
                        kept["inherited"] = False
                    continue
                entry = {"principal": principal, "kind": kind, "level": level, "inherited": inherited}
                seen[key] = entry
                entries.append(entry)
        if skipped:
            logger.warning("[databricks] read app permissions: skipped %d unreadable ACL record(s)", skipped)
        return entries

    async def set_app_permissions(
        self, workspace_url: str, token: str, app_name: str, entries: list[dict]
    ) -> None:
        """Assert an app's ACL: PUT the caller's full write-shaped list, replacing what was there.

        Refused before any request is issued: a list without ``admins`` at ``CAN_MANAGE``
        (``acl_missing_admins``; dropping it locks the workspace admins out of their own app and
        the next write needs ``CAN_MANAGE``, so the mistake is unrecoverable through AGP); an
        entry with no principal, an unrecognised kind or a level outside ``CAN_USE`` /
        ``CAN_MANAGE`` (``acl_entry_invalid``; defaulting a kind would grant a principal nobody
        named, and the vocabulary is closed); a list naming the same principal twice
        (``acl_entry_invalid``; undocumented upstream), users compared case-insensitively. This
        method translates and guards; it does not collapse, because a PUT body that differed from
        what the caller composed is the opposite of an assert."""
        if not any(
            e.get("principal") == _ADMINS_GROUP
            and e.get("kind") == "group"
            and e.get("level") == _PERMISSION_CAN_MANAGE
            for e in entries
        ):
            raise DatabricksError(
                "refusing to write an app ACL that does not grant the workspace admins "
                "CAN_MANAGE (assert app permissions)",
                kind="acl_missing_admins",
            )
        acl: list[dict] = []
        named: set[tuple[str, str]] = set()
        for entry in entries:
            key = _ACL_KEY_BY_KIND.get(str(entry.get("kind") or ""))
            principal = str(entry.get("principal") or "")
            level = str(entry.get("level") or "")
            if not key or not principal or level not in _APP_LEVEL_RANK:
                raise DatabricksError(
                    "an app ACL entry names no usable principal, kind, or level (assert app permissions)",
                    kind="acl_entry_invalid",
                )
            identity = (key, principal.casefold() if key == "user_name" else principal)
            if identity in named:
                raise DatabricksError(
                    "an app ACL list names the same principal twice (assert app permissions)",
                    kind="acl_entry_invalid",
                )
            named.add(identity)
            acl.append({key: principal, "permission_level": level})
        await self._request(
            "PUT",
            self._app_permissions_url(workspace_url, app_name),
            what="assert app permissions",
            json={"access_control_list": acl},
            headers={"Authorization": f"Bearer {token}"},
        )

    async def revoke_app_can_use(
        self, workspace_url: str, token: str, app_name: str, principal: str, kind: str
    ) -> None:
        """Remove one principal's entries from an app's ACL by read-modify-PUT: the permissions
        API has no single-entry DELETE.

        Matching is on the (principal, kind) pair, never the name alone, so revoking a user never
        strips a group of the same name. A ``user`` is matched case-insensitively: Databricks
        reports lowercased UPNs where Entra's ``mail`` may not be, and an exact match would
        re-PUT the list unchanged and report success. A revoke that matches no removable entry
        still re-asserts the list (idempotent, so it stays retryable) and warns. The read is
        collapsed to write shape first, and the ``admins`` ``CAN_MANAGE`` entry is ensured so the
        next assert is not refused; the warning that admins were added is decided from the
        pre-collapse read, because Databricks commonly reports that grant as inherited."""
        current = await self.get_app_permissions(workspace_url, token, app_name)

        def _matches(entry: dict) -> bool:
            if entry.get("kind") != kind:
                return False
            name = str(entry.get("principal") or "")
            if kind == "user":
                return name.casefold() == str(principal or "").casefold()
            return name == principal

        removable = sum(1 for e in current if _matches(e) and not e.get("inherited"))
        if not removable:
            logger.warning(
                "[databricks] revoke app permission: matched no removable %s entry; "
                "re-asserting the list unchanged",
                "user" if kind == "user" else "principal",
            )
        remaining = _to_write_shape([e for e in current if not _matches(e)])

        def _is_admins_can_manage(entry: dict) -> bool:
            return (
                entry.get("principal") == _ADMINS_GROUP
                and entry.get("kind") == "group"
                and entry.get("level") == _PERMISSION_CAN_MANAGE
            )

        if not any(_is_admins_can_manage(e) for e in remaining):
            if not any(_is_admins_can_manage(e) for e in current):
                logger.warning(
                    "[databricks] revoke app permission: ADDED the missing admins CAN_MANAGE entry "
                    "to the asserted ACL"
                )
            remaining.insert(0, dict(_ADMINS_ENTRY))
        await self.set_app_permissions(workspace_url, token, app_name, remaining)

    async def create_service_principal(self, workspace_url: str, token: str, display_name: str) -> dict:
        """Create a workspace service principal via SCIM and return ``{"id", "application_id"}``,
        both read defensively (``""`` when absent; SCIM responses vary by preview revision).
        Secret creation is a separate method, so no caller of "create me an SP" holds a credential
        it did not ask for."""
        body = await self._request(
            "POST",
            f"{_origin(workspace_url)}{_SCIM_SERVICE_PRINCIPALS_PATH}",
            what="create service principal",
            json={
                "schemas": [_SCIM_SP_SCHEMA],
                "displayName": display_name,
                "active": True,
            },
            headers={"Authorization": f"Bearer {token}"},
        )
        return {
            "id": str(body.get("id") or ""),
            "application_id": str(body.get("applicationId") or ""),
        }

    async def create_service_principal_secret(self, workspace_url: str, token: str, sp_id: str) -> dict:
        """Mint an OAuth secret for an existing service principal and return
        ``{"secret", "secret_hash", "id"}``.

        ``sp_id`` is the SCIM ``id`` (the ``"id"`` of ``create_service_principal``), not the
        ``application_id``: only the SCIM id addresses this sub-resource, and the application id
        404s as "the service principal does not exist". The returned ``secret`` is the one live
        credential this module hands back; the caller places it in Secrets Manager and nothing
        here logs or stores it. A response without a secret is ``bad_response``, never an empty
        string persisted as though it were real."""
        path = _SP_SECRETS_PATH_TEMPLATE.format(sp_id=quote(sp_id, safe=""))
        body = await self._request(
            "POST",
            f"{_origin(workspace_url)}{path}",
            what="create service principal secret",
            json={},
            headers={"Authorization": f"Bearer {token}"},
        )
        secret = body.get("secret")
        if not isinstance(secret, str) or not secret:
            raise DatabricksError(
                "Databricks returned no service-principal secret (create service principal secret)",
                kind="bad_response",
            )
        return {
            "secret": secret,
            "secret_hash": str(body.get("secret_hash") or ""),
            "id": str(body.get("id") or ""),
        }

    def _policies_url(self, account_host: str, account_id: str) -> str:
        """The account's federation-policy collection URL; ``account_id`` is quoted for the same
        reason as in ``mint_account_token``."""
        return (
            f"{_origin(account_host) or self._account_host}"
            f"/api/2.0/accounts/{quote(account_id, safe='')}/federationPolicies"
        )

    async def _list_federation_policies(self, account_host: str, account_id: str, token: str) -> list[dict]:
        body = await self._request(
            "GET",
            self._policies_url(account_host, account_id),
            what="list federation policies",
            headers={"Authorization": f"Bearer {token}"},
        )
        policies = body.get("policies")
        return [p for p in policies if isinstance(p, dict)] if isinstance(policies, list) else []

    async def ensure_federation_audience(
        self,
        account_host: str,
        account_id: str,
        token: str,
        audience: "str | Sequence[str]",
        *,
        present: bool,
    ) -> None:
        """Idempotently add or remove audience(s) on the account's Entra federation policy: one
        read, at most one PATCH, and no PATCH at all when the desired state already holds.

        ``audience`` is one string or a sequence; a multi-form removal is one call because two
        back-to-back single-audience calls tripped the account API's rate limit. ``present=True``
        with no Entra policy is ``federation_policy_missing`` (this method has no issuer or JWKS
        to invent one with); ``present=False`` with none is a no-op. The policy is selected by
        issuer, never by position, since an account may federate Okta beside Entra; two Entra
        policies is ``federation_policy_ambiguous``. An ``audiences`` field that is not a list of
        strings is ``federation_policy_unreadable``: writing would replace account-level trust
        state with a list this method did not read. The federation-policy API offers no ETag or
        If-Match, so a concurrent writer between the GET and the PATCH loses its change; the
        PATCH body is the freshly read list plus this call's one delta, so a lost update can
        never drop an audience this call read. That is accepted because the platform provisions
        sequentially per tenant; if that ever changes, the fix is an external lock, not a retry."""
        policies = await self._list_federation_policies(account_host, account_id, token)
        entra = [p for p in policies if _is_entra_policy(p)]
        if not entra:
            if not present:
                return
            raise DatabricksError(
                "the Databricks account has no Entra OIDC federation policy",
                kind="federation_policy_missing",
            )
        if len(entra) > 1:
            raise DatabricksError(
                "the Databricks account has multiple Entra OIDC federation policies",
                kind="federation_policy_ambiguous",
            )

        policy = entra[0]
        policy_id = str(policy.get("policy_id") or "")
        if not policy_id:
            raise DatabricksError("the Databricks federation policy has no id", kind="bad_response")

        oidc = policy.get("oidc_policy")
        raw_audiences = oidc.get("audiences") if isinstance(oidc, dict) else None
        if not isinstance(raw_audiences, list) or not all(isinstance(a, str) for a in raw_audiences):
            raise DatabricksError(
                "the Databricks federation policy's audience list could not be read",
                kind="federation_policy_unreadable",
            )
        current = list(raw_audiences)

        wanted = [audience] if isinstance(audience, str) else [a for a in audience if a]
        if present and all(a in current for a in wanted):
            return
        if not present and not any(a in current for a in wanted):
            return

        desired = (
            current + [a for a in wanted if a not in current]
            if present
            else [a for a in current if a not in wanted]
        )

        await self._request(
            "PATCH",
            f"{self._policies_url(account_host, account_id)}/{quote(policy_id, safe='')}",
            what="update federation policy audiences",
            params={"update_mask": "oidc_policy.audiences"},
            json={"oidc_policy": {"audiences": desired}},
            headers={"Authorization": f"Bearer {token}"},
        )

    async def probe_capabilities(
        self,
        workspace_url: str,
        account_id: str,
        client_id: str,
        client_secret: str,
        account_admin_client_id: Optional[str] = None,
        account_admin_secret: Optional[str] = None,
    ) -> dict:
        """What AGP can do on this tenant: ``{"can_discover", "account_admin", "user_sync"}``,
        from plain values and fail-closed: every branch catches its ``DatabricksError`` and
        answers False, because a capability flag is a promise the UI makes.

        ``can_discover``: mint a workspace token and read a listing, apps first and serving
        endpoints when apps answered nothing readable (``apps list`` returns nothing for an SP
        with no app grants). ``account_admin``: probed only when the separate account-admin
        credentials are supplied; mint an account token and list federation policies.
        ``user_sync``: True only when a listed policy's issuer is Entra's."""
        caps = {"can_discover": False, "account_admin": False, "user_sync": False}

        try:
            token = await self.mint_m2m_token(workspace_url, client_id, client_secret)
        except DatabricksError as err:
            logger.info("[databricks] capability probe: workspace token mint failed (%s)", err.kind)
            token = ""

        if token:
            try:
                await self.list_apps(workspace_url, token)
                caps["can_discover"] = True
            except DatabricksError as apps_err:
                try:
                    await self.list_serving_endpoints(workspace_url, token)
                    caps["can_discover"] = True
                except DatabricksError as endpoints_err:
                    logger.info(
                        "[databricks] capability probe: no readable listing (%s, %s)",
                        apps_err.kind,
                        endpoints_err.kind,
                    )

        if account_admin_client_id and account_admin_secret:
            try:
                account_token = await self.mint_account_token(
                    account_id, account_admin_client_id, account_admin_secret
                )
                policies = await self._list_federation_policies(self._account_host, account_id, account_token)
                caps["account_admin"] = True
                caps["user_sync"] = any(_is_entra_policy(p) for p in policies)
            except DatabricksError as err:
                logger.info("[databricks] capability probe: account-admin probe failed (%s)", err.kind)

        return caps
