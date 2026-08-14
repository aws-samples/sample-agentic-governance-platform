"""Governance Graph aggregation routes (Epic 11, Task 5).

Two READ-ONLY routes (both VIEWER), a thin pass-through over
:class:`GovernanceGraphService` (T-SERVICE), mirroring the lazy-singleton + RBAC +
GraphError→HTTP idiom of ``routes/grants.py``:

  - ``GET /governance-graph`` — the full ``{nodes, edges}`` governance graph
    (User/Group→Agent "access" + Agent→MCP "can_call" relationships).
  - ``GET /governance-graph/principals/{oid}?kind=user|group`` — lazy Entra detail
    for ONE clicked user/group node.

The service is composed PER REQUEST from the existing shared DI singletons via the
lazy ``_build_service`` factory below (lazy imports inside, mirroring grants.py, to
avoid a circular import — ``routes/agents.py`` already imports this package's
neighbours). The ``GraphService`` instance is the SHARED ``get_graph_service()``
singleton owned by ``routes/grants.py`` — never a new client.

Error discipline (mirror grants.py / the GraphError contract): a ``GraphError`` from
the principal lookup maps 404→404, any other status→502 — never a raw 500, never the
Graph error body. A ``ValueError`` (the service rejecting a bad ``kind``)→400.
"""

import logging

from fastapi import APIRouter, HTTPException, Query
from fastapi import Depends as RBACDepends

from core.rbac import Principal, Role, current_principal, require_role
from models.governance_graph import GovernanceGraph, PrincipalDetail
from services.graph_service import GraphError
from services.tenant_resolver import TenantContext

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/governance-graph", tags=["governance-graph"])


def _build_service():
    """Compose the aggregator from the shared DI singletons (lazy, per request).

    Lazy imports inside (mirroring ``routes/grants.py``) avoid a circular import at
    module load. Reuses the ONE shared ``GraphService`` singleton owned by
    ``routes/grants.py`` (``get_graph_service``) — does NOT construct a new client.
    """
    from api.routes.agents import get_service as get_agent_service
    from api.routes.mcp_servers import get_service as get_mcp_service
    from api.routes.grants import get_graph_service
    from services.governance_graph_service import GovernanceGraphService

    return GovernanceGraphService(
        agent_service=get_agent_service(),
        mcp_service=get_mcp_service(),
        graph_service=get_graph_service(),
    )


async def get_tenant_ctx(
    principal: Principal = RBACDepends(current_principal),
) -> TenantContext:
    """Delegate to the ONE resolver-singleton accessor (E24/T8).

    ``users.py`` owns the lazy ``TenantResolver`` singleton; this thin re-export
    gives governance_graph.py its own per-request ``tenant_ctx`` dependency
    WITHOUT a second resolver copy — tests patch
    ``api.routes.users._tenant_resolver`` and the graph route observes it.
    Imported lazily to avoid an import cycle at module load (mirrors
    ``mcp_servers.get_tenant_ctx``).
    """
    from api.routes.users import get_tenant_ctx as _users_get_tenant_ctx

    return await _users_get_tenant_ctx(principal)


@router.get("", response_model=GovernanceGraph)
async def get_governance_graph(
    principal: Principal = RBACDepends(current_principal),
    ctx: TenantContext = RBACDepends(get_tenant_ctx),
    _=RBACDepends(require_role(Role.VIEWER)),
):
    """Aggregate the read-only governance graph (Users/Groups→Agents→MCPs).

    E24/T8: a non-global caller gets the subgraph induced by their visible
    agent/MCP set (own tenants + shared MCPs — same ``visible()`` semantics as
    the registry list filters); admins get the full graph."""
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
    """Resolve ONE user/group principal's Entra detail (lazy, on node click).

    ``kind`` is a required query param validated to "user"/"group" (a bad value →
    422 at the query layer). A ``GraphError`` maps 404→404 / other→502 (never a raw
    500, never the Graph body); a service ``ValueError`` → 400.

    Tenant gate (E24 follow-up, triage item 3): a NON-global caller may only
    resolve principals that appear in THEIR induced graph — the same Task-8
    ``build(ctx=...)`` filtering that scopes ``GET /governance-graph``. A
    principal outside that subgraph 404s with the EXISTING "principal not found"
    literal (byte-identical to a genuinely missing oid — no existence oracle).
    Admin (``is_global``) is unchanged and skips the gate entirely. Cost note:
    the gate rebuilds the caller's induced graph per call (the service exposes no
    cheaper principal-set read); acceptable — this endpoint only fires on drawer
    clicks, and correctness comes first.
    """
    svc = _build_service()

    if not ctx.is_global:
        induced = await svc.build(ctx=ctx)
        node_id = f"{kind}:{oid}"
        if all(node.id != node_id for node in induced.nodes):
            raise HTTPException(status_code=404, detail="principal not found")

    try:
        return await svc.get_principal(oid, kind)
    except GraphError as err:
        if err.status == 404:
            raise HTTPException(status_code=404, detail="principal not found")
        raise HTTPException(status_code=502, detail="failed to resolve the principal")
    except ValueError:
        raise HTTPException(status_code=400, detail="kind must be 'user' or 'group'")
