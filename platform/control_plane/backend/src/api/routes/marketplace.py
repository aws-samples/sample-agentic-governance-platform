"""Marketplace API routes (Epic 9, Task T5).

Consumer-facing marketplace: any signed-in VIEWER browses agent blueprints + real
gateway-MCP products and subscribes (on behalf of a provisioned agent for MCP); an
ADMIN approves/rejects/retries, reads metrics, and edits listings. On MCP approval the
service applies the REAL E7 agent→MCP grant (via the injectable ``grant_fn``).

Structural clone of ``mcp_servers.py``: the RBAC handler shape
(``principal: Principal = RBACDepends(current_principal)`` + ``_=RBACDepends(require_role(Role.X))``),
the lazy ``_svc`` / ``get_marketplace_service()`` singleton, and the FIXED-``detail`` 5xx
convention (never ``str(err)`` — the secret-leak guard).

RBAC: browse/subscribe/list-own = VIEWER; admin list-all/approve/reject/retry/metrics/
set-listing = ADMIN. Identity is threaded from the validated ``principal``
(``requester_oid``/``requester_email``/``decided_by``/``caller_oid``), NEVER from the body.

E33 adds the publish flow: a publisher (OPERATOR) declares a datasheet for their own product
and reads that product's record; the approvals queue + approve/reject/unpublish are ADMIN,
because a declared datasheet is an attestation that must be approved, not self-asserted.
Amendment 1 generalized every publish route from agents to BOTH product types: the record is
addressed by the ``(product_type, product_id)`` pair, never by a bare id.
"""

import logging
from typing import List, Optional

from fastapi import APIRouter, HTTPException, Query, Response
from fastapi import Depends as RBACDepends
from pydantic import BaseModel

from core.config import settings
from core.rbac import Principal, Role, current_principal, require_role
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
from services.marketplace_service import MarketplaceError, MarketplaceService
from services.tenant_resolver import TenantContext

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/marketplace", tags=["marketplace"])

_svc: Optional[MarketplaceService] = None

# MarketplaceError.kind → HTTP status. FIXED detail literals (the secret-leak guard) keyed
# off the same hint — never str(err), which could carry a Graph/registry resource message.
_ERROR_STATUS = {
    "not_found": 404,
    "conflict": 409,
    "bad_request": 422,
    "grant_failed": 502,
    # E33 publish flow. All four 409s are distinct *reasons* a publish cannot proceed;
    # they get their own literals so the FE can explain the block without a str(err) leak.
    "agent_not_approved": 409,
    "publish_conflict": 409,
    "illegal_publish_state": 409,
    "identity_not_provisioned": 409,
    "databricks_publish_unsupported": 409,
    "publish_write_failed": 502,
}
_ERROR_DETAIL = {
    "not_found": "Marketplace product or subscription not found",
    "conflict": "Marketplace operation conflicts with the current state",
    "bad_request": "Invalid marketplace request",
    "grant_failed": "Failed to apply the agent grant; see backend logs and retry",
    # Amendment 1 (C9): the publish kind NAMES are frozen (the service raises them), but the
    # literals are PRODUCT-NEUTRAL — the same codes now answer MCP publish attempts, and a
    # detail that says "agent" would be a lie on half the traffic.
    "agent_not_approved": "Product must be approved before it can be published to the marketplace",
    "publish_conflict": "A marketplace publish request for this product is already pending",
    "illegal_publish_state": "Publish request is not pending",
    # C10 — identity is a publish precondition for BOTH product types (provisioned =
    # identity_status "provisioned" + entra_sp_id + invoker_role_id). Same literal the
    # sibling grant routes use, so the FE has one string to recognize.
    "identity_not_provisioned": "identity is not provisioned",
    # E29×E33 seam — the subscription grant path has no Databricks ACL mirror yet, so a
    # published Databricks agent would sell access the workspace's own door refuses.
    "databricks_publish_unsupported": "Databricks-governed agents cannot be published to the marketplace yet",
    "publish_write_failed": "Failed to write the marketplace publication; see backend logs and retry",
}


def get_marketplace_service() -> MarketplaceService:
    """Lazy ``MarketplaceService`` singleton.

    Wires the real E5/E4 registry + E7 graph singletons (imported lazily INSIDE the
    builder to avoid a circular import at module load) and the configured DDB table
    (empty ``MARKETPLACE_TABLE_NAME`` ⇒ in-memory fallback). The default ``grant_fn`` is
    the shared ``apply_agent_mcp_grant`` baked into the service. Tests patch ``_svc``
    directly so this never runs against live AWS.
    """
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
            # E33/T3 — the real E6 user→agent grant pair, so approving an AGENT subscription
            # applies actual Entra access (and revoking it removes that access) rather than
            # only flipping a status. The MCP pair keeps its own baked-in defaults.
            agent_grant_fn=apply_user_agent_grant,
            agent_revoke_fn=revoke_user_agent_grant,
            # E24/T8 — display-only tenant_name resolution on MCP product cards.
            tenant_service=get_tenant_service(),
        )
    return _svc


async def get_tenant_ctx(
    principal: Principal = RBACDepends(current_principal),
) -> TenantContext:
    """Delegate to the ONE resolver-singleton accessor (E24/T8).

    ``users.py`` owns the lazy ``TenantResolver`` singleton; this thin re-export
    gives marketplace.py its own per-request ``tenant_ctx`` dependency WITHOUT a
    second resolver copy — tests patch ``api.routes.users._tenant_resolver`` and
    the catalog route here observes it. Imported lazily to avoid an import cycle
    at module load (mirrors ``mcp_servers.get_tenant_ctx``).
    """
    from api.routes.users import get_tenant_ctx as _users_get_tenant_ctx

    return await _users_get_tenant_ctx(principal)


def _raise_marketplace_error(err: MarketplaceError) -> None:
    """Map a MarketplaceError to an HTTPException with a FIXED detail literal."""
    status = _ERROR_STATUS.get(err.kind, 400)
    detail = _ERROR_DETAIL.get(err.kind, "Marketplace operation failed")
    raise HTTPException(status_code=status, detail=detail)


def _product_type_value(product_type: str) -> str:
    """Validate a PATH ``product_type`` and return its plain enum value.

    The publish paths are keyed by the ``(product_type, product_id)`` pair (C9), so an
    unknown type must fail HERE rather than reach the service — which would otherwise be
    asked to look up a registry that does not exist. Same FIXED 422 literal the listings
    route uses (that one coerces inside the service; a path pair has no body model to lean
    on). The plain value is what the service keys and serializes on."""
    try:
        return ProductType(product_type).value
    except ValueError:
        raise HTTPException(status_code=422, detail="Invalid marketplace product type")


class AgentLite(BaseModel):
    """The lightest agent shape the MCP subscribe picker needs (F1).

    The FE picker only needs ``id`` + ``name``; ``identity_status`` / ``entra_sp_id``
    / ``sponsor_oid`` are included so the same payload is reusable for display."""

    id: str
    name: str
    identity_status: Optional[str] = None
    entra_sp_id: Optional[str] = None
    sponsor_oid: Optional[str] = None


async def _caller_group_ids(principal: Principal) -> List[str]:
    """Resolve the caller's Entra group object-ids from the validated principal ONLY.

    Prefers the token's ``groups`` claim. When that is absent/empty (Entra "groups
    overage") and we have the caller's oid, falls back to a Graph
    ``transitiveMemberOf`` read; a fallback FAILURE degrades to [] (the read is
    non-granting — the picker simply omits group-granted agents, and the subscribe
    guard still fails closed via the service's grant check + the sponsor
    short-circuit, so a missing group list can never elevate a non-sponsor).

    Identity comes ONLY from the validated principal — never from a request body."""
    claim_groups = principal.raw_claims.get("groups") or []
    if claim_groups:
        return list(claim_groups)
    if principal.oid:
        from api.routes.mcp_server_grants import get_mcp_graph_service

        try:
            return await get_mcp_graph_service().list_member_group_ids(principal.oid)
        except Exception:
            return []
    return []


# --- Catalog (VIEWER) --------------------------------------------------------

@router.get("/agent-products", response_model=List[ProductCard])
async def list_agent_products(
    principal: Principal = RBACDepends(current_principal),
    _=RBACDepends(require_role(Role.VIEWER)),
):
    svc = get_marketplace_service()
    return svc.list_agent_products(caller_oid=principal.oid)


@router.get("/mcp-products", response_model=List[ProductCard])
async def list_mcp_products(
    principal: Principal = RBACDepends(current_principal),
    ctx: TenantContext = RBACDepends(get_tenant_ctx),
    _=RBACDepends(require_role(Role.VIEWER)),
):
    # Amendment 1: the MCP catalog is NOT tenant-scoped. Every card here is
    # marketplace-published, and publishing IS the act of making a product
    # cross-tenant discoverable, so scoping would make the admin's approval a
    # no-op for every other tenant (the tenant badge still says whose it is).
    # The resolved TenantContext is threaded only for signature stability with
    # the E24/T8 service contract — ``list_mcp_products`` accepts ``ctx`` but no
    # longer scopes by it. Agent blueprint products are symmetric — no ctx there.
    svc = get_marketplace_service()
    return svc.list_mcp_products(caller_oid=principal.oid, ctx=ctx)


@router.get("/eligible-agents", response_model=List[AgentLite])
async def list_eligible_agents(
    principal: Principal = RBACDepends(current_principal),
    _=RBACDepends(require_role(Role.VIEWER)),
):
    """The provisioned agents the caller may subscribe an MCP on behalf of (F1).

    Eligibility = sponsor OR granted (direct user / group). The caller's groups come
    from the validated token (``groups`` claim) with a Graph ``transitiveMemberOf``
    fallback for the overage case (a fallback failure degrades to [] — listing is
    read-only / non-granting). Identity is taken ONLY from the principal."""
    svc = get_marketplace_service()
    group_ids = await _caller_group_ids(principal)
    agents = await svc.eligible_agents(
        caller_oid=principal.oid, caller_group_ids=group_ids
    )
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


# --- Subscriptions (VIEWER) --------------------------------------------------

@router.post("/subscriptions", response_model=Subscription, status_code=201)
async def create_subscription(
    req: SubscriptionCreate,
    principal: Principal = RBACDepends(current_principal),
    _=RBACDepends(require_role(Role.VIEWER)),
):
    # MCP subscriptions require an agent to grant on behalf of (the picker enforces it
    # in the UI; this is the server-side guard) → 422 before touching the service.
    if req.product_type is ProductType.MCP and not req.agent_id:
        raise HTTPException(status_code=422, detail="agent_id is required for an MCP subscription")

    svc = get_marketplace_service()
    # F1: resolve the caller's group ids the SAME way as the picker so the
    # server-side eligibility guard matches what the UI offered. Identity + groups
    # come ONLY from the validated principal, NEVER from the body.
    group_ids = await _caller_group_ids(principal)
    try:
        return await svc.create_subscription(
            product_type=req.product_type.value,
            product_id=req.product_id,
            requester_oid=principal.oid,
            requester_email=principal.email,
            agent_id=req.agent_id,
            message=req.message,
            caller_group_ids=group_ids,
        )
    except MarketplaceError as err:
        _raise_marketplace_error(err)


@router.get("/subscriptions", response_model=List[Subscription])
async def list_my_subscriptions(
    principal: Principal = RBACDepends(current_principal),
    _=RBACDepends(require_role(Role.VIEWER)),
):
    svc = get_marketplace_service()
    return svc.list_subscriptions(caller_oid=principal.oid)


# --- Admin (ADMIN) -----------------------------------------------------------

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
    try:
        return await svc.approve(subscription_id, decided_by=(principal.oid or principal.email))
    except MarketplaceError as err:
        _raise_marketplace_error(err)


@router.post("/subscriptions/{subscription_id}/reject", response_model=Subscription)
async def reject_subscription(
    subscription_id: str,
    req: RejectRequest,
    principal: Principal = RBACDepends(current_principal),
    _=RBACDepends(require_role(Role.ADMIN)),
):
    svc = get_marketplace_service()
    try:
        return svc.reject(
            subscription_id, decided_by=(principal.oid or principal.email), reason=req.reason
        )
    except MarketplaceError as err:
        _raise_marketplace_error(err)


@router.post("/subscriptions/{subscription_id}/revoke", response_model=Subscription)
async def revoke_subscription(
    subscription_id: str,
    req: RevokeRequest,
    principal: Principal = RBACDepends(current_principal),
    _=RBACDepends(require_role(Role.ADMIN)),
):
    svc = get_marketplace_service()
    try:
        return await svc.revoke_subscription(
            subscription_id, decided_by=(principal.oid or principal.email), reason=req.reason
        )
    except MarketplaceError as err:
        _raise_marketplace_error(err)


@router.post("/subscriptions/{subscription_id}/retry", response_model=Subscription)
async def retry_subscription(
    subscription_id: str,
    principal: Principal = RBACDepends(current_principal),
    _=RBACDepends(require_role(Role.ADMIN)),
):
    svc = get_marketplace_service()
    try:
        return await svc.retry_grant(subscription_id, decided_by=(principal.oid or principal.email))
    except MarketplaceError as err:
        _raise_marketplace_error(err)


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
    svc = get_marketplace_service()
    try:
        return svc.set_listing(
            product_type=product_type,
            product_id=product_id,
            available=req.available,
            auto_approve=req.auto_approve,
            pitch=req.pitch,
        )
    except (MarketplaceError, ValueError):
        # A bad product_type (not "agent"/"mcp") fails ProductType coercion in the
        # service → 422 with a FIXED detail.
        raise HTTPException(status_code=422, detail="Invalid marketplace product type")


# --- Publish requests (E33) --------------------------------------------------
#
# A publisher (OPERATOR) DECLARES a datasheet for their own product; an ADMIN approves the
# declaration, which is what writes the marketplace block onto that product's envelope (the
# request's ``product_type`` selects the registry). The split is the point: the attestation
# is approved, never self-asserted. So creating and reading one's own request is OPERATOR,
# while list-all / approve / reject / unpublish are ADMIN. ``declared_by``/``decided_by``
# come from the validated principal — a body field of that name is ignored, exactly like the
# subscription mutations above.


@router.post("/publish-requests", response_model=PublishRequest, status_code=201)
async def create_publish_request(
    req: PublishRequestCreate,
    principal: Principal = RBACDepends(current_principal),
    ctx: TenantContext = RBACDepends(get_tenant_ctx),
    _=RBACDepends(require_role(Role.OPERATOR)),
):
    """Declare a datasheet for a product (agent or MCP server) and request publication.

    The resolved ``TenantContext`` scopes the product lookup in the service, so a
    foreign-tenant product_id is indistinguishable from a nonexistent one (404, same
    literal) — no cross-tenant existence oracle.

    Amendment 1 (C9): the body carries ``product_type``, which is what tells the service
    WHICH registry holds the product. It has no default and is not inferred from the id
    shape, so a bogus value is a 422 from the body model before this runs."""
    svc = get_marketplace_service()
    try:
        return svc.create_publish_request(
            product_type=req.product_type.value,
            product_id=req.product_id,
            datasheet=req.datasheet,
            # The sibling-mutation fallback idiom (``decided_by=(oid or email)``): under
            # dev-auth the principal has no oid, and PublishRequest.requested_by is a
            # non-Optional str, so a bare principal.oid would fail model validation.
            requester_oid=(principal.oid or principal.email),
            requester_email=principal.email,
            ctx=ctx,
        )
    except MarketplaceError as err:
        _raise_marketplace_error(err)


@router.get("/publish-requests", response_model=List[PublishRequest])
async def list_publish_requests(
    _=RBACDepends(require_role(Role.ADMIN)),
    status: Optional[PublishRequestStatus] = Query(default=None),
):
    """The admin approvals queue (optionally filtered by status).

    The filter is typed as the enum so an unknown value is rejected by FastAPI with a 422
    BEFORE the service is reached — an untyped ``str`` passed a bogus status straight
    through to the service, which raised and surfaced as a 500. The service takes the plain
    ``str`` value (the C3 pin), so the enum is unwrapped here."""
    svc = get_marketplace_service()
    return svc.list_publish_requests(status=status.value if status else None)


@router.get(
    "/publish-requests/product/{product_type}/{product_id}", response_model=PublishRequest
)
async def get_publish_request_for_product(
    product_type: str,
    product_id: str,
    ctx: TenantContext = RBACDepends(get_tenant_ctx),
    _=RBACDepends(require_role(Role.OPERATOR)),
):
    """The publisher-side read: this product's single publish record.

    Keyed by the ``(product_type, product_id)`` PAIR (C9) — this REPLACES the agent-only
    path, because the two registries' id spaces are not distinguishable by shape and a
    product-type-blind read would answer from whichever one happened to match.

    The resolved ``TenantContext`` is threaded so the service scopes the read to the
    caller's tenant, exactly like the create path. A record the caller may not see is
    returned as None and answered with the SAME 404 literal as "no record at all", so the
    two cases are byte-identical on the wire."""
    ptype = _product_type_value(product_type)
    svc = get_marketplace_service()
    try:
        req = svc.get_publish_request_for_product(ptype, product_id, ctx=ctx)
    except MarketplaceError as err:
        _raise_marketplace_error(err)
    if req is None:
        raise HTTPException(status_code=404, detail=_ERROR_DETAIL["not_found"])
    return req


@router.post("/publish-requests/{request_id}/approve", response_model=PublishRequest)
async def approve_publish_request(
    request_id: str,
    principal: Principal = RBACDepends(current_principal),
    _=RBACDepends(require_role(Role.ADMIN)),
):
    """Approve the declaration — the service writes the product's marketplace block.

    The stored request's own ``product_type`` selects WHICH registry's ``persist_marketplace``
    receives the envelope write (C9), so this route stays id-only: re-stating the type here
    would let a caller aim an approval at the wrong registry.

    A failed envelope write leaves the request PENDING (retryable) and surfaces as 502."""
    svc = get_marketplace_service()
    try:
        return await svc.approve_publish(
            request_id, decided_by=(principal.oid or principal.email)
        )
    except MarketplaceError as err:
        _raise_marketplace_error(err)


@router.post("/publish-requests/{request_id}/reject", response_model=PublishRequest)
async def reject_publish_request(
    request_id: str,
    req: RejectRequest,
    principal: Principal = RBACDepends(current_principal),
    _=RBACDepends(require_role(Role.ADMIN)),
):
    svc = get_marketplace_service()
    try:
        return svc.reject_publish(
            request_id, decided_by=(principal.oid or principal.email), reason=req.reason
        )
    except MarketplaceError as err:
        _raise_marketplace_error(err)


@router.post("/products/{product_type}/{product_id}/unpublish", status_code=204)
async def unpublish_product(
    product_type: str,
    product_id: str,
    principal: Principal = RBACDepends(current_principal),
    _=RBACDepends(require_role(Role.ADMIN)),
):
    """Delist a published product (agent or MCP server).

    Keyed by the ``(product_type, product_id)`` pair (C9) — REPLACES the agent-only path.
    The declared block survives with ``published=False`` (the service's contract), so the
    attestation history outlives the delisting."""
    ptype = _product_type_value(product_type)
    svc = get_marketplace_service()
    try:
        await svc.unpublish(ptype, product_id, decided_by=(principal.oid or principal.email))
    except MarketplaceError as err:
        _raise_marketplace_error(err)
    return Response(status_code=204)
