"""Microsoft Entra / Graph / On-Behalf-Of adapter (Epic 6, Task T-GRAPH).

``GraphService`` is the single owner of ALL Microsoft Entra / Microsoft Graph /
OBO HTTP for the platform. It is **pure**: no boto3, no FastAPI, no DynamoDB —
just ``httpx.AsyncClient`` + the backend-secret loader. That makes it fully
unit-testable with mocked HTTP (``httpx.MockTransport``).

Downstream callers:
  - T-IDENTITY (provisioning orchestrator) → ``create_agent_app`` /
    ``set_assignment_required`` / ``grant_backend_obo_consent``.
  - T-ROUTES (``/grants``, ``/invoke``) → ``list_assignments`` / ``assign_app_role``
    / ``revoke_app_role`` / ``search_principals`` / ``obo_exchange``.

Mechanics source: research §2 (the OBO ``/token`` request shape + the
``AADSTS50105``/``65001``/``500011`` codes; the provisioning Graph calls; the
``appRoleAssignedTo`` read/assign/revoke shapes + the ``$search`` picker).

SECURITY (CRITIQUE-FIX-E) — the backend client secret, the inbound ``user_token``
(OBO assertion), and the OBO'd access token are NEVER logged, printed, or placed
into any exception message. ``GraphError`` carries ONLY the HTTP status + the
Graph/Entra error CODE — never the raw response body, the assertion, or any
token. Logs (if any) record only method + status + code.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
import uuid
import weakref
from typing import Callable, Optional

import httpx

from core.config import settings
from core.secrets_loader import load_entra_backend_client_secret

logger = logging.getLogger(__name__)

# Microsoft Graph scope for the client-credentials (app-only) token.
_GRAPH_DEFAULT_SCOPE = "https://graph.microsoft.com/.default"

# Refresh the cached app token this many seconds BEFORE its stated expiry, so an
# in-flight Graph call never races an expiry boundary.
_TOKEN_EXPIRY_SKEW_SECONDS = 60.0

# --- Entra directory-replication retry (shared) ---------------------------
# POST /applications returns the new appId IMMEDIATELY, but the application object —
# and, one step later, the freshly-created service principal — take time to replicate
# across the directory. Calls issued right after that REFERENCE a just-created object
# transiently fail until replication catches up:
#   - POST /servicePrincipals {"appId": <new app>}        → 400 Request_BadRequest
#     ("The appId 'X' ... does not reference a valid application object") or 404.
#   - PATCH /servicePrincipals/{new sp}                   → 404 Request_ResourceNotFound.
#   - POST /oauth2PermissionGrants {resourceId: <new sp>} → 404 ("Resource '…' does not
#     exist or one of its queried reference-property objects are not present").
# This is the Entra twin of the AWS CREATING→DRAFT race. We retry ONLY those transient
# shapes (400/404); a 403 (permissions) or any other status fails fast. The sleep is
# ASYNC (await asyncio.sleep) because these calls run on the single uvicorn event loop
# via BackgroundTasks (provision()) — a blocking time.sleep would freeze the loop.
#
# WINDOW: Entra replication is usually seconds but can take up to ~a minute under load,
# so the total backoff window is widened to ~52s. Linear backoff CAPPED per-attempt at
# _REPLICATION_RETRY_MAX_DELAY_SECONDS so it does not grow unboundedly. With 11 attempts,
# base=1.0, cap=8.0 the per-sleep delays (after attempts 1..10) are
# 1,2,3,4,5,6,7,8,8,8 = 52s total; the 11th attempt does not sleep. The whole window is
# bounded (it blocks only the background task, never the request — which already
# returned 201 pending — and is NEVER unbounded).
_REPLICATION_RETRY_ATTEMPTS = 11
_REPLICATION_RETRY_BASE_DELAY_SECONDS = 1.0
_REPLICATION_RETRY_MAX_DELAY_SECONDS = 8.0
# HTTP statuses that indicate the transient replication-lag race (retry these).
_REPLICATION_TRANSIENT_STATUSES = frozenset({400, 404})


# ===========================================================================
# Domain errors
# ===========================================================================
class NotAssignedError(Exception):
    """OBO refused because the user isn't assigned to the agent app (AADSTS50105)."""


class OboConfigError(Exception):
    """OBO failed for a CONFIG reason, not assignment — AADSTS65001 (no delegated grant
    backend→agent) or AADSTS500011 (resource/scope unresolved). Distinct so the route
    surfaces 'identity misconfigured' (re-provision), not a user 403 or a runtime 502."""


class GraphError(Exception):
    """Non-recoverable Graph/Entra API error. Carries status + Graph error code, plus
    an OPTIONAL safe ``message`` (CRITIQUE-FIX-E).

    SECURITY: ``message`` is populated ONLY from a Graph RESOURCE endpoint's
    ``error.message`` (``_graph_error``) — those bodies (``/applications``,
    ``/servicePrincipals``, ``/oauth2PermissionGrants``, ``appRoleAssignedTo``,
    ``$search``) carry NO token and NO client secret (Graph never echoes the
    Authorization header). The Entra ``/token`` path (``_token_error``) NEVER sets
    a message — its body's ``error_description`` can echo the assertion/secret, so
    that path stays status+code only. NEVER place the raw token body, the assertion,
    or any token into ``message``."""

    def __init__(self, status: int, code: str, message: str | None = None) -> None:
        # status + code always; message ONLY for safe resource-error detail.
        # NEVER the assertion / token / client secret.
        self.status = status
        self.code = code
        self.message = message
        base = f"Graph/Entra error (status={status}, code={code}"
        if message:
            base += f", message={message}"
        base += ")"
        super().__init__(base)


# ===========================================================================
# Service
# ===========================================================================
class GraphService:
    """Async adapter over Microsoft Entra's OAuth2 ``/token`` endpoint and Graph.

    The client-credentials app token (``_app_token``) is used as a Bearer for all
    Graph (``graph.microsoft.com``) calls. The OBO exchange and the
    client-credentials request itself go to the Entra token endpoint with
    form-encoded bodies carrying ``client_id`` + ``client_secret`` (no app-token
    Bearer).
    """

    def __init__(
        self,
        *,
        tenant_id: str,
        backend_client_id: str,
        login_base: str,
        graph_base: str,
        audience_prefix: str,
        client_secret_loader: Callable[[], str] = load_entra_backend_client_secret,
        http_client: Optional[httpx.AsyncClient] = None,
    ) -> None:
        self._tenant_id = tenant_id
        self._backend_client_id = backend_client_id
        self._login_base = login_base.rstrip("/")
        self._graph_base = graph_base.rstrip("/")
        # Identifier-URI prefix for per-agent app regs (e.g. "api://agp-agent-").
        # Injected (like login_base/graph_base) rather than read from settings, so
        # this adapter stays a pure DI component with no config coupling. The
        # production caller passes settings.AGENT_APP_AUDIENCE_PREFIX.
        self._audience_prefix = audience_prefix
        self._client_secret_loader = client_secret_loader
        # Build a default async client if none injected (tests inject a
        # MockTransport-backed one). When a client IS injected, the CALLER owns
        # its lifecycle; we only close a client we created ourselves (aclose()).
        self._owns_client = http_client is None
        # INJECTED client: caller-owned, single-loop (tests' MockTransport). Stored
        # directly and NEVER placed in the per-loop map — returned unchanged by _http.
        self._injected_client = http_client
        # SELF-created clients keyed by the event loop they are bound to. A default
        # httpx.AsyncClient binds its asyncio transport to the loop of its FIRST
        # request; the sync-driven teardown/provision paths
        # (project_service._delete_identity/_provision_identity) each run their
        # coroutine via asyncio.run(), which creates AND CLOSES a fresh loop per call.
        # Reusing a client from a later loop hits the now-closed transport →
        # "RuntimeError: ... the handler is closed" (E23 live-test bug). We keep ONE
        # client per loop so no live-loop client is ever silently orphaned: the main
        # uvicorn loop and each asyncio.run loop each retain their own. A
        # WeakKeyDictionary keyed by the loop lets a dead loop's client be GC'd
        # without manual cleanup (its transport already died with the loop). The lock
        # guards the get-or-create against concurrent threadpool asyncio.run calls
        # (sync routes run in FastAPI's threadpool).
        self._clients_by_loop: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, httpx.AsyncClient]" = (
            weakref.WeakKeyDictionary()
        )
        self._clients_lock = threading.Lock()
        # The Entra OAuth2 token endpoint (client-credentials + OBO both POST here).
        self._token_endpoint = f"{self._login_base}/{tenant_id}/oauth2/v2.0/token"

        # Cached client-credentials app token + its monotonic expiry timestamp.
        self._app_token_value: Optional[str] = None
        self._app_token_expiry: float = 0.0

        # E16: cache of (sp_object_id, {appRole.value: appRoleId}) per platform app
        # client id. The platform SP + its appRoles are stable for the process
        # lifetime, so resolve once and reuse across list/add/change/remove.
        self._platform_sp_cache: dict[str, tuple[str, dict[str, str]]] = {}

    @property
    def _http(self) -> httpx.AsyncClient:
        """The httpx client SAFE to use on the currently-running event loop.

        For an INJECTED client the caller owns the lifecycle and loop affinity —
        we return it unchanged (tests' single-loop MockTransport client is never
        swapped nor tracked). For a SELF-created client we keep ONE client PER
        event loop in ``_clients_by_loop`` and return the one bound to the current
        running loop, creating it lazily if absent. This never silently orphans a
        still-live loop's client (the main uvicorn loop and each ``asyncio.run``
        loop each retain their own), so there is no connection/fd leak or churn on
        main↔threadpool alternation, and no cross-loop reuse of a closed transport
        (the E23 closed-handler ``RuntimeError``). The get-or-create is guarded by
        ``_clients_lock`` because sync routes drive concurrent ``asyncio.run`` on
        FastAPI's threadpool. Accessed off any running loop it falls back to the
        injected client / a lock-guarded default (only ``aclose`` hits this, and it
        iterates the map directly).
        """
        if not self._owns_client:
            return self._injected_client  # caller-owned, single loop, never tracked
        running = asyncio.get_running_loop()  # a self-created client is only read on-loop
        with self._clients_lock:
            client = self._clients_by_loop.get(running)
            if client is None:
                client = httpx.AsyncClient()
                self._clients_by_loop[running] = client
            return client

    async def aclose(self) -> None:
        """Close every self-created httpx client this instance still holds.

        When an ``http_client`` was injected, the caller owns its lifecycle and
        this is a no-op; when we built default clients (one per loop), this closes
        all of them. Best-effort: a client whose loop is already gone is skipped on
        error (its transport died with the loop).
        """
        if not self._owns_client:
            return
        with self._clients_lock:
            # Snapshot the values; do NOT clear the map — a later read of ``_http``
            # on a still-live loop must return the SAME (now-closed) client, not
            # lazily mint a fresh open one (preserves the pre-fix aclose semantics).
            clients = list(self._clients_by_loop.values())
        for client in clients:
            try:
                await client.aclose()
            except Exception:  # noqa: BLE001 - best-effort; loop may already be gone
                pass

    # --- secret (never logged) --------------------------------------------

    def _secret(self) -> str:
        return self._client_secret_loader()

    # --- token endpoint helpers -------------------------------------------

    async def _app_token(self) -> str:
        """Return a client-credentials (app-only) Graph token, cached + refreshed.

        Caches the token until ``expires_in`` (minus a safety skew) elapses; a
        call within validity reuses the cached value (one HTTP call), a call
        after expiry refreshes (a second HTTP call).
        """
        now = time.monotonic()
        if self._app_token_value is not None and now < self._app_token_expiry:
            return self._app_token_value

        form = {
            "grant_type": "client_credentials",
            "client_id": self._backend_client_id,
            "client_secret": self._secret(),
            "scope": _GRAPH_DEFAULT_SCOPE,
        }
        try:
            response = await self._http.post(self._token_endpoint, data=form)
        except httpx.HTTPError as exc:
            # Same transport-error wrap as `_graph_request` / the nextLink page
            # fetch (E24/T5 carry-forward): a timeout/connect failure on the
            # token endpoint (cold/expired cache) must surface as GraphError so
            # callers' `except GraphError` (e.g. TenantResolver._group_ids)
            # catches it uniformly instead of leaking a raw httpx exception.
            # Fixed literal only — never str(exc) — outward; the chained `from
            # exc` keeps the detail for internal logs.
            raise GraphError(502, "transport_error") from exc
        if response.status_code != 200:
            raise self._token_error(response)

        payload = response.json()
        token = payload.get("access_token")
        if not token:
            raise GraphError(response.status_code, "no_access_token_in_response")
        expires_in = float(payload.get("expires_in", 3600))
        self._app_token_value = token
        self._app_token_expiry = time.monotonic() + max(
            0.0, expires_in - _TOKEN_EXPIRY_SKEW_SECONDS
        )
        return token

    @staticmethod
    def _token_error(response: httpx.Response) -> Exception:
        """Map a non-200 Entra ``/token`` response to a domain error.

        Reads ``error_codes`` from the JSON error body (CRITIQUE-FIX-D):
          - contains 50105 → NotAssignedError
          - contains 65001 or 500011 → OboConfigError
          - anything else → GraphError(status + code only)

        NEVER carries the response body, the assertion, or any token outward.
        """
        status = response.status_code
        codes: set[str] = set()
        error_slug = ""
        try:
            body = response.json()
            # Normalize to strings so the mapping is robust if a proxy ever
            # re-serializes the codes as strings (Entra returns ints natively).
            # Without this, a string "50105" would silently collapse a user-403
            # (NotAssignedError) into a generic 502 (GraphError) — a real
            # silent-degradation in security-sensitive mapping.
            codes = {str(c) for c in (body.get("error_codes") or [])}
            error_slug = body.get("error") or ""
        except (ValueError, AttributeError):
            body = {}

        if "50105" in codes:
            # No body/token in the message — the code is the signal.
            return NotAssignedError("user is not assigned to the agent app (AADSTS50105)")
        if "65001" in codes or "500011" in codes:
            return OboConfigError(
                "OBO failed for a configuration reason (AADSTS65001/500011) — re-provision"
            )
        # Generic: status + the Entra error slug/first code ONLY. Never the body.
        code = error_slug or (next(iter(codes)) if codes else "unknown")
        return GraphError(status, code)

    # --- Graph request helper ---------------------------------------------

    async def _graph_request(
        self,
        method: str,
        path: str,
        *,
        params: Optional[dict] = None,
        json_body: Optional[dict] = None,
        extra_headers: Optional[dict] = None,
    ) -> httpx.Response:
        """Issue an authenticated Graph request (Bearer app token).

        Raises ``GraphError(status, code)`` on a non-2xx response, reading the
        Graph error code from the standard ``{"error":{"code":...}}`` envelope.
        The raw body is never propagated outward. A TRANSPORT failure (timeout,
        connect error, etc — ``httpx.HTTPError``, raised on the request itself
        rather than as a response) is likewise wrapped into ``GraphError`` so
        every caller's ``except GraphError`` (e.g. ``TenantResolver._group_ids``)
        catches it uniformly instead of leaking a raw httpx exception.
        """
        token = await self._app_token()
        headers = {"Authorization": f"Bearer {token}"}
        if extra_headers:
            headers.update(extra_headers)
        url = f"{self._graph_base}{path}"
        try:
            response = await self._http.request(
                method, url, params=params, json=json_body, headers=headers
            )
        except httpx.HTTPError as exc:
            raise GraphError(502, "transport_error") from exc
        if not (200 <= response.status_code < 300):
            raise self._graph_error(response)
        return response

    @staticmethod
    def _graph_error(response: httpx.Response) -> GraphError:
        """Map a non-2xx Graph RESOURCE response to GraphError (status + code + safe message).

        This is the RESOURCE path (``/applications``, ``/servicePrincipals``,
        ``/oauth2PermissionGrants``, ``appRoleAssignedTo``, user/group ``$search``).
        Those error bodies carry NO token and NO client secret (Graph never echoes
        the Authorization header), so it is SAFE to surface ``error.message`` for
        observability. Contrast with ``_token_error`` (the ``/token`` path), which
        stays status+code only because its ``error_description`` can echo the
        assertion/secret."""
        code = "unknown"
        message: str | None = None
        try:
            err = response.json().get("error", {})
            if isinstance(err, dict):
                code = err.get("code") or code
                # SAFE: a resource endpoint's message contains no token/secret.
                message = err.get("message") or None
        except (ValueError, AttributeError):
            pass
        return GraphError(response.status_code, code, message)

    @staticmethod
    def _graph_error_code(response: httpx.Response) -> str:
        """Extract the Graph error ``code`` from a (failed) response body, or ''."""
        try:
            err = response.json().get("error", {})
            if isinstance(err, dict):
                return err.get("code") or ""
        except (ValueError, AttributeError):
            pass
        return ""

    # --- provisioning (research §2.2) -------------------------------------

    async def create_agent_app(self, agent_id: str, display_name: str) -> dict:
        """Create the per-AGENT Entra app reg + its SP (§2.2 steps 1-2).

        Thin wrapper over the shared ``_create_app_reg`` builder: the agent path
        uses the injected per-agent ``identifierUris`` prefix and ``["User"]``-only
        appRoles (user-grants depend on the user-only member type). Behaviour +
        request payloads + return shape are unchanged from the pre-refactor method
        (the E6 user→agent path is invariant — locked by the graph_service tests).

        Returns ``{app_id, sp_id, app_uri, invoke_scope_id, invoker_role_id,
        admin_role_id}``.
        """
        return await self._create_app_reg(
            resource_id=agent_id,
            display_name=display_name,
            audience_prefix=self._audience_prefix,
            member_types=["User"],
        )

    async def create_mcp_app(self, mcp_id: str, display_name: str) -> dict:
        """Create the per-MCP-server Entra app reg + its SP (E7, research §2.1).

        THE load-bearing difference from the agent path: the MCP app's appRoles
        allow ``["User", "Application"]`` member types — a ServicePrincipal (the
        agent) cannot be assigned a ``["User"]``-only role, so the MCP roles must
        be assignable to applications. ``["User", "Application"]`` is the safe
        superset (lets a human be assigned for testing AND an agent SP). The MCP
        identifier-URI prefix comes from ``settings.MCP_APP_AUDIENCE_PREFIX``
        (``api://agp-mcp-``). Everything else (the Invoke oauth2 scope, the SP
        create, the get-or-create/replication-retry logic) is reused unchanged.

        Returns the SAME 6-key dict as ``create_agent_app``.
        """
        return await self._create_app_reg(
            resource_id=mcp_id,
            display_name=display_name,
            audience_prefix=settings.MCP_APP_AUDIENCE_PREFIX,
            member_types=["User", "Application"],
        )

    async def _create_app_reg(
        self,
        *,
        resource_id: str,
        display_name: str,
        audience_prefix: str,
        member_types: list[str],
    ) -> dict:
        """Shared per-resource Entra app-reg + SP builder (§2.2 steps 1-2).

        Parameterised on the ``identifierUris`` prefix (``audience_prefix``) and the
        appRoles ``allowedMemberTypes`` (``member_types``) so the agent path
        (``["User"]``) and the MCP path (``["User", "Application"]``) share one
        implementation. The body shape, the Invoke oauth2 scope, the SP-create, and
        the get-or-create/replication-retry behaviour are identical across both.

        GET-or-create (CRITIQUE-FIX-A): ``identifierUris`` is unique tenant-wide,
        so a duplicate-identifierUri error on the create means the app already
        exists (the persist-failed-after-create window) — look it up + its SP and
        return their ids instead of raising. Makes re-provision resumable.

        Returns ``{app_id, sp_id, app_uri, invoke_scope_id, invoker_role_id,
        admin_role_id}`` where ``app_id`` is the app's client GUID and ``app_uri``
        is the ``api://`` identifier URI (AUDIENCE-FORM finding — T-IDENTITY decides
        which is the real ``aud``).
        """
        app_uri = f"{audience_prefix}{resource_id}"
        invoke_scope_id = str(uuid.uuid4())
        invoker_role_id = str(uuid.uuid4())
        admin_role_id = str(uuid.uuid4())

        application = {
            "displayName": display_name,
            "signInAudience": "AzureADMyOrg",
            "identifierUris": [app_uri],
            # E29 livefix-6 (proven live 2026-08-12): Databricks federation policies match
            # subjects on the `email` claim, and Entra puts `email` into an ACCESS token
            # only when the RESOURCE app opts in here — without this, every federated
            # exchange of an OBO'd per-agent token dies with a 400 invalid_grant at the
            # workspace /oidc/v1/token endpoint. Harmless for AgentCore (one extra claim).
            "optionalClaims": {
                "accessToken": [
                    {"name": "email", "essential": False, "additionalProperties": []}
                ]
            },
            "api": {
                "requestedAccessTokenVersion": 2,
                "oauth2PermissionScopes": [
                    {
                        "id": invoke_scope_id,
                        "value": "Invoke",
                        "type": "User",
                        "isEnabled": True,
                        "adminConsentDisplayName": f"Invoke {display_name}",
                        "adminConsentDescription": f"Allows invoking the agent {display_name}.",
                        "userConsentDisplayName": f"Invoke {display_name}",
                        "userConsentDescription": f"Allows invoking the agent {display_name}.",
                    }
                ],
            },
            "appRoles": [
                {
                    "id": invoker_role_id,
                    "value": "Invoker",
                    "displayName": "Invoker",
                    "description": "Can invoke the agent.",
                    "allowedMemberTypes": member_types,
                    "isEnabled": True,
                },
                {
                    "id": admin_role_id,
                    "value": "Admin",
                    "displayName": "Admin",
                    "description": "Administers the agent.",
                    "allowedMemberTypes": member_types,
                    "isEnabled": True,
                },
            ],
        }

        token = await self._app_token()
        headers = {"Authorization": f"Bearer {token}"}
        create_resp = await self._http.request(
            "POST",
            f"{self._graph_base}/applications",
            json=application,
            headers=headers,
        )

        if 200 <= create_resp.status_code < 300:
            app_obj = create_resp.json()
            app_client_id = app_obj["appId"]
            # The just-created app needs a beat to replicate before the SP-create
            # can reference its appId — retry the SP-create on the transient race.
            sp_obj = await self._create_sp(app_client_id)
            return {
                "app_id": app_client_id,
                "sp_id": sp_obj["id"],
                "app_uri": app_uri,
                "invoke_scope_id": invoke_scope_id,
                "invoker_role_id": invoker_role_id,
                "admin_role_id": admin_role_id,
            }

        # Non-2xx: is it the duplicate-identifierUri case? If so, GET-or-create.
        code = self._graph_error_code(create_resp)
        if self._is_duplicate_identifier(create_resp, code):
            logger.info(
                "[graph_service] _create_app_reg: identifierUri exists for resource_id "
                "(status=%s code=%s) — resolving existing app",
                create_resp.status_code,
                code,
            )
            return await self._resolve_existing_app(app_uri)

        raise self._graph_error(create_resp)

    async def _request_with_replication_retry(
        self,
        method: str,
        path: str,
        *,
        json_body: Optional[dict] = None,
        params: Optional[dict] = None,
        label: str = "",
    ) -> httpx.Response:
        """Issue a Graph RESOURCE request, retrying transient (400/404) replication-lag
        failures with bounded async backoff; returns the 2xx ``httpx.Response``.

        Use this for any call that REFERENCES a just-created object that may not have
        replicated yet — the SP-create (references the new app's ``appId``),
        set-assignment-required (``PATCH`` the new SP), and the OBO-consent grant
        (``resourceId`` = the new agent SP). Entra returns the create's id immediately
        but the object takes time to replicate, so these calls transiently return 400
        ``Request_BadRequest`` or 404 ``Request_ResourceNotFound`` until it catches up.

        Behaviour (identical across every caller):
          - ONLY the transient replication shapes (``_REPLICATION_TRANSIENT_STATUSES`` =
            400/404) are retried; a 403 (permissions) or any other status FAILS FAST
            (raised immediately, never masked by a retry).
          - Bounded ``_REPLICATION_RETRY_ATTEMPTS`` attempts with a linear backoff
            CAPPED at ``_REPLICATION_RETRY_MAX_DELAY_SECONDS`` (window ~52s — see the
            module constants). On the final attempt's transient failure, the
            ``GraphError`` is raised as usual.
          - The sleep is ``await asyncio.sleep`` (NOT ``time.sleep``): these run inside
            ``provision()`` on the single uvicorn event loop (FastAPI BackgroundTasks);
            a blocking sleep would freeze that loop. The wider window makes this even
            more critical.

        NOTE: this is for RESOURCE endpoints only — it uses ``_graph_error`` (which is
        SAFE to surface a message). It is NEVER used for the ``/token`` path, whose
        ``_token_error`` stays status+code only (redaction boundary, CRITIQUE-FIX-E).
        """
        url = f"{self._graph_base}{path}"
        for attempt in range(1, _REPLICATION_RETRY_ATTEMPTS + 1):
            token = await self._app_token()
            headers = {"Authorization": f"Bearer {token}"}
            response = await self._http.request(
                method, url, json=json_body, params=params, headers=headers
            )
            if 200 <= response.status_code < 300:
                return response

            # Non-2xx. Only the transient replication-lag shapes (400/404) are
            # retried; everything else (e.g. 403 permissions) fails fast.
            error = self._graph_error(response)
            is_transient = response.status_code in _REPLICATION_TRANSIENT_STATUSES
            is_last_attempt = attempt >= _REPLICATION_RETRY_ATTEMPTS
            if not is_transient or is_last_attempt:
                raise error

            delay = min(
                _REPLICATION_RETRY_BASE_DELAY_SECONDS * attempt,
                _REPLICATION_RETRY_MAX_DELAY_SECONDS,
            )
            logger.info(
                "[graph_service] %s: transient replication failure "
                "(status=%s code=%s) — retrying (attempt %s/%s, delay %ss)",
                label or method,
                error.status,
                error.code,
                attempt,
                _REPLICATION_RETRY_ATTEMPTS,
                delay,
            )
            await asyncio.sleep(delay)

        # Unreachable: the loop either returns the 2xx response or raises on the last
        # attempt. Guard for static analysis / defensive completeness.
        raise GraphError(500, "replication_retry_exhausted")  # pragma: no cover

    async def _create_sp(self, app_client_id: str) -> dict:
        """POST /servicePrincipals for a just-created app, with bounded async retry.

        Handles the create-app-then-create-SP replication race (a freshly-created
        application's ``appId`` is returned immediately but takes time to replicate, so
        the SP-create can transiently 400/404 until it catches up). Delegates the retry
        to the shared ``_request_with_replication_retry`` helper. Returns the SP object
        dict on success.
        """
        response = await self._request_with_replication_retry(
            "POST",
            "/servicePrincipals",
            json_body={"appId": app_client_id},
            label="create_sp",
        )
        return response.json()

    @staticmethod
    def _is_duplicate_identifier(response: httpx.Response, code: str) -> bool:
        """True if the failed /applications create is a duplicate-identifierUri error.

        Graph returns 400/409 with code ``Request_BadRequest`` and a message
        mentioning ``identifierUris``. We match on the code + the message marker
        (the message is the only reliable discriminator from other BadRequests).
        """
        if response.status_code not in (400, 409):
            return False
        if code not in ("Request_BadRequest", "Request_MultipleObjectsWithSameKeyValue"):
            return False
        try:
            message = (response.json().get("error", {}) or {}).get("message", "") or ""
        except (ValueError, AttributeError):
            message = ""
        return "identifieruris" in message.lower()

    async def _resolve_existing_app(self, app_uri: str) -> dict:
        """Look up an existing app by its identifierUri + its SP; return the 6 ids."""
        app_filter = f"identifierUris/any(u:u eq '{app_uri}')"
        app_resp = await self._graph_request(
            "GET", "/applications", params={"$filter": app_filter}
        )
        apps = app_resp.json().get("value", [])
        if not apps:
            # Should not happen (the create said it was a duplicate) — surface clearly.
            raise GraphError(404, "Request_ResourceNotFound")
        app_obj = apps[0]
        app_client_id = app_obj["appId"]

        scopes = (app_obj.get("api", {}) or {}).get("oauth2PermissionScopes", []) or []
        invoke_scope_id = next(
            (s["id"] for s in scopes if s.get("value") == "Invoke"), None
        )
        app_roles = app_obj.get("appRoles", []) or []
        invoker_role_id = next(
            (r["id"] for r in app_roles if r.get("value") == "Invoker"), None
        )
        admin_role_id = next(
            (r["id"] for r in app_roles if r.get("value") == "Admin"), None
        )

        sp_resp = await self._graph_request(
            "GET",
            "/servicePrincipals",
            params={"$filter": f"appId eq '{app_client_id}'"},
        )
        sps = sp_resp.json().get("value", [])
        sp_id = sps[0]["id"] if sps else None

        return {
            "app_id": app_client_id,
            "sp_id": sp_id,
            "app_uri": app_uri,
            "invoke_scope_id": invoke_scope_id,
            "invoker_role_id": invoker_role_id,
            "admin_role_id": admin_role_id,
        }

    async def set_assignment_required(self, sp_id: str, required: bool = True) -> None:
        """Set the SP's ``appRoleAssignmentRequired`` flag (§2.2 step 3).

        ``required`` defaults to **True** — the E6 agent path's user→agent gate
        (``agent_identity_service`` calls this with one positional arg, so it stays
        byte-identical: assignment is required, so OBO honours per-user/group assignment).

        E7 MCP provisioning passes ``required=False`` (research §2.4/§2.5): in a
        DELEGATED/On-Behalf-Of flow Entra enforces the resource app's
        ``appRoleAssignmentRequired`` against the **USER**, not the calling agent SP —
        and our locked design NEVER assigns users to MCP servers (the user is granted the
        AGENT; the AGENT is granted the MCP via a consent grant). So a True flag on the
        MCP SP would block the delegated user with ``AADSTS50105`` by design. The OBO gate
        that REMAINS is the per-agent agent→MCP ``oauth2PermissionGrant``
        (``grant_agent_obo_consent``); revoke it and OBO fails ``AADSTS65001``. This
        deliberately drops the agent-SP app-role *assignment* as an admission gate — it is
        only consulted in an app-only/M2M token (§2.5), a path we do NOT use since OBO works.

        This PATCHes the (possibly JUST-created) SP, which may not have replicated across
        the directory yet (transient 404 ``Request_ResourceNotFound``), so it goes through
        ``_request_with_replication_retry`` (transient 400/404 retried with bounded async
        backoff; 403/other fail fast). Idempotent: re-PATCHing the same value converges, so
        a re-provision of an MCP whose SP is currently True self-heals it to False.
        """
        await self._request_with_replication_retry(
            "PATCH",
            f"/servicePrincipals/{sp_id}",
            json_body={"appRoleAssignmentRequired": required},
            label="set_assignment_required",
        )

    async def grant_backend_obo_consent(self, agent_sp_id: str) -> None:
        """§2.2 step 4: wire backend→agent delegated consent so OBO can resolve.

        Resolves the backend SP object id by its appId, then POSTs an
        ``oauth2PermissionGrants`` for the ``Invoke`` scope.

        The backend-SP LOOKUP is for the long-lived BACKEND app (already replicated),
        so it stays on the plain ``_graph_request`` (no replication retry).

        The grant POST references the JUST-created agent SP as ``resourceId`` — that
        object may not have replicated yet (transient 404 ``Request_ResourceNotFound``
        — "one of its queried reference-property objects are not present"), so it is
        retried with bounded async backoff on the transient 400/404 shapes.

        Idempotent: a duplicate-grant ("already exists") error is SWALLOWED as success.
        That check takes PRECEDENCE over the transient retry: an already-exists is
        itself a 400, but it is a terminal success (the grant is in place), not a
        replication race, so we must NOT retry it. A bespoke loop is used here (rather
        than the generic helper) precisely so the already-exists swallow is evaluated
        BEFORE the transient-retry decision on every attempt.
        """
        sp_resp = await self._graph_request(
            "GET",
            "/servicePrincipals",
            params={"$filter": f"appId eq '{self._backend_client_id}'"},
        )
        sps = sp_resp.json().get("value", [])
        if not sps:
            raise GraphError(404, "Request_ResourceNotFound")
        backend_sp_id = sps[0]["id"]

        grant = {
            "clientId": backend_sp_id,
            "consentType": "AllPrincipals",
            "resourceId": agent_sp_id,
            "scope": "Invoke",
        }
        url = f"{self._graph_base}/oauth2PermissionGrants"
        for attempt in range(1, _REPLICATION_RETRY_ATTEMPTS + 1):
            token = await self._app_token()
            headers = {"Authorization": f"Bearer {token}"}
            response = await self._http.request("POST", url, json=grant, headers=headers)
            if 200 <= response.status_code < 300:
                return

            # Idempotent: swallow an already-exists / duplicate-grant error. This is
            # checked FIRST — it takes precedence over the transient retry (an
            # already-exists is a 400 but a terminal success, not a replication race).
            code = self._graph_error_code(response)
            if self._is_already_exists(response, code):
                logger.info(
                    "[graph_service] grant_backend_obo_consent: grant already exists "
                    "(status=%s code=%s) — treating as success",
                    response.status_code,
                    code,
                )
                return

            # Otherwise the grant references the just-created agent SP, which may not
            # have replicated yet: retry the transient 400/404 shapes; 403/other fail
            # fast; the final transient attempt raises.
            error = self._graph_error(response)
            is_transient = response.status_code in _REPLICATION_TRANSIENT_STATUSES
            is_last_attempt = attempt >= _REPLICATION_RETRY_ATTEMPTS
            if not is_transient or is_last_attempt:
                raise error

            delay = min(
                _REPLICATION_RETRY_BASE_DELAY_SECONDS * attempt,
                _REPLICATION_RETRY_MAX_DELAY_SECONDS,
            )
            logger.info(
                "[graph_service] grant_backend_obo_consent: transient replication "
                "failure (status=%s code=%s) — retrying (attempt %s/%s, delay %ss)",
                error.status,
                error.code,
                attempt,
                _REPLICATION_RETRY_ATTEMPTS,
                delay,
            )
            await asyncio.sleep(delay)

        # Unreachable: the loop returns (2xx or already-exists) or raises on the last
        # attempt. Guard for static analysis / defensive completeness.
        raise GraphError(500, "replication_retry_exhausted")  # pragma: no cover

    async def grant_agent_obo_consent(self, agent_sp_id: str, mcp_sp_id: str) -> None:
        """Wire AGENT→MCP delegated consent so the agent's OBO to the MCP resolves
        (E7, research §2.4) — the agent-as-client analog of
        ``grant_backend_obo_consent``.

        POSTs an ``oauth2PermissionGrants`` for the ``Invoke`` scope with the AGENT
        SP as ``clientId`` (the middle-tier app for the agent→MCP hop) and the MCP
        SP as ``resourceId``. The ONLY difference from ``grant_backend_obo_consent``
        is that the client SP is PASSED IN (the agent SP, resolved upstream from the
        agent record's ``entra_sp_id``), not resolved here — so there is no
        backend-SP ``$filter`` lookup. A missing grant later surfaces as
        ``AADSTS65001`` → ``OboConfigError`` ("identity misconfigured") at OBO time.

        The grant POST references the MCP SP as ``resourceId``; if it was just
        created it may not have replicated yet (transient 404), so the same
        already-exists-swallow + bounded async replication-retry loop as
        ``grant_backend_obo_consent`` is used VERBATIM (already-exists is checked
        FIRST so it takes precedence over the 400-transient retry; 403/other fail
        fast; the final transient attempt raises).
        """
        grant = {
            "clientId": agent_sp_id,
            "consentType": "AllPrincipals",
            "resourceId": mcp_sp_id,
            "scope": "Invoke",
        }
        url = f"{self._graph_base}/oauth2PermissionGrants"
        for attempt in range(1, _REPLICATION_RETRY_ATTEMPTS + 1):
            token = await self._app_token()
            headers = {"Authorization": f"Bearer {token}"}
            response = await self._http.request("POST", url, json=grant, headers=headers)
            if 200 <= response.status_code < 300:
                return

            # Idempotent: swallow an already-exists / duplicate-grant error FIRST —
            # it takes precedence over the transient retry (an already-exists is a
            # 400 but a terminal success, not a replication race).
            code = self._graph_error_code(response)
            if self._is_already_exists(response, code):
                logger.info(
                    "[graph_service] grant_agent_obo_consent: grant already exists "
                    "(status=%s code=%s) — treating as success",
                    response.status_code,
                    code,
                )
                return

            # Otherwise the grant references the (possibly just-created) MCP SP,
            # which may not have replicated yet: retry the transient 400/404 shapes;
            # 403/other fail fast; the final transient attempt raises.
            error = self._graph_error(response)
            is_transient = response.status_code in _REPLICATION_TRANSIENT_STATUSES
            is_last_attempt = attempt >= _REPLICATION_RETRY_ATTEMPTS
            if not is_transient or is_last_attempt:
                raise error

            delay = min(
                _REPLICATION_RETRY_BASE_DELAY_SECONDS * attempt,
                _REPLICATION_RETRY_MAX_DELAY_SECONDS,
            )
            logger.info(
                "[graph_service] grant_agent_obo_consent: transient replication "
                "failure (status=%s code=%s) — retrying (attempt %s/%s, delay %ss)",
                error.status,
                error.code,
                attempt,
                _REPLICATION_RETRY_ATTEMPTS,
                delay,
            )
            await asyncio.sleep(delay)

        # Unreachable: the loop returns (2xx or already-exists) or raises on the last
        # attempt. Guard for static analysis / defensive completeness.
        raise GraphError(500, "replication_retry_exhausted")  # pragma: no cover

    async def revoke_agent_obo_consent(self, agent_sp_id: str, mcp_sp_id: str) -> None:
        """Delete the AGENT→MCP delegated consent (``oauth2PermissionGrant``) — the
        DELETE twin of ``grant_agent_obo_consent`` (E7 security-fix).

        Under our delegated/OBO design the MCP app is ``appRoleAssignmentRequired=false``
        (research §2.4/§2.5), so the agent→MCP admission gate is THIS consent grant, NOT
        the app-role assignment (the assignment is inert under OBO — only an app-only/M2M
        token consults it, a path we do not use). So a revoke that deletes only the
        assignment is cosmetic for the delegated path: the agent can still OBO-reach the
        MCP. Deleting this consent is what makes revoke a REAL kill switch — once it is
        gone, the agent's OBO to the MCP fails ``AADSTS65001``.

        Resolves the grant via the agent SP's ``oauth2PermissionGrants`` NAVIGATION
        property (``GET /servicePrincipals/{agent_sp}/oauth2PermissionGrants``) and then
        filters IN-MEMORY to ``resourceId == mcp_sp_id`` — DELETEing each match (normally
        exactly one; all matches are deleted defensively so no stale consent survives).

        ⚠️ DO NOT resolve this with a top-level ``GET /oauth2PermissionGrants?$filter=
        clientId eq '…' and resourceId eq '…'`` (the original implementation): Microsoft
        Graph's ``$filter`` on the ``oauth2PermissionGrants`` COLLECTION is unreliable and
        was observed (live, 2026-06-17) to return an EMPTY ``value`` even though the grant
        exists — so the revoke hit the "no match → no-op" branch and SILENTLY LEFT THE
        CONSENT IN PLACE, leaving the agent able to OBO-reach the MCP after a UI "revoke"
        (the app-role assignment was deleted, the consent was not — exactly the kill-switch
        hole this method exists to close). The navigation-property read is authoritative
        (verified live to return the grant the ``$filter`` missed).

        IDEMPOTENT — the DESIRED end state is "no agent→MCP consent", so:
          - an EMPTY ``$filter`` result (already gone / never existed) is a no-op success;
          - a 404 on the DELETE (a concurrent revoke removed it between the GET and the
            DELETE) is swallowed as success.
        A non-404 ``GraphError`` on the DELETE (e.g. 403 permissions) is RE-RAISED — the
        revoke genuinely failed and the operator must act on it (re-revoke is idempotent).

        SECURITY: the long-lived agent/MCP SPs are already replicated (this fires at
        revoke time, not against a just-created object), so no replication-retry is
        needed — the plain ``_graph_request`` path is correct. The ids are our own stored
        SP object ids (server-minted), but the OData ``$filter`` delimiter is guarded
        defensively the same way ``get_application_object_id`` guards it (a single-quote
        delimits a ``$filter`` string literal; mirror that idiom rather than
        hand-concatenating an unescaped id)."""
        # Defensive: the SP ids are our own stored GUIDs, but guard the path-segment
        # delimiter the same way the rest of the adapter does (fail closed before any
        # interpolation). agent_sp_id is interpolated into the request PATH; mcp_sp_id is
        # only ever compared in-memory below, but guard both for symmetry / defence in depth.
        if "'" in agent_sp_id or "'" in mcp_sp_id:
            raise GraphError(400, "invalid_sp_id_format")

        # Resolve via the agent SP's oauth2PermissionGrants NAVIGATION property, NOT a
        # top-level collection $filter (which Graph evaluates unreliably — see the docstring;
        # the $filter was observed live to return empty while the grant existed, silently
        # no-op'ing the revoke). This read is authoritative; we then match resourceId in-memory.
        list_resp = await self._graph_request(
            "GET", f"/servicePrincipals/{agent_sp_id}/oauth2PermissionGrants"
        )
        all_grants = list_resp.json().get("value", [])
        grants = [g for g in all_grants if g.get("resourceId") == mcp_sp_id]
        if not grants:
            # No matching consent — the desired end state already holds. No-op.
            logger.info(
                "[graph_service] revoke_agent_obo_consent: no matching grant — "
                "nothing to revoke (already the desired end state)"
            )
            return

        for grant in grants:
            grant_id = grant.get("id")
            if not grant_id:
                continue
            try:
                await self._graph_request(
                    "DELETE", f"/oauth2PermissionGrants/{grant_id}"
                )
            except GraphError as err:
                # A 404 means the grant is already gone (a concurrent revoke) — that IS
                # the desired end state, so swallow it. Any other error genuinely failed
                # the revoke and must surface.
                if err.status == 404:
                    logger.info(
                        "[graph_service] revoke_agent_obo_consent: grant already gone "
                        "(status=404 code=%s) — treating as success",
                        err.code,
                    )
                    continue
                raise

    async def delete_agent_app(
        self, *, entra_app_id: Optional[str], entra_sp_id: Optional[str]
    ) -> None:
        """Tear down the AGENT's Entra application registration — the DELETE twin of
        ``create_agent_app``. Deleting the application object CASCADES its service
        principal in Entra, so this is the single call a repo-teardown needs to leave
        no orphaned identity behind.

        ``entra_app_id`` is the stored appId/client GUID (as returned by
        ``create_agent_app``), NOT the directory object id — so it is first resolved to
        the ``/applications/{objId}`` object id via ``get_application_object_id`` and
        then ``DELETE /applications/{objId}`` is issued. If ``entra_app_id`` is falsy but
        ``entra_sp_id`` is set, fall back to ``DELETE /servicePrincipals/{entra_sp_id}``
        directly (the SP object id is stored server-side and needs no resolve).

        IDEMPOTENT — the DESIRED end state is "no agent app / no agent SP", so every
        already-gone shape is a no-op success (mirrors ``revoke_agent_obo_consent``):
          - both ids blank → nothing to tear down, return immediately (no Graph call);
          - the app id no longer resolves (``get_application_object_id`` raises 404 on an
            empty ``value`` list) → the app is already gone, swallow and return;
          - a 404 on the DELETE (a concurrent teardown removed it) → swallow and return.
        A non-404 ``GraphError`` on the DELETE (e.g. 403 permissions) is RE-RAISED — the
        teardown genuinely failed and the operator must act on it (re-delete is idempotent).

        SECURITY: never logs/returns the app token or a raw Graph body. ``get_application_
        object_id`` guards the OData ``$filter`` delimiter; ``_graph_error`` surfaces only
        status + Graph error code + a safe resource message (an ``/applications`` or
        ``/servicePrincipals`` error body carries no token or secret)."""
        if entra_app_id:
            # Resolve the stored client GUID → directory object id. A missing app
            # (empty $filter result) raises GraphError(404) — that IS the desired end
            # state for a teardown, so swallow it exactly like the DELETE 404 below.
            try:
                object_id = await self.get_application_object_id(entra_app_id)
            except GraphError as err:
                if err.status == 404:
                    logger.info(
                        "[graph_service] delete_agent_app: application already gone "
                        "(resolve status=404 code=%s) — treating as success",
                        err.code,
                    )
                    return
                raise
            await self._delete_swallow_404(
                f"/applications/{object_id}", "application"
            )
            return

        if entra_sp_id:
            # No app id to resolve — delete the SP directly (fallback).
            await self._delete_swallow_404(
                f"/servicePrincipals/{entra_sp_id}", "service principal"
            )
            return

        # Both ids blank — nothing to tear down. No-op success (no Graph call).
        logger.info(
            "[graph_service] delete_agent_app: no app id and no sp id — "
            "nothing to delete (already the desired end state)"
        )

    async def _delete_swallow_404(self, path: str, kind: str) -> None:
        """DELETE ``path``, swallowing a 404 as success (the desired end state for a
        teardown) and re-raising any other ``GraphError``. Shared by both delete_agent_app
        branches so the 404-swallow idiom is expressed once."""
        try:
            await self._graph_request("DELETE", path)
        except GraphError as err:
            if err.status == 404:
                logger.info(
                    "[graph_service] delete_agent_app: %s already gone "
                    "(status=404 code=%s) — treating as success",
                    kind,
                    err.code,
                )
                return
            raise

    @staticmethod
    def _is_already_exists(response: httpx.Response, code: str) -> bool:
        """True if a failed oauth2PermissionGrants POST is an already-exists error."""
        if response.status_code not in (400, 409):
            return False
        try:
            message = (response.json().get("error", {}) or {}).get("message", "") or ""
        except (ValueError, AttributeError):
            message = ""
        message_l = message.lower()
        return "already exists" in message_l or "permission entry already" in message_l

    # --- credential-provider secret (E7, Tier-2 — research §2.4(d)/§3.2) ---

    async def get_application_object_id(self, client_id: str) -> str:
        """Resolve the application's DIRECTORY OBJECT id from its appId/clientId GUID.

        ``GET /applications?$filter=appId eq '{client_id}'&$select=id`` — the backend
        already holds ``Application.Read.All`` (E6 Decision 4). Returns ``value[0]["id"]``
        (the ``/applications/{objId}`` directory object id, distinct from the appId GUID).

        Raises ``GraphError`` if the app is not found (empty ``value``). Fails CLOSED: a
        wrong/missing id must never proceed to ``add_agent_password`` (which would mint a
        secret on the wrong app or 404 in Graph). The filter is exact-match on the appId
        GUID so there is no injection surface; as a defensive measure the client_id is
        checked to not contain a single-quote character (the OData filter delimiter) before
        being interpolated — mirroring the ``search_principals`` double-quote strip idiom.

        ``$select=id`` limits the response body to the one field we need (no sensitive
        fields returned; the body is safe to read and discard via ``_graph_error`` on a
        non-2xx).
        """
        # Defensive: client_id is our own stored GUID (server-minted), but guard the OData
        # filter delimiter to match the repo's defensive idiom (search_principals strips
        # double-quotes; here we guard single-quotes which delimit $filter string literals).
        if "'" in client_id:
            raise GraphError(400, "invalid_client_id_format")

        response = await self._graph_request(
            "GET",
            "/applications",
            params={"$filter": f"appId eq '{client_id}'", "$select": "id"},
        )
        apps = response.json().get("value", [])
        if not apps:
            raise GraphError(
                404,
                "Request_ResourceNotFound",
                f"Application with appId '{client_id}' not found",
            )
        return apps[0]["id"]

    async def add_agent_password(
        self, agent_app_object_id: str, display_name: str = "agp-obo"
    ) -> str:
        """Mint a client secret for the AGENT app reg via Graph ``addPassword``.

        ``POST /applications/{objId}/addPassword`` → returns the response's
        ``secretText`` (a fresh client secret for the AGENT app — the OBO middle
        tier). Called ONCE per agent at credential-provider setup
        (``agent_credential_service.ensure_agent_credential_provider``); the secret
        goes STRAIGHT into AgentCore Identity's Token Vault (the ``MicrosoftOauth2``
        credential provider's ``clientSecret``) and is NEVER persisted, printed, or
        logged by us — only the provider NAME (a non-secret) lands on the agent record.

        SECURITY: the returned ``secretText`` is the single most sensitive value this
        adapter handles. This method NEVER logs it, places it in an exception message,
        or echoes it anywhere — it is returned to exactly one caller, which vaults it.
        On a non-2xx, ``_graph_error`` surfaces only status + Graph error code + a safe
        resource ``message`` (an ``/applications`` error body carries NO secret — Graph
        never echoes the request body or the just-minted secret in an error).

        ``agent_app_object_id`` is the application's DIRECTORY OBJECT id (the
        ``/applications/{objId}`` id, i.e. ``application.id``), NOT the appId/clientId
        GUID. The caller resolves/supplies it.
        """
        body = {
            "passwordCredential": {"displayName": display_name},
        }
        response = await self._graph_request(
            "POST",
            f"/applications/{agent_app_object_id}/addPassword",
            json_body=body,
        )
        secret_text = response.json().get("secretText")
        if not secret_text:
            # Defensive: a 2xx that somehow lacks secretText. NEVER include any body
            # content in the error (status + code only) — mirror the no-token guard.
            raise GraphError(response.status_code, "no_secret_text_in_response")
        return secret_text

    # --- grants (research §2.3) -------------------------------------------

    async def list_assignments(self, agent_sp_id: str) -> list[dict]:
        """§2.3: the agent SP's app-role assignments (principal name/type inline)."""
        response = await self._graph_request(
            "GET", f"/servicePrincipals/{agent_sp_id}/appRoleAssignedTo"
        )
        return response.json().get("value", [])

    async def list_member_group_ids(self, user_oid: str) -> list[str]:
        """Return the Entra group object-ids the user is a member of (transitive).

        E9/F1 — the fallback for resolving the caller's group memberships when the
        token's ``groups`` claim is ABSENT (Entra omits it under "groups overage" for
        users in very many groups). Reads
        ``GET /users/{oid}/transitiveMemberOf/microsoft.graph.group?$select=id``,
        following ``@odata.nextLink`` to page through all results, and collects each
        object's ``id``. No new Graph permission (the backend already holds
        ``Directory.Read.All``/``GroupMember.Read.All``-class scopes used by the
        grants reads).

        BEST-EFFORT FAIL-CLOSED: on a Graph error this RAISES (via ``_graph_request`` →
        ``GraphError``); it does NOT silently return [] — the CALLER decides whether to
        fail closed (the subscribe guard) or degrade to [] (the read-only listing
        endpoint). Returns [] only when the user genuinely has no group memberships.
        """
        group_ids: list[str] = []
        # First page goes through the standard params; subsequent pages follow the
        # absolute @odata.nextLink URL Graph returns.
        path = f"/users/{user_oid}/transitiveMemberOf/microsoft.graph.group"
        params: Optional[dict] = {"$select": "id"}
        next_url: Optional[str] = None

        while True:
            if next_url is not None:
                # nextLink is an absolute URL already carrying the page cursor.
                token = await self._app_token()
                headers = {"Authorization": f"Bearer {token}"}
                try:
                    response = await self._http.request("GET", next_url, headers=headers)
                except httpx.HTTPError as exc:
                    # Same transport-error wrap as `_graph_request` — a timeout mid-page
                    # must surface as GraphError, not a raw httpx exception.
                    raise GraphError(502, "transport_error") from exc
                if not (200 <= response.status_code < 300):
                    raise self._graph_error(response)
            else:
                response = await self._graph_request("GET", path, params=params)

            body = response.json()
            for obj in body.get("value", []):
                gid = obj.get("id")
                if gid:
                    group_ids.append(gid)

            next_url = body.get("@odata.nextLink")
            if not next_url:
                break

        return group_ids

    async def get_principal(self, oid: str, kind: str) -> dict:
        """E11 — read-only Entra by-oid lookup for a USER or GROUP node detail.

        ``kind="user"``  → ``GET /users/{oid}?$select=displayName,userPrincipalName,
        mail,jobTitle``; ``kind="group"`` → ``GET /groups/{oid}?$select=displayName,
        mail``. Returns the RAW Graph object dict (``response.json()``) verbatim — no
        shaping here; the service layer maps it to ``PrincipalDetail``. Optional
        fields Graph may omit (``mail``/``jobTitle``) are simply absent from the dict.

        No new Graph permission (the backend already holds the ``User.Read.All`` /
        ``Directory.Read.All``-class scopes used by the existing reads, e.g.
        ``list_member_group_ids`` / ``search_principals``).

        RAISES ``GraphError`` on a non-2xx (via ``_graph_request`` → ``_graph_error``)
        — the caller maps 404 → 404 and any other error → 502. The raw body is never
        propagated outward. A ``kind`` other than ``"user"``/``"group"`` raises
        ``ValueError`` (defensive — the route layer validates, but so does this).
        """
        if kind == "user":
            path = f"/users/{oid}"
            select = "displayName,userPrincipalName,mail,jobTitle"
        elif kind == "group":
            path = f"/groups/{oid}"
            select = "displayName,mail"
        else:
            raise ValueError(f"unsupported principal kind: {kind!r}")

        response = await self._graph_request("GET", path, params={"$select": select})
        return response.json()

    async def list_agent_mcp_grants(self, agent_sp_id: str) -> list[dict]:
        """E7, research §2.3 — the PRINCIPAL-side read: what this agent SP is
        assigned TO (its outbound MCP app-role grants).

        ``GET /servicePrincipals/{agent_sp_id}/appRoleAssignments`` — a DISTINCT
        endpoint from ``appRoleAssignedTo`` (which is the resource-side "who is
        assigned to ME"). Returns each grant with ``resourceId`` /
        ``resourceDisplayName`` / ``appRoleId`` / ``principalId`` INLINE (no extra
        resolve). No new Graph permission (same ``Application.Read.All`` /
        ``Directory.Read.All`` the backend already holds). The route layer filters
        client-side to known-MCP ``resourceId``s — that is T-ROUTES' job, not this
        method's.
        """
        response = await self._graph_request(
            "GET", f"/servicePrincipals/{agent_sp_id}/appRoleAssignments"
        )
        return response.json().get("value", [])

    async def assign_app_role(
        self, agent_sp_id: str, principal_id: str, app_role_id: str
    ) -> dict:
        """§2.3: grant a user/group an app role on the agent SP → the new assignment."""
        body = {
            "principalId": principal_id,
            "resourceId": agent_sp_id,
            "appRoleId": app_role_id,
        }
        response = await self._graph_request(
            "POST",
            f"/servicePrincipals/{agent_sp_id}/appRoleAssignedTo",
            json_body=body,
        )
        return response.json()

    async def revoke_app_role(self, agent_sp_id: str, assignment_id: str) -> None:
        """§2.3: delete an app-role assignment by id."""
        await self._graph_request(
            "DELETE",
            f"/servicePrincipals/{agent_sp_id}/appRoleAssignedTo/{assignment_id}",
        )

    async def resolve_platform_sp(
        self, platform_app_client_id: str
    ) -> tuple[str, dict[str, str]]:
        """E16: resolve the platform app's SP object id + its appRole value→id map.

        ``GET /servicePrincipals?$filter=appId eq '{client_id}'&$select=id,appRoles``.
        Returns ``(sp_object_id, {appRole.value: appRoleId})`` for ENABLED appRoles
        with a truthy id + value. Raises ``GraphError(404, ...)`` if no SP matches.

        Guards the ``$filter`` single-quote delimiter (``GraphError(400, ...)``) like
        ``get_application_object_id`` before interpolating the client id.

        Caching: only a NON-EMPTY role map is cached per client id (the SP + its roles
        are stable once defined). An empty result (SP exists but its Platform.* roles
        are not yet defined/enabled — the normal first-run order: create app reg →
        define roles) is NOT cached, so a later call re-fetches and picks up the roles
        once they exist, rather than serving a stale empty map until process restart.
        """
        cached = self._platform_sp_cache.get(platform_app_client_id)
        if cached is not None:
            return cached

        # Defensive: guard the OData $filter single-quote delimiter before interpolating
        # the client id, mirroring get_application_object_id's idiom.
        if "'" in platform_app_client_id:
            raise GraphError(400, "invalid_client_id_format")

        response = await self._graph_request(
            "GET",
            "/servicePrincipals",
            params={
                "$filter": f"appId eq '{platform_app_client_id}'",
                "$select": "id,appRoles",
            },
        )
        values = response.json().get("value", [])
        if not values:
            raise GraphError(404, "platform_sp_not_found")

        sp = values[0]
        sp_id = sp.get("id")
        if not sp_id:
            raise GraphError(404, "platform_sp_not_found")

        role_id_by_value: dict[str, str] = {}
        for role in sp.get("appRoles", []):
            if role.get("isEnabled", True) and role.get("id") and role.get("value"):
                role_id_by_value[role["value"]] = role["id"]

        resolved = (sp_id, role_id_by_value)
        # Only cache a populated map; an empty result re-fetches next call so newly
        # enabled Platform.* roles are picked up without a restart.
        if role_id_by_value:
            self._platform_sp_cache[platform_app_client_id] = resolved
        return resolved

    async def search_principals(
        self, query: str, kinds: tuple[str, ...] = ("user", "group")
    ) -> list[dict]:
        """§2.3: ``$search`` users and/or groups (ConsistencyLevel: eventual) → merged hits.

        Each hit is ``{id, displayName, type:"user"|"group", mail?}``.
        """
        headers = {"ConsistencyLevel": "eventual"}
        results: list[dict] = []
        # ``query`` is raw end-user input from the picker route; a double-quote in
        # it would close the $search OData clause early and malform the request.
        # Strip double-quotes defensively (the route also guards min-length).
        safe_query = query.replace('"', "")

        if "user" in kinds:
            user_resp = await self._graph_request(
                "GET",
                "/users",
                params={"$search": f'"displayName:{safe_query}"', "$count": "true"},
                extra_headers=headers,
            )
            for u in user_resp.json().get("value", []):
                results.append(
                    {
                        "id": u.get("id"),
                        "displayName": u.get("displayName"),
                        "type": "user",
                        "mail": u.get("mail"),
                    }
                )

        if "group" in kinds:
            group_resp = await self._graph_request(
                "GET",
                "/groups",
                params={"$search": f'"displayName:{safe_query}"', "$count": "true"},
                extra_headers=headers,
            )
            for g in group_resp.json().get("value", []):
                results.append(
                    {
                        "id": g.get("id"),
                        "displayName": g.get("displayName"),
                        "type": "group",
                        "mail": g.get("mail"),
                    }
                )

        return results

    async def resolve_user_by_email(self, address: str) -> Optional[dict]:
        """Resolve an EMAIL ADDRESS to at most ONE user via an EXACT ``$filter``.

        ``search_principals`` is a fuzzy ``$search`` on ``displayName`` only, so it
        cannot resolve an address at all — it is the grants PICKER. This is the exact
        counterpart used where a stored address must be turned back into a principal
        (E27/T6's owner backfill):

            GET /users?$filter=mail eq '<addr>' or userPrincipalName eq '<addr>'

        Returns the SAME hit shape ``search_principals`` produces
        (``{id, displayName, type:"user", mail}``) so callers share one matcher, or
        ``None`` when no user (or more than one user) holds the address — an ambiguous
        address is never silently narrowed to the first hit.

        The address is a stored value, but it is still interpolated into an OData
        string literal, so a single-quote is DOUBLED (``'`` → ``''``) — OData's own
        escape — instead of being stripped or rejected: a quote is legal in the local
        part of an address, and doubling keeps the clause well-formed and injection-free
        (same care as ``search_principals``' double-quote strip and
        ``get_application_object_id``' single-quote guard).

        Raises ``GraphError(status, code)`` on a non-2xx (via ``_graph_request``); the
        caller decides whether to degrade. Never logs a token (nothing is logged here).
        """
        wanted = (address or "").strip()
        if not wanted:
            return None
        escaped = wanted.replace("'", "''")
        response = await self._graph_request(
            "GET",
            "/users",
            params={
                "$filter": (
                    f"mail eq '{escaped}' or userPrincipalName eq '{escaped}'"
                ),
                "$select": "id,displayName,mail,userPrincipalName",
            },
            extra_headers={"ConsistencyLevel": "eventual"},
        )
        users = response.json().get("value", [])
        if len(users) != 1:
            return None
        user = users[0]
        return {
            "id": user.get("id"),
            "displayName": user.get("displayName"),
            "type": "user",
            "mail": user.get("mail"),
            "userPrincipalName": user.get("userPrincipalName"),
        }

    # --- OBO (research §2.1) ----------------------------------------------

    async def obo_exchange(self, user_token: str, agent_audience: str) -> str:
        """§2.1: exchange the user's backend token for an agent-audience token.

        Returns the agent-audience ``access_token`` on 200. On a non-200,
        ``_token_error`` maps the Entra ``error_codes`` (CRITIQUE-FIX-D):
        50105→``NotAssignedError``, 65001/500011→``OboConfigError``, else
        ``GraphError`` (status + code only — never the body, the assertion, or any
        token).
        """
        form = {
            "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
            "client_id": self._backend_client_id,
            "client_secret": self._secret(),
            "assertion": user_token,
            "scope": f"{agent_audience}/.default",
            "requested_token_use": "on_behalf_of",
        }
        response = await self._http.post(self._token_endpoint, data=form)
        if response.status_code == 200:
            token = response.json().get("access_token")
            if not token:
                raise GraphError(response.status_code, "no_access_token_in_response")
            return token
        raise self._token_error(response)
