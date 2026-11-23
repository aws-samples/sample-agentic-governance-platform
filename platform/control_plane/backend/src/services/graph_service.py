"""Microsoft Entra, Microsoft Graph and On-Behalf-Of adapter.

``GraphService`` owns every Entra, Graph and OBO HTTP call the platform makes and nothing else: no
boto3, no FastAPI, no DynamoDB, only ``httpx.AsyncClient`` and the backend-secret loader, so it is
unit-tested against ``httpx.MockTransport``. The provisioning orchestrators call ``create_agent_app``,
``set_assignment_required`` and ``grant_backend_obo_consent``; the grant, invoke and users-admin
surfaces call ``list_assignments``, ``assign_app_role``, ``revoke_app_role``, ``search_principals``
and ``obo_exchange``. App registration and the platform role-holder sequences are module functions over
the service in :mod:`services.graph_app_registration` and :mod:`services.graph_platform_users`, and
``create_agent_app`` and ``create_mcp_app`` delegate to the first. ``graph_service_from_settings()`` is
the one constructor the routes call: under ``GRAPH_IN_MEMORY`` it builds the service over the in-memory
directory of :mod:`services.graph_memory` with that directory's fixed tenant and backend app, otherwise
over the configured Entra tenant and backend app.

The backend client secret, the inbound user token (the OBO assertion) and the OBO'd access token are
never logged, printed or placed in an exception message. :class:`services.graph_errors.GraphError`
carries the HTTP status and the Graph or Entra error code, plus a message only from a resource
endpoint's error body, which never echoes a token; the Entra ``/token`` path stays status and code
only.

Decision: docs/adr/backend-graph-in-memory-behind-the-adapter.md
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
import weakref
from typing import Callable, Literal, Optional, TypedDict

import httpx

from core.config import settings
from core.secrets_loader import load_entra_backend_client_secret
from services import graph_app_registration
from services.graph_errors import GraphError, NotAssignedError, OboConfigError

logger = logging.getLogger(__name__)

_GRAPH_DEFAULT_SCOPE = "https://graph.microsoft.com/.default"

_TOKEN_EXPIRY_SKEW_SECONDS = 60.0

_REPLICATION_RETRY_ATTEMPTS = 11
_REPLICATION_RETRY_BASE_DELAY_SECONDS = 1.0
_REPLICATION_RETRY_MAX_DELAY_SECONDS = 8.0
_REPLICATION_TRANSIENT_STATUSES = frozenset({400, 404})

_ANONYMOUS_CALLER = "the caller"

PrincipalHitType = Literal["user", "group"]


class PrincipalSearchHit(TypedDict):
    id: str
    displayName: Optional[str]
    type: PrincipalHitType
    mail: Optional[str]


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
        self._audience_prefix = audience_prefix
        self._client_secret_loader = client_secret_loader
        self._owns_client = http_client is None
        self._injected_client = http_client
        self._clients_by_loop: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, httpx.AsyncClient]" = (
            weakref.WeakKeyDictionary()
        )
        self._clients_lock = threading.Lock()
        self._token_endpoint = f"{self._login_base}/{tenant_id}/oauth2/v2.0/token"

        self._app_token_value: Optional[str] = None
        self._app_token_expiry: float = 0.0

        self._platform_sp_cache: dict[str, tuple[str, dict[str, str]]] = {}

    @property
    def _http(self) -> httpx.AsyncClient:
        """The httpx client safe to use on the running event loop.

        An injected client is returned unchanged: the caller owns its lifecycle and loop affinity.
        A self-created client is kept per event loop in ``_clients_by_loop`` and created lazily for
        the running loop, because a default ``httpx.AsyncClient`` binds its transport to the loop of
        its first request while the sync-driven teardown and provisioning paths each run
        ``asyncio.run`` on a fresh loop; reusing one client across loops hits a closed transport.
        The weak map lets a dead loop's client be collected, and the lock guards the get-or-create
        against concurrent ``asyncio.run`` calls from FastAPI's threadpool.
        """
        if self._injected_client is not None:
            return self._injected_client
        running = asyncio.get_running_loop()
        with self._clients_lock:
            client = self._clients_by_loop.get(running)
            if client is None:
                client = httpx.AsyncClient()
                self._clients_by_loop[running] = client
            return client

    async def aclose(self) -> None:
        """Close every self-created httpx client this instance still holds.

        When an ``http_client`` was injected, the caller owns its lifecycle and this is a no-op;
        otherwise every per-loop client is closed. The map is not cleared, so a later read of
        ``_http`` on a still-live loop returns the same, now closed, client rather than minting a
        fresh one. A client that fails to close (its loop already gone, a transport error) is
        skipped and logged at DEBUG: a shutdown hook never raises.
        """
        if not self._owns_client:
            return
        with self._clients_lock:
            clients = list(self._clients_by_loop.values())
        for client in clients:
            try:
                await client.aclose()
            except Exception:
                logger.debug("[graph_service] aclose: a client could not be closed; skipped", exc_info=True)

    def _secret(self) -> str:
        return self._client_secret_loader()

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
            raise GraphError(502, "transport_error") from exc
        if response.status_code != 200:
            raise self._token_error(response)

        payload = response.json()
        token = payload.get("access_token")
        if not token:
            raise GraphError(response.status_code, "no_access_token_in_response")
        expires_in = float(payload.get("expires_in", 3600))
        self._app_token_value = token
        self._app_token_expiry = time.monotonic() + max(0.0, expires_in - _TOKEN_EXPIRY_SKEW_SECONDS)
        return token

    @staticmethod
    def _token_error(response: httpx.Response, caller: str = _ANONYMOUS_CALLER) -> Exception:
        """Map a non-200 Entra ``/token`` response to a domain error by its ``error_codes``.

        50105 is ``NotAssignedError`` naming ``caller``; 65001 and 500011 are ``OboConfigError``;
        anything else is a ``GraphError`` carrying status and code only. The codes are compared as
        strings so a proxy that re-serializes them as strings keeps the mapping. The response body,
        the assertion and any token never leave this function.
        """
        status = response.status_code
        codes: set[str] = set()
        error_slug = ""
        try:
            body = response.json()
            codes = {str(c) for c in (body.get("error_codes") or [])}
            error_slug = body.get("error") or ""
        except (ValueError, AttributeError):
            pass

        if "50105" in codes:
            return NotAssignedError(f"{caller} is not assigned to this agent")
        if "65001" in codes or "500011" in codes:
            return OboConfigError("OBO failed for a configuration reason (AADSTS65001/500011)")
        code = error_slug or (next(iter(codes)) if codes else "unknown")
        return GraphError(status, code)

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
            response = await self._http.request(method, url, params=params, json=json_body, headers=headers)
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

    async def create_agent_app(self, agent_id: str, display_name: str) -> dict:
        return await graph_app_registration.create_agent_app(self, agent_id, display_name)

    async def create_mcp_app(self, mcp_id: str, display_name: str) -> dict:
        return await graph_app_registration.create_mcp_app(self, mcp_id, display_name)

    async def _request_with_replication_retry(
        self,
        method: str,
        path: str,
        *,
        json_body: Optional[dict] = None,
        params: Optional[dict] = None,
        label: str = "",
    ) -> httpx.Response:
        """Issue a Graph resource request, retrying the transient replication-lag failures.

        Entra answers ``POST /applications`` with the new object's id before the object has
        replicated across the directory, so a call that references it (the SP create, the PATCH of
        the new SP, the consent grant naming the new SP as ``resourceId``) answers 400
        ``Request_BadRequest`` or 404 ``Request_ResourceNotFound`` for up to about a minute. Only
        those two statuses are retried; a 403 or any other status is raised at once. The backoff is
        linear in the attempt number, capped at ``_REPLICATION_RETRY_MAX_DELAY_SECONDS``, over
        ``_REPLICATION_RETRY_ATTEMPTS`` attempts; the last transient failure raises its
        ``GraphError``. The sleep is ``asyncio.sleep`` because provisioning runs on the uvicorn event
        loop. Resource endpoints only: errors go through ``_graph_error``, never ``_token_error``.
        """
        url = f"{self._graph_base}{path}"
        for attempt in range(1, _REPLICATION_RETRY_ATTEMPTS + 1):
            token = await self._app_token()
            headers = {"Authorization": f"Bearer {token}"}
            response = await self._http.request(method, url, json=json_body, params=params, headers=headers)
            if 200 <= response.status_code < 300:
                return response

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

        raise GraphError(500, "replication_retry_exhausted")

    async def set_assignment_required(self, sp_id: str, required: bool = True) -> None:
        """Set the SP's ``appRoleAssignmentRequired`` flag, through the replication retry.

        Agent provisioning keeps the default ``True``: OBO then honours the per-user and per-group
        assignments. MCP provisioning passes ``False``: in a delegated flow Entra enforces the
        resource app's flag against the user, not the calling agent SP, and the design never assigns
        users to MCP servers (the user is granted the agent, the agent is granted the MCP through a
        consent grant), so ``True`` on an MCP SP would refuse every delegated user with AADSTS50105.
        The agent-to-MCP gate is the ``oauth2PermissionGrant`` of ``grant_agent_obo_consent``.
        Idempotent: re-sending the same value converges.
        """
        await self._request_with_replication_retry(
            "PATCH",
            f"/servicePrincipals/{sp_id}",
            json_body={"appRoleAssignmentRequired": required},
            label="set_assignment_required",
        )

    async def grant_backend_obo_consent(self, agent_sp_id: str) -> None:
        """Grant the backend app delegated consent on the agent app so OBO can resolve.

        Resolves the backend SP by its appId on the plain request path (the backend app is long
        replicated), then posts an ``oauth2PermissionGrants`` for the ``Invoke`` scope naming the
        just-created agent SP as ``resourceId``, with the replication retry inlined so that an
        already-exists answer (itself a 400) is treated as success before the transient-retry
        decision on every attempt. Idempotent.
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

            code = self._graph_error_code(response)
            if self._is_already_exists(response, code):
                logger.info(
                    "[graph_service] grant_backend_obo_consent: grant already exists "
                    "(status=%s code=%s) — treating as success",
                    response.status_code,
                    code,
                )
                return

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

        raise GraphError(500, "replication_retry_exhausted")

    async def grant_agent_obo_consent(self, agent_sp_id: str, mcp_sp_id: str) -> None:
        """Grant the agent app delegated consent on the MCP app so the agent's OBO to it resolves.

        The agent-as-client twin of ``grant_backend_obo_consent``: the agent SP is passed in as
        ``clientId`` instead of resolved, the MCP SP is the ``resourceId``, and the same
        already-exists-then-retry loop applies. A missing grant surfaces later as AADSTS65001, an
        ``OboConfigError``, at OBO time.
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

            code = self._graph_error_code(response)
            if self._is_already_exists(response, code):
                logger.info(
                    "[graph_service] grant_agent_obo_consent: grant already exists "
                    "(status=%s code=%s) — treating as success",
                    response.status_code,
                    code,
                )
                return

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

        raise GraphError(500, "replication_retry_exhausted")

    async def revoke_agent_obo_consent(self, agent_sp_id: str, mcp_sp_id: str) -> None:
        """Delete the agent app's delegated consent on the MCP app, the revoke that ends its OBO.

        The MCP app runs with ``appRoleAssignmentRequired=false``, so this consent grant, not the
        app-role assignment, is the agent-to-MCP admission gate; once it is gone the agent's OBO to
        the MCP fails with AADSTS65001. The grant is read through the agent SP's
        ``oauth2PermissionGrants`` navigation property and matched on ``resourceId`` in memory,
        never through a ``$filter`` on the top-level collection: Graph evaluates that filter
        unreliably and was seen live to answer an empty ``value`` for a grant that existed, which
        left the consent in place after a revoke. Every match is deleted. Idempotent: no match and a
        404 on the delete are the desired end state; any other ``GraphError`` is re-raised. The
        single-quote guard mirrors ``get_application_object_id``.
        """
        if "'" in agent_sp_id or "'" in mcp_sp_id:
            raise GraphError(400, "invalid_sp_id_format")

        list_resp = await self._graph_request(
            "GET", f"/servicePrincipals/{agent_sp_id}/oauth2PermissionGrants"
        )
        all_grants = list_resp.json().get("value", [])
        grants = [g for g in all_grants if g.get("resourceId") == mcp_sp_id]
        if not grants:
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
                await self._graph_request("DELETE", f"/oauth2PermissionGrants/{grant_id}")
            except GraphError as err:
                if err.status == 404:
                    logger.info(
                        "[graph_service] revoke_agent_obo_consent: grant already gone "
                        "(status=404 code=%s) — treating as success",
                        err.code,
                    )
                    continue
                raise

    async def delete_agent_app(self, *, entra_app_id: Optional[str], entra_sp_id: Optional[str]) -> None:
        """Tear down the agent's Entra application registration; deleting the app cascades its SP.

        ``entra_app_id`` is the stored appId GUID, so it is resolved to the directory object id
        through ``get_application_object_id`` before ``DELETE /applications/{objId}``; with no app id
        but an SP id, ``DELETE /servicePrincipals/{sp}`` is issued directly. Idempotent: both ids
        blank, an app id that no longer resolves, or a 404 on the delete are the desired end state;
        any other ``GraphError`` is re-raised.
        """
        if entra_app_id:
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
            await self._delete_swallow_404(f"/applications/{object_id}", "application")
            return

        if entra_sp_id:
            await self._delete_swallow_404(f"/servicePrincipals/{entra_sp_id}", "service principal")
            return

        logger.info(
            "[graph_service] delete_agent_app: no app id and no sp id — "
            "nothing to delete (already the desired end state)"
        )

    async def _delete_swallow_404(self, path: str, kind: str) -> None:
        """``DELETE path``; a 404 is a teardown's desired end state, any other ``GraphError`` re-raises."""
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

    async def get_application_object_id(self, client_id: str) -> str:
        """Resolve the application's directory object id from its appId GUID.

        ``/applications?$filter=appId eq '{client_id}'&$select=id``. An empty ``value`` raises
        ``GraphError(404)`` so a missing app never proceeds to ``add_agent_password``; a single
        quote in the id, the OData string delimiter, raises ``GraphError(400)`` before any request.
        """
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

    async def add_agent_password(self, agent_app_object_id: str, display_name: str = "agp-obo") -> str:
        """Mint a client secret for the agent app through ``POST /applications/{objId}/addPassword``.

        Returns the ``secretText``, the most sensitive value this adapter handles: it is returned to
        the one caller that vaults it in AgentCore Identity and is never logged, persisted or placed
        in an exception message; a non-2xx surfaces only status, code and the resource message,
        which never echoes the request body or the minted secret. ``agent_app_object_id`` is the
        directory object id, not the appId GUID.
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
            raise GraphError(response.status_code, "no_secret_text_in_response")
        return secret_text

    async def list_assignments(self, agent_sp_id: str) -> list[dict]:
        """The agent SP's app-role assignments, principal name and type inline."""
        response = await self._graph_request("GET", f"/servicePrincipals/{agent_sp_id}/appRoleAssignedTo")
        return response.json().get("value", [])

    async def list_member_group_ids(self, user_oid: str) -> list[str]:
        """The Entra group object ids the user is a transitive member of.

        The source of a caller's group memberships when the token carries no ``groups`` claim, which
        Entra omits under groups overage. Reads ``/users/{oid}/transitiveMemberOf/microsoft.graph.group``
        and follows ``@odata.nextLink`` to the end. A Graph failure raises ``GraphError``; the
        caller decides whether to fail closed or degrade to no groups. An empty list means the user
        has no memberships.
        """
        group_ids: list[str] = []
        path = f"/users/{user_oid}/transitiveMemberOf/microsoft.graph.group"
        params: Optional[dict] = {"$select": "id"}
        next_url: Optional[str] = None

        while True:
            if next_url is not None:
                token = await self._app_token()
                headers = {"Authorization": f"Bearer {token}"}
                try:
                    response = await self._http.request("GET", next_url, headers=headers)
                except httpx.HTTPError as exc:
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
        """The raw Graph object of one user or group, read by object id.

        ``kind="user"`` selects ``displayName,userPrincipalName,mail,jobTitle`` from ``/users/{oid}``;
        ``kind="group"`` selects ``displayName,mail`` from ``/groups/{oid}``. The dict is returned
        verbatim, optional fields absent when Graph omits them; the caller shapes it. A non-2xx
        raises ``GraphError``; any other ``kind`` raises ``ValueError`` before any request.
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
        """The app roles this agent SP holds on other apps: its outbound MCP grants.

        ``/servicePrincipals/{id}/appRoleAssignments`` is the principal-side read, distinct from
        ``appRoleAssignedTo``, the resource-side one. Each grant carries ``resourceId``,
        ``resourceDisplayName``, ``appRoleId`` and ``principalId`` inline; the caller filters to the
        MCP servers it knows.
        """
        response = await self._graph_request("GET", f"/servicePrincipals/{agent_sp_id}/appRoleAssignments")
        return response.json().get("value", [])

    async def assign_app_role(self, agent_sp_id: str, principal_id: str, app_role_id: str) -> dict:
        """Assign a principal an app role on the SP; returns the new assignment."""
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
        """Delete an app-role assignment on the SP by id."""
        await self._graph_request(
            "DELETE",
            f"/servicePrincipals/{agent_sp_id}/appRoleAssignedTo/{assignment_id}",
        )

    async def resolve_platform_sp(self, platform_app_client_id: str) -> tuple[str, dict[str, str]]:
        """The platform app's SP object id and its enabled appRole value-to-id map.

        Reads ``/servicePrincipals?$filter=appId eq '{client_id}'&$select=id,appRoles``; no match is
        ``GraphError(404)``, a single quote in the id is ``GraphError(400)`` before any request.
        Only a non-empty role map is cached per client id: an app whose Platform roles are not yet
        defined is re-read on the next call instead of serving an empty map until restart.
        """
        cached = self._platform_sp_cache.get(platform_app_client_id)
        if cached is not None:
            return cached

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
        if role_id_by_value:
            self._platform_sp_cache[platform_app_client_id] = resolved
        return resolved

    async def search_principals(
        self, query: str, kinds: tuple[str, ...] = ("user", "group")
    ) -> list[PrincipalSearchHit]:
        """``$search`` users and groups by display name (``ConsistencyLevel: eventual``), merged.

        A double quote in the query would close the ``$search`` clause early, so it is stripped
        before interpolation.
        """
        headers = {"ConsistencyLevel": "eventual"}
        results: list[PrincipalSearchHit] = []
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
                        "id": u["id"],
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
                        "id": g["id"],
                        "displayName": g.get("displayName"),
                        "type": "group",
                        "mail": g.get("mail"),
                    }
                )

        return results

    async def resolve_user_by_email(self, address: str) -> Optional[dict]:
        """Resolve an email address to at most one user through an exact ``$filter``.

        ``mail eq '<addr>' or userPrincipalName eq '<addr>'``, the exact counterpart of the fuzzy
        ``search_principals``. Returns the same hit shape plus ``userPrincipalName``, or ``None``
        when no user or more than one holds the address: an ambiguous address is never narrowed to
        the first hit. A single quote in the address is doubled, OData's own escape, because a
        quote is legal in the local part. A non-2xx raises ``GraphError``.
        """
        wanted = (address or "").strip()
        if not wanted:
            return None
        escaped = wanted.replace("'", "''")
        response = await self._graph_request(
            "GET",
            "/users",
            params={
                "$filter": (f"mail eq '{escaped}' or userPrincipalName eq '{escaped}'"),
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

    async def obo_exchange(
        self, user_token: str, agent_audience: str, *, caller: str = _ANONYMOUS_CALLER
    ) -> str:
        """Exchange the user's backend token for an agent-audience token (On-Behalf-Of).

        Returns the ``access_token`` on 200. A non-200 goes through ``_token_error``: 50105 is
        ``NotAssignedError`` whose message names ``caller`` (the principal's email or oid as the route
        shows it), 65001 and 500011 are ``OboConfigError``, anything else a ``GraphError`` carrying
        status and code only.
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
        raise self._token_error(response, caller)


def graph_service_from_settings() -> GraphService:
    if settings.GRAPH_IN_MEMORY:
        from services.graph_memory import (
            LOCAL_BACKEND_APP_ID,
            LOCAL_CLIENT_CREDENTIAL,
            LOCAL_TENANT_ID,
            memory_graph_client,
        )

        return GraphService(
            tenant_id=LOCAL_TENANT_ID,
            backend_client_id=LOCAL_BACKEND_APP_ID,
            login_base=settings.ENTRA_LOGIN_BASE,
            graph_base=settings.GRAPH_API_BASE,
            audience_prefix=settings.AGENT_APP_AUDIENCE_PREFIX,
            client_secret_loader=lambda: LOCAL_CLIENT_CREDENTIAL,
            http_client=memory_graph_client(settings.GRAPH_API_BASE),
        )
    return GraphService(
        tenant_id=settings.ENTRA_TENANT_ID,
        backend_client_id=settings.ENTRA_BACKEND_CLIENT_ID,
        login_base=settings.ENTRA_LOGIN_BASE,
        graph_base=settings.GRAPH_API_BASE,
        audience_prefix=settings.AGENT_APP_AUDIENCE_PREFIX,
    )
