"""Cedar per-tool authorization on gateway MCP servers, over ``McpCedarService``.

Four gateway-only routes nested under ``/mcp-servers``: ``GET /{mcp_id}/policies`` (VIEWER, the
policy set), ``POST /{mcp_id}/policies`` (OPERATOR, one Cedar policy, 201), ``DELETE
/{mcp_id}/policies/{pid}`` (OPERATOR, 204) and ``PUT /{mcp_id}/policy-enforcement`` (OPERATOR, the
engine mode). Every route resolves the gateway through ``mcp_servers._load_visible_mcp_server``,
the one visibility gate, before the gateway-kind and provisioned checks and before any side effect,
so a foreign tenant's gateway is the same 404 as a missing id and never reaches the 409 kind branch
(which would be an existence oracle); the list gates as a READ, the three mutating routes pass
``for_write=True``. A non-gateway MCP is 409; a gateway whose identity is not provisioned is 409 on
the mutating routes. The service's ``McpCedarError`` kinds render through the one error handler.
"""

import logging
from typing import List, Literal, Optional

from fastapi import APIRouter, Response
from fastapi import Depends as RBACDepends
from pydantic import Field

from core.config import settings
from core.rbac import Principal, Role, current_principal, require_role
from models.base import WireModel
from models.mcp_server import CedarEnforcementMode, EnforcementState, Kind
from services.cedar_policy_text import CedarEffect
from services.mcp_cedar_service import McpCedarError, McpCedarService
from services.tenant_resolver import TenantContext

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/mcp-servers", tags=["mcp-cedar"])

_cedar_svc: Optional[McpCedarService] = None


def get_cedar_service() -> McpCedarService:
    """Lazy ``McpCedarService`` singleton over the MCP registry singleton (tests patch
    ``mcp_cedar._cedar_svc``)."""
    global _cedar_svc
    if _cedar_svc is None:
        from api.routes.mcp_servers import get_service as get_mcp_registry

        _cedar_svc = McpCedarService(
            registry=get_mcp_registry(),
            region=getattr(settings, "MCP_REGISTRY_REGION", "") or "us-east-1",
            engine_name_prefix=getattr(settings, "CEDAR_POLICY_ENGINE_PREFIX", "agp-cedar-"),
        )
    return _cedar_svc


async def get_tenant_ctx(
    principal: Principal = RBACDepends(current_principal),
) -> TenantContext:
    """The per-request tenant context, through the one resolver singleton ``users.py`` owns
    (tests patch ``api.routes.users._tenant_resolver``). Imported lazily to avoid an import cycle."""
    from api.routes.users import get_tenant_ctx as _users_get_tenant_ctx

    return await _users_get_tenant_ctx(principal)


class ConditionIn(WireModel):
    param: str
    op: str
    value: str
    type: str


class ConditionOut(WireModel):
    param: str
    op: str
    value: str
    type: str


class CedarPolicyRow(WireModel):
    policy_id: str
    user_oid: Optional[str] = Field(
        default=None, description="None for a foreign or headerless policy and for an all-users deny."
    )
    user_label: Optional[str] = None
    tool: Optional[str] = Field(default=None, description="None means all tools, or a foreign policy.")
    effect: CedarEffect = Field(default="allow", description="'allow' or 'deny'.")
    conditions: List[ConditionOut] = Field(default_factory=list)
    managed: bool = Field(default=True, description="False for a foreign or headerless policy.")
    cedar_text: str
    kind: Optional[str] = Field(default=None, description="'contain' for an agent-containment forbid.")
    agent_app_id: Optional[str] = Field(
        default=None, description="The contained agent's Entra app id when kind is 'contain'."
    )


class CedarPolicySet(WireModel):
    enforcement_mode: CedarEnforcementMode = Field(description="'none', 'log_only' or 'enforce'.")
    engine_id: Optional[str] = None
    policies: List[CedarPolicyRow]


class AddPolicyRequest(WireModel):
    principal_oid: Optional[str] = Field(
        default=None, description="Required for an allow; optional (all users) for a deny."
    )
    principal_label: str
    tool_name: Optional[str] = Field(default=None, description="None means all tools.")
    all_tools: bool = Field(default=False, description="When true, tool_name is forced to None.")
    effect: CedarEffect = Field(default="allow", description="'allow' or 'deny'.")
    conditions: List[ConditionIn] = Field(default_factory=list)


class EnforcementRequest(WireModel):
    mode: Literal["log_only", "enforce", "disabled"] = Field(
        description="'log_only', 'enforce' or 'disabled'."
    )


async def _resolve_gateway_mcp(
    mcp_id: str, ctx: TenantContext, *, require_provisioned: bool, for_write: bool
):
    """Resolve the MCP and apply the tenant and gateway guards: 404 if missing or not visible to
    ``ctx`` (the tenant gate runs first, so a foreign gateway never reaches the kind branch), 409 if
    not a gateway, 409 if ``require_provisioned`` and its gateway identity is not provisioned.
    ``for_write`` carries ``_load_visible_mcp_server``'s semantics: reads honour the cross-tenant
    ``shared`` bypass, writes drop it."""
    from api.routes.mcp_servers import _load_visible_mcp_server

    mcp = await _load_visible_mcp_server(mcp_id, ctx, for_write=for_write)
    if mcp.kind != Kind.GATEWAY:
        raise McpCedarError(f"MCP server {mcp_id} is a {mcp.kind.value} MCP", kind="not_gateway")
    if require_provisioned and not mcp.gateway_id:
        raise McpCedarError(f"MCP server {mcp_id} has no gateway id yet", kind="gateway_not_provisioned")
    return mcp


@router.get("/{mcp_id}/policies", response_model=CedarPolicySet)
async def list_policies(
    mcp_id: str,
    principal: Principal = RBACDepends(current_principal),
    ctx: TenantContext = RBACDepends(get_tenant_ctx),
    _=RBACDepends(require_role(Role.VIEWER)),
):
    """The gateway's Cedar policy set: the engine mode and one friendly row per policy."""
    mcp = await _resolve_gateway_mcp(mcp_id, ctx, require_provisioned=False, for_write=False)
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
    """Create a Cedar per-tool policy, or an all-tools one when ``all_tools``."""
    mcp = await _resolve_gateway_mcp(mcp_id, ctx, require_provisioned=True, for_write=True)
    tool_name = None if (body.all_tools or not body.tool_name) else body.tool_name
    row = await get_cedar_service().add_policy(
        mcp,
        principal_oid=body.principal_oid,
        principal_label=body.principal_label,
        tool_name=tool_name,
        effect=body.effect,
        conditions=[c.model_dump() for c in body.conditions],
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
    """Remove one policy by id (the engine survives). A containment forbid is removed only by
    restoring the agent, never by hand: deleting it here would reopen the gateway while the agent
    still reads CONTAINED, so it is 409."""
    mcp = await _resolve_gateway_mcp(mcp_id, ctx, require_provisioned=True, for_write=True)
    svc = get_cedar_service()
    if await svc.policy_kind(mcp, policy_id) == "contain":
        raise McpCedarError(
            f"policy {policy_id} on MCP server {mcp_id} is a containment forbid", kind="containment_policy"
        )
    await svc.delete_policy(mcp, policy_id)
    return Response(status_code=204)


@router.put("/{mcp_id}/policy-enforcement", response_model=EnforcementState)
async def set_enforcement(
    mcp_id: str,
    body: EnforcementRequest,
    principal: Principal = RBACDepends(current_principal),
    ctx: TenantContext = RBACDepends(get_tenant_ctx),
    _=RBACDepends(require_role(Role.OPERATOR)),
):
    """Set the engine enforcement mode."""
    mcp = await _resolve_gateway_mcp(mcp_id, ctx, require_provisioned=True, for_write=True)
    updated = await get_cedar_service().set_enforcement(mcp, body.mode)
    return EnforcementState(enforcement_mode=updated.cedar_enforcement_mode)
