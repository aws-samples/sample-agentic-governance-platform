"""Marketplace routes: the catalog, subscriptions and listings, and the publish flow.

Browsing, subscribing and listing one's own subscriptions take VIEWER; declaring a datasheet for one's
own product and reading that product's publish record take OPERATOR; the approvals queues, decisions,
retries, metrics and listings take ADMIN, because a declared datasheet is an attestation an admin
approves, never self-asserted. Every identity the service receives (``caller_oid``, ``requester_oid``,
``decided_by``) is read from the validated principal, never from a body. Errors are the service's
``MarketplaceError`` kinds, rendered by the one handler in ``main.py``, the refusals decided here (a
path ``product_type`` outside the enum, an MCP subscription without an agent, a missing publish record)
included.
"""

import logging
from typing import List, Optional

from fastapi import APIRouter, Query, Response
from fastapi import Depends as RBACDepends

from core.config import settings
from core.rbac import Principal, Role, current_principal, require_role
from models.base import WireModel
from models.marketplace import (
    Listing,
    ListingUpdate,
    MarketplaceMetrics,
    ProductCard,
    ProductType,
    PublishRequest,
    PublishRequestCreate,
    PublishRequestStatus,
    RejectRequest,
    RevokeRequest,
    Subscription,
    SubscriptionCreate,
)
from services.graph_errors import GraphError
from services.marketplace_errors import MarketplaceError
from services.marketplace_service import MarketplaceService
from services.tenant_resolver import TenantContext

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/marketplace", tags=["marketplace"])

_svc: Optional[MarketplaceService] = None

_PRODUCT_TYPE_VALUES = frozenset(member.value for member in ProductType)


def get_marketplace_service() -> MarketplaceService:
    """The lazy ``MarketplaceService`` singleton over the registry, graph and tenant singletons (imported
    at call time to avoid an import cycle) and the configured table; tests set ``_svc`` directly."""
    global _svc
    if _svc is None:
        from api.routes.agents import get_service as get_agent_service
        from api.routes.mcp_server_grants import get_mcp_graph_service
        from api.routes.mcp_servers import get_service as get_mcp_service
        from api.routes.tenants import get_tenant_service
        from services.agent_user_grant import (
            apply_user_agent_grant,
            revoke_user_agent_grant,
        )

        _svc = MarketplaceService(
            table_name=getattr(settings, "MARKETPLACE_TABLE_NAME", ""),
            region=getattr(settings, "MCP_REGISTRY_REGION", "") or "us-east-1",
            mcp_registry=get_mcp_service(),
            agent_registry=get_agent_service(),
            mcp_graph=get_mcp_graph_service(),
            agent_grant_fn=apply_user_agent_grant,
            agent_revoke_fn=revoke_user_agent_grant,
            tenant_service=get_tenant_service(),
        )
    return _svc


async def get_tenant_ctx(
    principal: Principal = RBACDepends(current_principal),
) -> TenantContext:
    """The per-request ``TenantContext`` through the one resolver singleton ``users.py`` owns (imported at
    call time to avoid an import cycle), so tests that seed that singleton are observed here too."""
    from api.routes.users import get_tenant_ctx as _users_get_tenant_ctx

    return await _users_get_tenant_ctx(principal)


def _caller_id(principal: Principal) -> str:
    """The identity the service stores for the caller: the Entra oid, or the email under dev-auth."""
    return principal.oid or principal.email or ""


def _product_type_value(product_type: str) -> str:
    """A path ``product_type`` as its plain enum value; an unknown type is refused here, so the service
    is never asked for a registry that does not exist."""
    if product_type not in _PRODUCT_TYPE_VALUES:
        raise MarketplaceError(f"unknown product type {product_type!r}", kind="invalid_product_type")
    return product_type


class AgentLite(WireModel):
    """The agent shape the MCP subscribe picker needs."""

    id: str
    name: str
    identity_status: Optional[str] = None
    entra_sp_id: Optional[str] = None
    sponsor_oid: Optional[str] = None


async def _caller_group_ids(principal: Principal) -> List[str]:
    """The caller's Entra group ids: the token's ``groups`` claim, else a Graph ``transitiveMemberOf``
    read for the groups-overage case. A failed Graph read degrades to no groups: the read grants
    nothing, and the service's own eligibility check still fails closed."""
    claim_groups = principal.raw_claims.get("groups") or []
    if claim_groups:
        return list(claim_groups)
    if principal.oid:
        from api.routes.mcp_server_grants import get_mcp_graph_service

        try:
            return await get_mcp_graph_service().list_member_group_ids(principal.oid)
        except GraphError:
            logger.warning("[marketplace] group membership read failed; continuing without groups")
            return []
    return []


@router.get("/agent-products", response_model=List[ProductCard])
async def list_agent_products(
    principal: Principal = RBACDepends(current_principal),
    _=RBACDepends(require_role(Role.VIEWER)),
):
    svc = get_marketplace_service()
    return svc.list_agent_products(caller_oid=_caller_id(principal))


@router.get("/mcp-products", response_model=List[ProductCard])
async def list_mcp_products(
    principal: Principal = RBACDepends(current_principal),
    _=RBACDepends(require_role(Role.VIEWER)),
):
    svc = get_marketplace_service()
    return svc.list_mcp_products(caller_oid=_caller_id(principal))


@router.get("/eligible-agents", response_model=List[AgentLite])
async def list_eligible_agents(
    principal: Principal = RBACDepends(current_principal),
    _=RBACDepends(require_role(Role.VIEWER)),
):
    svc = get_marketplace_service()
    group_ids = await _caller_group_ids(principal)
    agents = await svc.eligible_agents(caller_oid=_caller_id(principal), caller_group_ids=group_ids)
    return [
        AgentLite(
            id=getattr(a, "id", ""),
            name=getattr(a, "name", "") or "",
            identity_status=getattr(a, "identity_status", None),
            entra_sp_id=getattr(a, "entra_sp_id", None),
            sponsor_oid=getattr(a, "sponsor_oid", None),
        )
        for a in agents
    ]


@router.post("/subscriptions", response_model=Subscription, status_code=201)
async def create_subscription(
    req: SubscriptionCreate,
    principal: Principal = RBACDepends(current_principal),
    _=RBACDepends(require_role(Role.VIEWER)),
):
    if req.product_type is ProductType.MCP and not req.agent_id:
        raise MarketplaceError(
            f"MCP subscription to {req.product_id} names no agent", kind="agent_id_required"
        )

    svc = get_marketplace_service()
    group_ids = await _caller_group_ids(principal)
    return await svc.create_subscription(
        product_type=req.product_type.value,
        product_id=req.product_id,
        requester_oid=_caller_id(principal),
        requester_email=principal.email,
        agent_id=req.agent_id,
        message=req.message,
        caller_group_ids=group_ids,
    )


@router.get("/subscriptions", response_model=List[Subscription])
async def list_my_subscriptions(
    principal: Principal = RBACDepends(current_principal),
    _=RBACDepends(require_role(Role.VIEWER)),
):
    svc = get_marketplace_service()
    return svc.list_subscriptions(caller_oid=_caller_id(principal))


@router.get("/admin/subscriptions", response_model=List[Subscription])
async def list_admin_subscriptions(
    _=RBACDepends(require_role(Role.ADMIN)),
    status: Optional[str] = Query(default=None),
    product_type: Optional[str] = Query(default=None),
):
    svc = get_marketplace_service()
    return svc.list_subscriptions(caller_oid=None, status=status, product_type=product_type)


@router.post("/subscriptions/{subscription_id}/approve", response_model=Subscription)
async def approve_subscription(
    subscription_id: str,
    principal: Principal = RBACDepends(current_principal),
    _=RBACDepends(require_role(Role.ADMIN)),
):
    svc = get_marketplace_service()
    return await svc.approve(subscription_id, decided_by=_caller_id(principal))


@router.post("/subscriptions/{subscription_id}/reject", response_model=Subscription)
async def reject_subscription(
    subscription_id: str,
    req: RejectRequest,
    principal: Principal = RBACDepends(current_principal),
    _=RBACDepends(require_role(Role.ADMIN)),
):
    svc = get_marketplace_service()
    return svc.reject(subscription_id, decided_by=_caller_id(principal), reason=req.reason)


@router.post("/subscriptions/{subscription_id}/revoke", response_model=Subscription)
async def revoke_subscription(
    subscription_id: str,
    req: RevokeRequest,
    principal: Principal = RBACDepends(current_principal),
    _=RBACDepends(require_role(Role.ADMIN)),
):
    svc = get_marketplace_service()
    return await svc.revoke_subscription(subscription_id, decided_by=_caller_id(principal), reason=req.reason)


@router.post("/subscriptions/{subscription_id}/retry", response_model=Subscription)
async def retry_subscription(
    subscription_id: str,
    principal: Principal = RBACDepends(current_principal),
    _=RBACDepends(require_role(Role.ADMIN)),
):
    svc = get_marketplace_service()
    return await svc.retry_grant(subscription_id, decided_by=_caller_id(principal))


@router.get("/admin/metrics", response_model=MarketplaceMetrics)
async def get_metrics(_=RBACDepends(require_role(Role.ADMIN))):
    svc = get_marketplace_service()
    return svc.metrics()


@router.put("/listings/{product_type}/{product_id}", response_model=Listing)
async def set_listing(
    product_type: str,
    product_id: str,
    req: ListingUpdate,
    _=RBACDepends(require_role(Role.ADMIN)),
):
    ptype = _product_type_value(product_type)
    svc = get_marketplace_service()
    return svc.set_listing(
        product_type=ptype,
        product_id=product_id,
        available=req.available,
        auto_approve=req.auto_approve,
        pitch=req.pitch,
    )


@router.post("/publish-requests", response_model=PublishRequest, status_code=201)
async def create_publish_request(
    req: PublishRequestCreate,
    principal: Principal = RBACDepends(current_principal),
    ctx: TenantContext = RBACDepends(get_tenant_ctx),
    _=RBACDepends(require_role(Role.OPERATOR)),
):
    """Declare a datasheet for a product and request publication; the body's ``product_type`` selects
    the registry, and the resolved ``TenantContext`` scopes the lookup so a foreign tenant's product
    answers like a missing one."""
    svc = get_marketplace_service()
    return svc.create_publish_request(
        product_type=req.product_type.value,
        product_id=req.product_id,
        datasheet=req.datasheet,
        requester_oid=_caller_id(principal),
        requester_email=principal.email,
        ctx=ctx,
    )


@router.get("/publish-requests", response_model=List[PublishRequest])
async def list_publish_requests(
    _=RBACDepends(require_role(Role.ADMIN)),
    status: Optional[PublishRequestStatus] = Query(default=None),
):
    """The approvals queue; the enum-typed filter makes an unknown status a 422 before the service."""
    svc = get_marketplace_service()
    return svc.list_publish_requests(status=status.value if status else None)


@router.get("/publish-requests/product/{product_type}/{product_id}", response_model=PublishRequest)
async def get_publish_request_for_product(
    product_type: str,
    product_id: str,
    ctx: TenantContext = RBACDepends(get_tenant_ctx),
    _=RBACDepends(require_role(Role.OPERATOR)),
):
    """The product's publish record, scoped to the caller's tenant; a record the caller may not see and
    no record at all are the same 404."""
    ptype = _product_type_value(product_type)
    svc = get_marketplace_service()
    req = svc.get_publish_request_for_product(ptype, product_id, ctx=ctx)
    if req is None:
        raise MarketplaceError(f"no publish record visible for {ptype} {product_id}", kind="not_found")
    return req


@router.post("/publish-requests/{request_id}/approve", response_model=PublishRequest)
async def approve_publish_request(
    request_id: str,
    principal: Principal = RBACDepends(current_principal),
    _=RBACDepends(require_role(Role.ADMIN)),
):
    """Approve the declaration; the stored request's own ``product_type`` selects the registry written."""
    svc = get_marketplace_service()
    return await svc.approve_publish(request_id, decided_by=_caller_id(principal))


@router.post("/publish-requests/{request_id}/reject", response_model=PublishRequest)
async def reject_publish_request(
    request_id: str,
    req: RejectRequest,
    principal: Principal = RBACDepends(current_principal),
    _=RBACDepends(require_role(Role.ADMIN)),
):
    svc = get_marketplace_service()
    return svc.reject_publish(request_id, decided_by=_caller_id(principal), reason=req.reason)


@router.post("/products/{product_type}/{product_id}/unpublish", status_code=204)
async def unpublish_product(
    product_type: str,
    product_id: str,
    principal: Principal = RBACDepends(current_principal),
    _=RBACDepends(require_role(Role.ADMIN)),
):
    ptype = _product_type_value(product_type)
    svc = get_marketplace_service()
    await svc.unpublish(ptype, product_id, decided_by=_caller_id(principal))
    return Response(status_code=204)
