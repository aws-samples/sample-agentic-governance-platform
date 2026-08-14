"""Cedar per-tool authorization on gateway MCP servers (Epic 8, Task T4).

Four gateway-only routes that expose :class:`McpCedarService` (the AgentCore Gateway
**Policy Engine** orchestrator, T3) over HTTP, nested under ``/mcp-servers``:

  - GET    ``/{mcp_id}/policies``           VIEWER   → the policy set (engine mode + rows)
  - POST   ``/{mcp_id}/policies``           OPERATOR → create one Cedar permit (201)
  - DELETE ``/{mcp_id}/policies/{pid}``     OPERATOR → remove one policy (204)
  - PUT    ``/{mcp_id}/policy-enforcement`` OPERATOR → set the engine mode

Mirrors the idiom of ``routes/mcp_server_grants.py``: a lazy per-module service singleton
(``_cedar_svc`` + ``get_cedar_service()`` — tests patch ``mcp_cedar._cedar_svc`` directly),
the E7 RBAC idiom (``RBACDepends(current_principal)`` + ``RBACDepends(require_role(...))``;
list=VIEWER, mutate=OPERATOR), the ``mcp_servers.get_service().get(mcp_id)`` + 404 resolve,
and the FIXED ``detail=`` literal convention on 4xx/5xx (NEVER ``str(err)`` — a service
error message must not surface to clients).

Every route is GATEWAY-only: a non-gateway MCP → 409; a gateway whose identity is not yet
provisioned (no ``gateway_id``) → 409 on the mutating routes. ``McpCedarError`` maps to 422
on add (bad Cedar / validation), 404 on delete-missing, and 502 on an enforcement failure.
The cedar service methods are async (they off-load their blocking boto3 internally), so the
handlers just ``await`` them.

Multi-tenancy (Epic 24/T5 invariant, applied here by E34/T6): all four routes resolve a
``tenant_ctx`` per-request dependency and gate the gateway through
``mcp_servers._load_visible_mcp_server`` — the ONE visibility gate in the codebase — BEFORE
the gateway-kind / provisioned checks and BEFORE any side effect. A foreign tenant's gateway
is therefore 404 ("MCP server not found", byte-identical to a truly-missing id: the
404-not-403 contract) and never reaches the 409 kind branch, which would itself be an
existence oracle. ``list_policies`` gates as a READ (``shared`` MCPs stay cross-tenant
readable); the three mutating routes pass ``for_write=True``, so a foreign shared gateway's
policies cannot be edited by another tenant's OPERATOR.
"""

import logging
from typing import List, Optional

from fastapi import APIRouter, HTTPException, Response
from fastapi import Depends as RBACDepends
from pydantic import BaseModel

from core.config import settings
from core.rbac import Principal, Role, current_principal, require_role
from models.mcp_server import Kind
from services.mcp_cedar_service import McpCedarError, McpCedarService
from services.tenant_resolver import TenantContext

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/mcp-servers", tags=["mcp-cedar"])

# Lazy McpCedarService singleton — same pattern as ``routes/mcp_server_grants.py``'s
# service singletons (tests patch ``mcp_cedar._cedar_svc`` directly).
_cedar_svc: Optional[McpCedarService] = None


def get_cedar_service() -> McpCedarService:
    """Lazy McpCedarService singleton (the gateway Policy Engine orchestrator).

    Wires the shared MCP registry singleton (resolved via a local import to avoid
    import-order coupling, like ``mcp_server_grants`` does) + the registry region +
    the engine name prefix from settings.
    """
    global _cedar_svc
    if _cedar_svc is None:
        from api.routes.mcp_servers import get_service as get_mcp_registry

        _cedar_svc = McpCedarService(
            registry=get_mcp_registry(),
            region=getattr(settings, "MCP_REGISTRY_REGION", "") or "us-east-1",
            engine_name_prefix=getattr(
                settings, "CEDAR_POLICY_ENGINE_PREFIX", "agp-cedar-"
            ),
        )
    return _cedar_svc


async def get_tenant_ctx(
    principal: Principal = RBACDepends(current_principal),
) -> TenantContext:
    """Delegate to the ONE resolver-singleton accessor (E24/T5).

    ``users.py`` owns the lazy ``TenantResolver`` singleton (``_tenant_resolver`` /
    ``get_tenant_resolver()``); this is a thin re-export so mcp_cedar.py has its own
    per-request ``tenant_ctx`` dependency WITHOUT keeping a second resolver copy —
    tests patch ``api.routes.users._tenant_resolver`` and both /users/me and every
    route here observe it. Imported lazily to avoid an import cycle at module load
    (identical to ``mcp_servers.get_tenant_ctx`` / ``agents.get_tenant_ctx``).
    """
    from api.routes.users import get_tenant_ctx as _users_get_tenant_ctx

    return await _users_get_tenant_ctx(principal)


# --- models ------------------------------------------------------------------

class ConditionIn(BaseModel):
    param: str
    op: str
    value: str
    type: str


class ConditionOut(BaseModel):
    param: str
    op: str
    value: str
    type: str


class CedarPolicyRow(BaseModel):
    policy_id: str
    user_oid: Optional[str] = None      # None for a foreign/headerless policy OR all-users deny
    user_label: Optional[str] = None
    tool: Optional[str] = None          # None == "All tools" OR a foreign policy
    effect: str = "allow"               # "allow" | "deny"
    conditions: List[ConditionOut] = []
    managed: bool = True                # False for a foreign/headerless policy
    cedar_text: str


class CedarPolicySet(BaseModel):
    enforcement_mode: str               # none | log_only | enforce
    engine_id: Optional[str] = None
    policies: List[CedarPolicyRow]


class AddPolicyRequest(BaseModel):
    principal_oid: Optional[str] = None # required for allow; optional (all-users) for deny
    principal_label: str
    tool_name: Optional[str] = None     # None == "All tools"
    all_tools: bool = False             # explicit flag; when True, tool_name is forced None
    effect: str = "allow"               # "allow" | "deny"
    conditions: List[ConditionIn] = []


class EnforcementRequest(BaseModel):
    mode: str                           # "log_only" | "enforce" | "disabled"


# --- helpers -----------------------------------------------------------------

async def _resolve_gateway_mcp(
    mcp_id: str, ctx: TenantContext, *, require_provisioned: bool, for_write: bool
):
    """Resolve the MCP + apply the tenant and gateway guards. 404 if missing OR not visible
    to ``ctx``; 409 if not a gateway; 409 (mutating routes only) if its gateway identity is
    not yet provisioned.

    The TENANT gate runs FIRST (E24 invariant): a foreign gateway must 404 with the same
    literal as a missing id and must NOT fall through to the 409 kind branch, which would
    leak its existence. Delegates to ``mcp_servers._load_visible_mcp_server`` rather than
    re-implementing ``visible()`` — one visibility gate in the codebase — so ``for_write``
    carries that helper's exact semantics: reads honor the cross-tenant ``shared`` bypass,
    writes (``for_write=True``) drop it.
    """
    # Local import avoids any import-order coupling with the mcp_servers route module.
    from api.routes.mcp_servers import _load_visible_mcp_server

    mcp = await _load_visible_mcp_server(mcp_id, ctx, for_write=for_write)
    if mcp.kind != Kind.GATEWAY:
        raise HTTPException(
            status_code=409,
            detail="policies are only available for gateway MCP servers",
        )
    if require_provisioned and not mcp.gateway_id:
        raise HTTPException(status_code=409, detail="gateway identity is not provisioned")
    return mcp


# --- routes ------------------------------------------------------------------

@router.get("/{mcp_id}/policies", response_model=CedarPolicySet)
async def list_policies(
    mcp_id: str,
    principal: Principal = RBACDepends(current_principal),
    ctx: TenantContext = RBACDepends(get_tenant_ctx),
    _=RBACDepends(require_role(Role.VIEWER)),
):
    """List the gateway's Cedar policy set (engine mode + one friendly row per policy)."""
    mcp = await _resolve_gateway_mcp(
        mcp_id, ctx, require_provisioned=False, for_write=False
    )
    policy_set = await get_cedar_service().list_policies(mcp)
    return CedarPolicySet(**policy_set)


@router.post("/{mcp_id}/policies", response_model=CedarPolicyRow, status_code=201)
async def add_policy(
    mcp_id: str,
    body: AddPolicyRequest,
    principal: Principal = RBACDepends(current_principal),
    ctx: TenantContext = RBACDepends(get_tenant_ctx),
    _=RBACDepends(require_role(Role.OPERATOR)),
):
    """Create a Cedar per-tool permit (or all-tools when ``all_tools``). 422 on bad Cedar /
    validation (``McpCedarError``)."""
    mcp = await _resolve_gateway_mcp(
        mcp_id, ctx, require_provisioned=True, for_write=True
    )
    tool_name = None if (body.all_tools or not body.tool_name) else body.tool_name
    try:
        row = await get_cedar_service().add_policy(
            mcp,
            principal_oid=body.principal_oid,
            principal_label=body.principal_label,
            tool_name=tool_name,
            effect=body.effect,
            conditions=[c.model_dump() for c in body.conditions],
        )
    except McpCedarError:
        # FIXED literal — never str(err) (T-GRAPH convention; no service detail leaks).
        raise HTTPException(
            status_code=422,
            detail="failed to create policy (invalid Cedar or gateway error)",
        )
    return CedarPolicyRow(**row)


@router.delete("/{mcp_id}/policies/{policy_id}", status_code=204)
async def delete_policy(
    mcp_id: str,
    policy_id: str,
    principal: Principal = RBACDepends(current_principal),
    ctx: TenantContext = RBACDepends(get_tenant_ctx),
    _=RBACDepends(require_role(Role.OPERATOR)),
):
    """Remove one policy by id → 204 (the engine survives). A missing policy / unset engine
    (``McpCedarError``) → 404."""
    mcp = await _resolve_gateway_mcp(
        mcp_id, ctx, require_provisioned=True, for_write=True
    )
    try:
        await get_cedar_service().delete_policy(mcp, policy_id)
    except McpCedarError:
        raise HTTPException(status_code=404, detail="policy not found")
    return Response(status_code=204)


@router.put("/{mcp_id}/policy-enforcement")
async def set_enforcement(
    mcp_id: str,
    body: EnforcementRequest,
    principal: Principal = RBACDepends(current_principal),
    ctx: TenantContext = RBACDepends(get_tenant_ctx),
    _=RBACDepends(require_role(Role.OPERATOR)),
):
    """Set the engine enforcement mode. The ``mode`` literal is validated FIRST (→ 400)
    before any service call; an AWS failure (``McpCedarError``) → 502."""
    if body.mode not in {"log_only", "enforce", "disabled"}:
        raise HTTPException(
            status_code=400, detail="mode must be log_only, enforce, or disabled"
        )
    mcp = await _resolve_gateway_mcp(
        mcp_id, ctx, require_provisioned=True, for_write=True
    )
    try:
        updated = await get_cedar_service().set_enforcement(mcp, body.mode)
    except McpCedarError:
        raise HTTPException(status_code=502, detail="failed to update enforcement")
    return {"enforcement_mode": updated.cedar_enforcement_mode}
