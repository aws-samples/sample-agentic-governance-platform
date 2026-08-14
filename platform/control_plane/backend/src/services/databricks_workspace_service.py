"""The ONE Databricks REST client (E29/T2 — contract C-2).

Databricks' governed surface is small enough that a whole SDK dependency would be worse than a
handful of documented request shapes: token minting, the federated-token exchange, two paginated
listings, the app-permissions surface (read, additive grant, and the asserting replace design §3A
needs), one SCIM create, and the account federation policy's audience
list. So this module is plain ``httpx`` over a handful of endpoints, and its request shapes are the
product — every one of them is pinned by ``tests/test_databricks_workspace_service.py`` against a
fake that answers 400 to a drifted form.

DESIGN — credentials are PARAMETERS, never fetched here (contract C-2). This service reads no
Secrets Manager entry and holds no state: the caller (``tenant_service`` / the provisioning half)
resolves a workspace's SP secret and passes it in. That is what keeps an adapter from widening its
own authority, and it is why ``probe_capabilities`` takes plain values rather than a ``Tenant`` —
which ALSO keeps this task independent of T1's model work (pinned by test).

SECURITY — a Databricks error body is not safe to propagate. A real ``PERMISSION_DENIED`` message
names workspace paths and principal ids, and an OIDC failure can echo the form back. So every
non-2xx becomes a ``DatabricksError`` whose ``.message`` is composed HERE from the method, the
path, and the status, and whose ``.kind`` is a code matched against ``_SAFE_KIND``
(``^[A-Za-z_]{1,64}$``) — the ``agent_identity_service._safe_probe_detail`` idiom. No secret, JWT,
token, or upstream body ever reaches a message or a log line.

DEFENSIVE READS — the Apps response field names (``url``, ``oauth2_app_client_id``, the app's
dedicated service-principal id) are UNVERIFIED against a published schema — the E29 live test is
what pins the real one. The listings therefore key on ``name`` ONLY, skip a
record that lacks it (with a logged safe code, never a ``KeyError``), and pass every other field
through untouched. A discovery listing that crashes on one odd record is worse than one that
reports the rest.

Mechanics (proven in the E29 live test): Apps and
serving-endpoint listings require an authenticated workspace client; the federation policy's
RFC 8693 exchange REQUIRES ``client_id`` for service-principal policies but PROHIBITS it for
account-wide ones; token acquisition is ``client_credentials`` over HTTP Basic; ACLs are
level-based; account and workspace hosts differ, and ``apps list`` visibility follows app
permissions.
"""

from __future__ import annotations

import contextlib
import logging
import re
from typing import Optional, Sequence
from urllib.parse import quote

import httpx

logger = logging.getLogger(__name__)

# Explicit timeout on every call. httpx will happily wait forever on some transports, and an
# unbounded Databricks read would pin a uvicorn worker on a customer's slow workspace.
_HTTP_TIMEOUT_SECONDS = 10.0

# Hard bound on a paginated listing. The exit condition is upstream-controlled, so a workspace
# that never stops handing out page tokens must not be able to spin a request slot forever.
_MAX_PAGES = 100

# Databricks' AWS account console — the host for account-level APIs (federation policies,
# account SPs). NOT a customer value and NOT derivable from the workspace URL (research §5.1),
# so it is a default the caller may override (Azure/GCP hosts, and tests' fake).
_DEFAULT_ACCOUNT_HOST = "https://accounts.cloud.databricks.com"

_TOKEN_PATH = "/oidc/v1/token"
_APPS_PATH = "/api/2.0/apps"
_SERVING_ENDPOINTS_PATH = "/api/2.0/serving-endpoints"
_APP_PERMISSIONS_PATH = "/api/2.0/permissions/apps"
_SCIM_SERVICE_PRINCIPALS_PATH = "/api/2.0/preview/scim/v2/ServicePrincipals"

# The per-SP OAuth secret sub-resource. PINNED LIVE 2026-08-11 (B2.5): the workspace host
# serves it under an ``/accounts/`` prefix — ``/api/2.0/accounts/servicePrincipals/{id}/
# credentials/secrets`` returned 200 with ``{id, secret, secret_hash, status, expire_time}``
# (secret lifetime ~2 years), while the previously-inferred un-prefixed path 404s. It is a
# ``{}``-template rather than a prefix so the SCIM id is quoted into ONE segment.
_SP_SECRETS_PATH_TEMPLATE = "/api/2.0/accounts/servicePrincipals/{sp_id}/credentials/secrets"

_SCOPE_ALL_APIS = "all-apis"
_GRANT_CLIENT_CREDENTIALS = "client_credentials"
_GRANT_TOKEN_EXCHANGE = "urn:ietf:params:oauth:grant-type:token-exchange"
_SUBJECT_TOKEN_TYPE_JWT = "urn:ietf:params:oauth:token-type:jwt"

_SCIM_SP_SCHEMA = "urn:ietf:params:scim:schemas:core:2.0:ServicePrincipal"
_PERMISSION_CAN_USE = "CAN_USE"
_PERMISSION_CAN_MANAGE = "CAN_MANAGE"

# The three principal kinds an app ACL entry can name, and the response/request key each uses.
# ONE map, read in both directions (``_ACL_KEY_BY_KIND`` writes, its inverse normalizes reads),
# so ``get_app_permissions`` and the ``set_app_permissions`` that follows it can never disagree
# about what a "user" is keyed on. NOTE the scope: ``grant_app_can_use`` keeps its own legacy
# UUID heuristic for ``kind="group"`` (back-compat), so the invariant covers get/set only.
_ACL_KEY_BY_KIND = {
    "user": "user_name",
    "group": "group_name",
    "service_principal": "service_principal_name",
}
_KIND_BY_ACL_KEY = {key: kind for kind, key in _ACL_KEY_BY_KIND.items()}

# The app ACL vocabulary is CLOSED (research §4). Ranked, because a write shape keeps one level
# per principal and the strongest must win — a collapse that downgraded a CAN_MANAGE would cost
# AGP the permission its next write needs.
_APP_LEVEL_RANK = {_PERMISSION_CAN_USE: 1, _PERMISSION_CAN_MANAGE: 2}

# The workspace admins group. Design §3A: platform admins stay above the governance layer, so
# every asserted ACL carries their ``CAN_MANAGE`` — see ``set_app_permissions``.
_ADMINS_GROUP = "admins"
_ADMINS_ENTRY = {"principal": _ADMINS_GROUP, "kind": "group", "level": _PERMISSION_CAN_MANAGE}

# Entra's issuer form is ``https://login.microsoftonline.com/{tenantId}/v2.0`` (research §3.3);
# its presence on an account federation policy is what makes "Entra identities are federated
# into this account" an honest claim rather than a guess.
_ENTRA_ISSUER_MARKER = "login.microsoftonline.com"

# A ``.kind`` may only ever be a short opaque code — see the module docstring. Underscore is
# allowed because Databricks' own codes are SCREAMING_SNAKE (``PERMISSION_DENIED``).
_SAFE_KIND = re.compile(r"^[A-Za-z_]{1,64}$")

# Fallback kinds by HTTP status, used when the body carries no usable code.
_KIND_BY_STATUS = {
    400: "bad_request",
    401: "unauthorized",
    403: "forbidden",
    404: "not_found",
    409: "conflict",
    429: "rate_limited",
}

# A UUID-shaped principal is a service principal's application id; anything else is a group
# name (research §4's ACL entry shapes). Guessing wrong would 400 loudly, not grant silently.
_UUID = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")


def _is_entra_policy(policy: dict) -> bool:
    """Is this federation policy issued by Entra? The ONE predicate both scan sites share.

    A policy whose ``oidc_policy`` is not a dict is treated as not-an-Entra-policy rather than
    inspected: ``(p.get("oidc_policy") or {}).get(...)`` raised a bare ``AttributeError`` on a
    string/list/int value, which escaped ``ensure_federation_audience`` unmapped and — worse — hit
    ``probe_capabilities`` AFTER it had already set ``account_admin=True``, so a malformed policy
    crashed a probe whose entire contract is "never raise". Skipping is also the honest reading: a
    shape we cannot parse is not evidence of Entra federation.

    This lives at module level because the two callers MUST agree. When they disagreed,
    ``ensure_federation_audience`` wrote to a policy that ``probe_capabilities`` was not reporting on.
    """
    oidc = policy.get("oidc_policy")
    if not isinstance(oidc, dict):
        return False
    return _ENTRA_ISSUER_MARKER in str(oidc.get("issuer") or "")


def _to_write_shape(entries: list[dict]) -> list[dict]:
    """Collapse READ-shaped ACL entries into a shape that is safe to PUT.

    :meth:`DatabricksWorkspaceService.get_app_permissions` is deliberately flattening and
    deliberately inclusive of inherited grants — right for the drift diff, WRONG as a write body.
    Two things must happen before any read is re-asserted:

    * ONE entry per (principal, kind), strongest level wins. A principal holding two levels reads
      as two entries, and a PUT naming the same principal twice is undocumented upstream: last-wins
      would DOWNGRADE (e.g. the tenant SP from ``CAN_MANAGE`` to an inherited ``CAN_USE``, costing
      AGP the permission its next write needs), a 400 would make revoke unusable on that app.
    * INHERITED entries are dropped. §3A distinguishes direct entries (assertable, strippable) from
      what AGP cannot remove; re-PUTting an inherited level converts it into a permanent DIRECT
      grant — a durability widening the drift probe would then report forever.
    * An OUT-OF-VOCABULARY level is dropped here, loudly. :meth:`set_app_permissions` refuses one
      (the vocabulary is closed, research §4), so leaving it in the write shape made every revoke
      on that app raise ``acl_entry_invalid`` FOREVER — blaming the caller's composition for
      something the workspace reported. A replace genuinely cannot preserve a level the client may
      not transmit, so dropping is the only honest option; the warning is what makes it a chosen
      behavior rather than a silent one.

    Module level, not a method, because every read-modify-write path (revoke today, §3A's
    re-assert next) must collapse identically or they drift apart."""
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
        # A count and a safe label only — an ACL entry names customer principals.
        logger.warning(
            "[databricks] app ACL write shape: dropped %d entr(y/ies) at a permission level this "
            "client may not transmit; the re-asserted list will not carry them",
            dropped,
        )
    return list(collapsed.values())


class DatabricksError(Exception):
    """A Databricks operation failed. Carries a SAFE ``.message`` (never a token, secret, JWT, or
    upstream body) plus a ``.kind`` code callers map to a fixed HTTP status + fixed detail —
    the ``connection_service.ConnectionError`` idiom."""

    def __init__(self, message: str, kind: str) -> None:
        super().__init__(message)
        self.message = message
        self.kind = kind


def _origin(url: str) -> str:
    """Normalize a caller-supplied host to an origin with no trailing slash."""
    return (url or "").rstrip("/")


def _safe_kind(resp: httpx.Response) -> str:
    """The safest ``.kind`` derivable from a response: the upstream error CODE when it is a bare
    code, else a fixed per-status word. Never the upstream MESSAGE, which names paths, principals,
    and sometimes the request form."""
    code = ""
    with contextlib.suppress(Exception):
        body = resp.json()
        if isinstance(body, dict):
            # REST APIs use ``error_code``; the OIDC token endpoint uses ``error``.
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
    """Token minting, discovery listings, grants, SCIM SPs, and federation audiences.

    ``http_client`` is injectable (tests pass a ``MockTransport`` client); when absent, each call
    opens a fresh timed ``httpx.AsyncClient`` — the ``langfuse_metrics_service`` idiom, which is
    safe here because every route runs on the uvicorn loop.
    """

    def __init__(
        self,
        *,
        http_client: Optional[httpx.AsyncClient] = None,
        account_host: str = _DEFAULT_ACCOUNT_HOST,
    ) -> None:
        self._injected_client = http_client
        self._account_host = _origin(account_host)

    # ------------------------------------------------------------------ #
    # HTTP plumbing
    # ------------------------------------------------------------------ #
    @contextlib.asynccontextmanager
    async def _client(self):
        """Yield the injected (caller-owned) client, or a fresh per-call timed one."""
        if self._injected_client is not None:
            yield self._injected_client
        else:  # pragma: no cover — the live path; unit tests always inject.
            async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT_SECONDS) as client:
                yield client

    async def _request(self, method: str, url: str, *, what: str, **kwargs) -> dict:
        """One request → its JSON body, or ``DatabricksError``.

        ``what`` is a fixed caller-supplied label (e.g. ``"list apps"``) used to build the safe
        message; nothing from ``kwargs`` or the response body ever reaches it. A transport failure
        is ``unreachable``; unparseable JSON is ``bad_response``.

        The except clause names three types, not one: ``httpx.InvalidURL`` and the bare
        ``ValueError`` httpx raises for an unroutable URL are NOT ``httpx.HTTPError`` subclasses,
        so an empty or malformed ``workspace_url`` (a plausible half-configured tenant record)
        escaped this boundary raw as ``ValueError: unknown url type`` — a 500 with an
        httpx-internal message instead of a safe, mapped error."""
        try:
            async with self._client() as client:
                resp = await client.request(
                    method, url, timeout=_HTTP_TIMEOUT_SECONDS, **kwargs
                )
        except httpx.HTTPError:
            # The exception text can contain the URL but never a credential; even so, only a
            # fixed message is surfaced.
            logger.warning("[databricks] transport failure on %s", what)
            raise DatabricksError(
                f"Databricks was unreachable ({what})", kind="unreachable"
            ) from None
        except (httpx.InvalidURL, ValueError):
            logger.warning("[databricks] malformed request URL on %s", what)
            raise DatabricksError(
                f"the Databricks URL is not usable ({what})", kind="bad_request_url"
            ) from None

        if resp.status_code >= 400:
            kind = _safe_kind(resp)
            logger.warning(
                "[databricks] %s failed with status %s (%s)", what, resp.status_code, kind
            )
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
            raise DatabricksError(
                f"Databricks returned no access token ({what})", kind="bad_response"
            )
        return token

    # ------------------------------------------------------------------ #
    # Tokens
    # ------------------------------------------------------------------ #
    async def mint_m2m_token(
        self, workspace_url: str, client_id: str, client_secret: str
    ) -> str:
        """OAuth M2M: a service principal's ``client_id``/``client_secret`` → a workspace token.

        The credentials go as HTTP Basic (research §3.7's ``--user`` form), never as form fields,
        so they stay out of any body that could be logged by an intermediary."""
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
        """RFC 8693 token exchange: an Entra JWT → a Databricks token (research §3.2).

        Sends NO ``client_id``. That is not an omission: ``client_id`` is REQUIRED for a
        service-principal federation policy and PROHIBITED for the account-wide policy this path
        uses, and sending it turns a working exchange into ``invalid_request``. Pinned by test."""
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
        """Mint an ACCOUNT-scoped token (research §3.2's account token endpoint). Account-level
        APIs — federation policies, account SPs — do not accept a workspace token.

        PUBLIC since E29/T6. ``ensure_federation_audience`` REQUIRES an account token and this is
        the only place one is minted, so while this was private its only caller had to reach
        through the underscore. The old name is kept as an alias below.

        ``account_host`` overrides the instance default for THIS CALL. The account console host
        is per-cloud and NOT derivable from the workspace URL (research §5.1), and a tenant
        records its cloud per stage — so the caller that knows the cloud passes the host, rather
        than every caller having to construct a second client to talk to Azure or GCP. It goes
        through ``_origin`` so a trailing slash cannot produce a ``//`` path.

        ``account_id`` is quoted for the same reason ``app_name`` is: it is a stored tenant-record
        field, and unquoted it is a path, not an identifier. ``a1/../../accounts/other/...``
        retargeted this ACCOUNT-ADMIN token mint at a different Databricks account — the highest-
        privilege call in the module aimed by a config value."""
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

    # Back-compat alias: ``probe_capabilities`` below (and any caller written against the
    # pre-T6 private name) keeps working. One line, so the two names cannot drift.
    _mint_account_token = mint_account_token

    # ------------------------------------------------------------------ #
    # Discovery listings (paginated, defensively read)
    # ------------------------------------------------------------------ #
    async def _list_paginated(
        self, url: str, token: str, *, key: str, what: str
    ) -> list[dict]:
        """Page a Databricks list endpoint via ``next_page_token`` and return the named records.

        Each record is kept ONLY if it carries a ``name`` — the one field both listings are keyed
        on. Everything else (the unverified ``url`` / ``oauth2_app_client_id`` / SP id) is passed
        through untouched: this layer never indexes a field it has not confirmed exists.

        Two loop bounds, because the exit condition is entirely upstream-controlled: a workspace
        that echoes a CONSTANT ``next_page_token`` (or cycles a few) would spin this coroutine
        forever, holding a request slot and growing ``records`` without limit. A repeated token is
        caught immediately by ``seen``; ``_MAX_PAGES`` catches the slower cycle. Both surface
        ``pagination_overflow`` rather than silently truncating, since a truncated inventory
        presented as complete is a governance lie."""
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
            # A safe code only — never the record, which may carry customer paths.
            logger.info("[databricks] %s: skipped %d record(s) with no name", what, skipped)
        return records

    async def list_apps(self, workspace_url: str, token: str) -> list[dict]:
        """List Databricks Apps — the primary agent-hosting shape (research §2.1).

        Note for callers: ``apps list`` visibility follows app permissions (§5.3), so a
        discovery SP with no app grants legitimately sees an EMPTY list rather than an error."""
        return await self._list_paginated(
            f"{_origin(workspace_url)}{_APPS_PATH}", token, key="apps", what="list apps"
        )

    async def list_serving_endpoints(self, workspace_url: str, token: str) -> list[dict]:
        """List Model Serving endpoints — the secondary agent shape (research §2.2). ``CAN VIEW``
        suffices to list, so AGP's discovery credential can be genuinely read-only."""
        return await self._list_paginated(
            f"{_origin(workspace_url)}{_SERVING_ENDPOINTS_PATH}",
            token,
            key="endpoints",
            what="list serving endpoints",
        )

    # ------------------------------------------------------------------ #
    # Grants + service principals
    # ------------------------------------------------------------------ #
    def _app_permissions_url(self, workspace_url: str, app_name: str) -> str:
        """The app's permissions URL. ``app_name`` is quoted with ``safe=''`` (the
        ``connection_verify`` idiom) because it is upstream-controlled — it arrives from a
        discovery listing, not from AGP. Unquoted, ``"../../secrets"`` resolved to
        ``/api/2.0/secrets`` and ``"a?x=1"`` injected a query string: a name in a listing became a
        choice of endpoint. It is one helper, not four copies, so the four methods that build this
        URL cannot drift apart on the quoting."""
        return f"{_origin(workspace_url)}{_APP_PERMISSIONS_PATH}/{quote(app_name, safe='')}"

    async def grant_app_can_use(
        self,
        workspace_url: str,
        token: str,
        app_name: str,
        service_principal_or_group: str,
        kind: str = "group",
    ) -> None:
        """Additively grant ``CAN_USE`` on an app — the level required to call its ``/api/``
        routes (§3.6).

        PATCH, and additive on purpose: this is the *incremental* write (one new grant on an
        already-asserted app). The replacing write is :meth:`set_app_permissions` — design §3A
        makes assert-by-PUT the takeover semantic for an AGP-governed app, so "PUT would silently
        revoke the customer's grants" is no longer an argument against PUT existing here; it is
        the reason the two are separate methods with separate names. Use PATCH to add a grant,
        PUT to assert the whole list.

        ``kind`` selects the ACL key (``user`` → ``user_name``, ``group`` → ``group_name``,
        ``service_principal`` → ``service_principal_name``). §3A's per-user mirror needs
        ``user``: a UPN is not UUID-shaped, so without an explicit kind it would be written as a
        group name and grant the person nothing. The DEFAULT ``"group"`` keeps the original
        behavior exactly — a UUID-shaped principal is still read as a service principal's
        application id — so every pre-T13a call site is unchanged."""
        principal = (service_principal_or_group or "").strip()
        if kind == "group":
            # Legacy heuristic, retained for the existing callers: a UUID-shaped principal is an
            # application id, anything else a group name (research §4).
            key = "service_principal_name" if _UUID.match(principal) else "group_name"
        else:
            key = _ACL_KEY_BY_KIND.get(kind, "group_name")
        await self._request(
            "PATCH",
            self._app_permissions_url(workspace_url, app_name),
            what="grant app permission",
            json={
                "access_control_list": [
                    {key: principal, "permission_level": _PERMISSION_CAN_USE}
                ]
            },
            headers={"Authorization": f"Bearer {token}"},
        )

    async def get_app_permissions(
        self, workspace_url: str, token: str, app_name: str
    ) -> list[dict]:
        """Read an app's ACL as flat ``{"principal", "kind", "level", "inherited"}`` entries.

        This is the read half of design §3A: the returned list is what the drift probe diffs
        against the Entra assignment list, and what a revoke re-composes from. Databricks reports
        one record per principal carrying an ``all_permissions[]`` array, so a principal holding
        two levels yields TWO entries — flattening here means no caller has to know that shape.

        INHERITED permissions are reported as they arrive, not filtered: an entry AGP cannot
        remove still grants access, and hiding it would make the drift report a comfortable lie.
        They are FLAGGED (``"inherited": bool``) because that is the one thing a write path must
        know — see :func:`_to_write_shape`, which drops them rather than materializing them as
        permanent direct grants. A level reported both directly and by inheritance is one entry,
        flagged direct: the direct grant is the one AGP can strip.

        DEFENSIVE READS, same rule as the listings: a record that is not a dict, names no
        recognised principal key, or carries no readable level is SKIPPED with a counted log line
        — never a ``KeyError``. A drift probe that dies on one odd ACL record reports nothing
        about the rest. The skip is logged at WARNING, not info: a caller that re-asserts this list
        would write back a list missing those records, i.e. "I could not parse this" becomes
        "I deleted this", so the line must be loud enough to find afterwards. Duplicate
        (principal, level) pairs collapse, so a repeated level cannot inflate a diff."""
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
            if not isinstance(record, dict):
                skipped += 1
                continue
            principal, kind = "", ""
            for key, candidate_kind in _KIND_BY_ACL_KEY.items():
                value = record.get(key)
                if isinstance(value, str) and value:
                    principal, kind = value, candidate_kind
                    break
            permissions = record.get("all_permissions")
            if not principal or not isinstance(permissions, list):
                skipped += 1
                continue
            levels = 0
            for permission in permissions:
                if not isinstance(permission, dict):
                    continue
                level = permission.get("permission_level")
                if not isinstance(level, str) or not level:
                    continue
                levels += 1
                inherited = bool(permission.get("inherited"))
                key3 = (principal, kind, level)
                kept = seen.get(key3)
                if kept is not None:
                    # Same level twice: a DIRECT report wins over an inherited one.
                    if not inherited:
                        kept["inherited"] = False
                    continue
                entry = {
                    "principal": principal,
                    "kind": kind,
                    "level": level,
                    "inherited": inherited,
                }
                seen[key3] = entry
                entries.append(entry)
            if not levels:
                skipped += 1
        if skipped:
            # A count and a safe label only — an ACL record names customer principals.
            logger.warning(
                "[databricks] read app permissions: skipped %d unreadable ACL record(s)", skipped
            )
        return entries

    async def set_app_permissions(
        self, workspace_url: str, token: str, app_name: str, entries: list[dict]
    ) -> None:
        """ASSERT an app's ACL: PUT the caller's full list, replacing whatever was there.

        Design §3A's takeover write. Replacement is the POINT — pre-existing direct entries are
        how a workspace owner grants access around AGP, and an asserted app is one whose list AGP
        owns. The caller composes the entries (``{"principal", "kind", "level"}``, the shape
        :meth:`get_app_permissions` returns); this method only translates and guards.

        THE GUARD: an entries list without ``admins`` at ``CAN_MANAGE`` is REFUSED
        (``acl_missing_admins``) before any request is issued. A PUT that drops it locks the
        workspace admins out of their own app, and the next write needs ``CAN_MANAGE`` — so the
        mistake is unrecoverable through AGP, which is exactly the class of bug a client library
        should refuse to transmit rather than report afterwards. §3A also requires admins to
        remain above the governance layer, so the guard and the design invariant are the same
        line. An entry naming ``admins`` at a LOWER level does not satisfy it.

        An unrecognised ``kind`` is refused too: silently defaulting it to a group name would
        write a grant to a principal nobody named. So is an unrecognised ``level``: the app ACL
        vocabulary is closed (``CAN_USE``/``CAN_MANAGE``, research §4), and a composer bug that
        wrote ``CAN_MANAGE`` on a mirrored USER entry would hand that person the ability to rewrite
        the app's ACL and un-govern the agent. This method is the last chokepoint before the wire.

        The caller is expected to pass a WRITE-shaped list — one entry per (principal, kind), no
        inherited entries. :func:`_to_write_shape` produces it from a read; this method does not
        collapse, because a PUT body that silently differed from what the caller composed is the
        opposite of an assert. It does REFUSE a list that names the same principal twice
        (``acl_entry_invalid``) — refusing is not rewriting. A PUT naming a principal twice is
        undocumented upstream: last-wins could seat that principal at whichever record arrives
        last, or 400 the whole assert and leave the app unasserted. The caller's own composer is
        the likely source (a user holding two Entra app-role assignments), so the client refuses to
        transmit rather than report afterwards — the same discipline as the admins guard. Users are
        compared case-insensitively, matching :meth:`revoke_app_can_use`."""
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
                    "an app ACL entry names no usable principal, kind, or level "
                    "(assert app permissions)",
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
        """Remove one principal's entries from an app's ACL — read-modify-PUT.

        There is no single-entry DELETE on the permissions API, so removal is necessarily
        "re-assert the list without it". Matching is on the (principal, kind) PAIR, never the
        name alone: a group and a user can share a string, and revoking the user by name would
        silently strip the group of the same name — widening one person's revoke to everyone in
        it.

        A ``user`` principal is matched CASE-INSENSITIVELY. Databricks usernames are email/UPN
        shaped and case-insensitive in practice, while §3A feeds Entra's ``mail`` — which commonly
        returns ``Lars.Svensson@example.com`` where Databricks reports the lowercased form. Exact
        equality there is the §3A bypass itself: the revoke would match nothing, re-PUT the
        unchanged list, report success, and leave the person holding ``CAN_USE`` on the app.

        A revoke that matches nothing still succeeds (see idempotence) but LOGS A WARNING. Silent
        idempotence is correct behavior; a blind success is not worth having when one line
        distinguishes "already gone" from "never matched".

        The freshly-read list is collapsed to write shape (:func:`_to_write_shape`) before the
        re-PUT: the read is flattening and inherited-inclusive by design, and PUTting that back
        verbatim would duplicate a multi-level principal in one body and turn inherited grants into
        permanent direct ones.

        Idempotent: a principal that already holds nothing still re-PUTs the freshly-read list,
        because §3A's revoke must stay retryable after a half-completed write and the desired
        state ("this principal has no grant") is what the caller asked for, not "a change
        happened".

        The ``admins`` ``CAN_MANAGE`` entry is ensured, not merely preserved: if the read reports
        none (a workspace that stripped it, or a record the defensive read skipped),
        :meth:`set_app_permissions`' guard would refuse every re-PUT and revoke would be
        permanently unusable on that app. Putting it back is the §3A invariant anyway."""
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
            # A count and a safe label only — never the principal, which is customer data.
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
            # The WARNING is decided from ``current`` — the PRE-collapse read, inherited included —
            # not from ``remaining``. Databricks commonly reports the admins group's CAN_MANAGE as
            # INHERITED, which the collapse drops; deciding from ``remaining`` would fire this on
            # EVERY revoke, drowning the one event it exists for (a workspace that really stripped
            # admins) in routine noise.
            if not any(_is_admins_can_manage(e) for e in current):
                # WARNING, not info: on a workspace that deliberately stripped the admins group's
                # explicit CAN_MANAGE, this revoke ADDS a grant nobody asked for. §3A asserts the
                # entry anyway (and without it the guard would refuse every re-PUT, making revoke
                # permanently unusable on that app) — but a takeover is loud, never silent.
                logger.warning(
                    "[databricks] revoke app permission: ADDED the missing admins CAN_MANAGE entry "
                    "to the asserted ACL"
                )
            remaining.insert(0, dict(_ADMINS_ENTRY))
        await self.set_app_permissions(workspace_url, token, app_name, remaining)

    async def create_service_principal(
        self, workspace_url: str, token: str, display_name: str
    ) -> dict:
        """Create a workspace service principal via SCIM (research §2.4) and return
        ``{"id", "application_id"}``.

        Secret creation is deliberately NOT here: a per-SP secret is a Secrets-Manager-owned
        lifecycle (Global Constraints), and one method that both creates an identity and hands
        back a credential is the shape that leaks. Both fields are read defensively — SCIM
        responses vary by preview revision — so a missing one is ``""``, never a ``KeyError``."""
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

    async def create_service_principal_secret(
        self, workspace_url: str, token: str, sp_id: str
    ) -> dict:
        """Mint an OAuth secret for an existing service principal. Returns
        ``{"secret", "secret_hash", "id"}``.

        ``sp_id`` is the SCIM **id** (the ``"id"`` key of :meth:`create_service_principal`), NOT
        the ``application_id``. The two are different identifiers for the same principal and only
        the SCIM id addresses this sub-resource; passing the application id yields a 404 that
        reads like "the service principal does not exist". Quoted with ``safe=''`` for the same
        reason every other path segment here is: it is a stored value, and unquoted a segment is
        a choice of endpoint.

        SECURITY — this is the ONE method in this module that returns a live credential, and it
        is deliberately separate from :meth:`create_service_principal`: a single method that both
        creates an identity and hands back its secret is the shape that leaks, because every
        caller of "create me an SP" then holds a credential it did not ask for. The returned
        ``secret`` is a plain value the CALLER must place in Secrets Manager immediately; nothing
        here logs it, stores it, or puts it in an error (``_request`` composes messages from the
        method, path and status only, so the 4xx path cannot echo it either).

        ``secret`` is required — a response without one is ``bad_response``, never an empty
        string, because an empty secret persisted as though it were real produces a service
        principal that can never authenticate and a record that claims it can. ``secret_hash``
        and ``id`` are read defensively (missing → ``""``): they are useful for correlation, not
        load-bearing."""
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
                "Databricks returned no service-principal secret (create service principal "
                "secret)",
                kind="bad_response",
            )
        return {
            "secret": secret,
            "secret_hash": str(body.get("secret_hash") or ""),
            "id": str(body.get("id") or ""),
        }

    # ------------------------------------------------------------------ #
    # Federation policy audiences
    # ------------------------------------------------------------------ #
    def _policies_url(self, account_host: str, account_id: str) -> str:
        """The account's federation-policy collection URL. ``account_id`` is quoted (see
        ``_mint_account_token``): unquoted it could retarget these account-level reads and writes
        at another customer's account."""
        return (
            f"{_origin(account_host) or self._account_host}"
            f"/api/2.0/accounts/{quote(account_id, safe='')}/federationPolicies"
        )

    async def _list_federation_policies(
        self, account_host: str, account_id: str, token: str
    ) -> list[dict]:
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
        """Idempotently add or remove audience(s) on the account-wide federation policy.

        ``audience`` may be one string or a sequence of them; EITHER WAY the policy is read
        once and PATCHed at most once (E29 livefix-7: teardown removes both audience forms —
        the client-id GUID and the legacy api:// URI — and two back-to-back single-audience
        calls tripped the account API's rate limit live, reporting failure over a removal
        that had already succeeded).

        Idempotent in BOTH directions, and the no-op is a genuine no-op — an audience already in
        the desired state issues NO PATCH at all (pinned by call count), so re-running provisioning
        cannot append a duplicate entry or churn a policy other tenants share.

        ``present=True`` with no policy on the account is an ERROR, not a create: this method has
        no issuer, no subject claim, and no JWKS to invent one with, and guessing would produce a
        trust statement nobody authored. ``present=False`` with no policy is a no-op — the desired
        state already holds.

        THE POLICY IS SELECTED BY ISSUER, never by position. An account may hold up to 20 policies
        (research §3.2) and an enterprise plausibly federates Okta for humans alongside Entra;
        ``policies[0]`` wrote AGP's audience onto whichever happened to be first, which in that
        setup means editing Okta's trust statement while ``probe_capabilities`` reports on the
        Entra one. The same ``_ENTRA_ISSUER_MARKER`` discrimination is used in both places so they
        can never disagree. Two Entra policies is ``federation_policy_ambiguous`` — with no
        subject-claim or audience hint to choose on, guessing would edit shared trust state on a
        coin flip.

        LOST UPDATE (accepted, documented): audiences are read then written, and the Databricks
        federation-policy API offers NO ETag, version, or If-Match concurrency token, so there is
        no CAS to use — a concurrent writer between the GET and the PATCH loses its change. The
        blast radius is bounded on purpose: the PATCH body is the freshly-read list plus exactly
        this call's one addition or removal (never a reconstructed or defaulted list), so a lost
        update can drop a *concurrent* edit but can never drop audiences this call read. AGP's
        provisioning is sequential per tenant, which is the assumption this relies on; if that ever
        changes, this needs an external lock rather than a retry."""
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
            raise DatabricksError(
                "the Databricks federation policy has no id", kind="bad_response"
            )

        oidc = policy.get("oidc_policy") if isinstance(policy.get("oidc_policy"), dict) else {}
        raw_audiences = oidc.get("audiences")
        # A missing or wrong-shaped ``audiences`` must NOT read as "empty". The field name is
        # inferred from the CLI examples, not from a loaded reference page (research §3.2), so a
        # singular ``audience`` or a bare string would have made ``current == []`` and turned this
        # method into "replace the customer's audience list with one element" — silent deletion of
        # account-level trust state. Refusing to write is the only safe reading of a shape we
        # cannot parse.
        if not isinstance(raw_audiences, list) or not all(
            isinstance(a, str) for a in raw_audiences
        ):
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

    # ------------------------------------------------------------------ #
    # Capability probe (plain values, fail-closed)
    # ------------------------------------------------------------------ #
    async def probe_capabilities(
        self,
        workspace_url: str,
        account_id: str,
        client_id: str,
        client_secret: str,
        account_admin_client_id: Optional[str] = None,
        account_admin_secret: Optional[str] = None,
    ) -> dict:
        """What can AGP actually DO on this tenant? Returns
        ``{"can_discover", "account_admin", "user_sync"}``.

        Plain values, no tenant model — C-2's binding, so this stays callable before T1 lands.

        FAIL-CLOSED: every branch is wrapped independently and NEVER raises. A capability flag is
        a promise made to the UI ("connect a Databricks tenant" shows the user what each grant
        unlocks — research §5.3), and the honest answer to "the probe blew up" is False, not an
        error page and not an optimistic True.

        - ``can_discover`` — mint a workspace M2M token and read a listing. Apps first;
          serving endpoints as the fallback, because ``apps list`` returns nothing for an SP with
          no app grants (§5.3), which would otherwise read as "discovery is broken".
        - ``account_admin`` — only probed when the SEPARATE, optional account-admin credentials
          are supplied (Tier 3 in §5.3 is a deliberately distinct grant): mint an account token
          and list federation policies.
        - ``user_sync`` — True only when an account policy's issuer is an Entra issuer (§3.3),
          i.e. Entra identities really are federated in. Everything else is False.
        """
        caps = {"can_discover": False, "account_admin": False, "user_sync": False}

        try:
            token = await self.mint_m2m_token(workspace_url, client_id, client_secret)
        except Exception:
            logger.info("[databricks] capability probe: workspace token mint failed")
            token = ""

        if token:
            try:
                await self.list_apps(workspace_url, token)
                caps["can_discover"] = True
            except Exception:
                try:
                    await self.list_serving_endpoints(workspace_url, token)
                    caps["can_discover"] = True
                except Exception:
                    logger.info("[databricks] capability probe: no readable listing")

        if account_admin_client_id and account_admin_secret:
            try:
                account_token = await self._mint_account_token(
                    account_id, account_admin_client_id, account_admin_secret
                )
                policies = await self._list_federation_policies(
                    self._account_host, account_id, account_token
                )
                caps["account_admin"] = True
                caps["user_sync"] = any(_is_entra_policy(p) for p in policies)
            except Exception:
                logger.info("[databricks] capability probe: account-admin probe failed")

        return caps
