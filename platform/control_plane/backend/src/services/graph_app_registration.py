"""Entra app registration: the per-agent and per-MCP-server application with its service principal.

Functions over a ``GraphService`` passed as ``graph``, whose HTTP client, app token and error mapping are read
at call time. ``GraphService`` delegates ``create_agent_app`` and ``create_mcp_app`` here.
"""

from __future__ import annotations

import logging
import uuid
from typing import TYPE_CHECKING

import httpx

from core.config import settings
from services.graph_errors import GraphError

if TYPE_CHECKING:
    from services.graph_service import GraphService

logger = logging.getLogger(__name__)


async def create_agent_app(graph: GraphService, agent_id: str, display_name: str) -> dict:
    """Create the per-agent Entra app registration and its service principal.

    The agent path uses the injected ``identifierUris`` prefix and ``["User"]``-only appRoles,
    because user grants depend on the user-only member type. Returns ``{app_id, sp_id, app_uri,
    invoke_scope_id, invoker_role_id, admin_role_id}``.
    """
    return await _create_app_reg(
        graph,
        resource_id=agent_id,
        display_name=display_name,
        audience_prefix=graph._audience_prefix,
        member_types=["User"],
    )


async def create_mcp_app(graph: GraphService, mcp_id: str, display_name: str) -> dict:
    """Create the per-MCP-server Entra app registration and its service principal.

    The one difference from the agent path: the MCP app's appRoles allow ``["User",
    "Application"]`` member types, because a service principal (the agent) cannot be assigned a
    ``["User"]``-only role. The identifier-URI prefix is ``settings.MCP_APP_AUDIENCE_PREFIX``.
    Returns the same six keys as ``create_agent_app``.
    """
    return await _create_app_reg(
        graph,
        resource_id=mcp_id,
        display_name=display_name,
        audience_prefix=settings.MCP_APP_AUDIENCE_PREFIX,
        member_types=["User", "Application"],
    )


async def _create_app_reg(
    graph: GraphService,
    *,
    resource_id: str,
    display_name: str,
    audience_prefix: str,
    member_types: list[str],
) -> dict:
    """Create one Entra app registration plus its service principal, or resolve the existing pair.

    Parameterised on the ``identifierUris`` prefix and the appRoles ``allowedMemberTypes`` so the
    agent and MCP paths share one body shape, one Invoke oauth2 scope, one SP create and one
    replication retry. ``optionalClaims`` opts the access token into the ``email`` claim:
    Databricks federation policies match the subject on it, and Entra emits it only when the
    resource app asks. ``identifierUris`` is unique tenant-wide, so a duplicate-identifierUri
    error on the create means the app already exists (a persist failed after an earlier create);
    it is looked up with its SP and the ids returned, which makes re-provisioning resumable.

    Returns ``{app_id, sp_id, app_uri, invoke_scope_id, invoker_role_id, admin_role_id}``,
    ``app_id`` being the client GUID and ``app_uri`` the ``api://`` identifier URI.
    """
    app_uri = f"{audience_prefix}{resource_id}"
    invoke_scope_id = str(uuid.uuid4())
    invoker_role_id = str(uuid.uuid4())
    admin_role_id = str(uuid.uuid4())

    application = {
        "displayName": display_name,
        "signInAudience": "AzureADMyOrg",
        "identifierUris": [app_uri],
        "optionalClaims": {
            "accessToken": [{"name": "email", "essential": False, "additionalProperties": []}]
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

    token = await graph._app_token()
    headers = {"Authorization": f"Bearer {token}"}
    create_resp = await graph._http.request(
        "POST",
        f"{graph._graph_base}/applications",
        json=application,
        headers=headers,
    )

    if 200 <= create_resp.status_code < 300:
        app_obj = create_resp.json()
        app_client_id = app_obj["appId"]
        sp_obj = await _create_sp(graph, app_client_id)
        return {
            "app_id": app_client_id,
            "sp_id": sp_obj["id"],
            "app_uri": app_uri,
            "invoke_scope_id": invoke_scope_id,
            "invoker_role_id": invoker_role_id,
            "admin_role_id": admin_role_id,
        }

    code = graph._graph_error_code(create_resp)
    if _is_duplicate_identifier(create_resp, code):
        logger.info(
            "[graph_service] _create_app_reg: identifierUri exists for resource_id "
            "(status=%s code=%s) — resolving existing app",
            create_resp.status_code,
            code,
        )
        return await _resolve_existing_app(graph, app_uri)

    raise graph._graph_error(create_resp)


async def _create_sp(graph: GraphService, app_client_id: str) -> dict:
    """``POST /servicePrincipals`` for a just-created app, through the replication retry."""
    response = await graph._request_with_replication_retry(
        "POST",
        "/servicePrincipals",
        json_body={"appId": app_client_id},
        label="create_sp",
    )
    return response.json()


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


async def _resolve_existing_app(graph: GraphService, app_uri: str) -> dict:
    """Look up an existing app by its identifierUri + its SP; return the 6 ids."""
    app_filter = f"identifierUris/any(u:u eq '{app_uri}')"
    app_resp = await graph._graph_request("GET", "/applications", params={"$filter": app_filter})
    apps = app_resp.json().get("value", [])
    if not apps:
        raise GraphError(404, "Request_ResourceNotFound")
    app_obj = apps[0]
    app_client_id = app_obj["appId"]

    scopes = (app_obj.get("api", {}) or {}).get("oauth2PermissionScopes", []) or []
    invoke_scope_id = next((s["id"] for s in scopes if s.get("value") == "Invoke"), None)
    app_roles = app_obj.get("appRoles", []) or []
    invoker_role_id = next((r["id"] for r in app_roles if r.get("value") == "Invoker"), None)
    admin_role_id = next((r["id"] for r in app_roles if r.get("value") == "Admin"), None)

    sp_resp = await graph._graph_request(
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
