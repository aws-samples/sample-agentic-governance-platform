"""Governance-graph routes: two read-only VIEWER routes over :class:`GovernanceGraphService`.

``GET /governance-graph`` is the full ``{nodes, edges}`` graph (User/Group to Agent "access",
Agent to MCP "can_call"); ``GET /governance-graph/principals/{oid}?kind=user|group`` is the lazy
Entra detail of one clicked node. The service is composed per request from the shared singletons
through ``_build_service``, with the ``GraphService`` instance ``routes/grants.py`` owns, never a new
client. The service's ``GovernanceGraphError`` kinds are rendered by the one ``AppError`` handler.
"""

from fastapi import APIRouter, Query
from fastapi import Depends as RBACDepends

from core.rbac import Principal, Role, current_principal, require_role
from models.governance_graph import GovernanceGraph, PrincipalDetail
from services.governance_graph_service import GovernanceGraphError, GovernanceGraphService
from services.tenant_resolver import TenantContext

router = APIRouter(prefix="/governance-graph", tags=["governance-graph"])


def _build_service():
    """Compose the aggregator from the shared singletons; imported lazily because the routes form a cycle."""
    from api.routes.agents import get_service as get_agent_service
    from api.routes.grants import get_graph_service
    from api.routes.mcp_servers import get_service as get_mcp_service

    return GovernanceGraphService(
        agent_service=get_agent_service(),
        mcp_service=get_mcp_service(),
        graph_service=get_graph_service(),
    )


async def get_tenant_ctx(
    principal: Principal = RBACDepends(current_principal),
) -> TenantContext:
    """The per-request ``TenantContext`` through the one resolver singleton ``users.py`` owns.

    Tests patch ``api.routes.users._tenant_resolver`` and the graph route observes it. Imported
    lazily because a module-level import is a cycle.
    """
    from api.routes.users import get_tenant_ctx as _users_get_tenant_ctx

    return await _users_get_tenant_ctx(principal)


@router.get("", response_model=GovernanceGraph)
async def get_governance_graph(
    principal: Principal = RBACDepends(current_principal),
    ctx: TenantContext = RBACDepends(get_tenant_ctx),
    _=RBACDepends(require_role(Role.VIEWER)),
):
    """The read-only governance graph.

    A non-global caller gets the subgraph induced by their visible agents and MCPs (own tenants
    plus shared MCPs, the registry list filters' semantics); an admin gets the full graph.
    """
    svc = _build_service()
    return await svc.build(ctx=ctx)


@router.get("/principals/{oid}", response_model=PrincipalDetail)
async def get_principal(
    oid: str,
    kind: str = Query(..., pattern="^(user|group)$"),
    principal: Principal = RBACDepends(current_principal),
    ctx: TenantContext = RBACDepends(get_tenant_ctx),
    _=RBACDepends(require_role(Role.VIEWER)),
):
    """One user or group principal's Entra detail, on node click.

    ``kind`` is validated to "user" or "group" at the query layer (anything else is a 422). A
    non-global caller may only resolve principals that appear in their induced graph, the same
    ``build(ctx=...)`` filtering that scopes the collection route; a principal outside it 404s
    with the same "principal not found" as a genuinely missing oid, so there is no existence
    oracle. An admin skips the gate. The gate rebuilds the caller's induced graph per call, which
    is acceptable because this endpoint fires only on drawer clicks.
    """
    svc = _build_service()

    if not ctx.is_global:
        induced = await svc.build(ctx=ctx)
        node_id = f"{kind}:{oid}"
        if all(node.id != node_id for node in induced.nodes):
            raise GovernanceGraphError(f"{node_id} is outside the caller's graph", kind="principal_not_found")

    return await svc.get_principal(oid, kind)
