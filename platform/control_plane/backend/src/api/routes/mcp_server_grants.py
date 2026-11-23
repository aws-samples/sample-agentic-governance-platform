"""Agent-to-MCP grants: Entra app-role assignments on the MCP service principal.

Live Microsoft Graph reads and writes, no DynamoDB: ``appRoleAssignedTo`` on the MCP's SP is the
source of truth, with the MCP SP as the resource and an agent SP as the principal. Two routers:
``router`` (``/mcp-servers/{id}/grants``: list, create, delete) and ``agent_mcp_router``
(``/agents/{id}/mcp-grants``, the reverse read "which MCPs can this agent reach", which has to live
on an ``/agents``-prefixed router to be reachable). The POST applies the assignment, the delegated
consent, the credential provider and the runtime env through ``services.agent_mcp_grant``; the
DELETE is its kill switch. The resolved internal GUIDs flow into the Graph service; no raw path
segment is echoed into a Graph URL.
"""

import logging
from types import SimpleNamespace
from typing import List, Optional

import anyio.to_thread
from fastapi import APIRouter, Response
from fastapi import Depends as RBACDepends
from pydantic import Field

from core.config import settings
from core.rbac import Principal, Role, current_principal, require_role
from models.base import WireModel
from services.agent_credential_service import AgentCredentialService
from services.agent_identity_service import AgentIdentityService
from services.agent_mcp_grant import McpGrantRefusedError, apply_agent_mcp_grant, revoke_agent_mcp_grant
from services.graph_service import GraphService, graph_service_from_settings
from services.tenant_resolver import TenantContext

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/mcp-servers", tags=["mcp-grants"])
agent_mcp_router = APIRouter(prefix="/agents", tags=["agent-mcp-grants"])

_graph_svc: Optional[GraphService] = None
_cred_svc: Optional[AgentCredentialService] = None
_identity_svc: Optional[AgentIdentityService] = None


def get_mcp_graph_service() -> GraphService:
    """Lazy ``GraphService`` singleton shared by the MCP-side services (tests patch
    ``mcp_server_grants._graph_svc``)."""
    global _graph_svc
    if _graph_svc is None:
        _graph_svc = graph_service_from_settings()
    return _graph_svc


def get_agent_credential_service() -> AgentCredentialService:
    """Lazy ``AgentCredentialService`` singleton (the per-agent OBO credential provider) over the
    shared Graph service; the region is the agent registry's."""
    global _cred_svc
    if _cred_svc is None:
        _cred_svc = AgentCredentialService(
            graph=get_mcp_graph_service(),
            region=getattr(settings, "AGENT_REGISTRY_REGION", "") or "us-east-1",
        )
    return _cred_svc


async def get_tenant_ctx(
    principal: Principal = RBACDepends(current_principal),
) -> TenantContext:
    """The per-request tenant context, through the one resolver singleton ``users.py`` owns
    (tests patch ``api.routes.users._tenant_resolver``). Imported lazily to avoid an import cycle."""
    from api.routes.users import get_tenant_ctx as _users_get_tenant_ctx

    return await _users_get_tenant_ctx(principal)


def get_agent_identity_service() -> AgentIdentityService:
    """Lazy ``AgentIdentityService`` singleton (the runtime env writer) over the shared Graph
    service and the agent registry singleton."""
    global _identity_svc
    if _identity_svc is None:
        from api.routes.agents import get_service as get_agent_service

        _identity_svc = AgentIdentityService(
            graph=get_mcp_graph_service(),
            registry=get_agent_service(),
            tenant_id=settings.ENTRA_TENANT_ID,
            login_base=settings.ENTRA_LOGIN_BASE,
            region=getattr(settings, "AGENT_REGISTRY_REGION", "") or "us-east-1",
        )
    return _identity_svc


class McpGrantCreate(WireModel):
    principal_id: str = Field(description="The agent's Entra service principal object id.")
    principal_type: str = Field(description="Display only; 'agent' here. Graph assigns identically.")
    role: str = Field(description="'Invoker' or 'Admin'.")


class McpGrantRead(WireModel):
    assignment_id: str = Field(description="The appRoleAssignment id, used to revoke.")
    principal_id: str
    principal_display: str
    principal_type: str = Field(description="Graph's principalType, e.g. 'ServicePrincipal'.")
    role: str = Field(description="'Invoker', 'Admin' or 'Unknown', mapped from appRoleId.")


class AgentMcpGrant(WireModel):
    """One row of the agent-direction reverse read (the agent's outbound MCP grants)."""

    mcp_id: str = Field(description="Our McpServer id, reverse-joined from resourceId via entra_sp_id.")
    mcp_name: str = Field(description="Graph's resourceDisplayName, else the record's name.")
    role: str = Field(description="'Invoker', 'Admin' or 'Unknown', against that MCP's role ids.")
    assignment_id: str = Field(description="For DELETE /mcp-servers/{mcp_id}/grants/{assignment_id}.")


def _role_for_app_role_id(mcp, app_role_id: Optional[str]) -> str:
    """``Invoker`` or ``Admin`` from the MCP's stored role ids; anything else (a role minted by
    another tool, a stale assignment, a missing id) is ``Unknown`` so the list view never breaks."""
    if app_role_id and app_role_id == mcp.invoker_role_id:
        return "Invoker"
    if app_role_id and app_role_id == mcp.admin_role_id:
        return "Admin"
    return "Unknown"


def _app_role_id_for_role(mcp, role: str) -> str:
    """The MCP's stored app role id for the requested role name; 400 for anything but
    ``Invoker`` or ``Admin``."""
    if role == "Invoker":
        return mcp.invoker_role_id
    if role == "Admin":
        return mcp.admin_role_id
    raise McpGrantRefusedError("the requested role is neither Invoker nor Admin", kind="invalid_role")


def _provisioned_sp_id(resource) -> Optional[str]:
    """The Entra SP object id of a grant-capable MCP or agent: identity provisioned and an SP id
    present; ``None`` otherwise."""
    if resource.identity_status != "provisioned":
        return None
    return resource.entra_sp_id or None


def _to_grant_read(mcp, assignment: dict) -> McpGrantRead:
    return McpGrantRead(
        assignment_id=assignment.get("id", ""),
        principal_id=assignment.get("principalId", ""),
        principal_display=assignment.get("principalDisplayName") or "",
        principal_type=assignment.get("principalType") or "",
        role=_role_for_app_role_id(mcp, assignment.get("appRoleId")),
    )


@router.get("/{mcp_id}/grants", response_model=List[McpGrantRead])
async def list_mcp_grants(
    mcp_id: str,
    principal: Principal = RBACDepends(current_principal),
    ctx: TenantContext = RBACDepends(get_tenant_ctx),
    _=RBACDepends(require_role(Role.VIEWER)),
):
    """List the MCP's app-role assignments (which agents can reach it), read live from Graph.

    Gated on the same visibility helper as ``GET /mcp-servers/{id}`` with READ semantics (a shared
    MCP stays readable across tenants); a foreign non-shared MCP is the same 404. An unprovisioned
    MCP is ``[]``, not 409: the UI shows a banner.
    """
    from api.routes.mcp_servers import _load_visible_mcp_server

    mcp = await _load_visible_mcp_server(mcp_id, ctx)
    sp_id = _provisioned_sp_id(mcp)
    if sp_id is None:
        return []
    assignments = await get_mcp_graph_service().list_assignments(sp_id)
    return [_to_grant_read(mcp, a) for a in assignments]


@router.post("/{mcp_id}/grants", response_model=McpGrantRead, status_code=201)
async def create_mcp_grant(
    mcp_id: str,
    body: McpGrantCreate,
    principal: Principal = RBACDepends(current_principal),
    ctx: TenantContext = RBACDepends(get_tenant_ctx),
    _=RBACDepends(require_role(Role.OPERATOR)),
):
    """Grant an agent an ``Invoker`` or ``Admin`` app role on the MCP SP and wire the agent-to-MCP
    delegated consent, credential provider and runtime env (``apply_agent_mcp_grant``).

    The MCP loads through ``_load_visible_mcp_server`` with ``for_write=True``: a missing MCP and
    a foreign tenant's MCP, shared or not, answer the same 404 "MCP server not found" before the
    provisioning 409 and any Graph call, so a foreign shared MCP is readable but never a grant
    target. Then 409 for an unprovisioned MCP (no SP to assign on) and 400 for a role other than
    ``Invoker`` / ``Admin``, each decided before any Graph write. A cross-tenant grantee needs
    ADMIN and fails closed: same tenant (both ids present and equal) or a platform-shared MCP
    suffices at OPERATOR; a foreign tenant, an unstamped record (``None`` is never same-tenant) or
    a principal that is not a governed agent is 403. The grant principal is passed as a bare
    carrier of the SP id; the service resolves the governed record only after the Graph writes
    succeed.
    """
    from api.routes.mcp_servers import _load_visible_mcp_server

    mcp = await _load_visible_mcp_server(mcp_id, ctx, for_write=True)
    if _provisioned_sp_id(mcp) is None:
        raise McpGrantRefusedError(f"MCP server {mcp_id} has no provisioned identity", kind="not_provisioned")

    app_role_id = _app_role_id_for_role(mcp, body.role)

    if principal.role < Role.ADMIN and not mcp.shared:
        from api.routes.agents import get_service as get_agent_service

        grant_agent = next(
            (a for a in get_agent_service().list() if a.entra_sp_id == body.principal_id), None
        )
        if (
            grant_agent is None
            or grant_agent.tenant_id is None
            or mcp.tenant_id is None
            or grant_agent.tenant_id != mcp.tenant_id
        ):
            raise McpGrantRefusedError(
                f"the grantee is not an agent of MCP server {mcp_id}'s tenant", kind="cross_tenant"
            )

    grant_principal = SimpleNamespace(entra_sp_id=body.principal_id)
    assignment_id = await apply_agent_mcp_grant(grant_principal, mcp, role=body.role)
    return McpGrantRead(
        assignment_id=assignment_id,
        principal_id=body.principal_id,
        principal_display="",
        principal_type="ServicePrincipal",
        role=_role_for_app_role_id(mcp, app_role_id),
    )


@router.delete("/{mcp_id}/grants/{assignment_id}", status_code=204)
async def delete_mcp_grant(
    mcp_id: str,
    assignment_id: str,
    principal: Principal = RBACDepends(current_principal),
    ctx: TenantContext = RBACDepends(get_tenant_ctx),
    _=RBACDepends(require_role(Role.OPERATOR)),
):
    """Revoke an app-role assignment and, when the agent holds no other role on this MCP, its
    delegated consent, so the revoke is a real kill switch (``revoke_agent_mcp_grant``). 204.

    The MCP loads through ``_load_visible_mcp_server`` with ``for_write=True``: a missing MCP and
    a foreign tenant's MCP, shared or not, answer the same 404 "MCP server not found" before the
    provisioning 409 and the Graph revoke. Then 409 for an unprovisioned MCP (no SP id, so the
    Graph path would be malformed), decided before any Graph call.
    """
    from api.routes.mcp_servers import _load_visible_mcp_server

    mcp = await _load_visible_mcp_server(mcp_id, ctx, for_write=True)
    if _provisioned_sp_id(mcp) is None:
        raise McpGrantRefusedError(f"MCP server {mcp_id} has no provisioned identity", kind="not_provisioned")

    await revoke_agent_mcp_grant(mcp, assignment_id)
    return Response(status_code=204)


@agent_mcp_router.get("/{agent_id}/mcp-grants", response_model=List[AgentMcpGrant])
async def list_agent_mcp_grants(
    agent_id: str,
    principal: Principal = RBACDepends(current_principal),
    ctx: TenantContext = RBACDepends(get_tenant_ctx),
    _=RBACDepends(require_role(Role.VIEWER)),
):
    """Reverse read: which MCPs the agent can reach (its outbound app-role grants).

    Gated on the agent's tenant through the same helper as ``GET /agents/{id}``; an unprovisioned
    agent is ``[]``. Each assignment's ``resourceId`` (an MCP SP object id) is joined back to our
    record on ``entra_sp_id`` for the ``mcp_id`` and that MCP's role ids; assignments on a
    resource that is not a known MCP SP (Microsoft Graph, for one) are filtered out.
    """
    from api.routes.agents import _load_visible_agent
    from api.routes.mcp_servers import get_service as get_mcp_service

    agent = await _load_visible_agent(agent_id, ctx)
    sp_id = _provisioned_sp_id(agent)
    if sp_id is None:
        return []

    assignments = await get_mcp_graph_service().list_agent_mcp_grants(sp_id)
    by_sp = {m.entra_sp_id: m for m in get_mcp_service().list() if m.entra_sp_id}

    rows: List[AgentMcpGrant] = []
    for a in assignments:
        resource_sp_id = a.get("resourceId")
        mcp = by_sp.get(resource_sp_id) if resource_sp_id else None
        if mcp is None:
            continue
        rows.append(
            AgentMcpGrant(
                mcp_id=mcp.id,
                mcp_name=a.get("resourceDisplayName") or mcp.name,
                role=_role_for_app_role_id(mcp, a.get("appRoleId")),
                assignment_id=a.get("id", ""),
            )
        )
    return rows


class MissingMcpGrant(WireModel):
    """An MCP server the agent's harness lists in ``tools`` but holds no grant for."""

    mcp_id: str
    mcp_name: str


@agent_mcp_router.get("/{agent_id}/mcp-grants/missing", response_model=List[MissingMcpGrant])
async def list_missing_agent_mcp_grants(
    agent_id: str,
    principal: Principal = RBACDepends(current_principal),
    ctx: TenantContext = RBACDepends(get_tenant_ctx),
    _=RBACDepends(require_role(Role.VIEWER)),
):
    """The agent-detail hint: governed MCP servers whose gateway one of the agent's harnesses
    declares in ``tools`` with no grant behind it (such a harness fails every invoke). ``[]`` for a
    runtime agent and whenever the harness cannot be read: a hint, not a gate."""
    from api.routes.agents import _load_visible_agent
    from services import agent_mcp_env

    agent = await _load_visible_agent(agent_id, ctx)
    missing = await anyio.to_thread.run_sync(
        agent_mcp_env.ungranted_tool_mcps, agent, list(agent.runtime_arns().values())
    )
    return [MissingMcpGrant(mcp_id=m.id, mcp_name=m.name) for m in missing]
