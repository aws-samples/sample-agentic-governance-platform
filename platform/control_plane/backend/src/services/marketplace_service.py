"""The marketplace: subscriptions, listings and publish requests over one DynamoDB table.

Persistence is :class:`services.marketplace_store.MarketplaceStore` over one table, in memory when no
table name is configured. The agent and MCP registries are injected and read for the catalog and for
product resolution. The Entra grants are applied and revoked by the functions in
:mod:`services.marketplace_grants`, through injectable functions and never re-implemented: an MCP
product confers the agent-to-MCP app-role assignment (``grant_fn`` / ``revoke_fn``), an agent product the
user-to-agent one (``agent_grant_fn`` / ``agent_revoke_fn``). The service never writes
``appRoleAssignedTo`` itself and never logs a token or a secret; identity arrives from the validated
principal through the route.

A published card is marketplace-wide: neither catalog filters by tenant, because publishing is the act
of making a product discoverable across tenants, and the subscribe gate reads the same predicate the
catalogs do. Publish requests are the one tenant-scoped read: a foreign tenant's product looks absent.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import anyio.to_thread

from core.state_store import ABSENT, StateStore
from models.agent import GOVERNABLE_PLATFORMS, Platform
from models.marketplace import (
    Datasheet,
    Listing,
    MarketplaceMetrics,
    MarketplacePublication,
    ProductCard,
    ProductType,
    ProductTypeCounts,
    PublishRequest,
    PublishRequestStatus,
    Subscription,
    SubscriptionStatus,
    TopProduct,
)
from services import marketplace_grants
from services.agent_mcp_grant import apply_agent_mcp_grant, revoke_agent_mcp_grant
from services.agent_user_grant import apply_user_agent_grant, revoke_user_agent_grant
from services.marketplace_errors import _DATABRICKS_PUBLISH_UNSUPPORTED, _REAPPROVAL_ERROR, MarketplaceError
from services.marketplace_store import _ACTIVE_STATUSES, MarketplaceStore, _listing_sk
from services.platform_registry import TenantPlatform, declaration_for
from services.tenant_resolver import TenantContext, visible

# Decision: docs/adr/backend-marketplace-publication-is-cross-tenant.md

logger = logging.getLogger(__name__)

_PUBLISH_WRITE_ERROR = "Marketplace publication write failed; see backend logs and retry."
_PUBLISH_WRITE_RAISE = "Publish write failed"
_IDENTITY_NOT_PROVISIONED = "identity is not provisioned"
_PUBLISH_UNSUPPORTED_PLATFORM = "this agent's platform does not offer marketplace publication yet"
_UNKNOWN_PRODUCT = "Unknown product"
_PRODUCT_NOT_APPROVED = "Product is not approved for marketplace publication"
_PUBLISH_PENDING_CONFLICT = "A publish request for this product is already pending"
_SUBSCRIPTION_EXISTS = "Subscription id already exists"
_PRODUCT_NOT_PUBLISHED = "Product is not published"

_PUBLISH_PENDING = frozenset({PublishRequestStatus.PENDING.value})
_PUBLISH_DECIDED_OR_ABSENT = frozenset(
    {ABSENT, PublishRequestStatus.APPROVED.value, PublishRequestStatus.REJECTED.value}
)

GrantFn = Callable[..., Awaitable[Any]]


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _new_id() -> str:
    return "mkt-" + uuid.uuid4().hex[:10]


def _new_publish_id() -> str:
    return "pub-" + uuid.uuid4().hex[:10]


def _enum_value(value: Any) -> Any:
    return getattr(value, "value", value)


def _identity_is_provisioned(product) -> bool:
    """``identity_status == "provisioned"`` with both ``entra_sp_id`` and ``invoker_role_id``: the three
    fields an app-role assignment needs, spelled the same on ``Agent`` and ``McpServer``. A record
    missing a field reads as not provisioned."""
    return (
        getattr(product, "identity_status", None) == "provisioned"
        and bool(getattr(product, "entra_sp_id", None))
        and bool(getattr(product, "invoker_role_id", None))
    )


def _publish_platform_refusal(product) -> Optional[MarketplaceError]:
    """The refusal to raise when the product's platform does not offer marketplace publish, else None.

    Read from the platform declaration (``declaration_for``), so a new platform is unpublishable until
    its declaration says otherwise. An inventory-only or unparseable label (an MCP product has none)
    has no governed publish path and is left alone. Databricks keeps its own kind, which the frontend
    maps; every other platform gets the generic one.
    """
    try:
        tenant_platform = GOVERNABLE_PLATFORMS.get(Platform(getattr(product, "platform", None)))
    except ValueError:
        return None
    if tenant_platform is None:
        return None
    try:
        offered = declaration_for(tenant_platform).marketplace_publish
    except ValueError:
        offered = False
    if offered:
        return None
    if tenant_platform is TenantPlatform.DATABRICKS:
        return MarketplaceError(_DATABRICKS_PUBLISH_UNSUPPORTED, kind="databricks_publish_unsupported")
    return MarketplaceError(_PUBLISH_UNSUPPORTED_PLATFORM, kind="publish_unsupported_platform")


class MarketplaceService:
    def __init__(
        self,
        *,
        table_name: str = "",
        region: str | None = None,
        mcp_registry: Any = None,
        agent_registry: Any = None,
        mcp_graph: Any = None,
        grant_fn: Optional[GrantFn] = None,
        revoke_fn: Optional[GrantFn] = None,
        agent_grant_fn: Optional[GrantFn] = None,
        agent_revoke_fn: Optional[GrantFn] = None,
        tenant_service: Any = None,
    ) -> None:
        self.table_name = table_name
        self.region = region
        self._mcp_registry = mcp_registry
        self._agent_registry = agent_registry
        self._mcp_graph = mcp_graph
        self._tenant_service = tenant_service
        self._grant_fn: GrantFn = grant_fn or apply_agent_mcp_grant
        self._revoke_fn: GrantFn = revoke_fn or revoke_agent_mcp_grant
        self._agent_grant_fn: GrantFn = agent_grant_fn or apply_user_agent_grant
        self._agent_revoke_fn: GrantFn = agent_revoke_fn or revoke_user_agent_grant
        self._store = StateStore(table_name, region=region)

    @property
    def _store(self) -> StateStore:
        return self._rows._store

    @_store.setter
    def _store(self, state_store: StateStore) -> None:
        self._rows = MarketplaceStore(state_store)

    def list_agent_products(self, *, caller_oid: str) -> List[ProductCard]:
        """One card per agent that carries an approved, published ``marketplace`` block and is still
        lifecycle-approved. The datasheet is the publisher's declaration as approved by an admin; a
        listing's pitch overrides the declared one; ``consumers`` counts distinct requesters with an
        active subscription. Not filtered by tenant (module docstring)."""
        listings = self._rows.load_listings()
        my_subs = self._rows.latest_sub_by_product(caller_oid, ProductType.AGENT)
        tenant_names = self._tenant_names()
        consumers_live = self._agent_consumers_by_product()
        cards: List[ProductCard] = []
        for agent in self._agent_registry.list():
            publication = getattr(agent, "marketplace", None)
            if publication is None or not publication.published:
                continue
            if getattr(agent, "lifecycle_state", None) != "approved":
                continue
            datasheet = publication.datasheet
            pid = agent.id
            listing = listings.get(_listing_sk(ProductType.AGENT.value, pid))
            mine = my_subs.get(pid)
            tenant_id = getattr(agent, "tenant_id", None)
            cards.append(
                ProductCard(
                    product_type=ProductType.AGENT,
                    product_id=pid,
                    name=agent.name,
                    pitch=(listing.pitch if listing and listing.pitch else datasheet.pitch),
                    capabilities=[],
                    available=(listing.available if listing else True),
                    auto_approve=(listing.auto_approve if listing else False),
                    business_unit=getattr(agent, "business_unit", None),
                    data_classification=datasheet.data_classification,
                    owner_team=datasheet.owner_team,
                    support_contact=datasheet.support_contact,
                    region=datasheet.region,
                    version=datasheet.version,
                    sla_tier=datasheet.sla_tier,
                    support_hours=datasheet.support_hours,
                    compliance=list(datasheet.compliance),
                    guardrails=list(datasheet.guardrails),
                    declared_at=publication.declared_at.isoformat(),
                    consumers=consumers_live.get(pid, 0),
                    tenant_id=tenant_id,
                    tenant_name=tenant_names.get(tenant_id) if tenant_id else None,
                    published=True,
                    my_status=(mine.status if mine else None),
                    my_subscription_id=(mine.id if mine else None),
                )
            )
        return cards

    def _agent_consumers_by_product(self) -> Dict[str, int]:
        """Distinct requesters with a pending or approved subscription, per agent product."""
        teams: Dict[str, set] = {}
        for s in self._rows.load_subs():
            if s.product_type is not ProductType.AGENT:
                continue
            if s.status not in _ACTIVE_STATUSES:
                continue
            teams.setdefault(s.product_id, set()).add(s.requester_oid)
        return {pid: len(oids) for pid, oids in teams.items()}

    def list_mcp_products(self, *, caller_oid: str) -> List[ProductCard]:
        """One card per MCP server that carries an approved, published ``marketplace`` block and is
        still lifecycle-approved; ``kind`` is display only. The registry's classification, region and
        version stay the card's, the rest of the datasheet is the declaration. The owner email is not
        projected: a published card is marketplace-wide and the declared ``support_contact`` is the
        address the publisher chose to expose."""
        listings = self._rows.load_listings()
        my_subs = self._rows.latest_sub_by_product(caller_oid, ProductType.MCP)
        tenant_names = self._tenant_names()
        cards: List[ProductCard] = []
        for mcp in self._mcp_registry.list():
            publication = getattr(mcp, "marketplace", None)
            if publication is None or not publication.published:
                continue
            if getattr(mcp, "lifecycle_state", None) != "approved":
                continue
            datasheet = publication.datasheet
            tenant_id = getattr(mcp, "tenant_id", None)
            pid = mcp.id
            listing = listings.get(_listing_sk(ProductType.MCP.value, pid))
            mine = my_subs.get(pid)
            cards.append(
                ProductCard(
                    product_type=ProductType.MCP,
                    product_id=pid,
                    name=mcp.name,
                    pitch=(listing.pitch if listing and listing.pitch else datasheet.pitch),
                    capabilities=[],
                    available=(listing.available if listing else True),
                    auto_approve=(listing.auto_approve if listing else False),
                    kind=_enum_value(getattr(mcp, "kind", None)),
                    business_unit=getattr(mcp, "business_unit", None),
                    data_classification=_enum_value(getattr(mcp, "data_classification", None)),
                    region=getattr(mcp, "region", None),
                    version=getattr(mcp, "version", None),
                    created_at=(mcp.created_at.isoformat() if getattr(mcp, "created_at", None) else None),
                    updated_at=(mcp.updated_at.isoformat() if getattr(mcp, "updated_at", None) else None),
                    owner_team=datasheet.owner_team,
                    support_contact=datasheet.support_contact,
                    sla_tier=datasheet.sla_tier,
                    support_hours=datasheet.support_hours,
                    compliance=list(datasheet.compliance),
                    guardrails=list(datasheet.guardrails),
                    declared_at=publication.declared_at.isoformat(),
                    tenant_id=tenant_id,
                    tenant_name=tenant_names.get(tenant_id) if tenant_id else None,
                    published=True,
                    shared=bool(getattr(mcp, "shared", False)),
                    my_status=(mine.status if mine else None),
                    my_subscription_id=(mine.id if mine else None),
                    my_agent_id=(mine.agent_id if mine else None),
                )
            )
        return cards

    def _tenant_names(self) -> Dict[str, str]:
        """``tenant_id`` to tenant name from one ``tenant_service.list()`` per catalog read; no service
        or a failed read gives ``{}``, because the badge name must never break the catalog."""
        if self._tenant_service is None:
            return {}
        try:
            return {t.id: t.name for t in self._tenant_service.list()}
        except Exception:
            logger.warning("[marketplace] tenant name resolution failed", exc_info=True)
            return {}

    async def create_subscription(
        self,
        *,
        product_type,
        product_id: str,
        requester_oid: str,
        requester_email: Optional[str] = None,
        agent_id: Optional[str] = None,
        message: Optional[str] = None,
        caller_group_ids: Optional[List[str]] = None,
    ) -> Subscription:
        """A PENDING subscription, or the caller's existing active one for the same key. An MCP
        subscription names a provisioned agent the caller sponsors or is granted on (the sponsor check
        runs before any Graph read; a failed Graph read propagates, fail closed). An auto-approving
        listing applies the grant inline."""
        ptype = ProductType(product_type)
        product_name = self._resolve_product_name(ptype, product_id)

        agent = None
        agent_name = None
        agent_sp_id = None
        if ptype is ProductType.MCP:
            if not agent_id:
                raise MarketplaceError("agent_id is required for an MCP subscription", kind="bad_request")
            agent = self._agent_registry.get(agent_id)
            if agent is None:
                raise MarketplaceError("Unknown agent", kind="not_found")
            if getattr(agent, "identity_status", None) != "provisioned" or not getattr(
                agent, "entra_sp_id", None
            ):
                raise MarketplaceError("Agent is not provisioned for MCP access", kind="conflict")
            if not await self._is_agent_eligible(
                agent, caller_oid=requester_oid, caller_group_ids=caller_group_ids or []
            ):
                raise MarketplaceError(
                    "you may only subscribe on behalf of an agent you sponsor or are granted access to",
                    kind="conflict",
                )
            agent_name = getattr(agent, "name", None)
            agent_sp_id = getattr(agent, "entra_sp_id", None)

        existing = self._rows.active_sub_for(ptype, product_id, requester_oid, agent_id)
        if existing is not None:
            return existing

        now = _now()
        sub = Subscription(
            id=_new_id(),
            product_type=ptype,
            product_id=product_id,
            product_name=product_name,
            agent_id=agent_id if ptype is ProductType.MCP else None,
            agent_name=agent_name,
            agent_sp_id=agent_sp_id,
            requester_oid=requester_oid,
            requester_email=requester_email,
            message=message,
            status=SubscriptionStatus.PENDING,
            created_at=now,
            updated_at=now,
        )

        sub.auto_approved = self._effective_auto_approve(ptype, product_id)
        if not self._rows.create(sub):
            raise MarketplaceError(_SUBSCRIPTION_EXISTS, kind="conflict")
        if not sub.auto_approved:
            return sub
        if ptype is ProductType.AGENT:
            return await marketplace_grants.apply_agent_grant(self, sub, decided_by="auto")
        return await marketplace_grants.apply_grant(self, sub, decided_by="auto")

    async def eligible_agents(self, *, caller_oid: Optional[str], caller_group_ids: List[str]) -> List:
        """The provisioned agents the caller may subscribe an MCP on behalf of: the ones the caller
        sponsors or is granted on, directly or through a group in ``caller_group_ids``. Deduplicated,
        sorted by name; shares ``_is_agent_eligible`` with the subscribe guard."""
        agents = self._agent_registry.list()
        eligible: List = []
        seen: set = set()
        for agent in agents:
            if getattr(agent, "identity_status", None) != "provisioned" or not getattr(
                agent, "entra_sp_id", None
            ):
                continue
            if await self._is_agent_eligible(agent, caller_oid=caller_oid, caller_group_ids=caller_group_ids):
                aid = getattr(agent, "id", None)
                if aid in seen:
                    continue
                seen.add(aid)
                eligible.append(agent)
        eligible.sort(key=lambda a: (getattr(a, "name", "") or "").lower())
        return eligible

    async def _is_agent_eligible(
        self, agent, *, caller_oid: Optional[str], caller_group_ids: List[str]
    ) -> bool:
        """Sponsor first, a local compare that needs no Graph; otherwise a User assignment for the
        caller or a Group assignment for one of the caller's groups on the agent's SP. A failed Graph
        read propagates."""
        if caller_oid is not None and getattr(agent, "sponsor_oid", None) == caller_oid:
            return True

        agent_sp_id = getattr(agent, "entra_sp_id", None)
        if not agent_sp_id:
            return False

        assignments = await self._mcp_graph.list_assignments(agent_sp_id)
        group_ids = set(caller_group_ids or [])
        for a in assignments:
            ptype = a.get("principalType")
            pid = a.get("principalId")
            if ptype == "User" and caller_oid is not None and pid == caller_oid:
                return True
            if ptype == "Group" and pid in group_ids:
                return True
        return False

    def list_subscriptions(
        self,
        *,
        caller_oid: Optional[str] = None,
        status=None,
        product_type=None,
    ) -> List[Subscription]:
        """Pending first, then newest first; ``caller_oid=None`` is the admin's unscoped list."""
        subs = self._rows.load_subs()
        if caller_oid is not None:
            subs = [s for s in subs if s.requester_oid == caller_oid]
        if status is not None:
            want = SubscriptionStatus(status)
            subs = [s for s in subs if s.status == want]
        if product_type is not None:
            want_t = ProductType(product_type)
            subs = [s for s in subs if s.product_type == want_t]
        subs.sort(
            key=lambda s: (0 if s.status == SubscriptionStatus.PENDING else 1, _neg_ts(s.created_at)),
        )
        return subs

    async def approve(self, subscription_id: str, *, decided_by: str) -> Subscription:
        sub = self._rows.get_sub(subscription_id)
        if sub is None:
            raise MarketplaceError("Unknown subscription", kind="not_found")
        if sub.status != SubscriptionStatus.PENDING:
            raise MarketplaceError("Subscription is not pending", kind="conflict")
        if sub.product_type is ProductType.AGENT:
            return await marketplace_grants.apply_agent_grant(self, sub, decided_by=decided_by)
        return await marketplace_grants.apply_grant(self, sub, decided_by=decided_by)

    def reject(self, subscription_id: str, *, decided_by: str, reason: Optional[str] = None) -> Subscription:
        sub = self._rows.get_sub(subscription_id)
        if sub is None:
            raise MarketplaceError("Unknown subscription", kind="not_found")
        if sub.status != SubscriptionStatus.PENDING:
            raise MarketplaceError("Subscription is not pending", kind="conflict")
        sub.status = SubscriptionStatus.REJECTED
        sub.decided_by = decided_by
        sub.decided_at = _now()
        sub.decision_reason = reason
        sub.updated_at = _now()
        self._rows.save(sub, after={SubscriptionStatus.PENDING.value})
        return sub

    async def retry_grant(self, subscription_id: str, *, decided_by: str) -> Subscription:
        """Re-run the grant of a FAILED subscription; both apply paths are idempotent."""
        sub = self._rows.get_sub(subscription_id)
        if sub is None:
            raise MarketplaceError("Unknown subscription", kind="not_found")
        if sub.status != SubscriptionStatus.FAILED:
            raise MarketplaceError("Subscription is not in a failed state", kind="conflict")
        if sub.product_type is ProductType.AGENT:
            return await marketplace_grants.apply_agent_grant(self, sub, decided_by=decided_by)
        return await marketplace_grants.apply_grant(self, sub, decided_by=decided_by)

    async def revoke_subscription(
        self, subscription_id: str, *, decided_by: str, reason: Optional[str] = None
    ) -> Subscription:
        """Revoke an APPROVED subscription: tear its grant down, then persist REVOKED with the revoke
        audit beside the untouched approval audit. A grant already gone from Graph counts as torn
        down; a Graph failure persists nothing and raises ``grant_failed`` so the admin retries."""
        sub = self._rows.get_sub(subscription_id)
        if sub is None:
            raise MarketplaceError("Unknown subscription", kind="not_found")
        if sub.status != SubscriptionStatus.APPROVED:
            raise MarketplaceError("Subscription is not approved", kind="conflict")

        if sub.product_type is ProductType.AGENT:
            await marketplace_grants.revoke_agent_grant(self, sub)
        if sub.product_type is ProductType.MCP:
            await marketplace_grants.revoke_mcp_grant(self, sub)

        sub.status = SubscriptionStatus.REVOKED
        sub.revoked_by = decided_by
        sub.revoked_at = _now()
        sub.revoke_reason = reason
        sub.updated_at = _now()
        self._rows.save(sub, after={SubscriptionStatus.APPROVED.value})
        return sub

    def set_listing(
        self,
        *,
        product_type,
        product_id: str,
        available: Optional[bool] = None,
        auto_approve: Optional[bool] = None,
        pitch: Optional[str] = None,
    ) -> Listing:
        """Set the provided fields alone, so two admins setting different fields both land; the
        answer is the row as written."""
        ptype = ProductType(product_type)
        provided = {"available": available, "auto_approve": auto_approve, "pitch": pitch}
        attrs = {name: value for name, value in provided.items() if value is not None}
        return self._rows.save_listing(ptype, product_id, attrs)

    def create_publish_request(
        self,
        *,
        product_type,
        product_id: str,
        datasheet: Datasheet,
        requester_oid: str,
        requester_email: Optional[str],
        ctx: TenantContext,
    ) -> PublishRequest:
        """A publisher declares a datasheet for one of their products; the request is PENDING until an
        admin decides. ``product_type`` selects the registry and is stamped on the record.

        Guards, in order: the product exists and is visible to ``ctx`` (a foreign tenant's product is
        ``not_found``, the same answer as a missing one; ``shared`` confers no publish right), its
        lifecycle is approved, its platform offers publish, its identity is provisioned, and no PENDING
        request exists for it. A decided request is overwritten: one record per product.
        """
        ptype = ProductType(product_type)
        product = self._get_product(ptype, product_id)
        if product is None or not visible(ctx, getattr(product, "tenant_id", None)):
            raise MarketplaceError(_UNKNOWN_PRODUCT, kind="not_found")
        if getattr(product, "lifecycle_state", None) != "approved":
            raise MarketplaceError(_PRODUCT_NOT_APPROVED, kind="agent_not_approved")
        if ptype is ProductType.AGENT:
            refusal = _publish_platform_refusal(product)
            if refusal is not None:
                raise refusal
        if not _identity_is_provisioned(product):
            raise MarketplaceError(_IDENTITY_NOT_PROVISIONED, kind="identity_not_provisioned")

        existing = self._rows.get_publish_for_product(ptype, product_id)
        if existing is not None and existing.status is PublishRequestStatus.PENDING:
            raise MarketplaceError(_PUBLISH_PENDING_CONFLICT, kind="publish_conflict")

        now = _now()
        req = PublishRequest(
            id=_new_publish_id(),
            product_type=ptype,
            product_id=product_id,
            product_name=product.name,
            tenant_id=getattr(product, "tenant_id", None),
            datasheet=datasheet,
            status=PublishRequestStatus.PENDING,
            requested_by=requester_oid,
            requested_by_email=requester_email,
            created_at=now,
            updated_at=now,
        )
        self._rows.save_publish(req, after=_PUBLISH_DECIDED_OR_ABSENT, refused="publish_conflict")
        return req

    def list_publish_requests(self, status: Optional[str] = None) -> List[PublishRequest]:
        """Every publish request, pending first then newest first, optionally filtered by status."""
        reqs = list(self._rows.load_publish_requests().values())
        if status is not None:
            want = PublishRequestStatus(status)
            reqs = [r for r in reqs if r.status == want]
        reqs.sort(
            key=lambda r: (
                0 if r.status is PublishRequestStatus.PENDING else 1,
                _neg_ts(r.created_at),
            ),
        )
        return reqs

    def get_publish_request_for_product(
        self, product_type, product_id: str, *, ctx: TenantContext
    ) -> Optional[PublishRequest]:
        """The product's one publish record, or None when none exists or when the record's own
        ``tenant_id`` is not visible to ``ctx``: a foreign tenant's record looks absent. The record's
        stamped tenant is read, not the product's, so the scope survives the product moving."""
        req = self._rows.get_publish_for_product(ProductType(product_type), product_id)
        if req is None:
            return None
        if not visible(ctx, req.tenant_id):
            return None
        return req

    async def approve_publish(self, request_id: str, decided_by: str) -> PublishRequest:
        """Write the publication onto the product's envelope in the registry the request names, then
        flip the request APPROVED.

        The create-time gates (lifecycle, platform, identity) are re-asserted on the current record,
        because a request outlives them; a product that no longer resolves falls through to the
        registry write. The write goes first and the request flips only once it landed: a registry
        failure leaves the request PENDING with ``_PUBLISH_WRITE_ERROR`` persisted and raises
        ``publish_write_failed``. The catch is broad on purpose: both registries' not-found errors and
        a raw ``ClientError`` from a record deleted between the registry's read and its write must
        all leave the request retryable. A write that landed on a record no longer approved leaves the
        request PENDING with ``_REAPPROVAL_ERROR`` and raises ``publish_needs_reapproval``. Every
        write of the request is conditioned on the row still being PENDING, so two admins deciding
        at once yield one decision and one ``illegal_publish_state``.
        """
        req = self._rows.get_publish_request(request_id)
        if req is None:
            raise MarketplaceError("Unknown publish request", kind="not_found")
        if req.status is not PublishRequestStatus.PENDING:
            raise MarketplaceError("Publish request is not pending", kind="illegal_publish_state")

        product = self._get_product(req.product_type, req.product_id)
        if product is not None:
            if getattr(product, "lifecycle_state", None) != "approved":
                raise MarketplaceError(_PRODUCT_NOT_APPROVED, kind="agent_not_approved")
            if req.product_type is ProductType.AGENT:
                refusal = _publish_platform_refusal(product)
                if refusal is not None:
                    raise refusal
            if not _identity_is_provisioned(product):
                raise MarketplaceError(_IDENTITY_NOT_PROVISIONED, kind="identity_not_provisioned")

        publication = MarketplacePublication(
            published=True,
            datasheet=req.datasheet,
            declared_by=decided_by,
            declared_at=_now(),
        )
        try:
            written = await anyio.to_thread.run_sync(
                self._registry_for(req.product_type).persist_marketplace,
                req.product_id,
                publication,
            )
        except Exception:
            logger.exception(
                "[marketplace] publication write failed for request %s (%s %s)",
                req.id,
                req.product_type.value,
                req.product_id,
            )
            req.error = _PUBLISH_WRITE_ERROR
            req.updated_at = _now()
            self._rows.save_publish(req, after=_PUBLISH_PENDING, refused="illegal_publish_state")
            raise MarketplaceError(_PUBLISH_WRITE_RAISE, kind="publish_write_failed") from None

        if getattr(written, "lifecycle_state", None) != "approved":
            logger.warning(
                "[marketplace] publish request %s: %s %s is no longer APPROVED after the "
                "publication write; request left pending",
                req.id,
                req.product_type.value,
                req.product_id,
            )
            req.error = _REAPPROVAL_ERROR
            req.updated_at = _now()
            self._rows.save_publish(req, after=_PUBLISH_PENDING, refused="illegal_publish_state")
            raise MarketplaceError(_REAPPROVAL_ERROR, kind="publish_needs_reapproval")

        req.status = PublishRequestStatus.APPROVED
        req.decided_by = decided_by
        req.decided_at = _now()
        req.error = None
        req.updated_at = _now()
        self._rows.save_publish(req, after=_PUBLISH_PENDING, refused="illegal_publish_state")
        return req

    def reject_publish(self, request_id: str, decided_by: str, reason: Optional[str]) -> PublishRequest:
        """Reject a PENDING declaration; no envelope is written, so nothing can fail."""
        req = self._rows.get_publish_request(request_id)
        if req is None:
            raise MarketplaceError("Unknown publish request", kind="not_found")
        if req.status is not PublishRequestStatus.PENDING:
            raise MarketplaceError("Publish request is not pending", kind="illegal_publish_state")
        req.status = PublishRequestStatus.REJECTED
        req.decided_by = decided_by
        req.decided_at = _now()
        req.decision_reason = reason
        req.updated_at = _now()
        self._rows.save_publish(req, after=_PUBLISH_PENDING, refused="illegal_publish_state")
        return req

    async def unpublish(self, product_type, product_id: str, decided_by: str) -> None:
        """Delist a published product: the block stays with ``published=False``, so the declared
        datasheet and its attestation outlive the delisting. An absent or already unpublished product
        is ``not_found``, so a second click writes nothing."""
        ptype = ProductType(product_type)
        product = self._get_product(ptype, product_id)
        publication = getattr(product, "marketplace", None) if product is not None else None
        if publication is None or not publication.published:
            raise MarketplaceError(_PRODUCT_NOT_PUBLISHED, kind="not_found")

        try:
            await anyio.to_thread.run_sync(
                self._registry_for(ptype).persist_marketplace,
                product_id,
                publication.model_copy(update={"published": False}),
            )
        except Exception:
            logger.exception(
                "[marketplace] unpublish write failed for %s %s (requested by %s)",
                ptype.value,
                product_id,
                decided_by,
            )
            raise MarketplaceError(_PUBLISH_WRITE_RAISE, kind="publish_write_failed") from None

    def _registry_for(self, product_type):
        """The registry that owns a product type; both expose ``get``, ``list``, ``persist_marketplace``."""
        return self._agent_registry if ProductType(product_type) is ProductType.AGENT else self._mcp_registry

    def _get_product(self, product_type, product_id: str):
        return self._registry_for(product_type).get(product_id)

    def metrics(self) -> MarketplaceMetrics:
        subs = self._rows.load_subs()
        total = len(subs)
        pending = sum(1 for s in subs if s.status == SubscriptionStatus.PENDING)
        approved = sum(1 for s in subs if s.status == SubscriptionStatus.APPROVED)
        rejected = sum(1 for s in subs if s.status == SubscriptionStatus.REJECTED)
        failed = sum(1 for s in subs if s.status == SubscriptionStatus.FAILED)
        revoked = sum(1 for s in subs if s.status == SubscriptionStatus.REVOKED)
        denom = approved + rejected
        approval_rate = (approved / denom) if denom else 0.0

        by_type = ProductTypeCounts(
            agent=sum(1 for s in subs if s.product_type == ProductType.AGENT),
            mcp=sum(1 for s in subs if s.product_type == ProductType.MCP),
        )
        counts: Dict[str, TopProduct] = {}
        for s in subs:
            entry = counts.setdefault(
                s.product_id, TopProduct(product_id=s.product_id, product_name=s.product_name, count=0)
            )
            entry.count += 1
        top_products = sorted(counts.values(), key=lambda e: e.count, reverse=True)[:5]

        return MarketplaceMetrics(
            total=total,
            pending=pending,
            approved=approved,
            rejected=rejected,
            failed=failed,
            revoked=revoked,
            approval_rate=approval_rate,
            by_type=by_type,
            top_products=top_products,
        )

    def _resolve_product_name(self, ptype: ProductType, product_id: str) -> str:
        """The product's display name, after the gate a subscription is born behind: the record exists
        and is an offered product (``_product_is_offered``). Every refusal is ``not_found`` with the
        same message per type, so an unpublished or delisted product is indistinguishable from an
        unknown id. No tenant check: a published card is marketplace-wide."""
        if ptype is ProductType.AGENT:
            agent = self._agent_registry.get(product_id)
            if agent is None:
                raise MarketplaceError("Unknown agent product", kind="not_found")
            if not self._product_is_offered(agent, ProductType.AGENT, self._rows.load_listings()):
                raise MarketplaceError("Unknown agent product", kind="not_found")
            return agent.name
        mcp = self._mcp_registry.get(product_id)
        if mcp is None or not self._product_is_offered(mcp, ProductType.MCP, self._rows.load_listings()):
            raise MarketplaceError("Unknown MCP product", kind="not_found")
        return mcp.name

    def _product_is_offered(self, product, product_type, listings) -> bool:
        """Published, still lifecycle-approved and not delisted by a listing (no listing means
        available): the one predicate behind the catalogs, the subscribe gate and both grant-time
        re-checks, so "visible as a card" and "grantable" stay the same set. ``listings`` is passed in
        so a caller holding the partition read reuses it."""
        publication = getattr(product, "marketplace", None)
        if publication is None or not publication.published:
            return False
        if getattr(product, "lifecycle_state", None) != "approved":
            return False
        listing = listings.get(_listing_sk(ProductType(product_type).value, product.id))
        return listing is None or bool(listing.available)

    def _effective_auto_approve(self, product_type, product_id: str) -> bool:
        """Only an admin's listing turns auto-approve on; a declaration cannot waive the gate."""
        ptype = ProductType(product_type)
        listing = self._rows.load_listings().get(_listing_sk(ptype.value, product_id))
        if listing is not None:
            return bool(listing.auto_approve)
        return False


def _neg_ts(dt: datetime) -> float:
    return -dt.timestamp()
