"""Service layer for the Epic 9 Marketplace — the subscription lifecycle orchestrator.

Backed by a single DynamoDB table (subscriptions + listings + E33 publish requests,
distinguished by a ``_record_kind`` discriminator on ``sk``) with an in-memory local
fallback when no table name is configured — the standard platform service idiom (``boto3.resource("dynamodb")``,
the ``_has_ddb`` guard, the local dict + lock, ``_to_item``/``_from_item`` via
``json.loads(model_dump_json())``, ``ClientError`` → log + local fallback).

It reads the E4/E5 registries (injected; the route builds them from the ``get_service()``
singletons) for product catalog + agent resolution — since E33 the AGENT catalog is the agent
registry itself (one card per agent carrying an approved, published ``marketplace``
declaration; the static blueprint list is gone) — and it ORCHESTRATES — never re-implements —
BOTH real Entra grants via injectable functions (tests inject fakes):

  - MCP products → the live E7 AGENT→MCP grant, ``grant_fn`` / ``revoke_fn`` (defaulting to
    ``services.agent_mcp_grant``), principal = the subscribing agent's SP;
  - AGENT products → the live E6 USER→AGENT grant, ``agent_grant_fn`` / ``agent_revoke_fn``
    (defaulting to ``services.agent_user_grant``), principal = the SUBSCRIBER's oid. Added in
    E33/T3: before it, approving an agent subscription only flipped a status, so an
    "approved" row conferred no access.

The service NEVER writes ``appRoleAssignedTo`` itself, and NEVER logs a token or secret.
``MarketplaceError`` carries a SAFE message + a ``.kind`` hint the route maps to a status.

Identity comes from the validated principal (``requester_oid``/``requester_email`` are passed
in by the route), never from caller-supplied body fields.
"""

from __future__ import annotations

import json
import logging
import threading
import uuid
from datetime import datetime, timezone
from typing import Dict, List, Optional

import boto3
from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError

from models.marketplace import (
    Datasheet,
    Listing,
    MarketplaceMetrics,
    MarketplacePublication,
    ProductCard,
    ProductType,
    PublishRequest,
    PublishRequestStatus,
    Subscription,
    SubscriptionStatus,
)
from services.agent_mcp_grant import (
    GrantNotFoundError,
    GrantRevokeError,
    apply_agent_mcp_grant,
    revoke_agent_mcp_grant,
)
from services.agent_user_grant import (
    UserGrantError,
    UserGrantNotFoundError,
    apply_user_agent_grant,
    revoke_user_agent_grant,
)
from services.tenant_resolver import visible

logger = logging.getLogger(__name__)

_PARTITION_KEY = "MARKETPLACE"  # single partition (single-partition scan at demo density)
_KIND_SUBSCRIPTION = "subscription"
_KIND_LISTING = "listing"
_KIND_PUBLISH = "publish"

# A subscription is "active" (and therefore idempotency-deduped) while pending or approved.
_ACTIVE_STATUSES = (SubscriptionStatus.PENDING, SubscriptionStatus.APPROVED)

# FIXED safe literals for the E33 publish failure seam. The registry write is the only
# thing that can fail here, and its exception may carry a registry id / ARN / AWS message —
# so the text persisted on the request and the text raised to the route are LITERALS, never
# ``str(exc)`` (Global Constraints §secrets; the ``_apply_grant`` precedent below).
_PUBLISH_WRITE_ERROR = "Marketplace publication write failed; see backend logs and retry."
_PUBLISH_WRITE_RAISE = "Publish write failed"

# T3 — the agent-subscription grant precondition. A published product whose agent identity
# is not provisioned (or which lacks an SP / Invoker role id) cannot be granted on, so the
# subscription is persisted FAILED with THIS literal. Deliberately the same wording as the E6
# route's own 409 detail (``routes/grants.py``) so the two surfaces explain the block
# identically. A FIXED literal, never ``str(exc)``.
_AGENT_NOT_PROVISIONED = "agent identity is not provisioned"

# T3 (fix round 2) — publication is MUTABLE state, so the birth-time gate in
# ``_resolve_product_name`` can go stale: a product may be unpublished or delisted while a
# PENDING row is still open (and rows persisted before that gate existed never passed it at
# all). ``_apply_agent_grant`` therefore re-asserts the gate at grant time and persists THIS
# literal on the row, so the admin sees WHY the approval was refused and can retry once the
# product is published again. A FIXED literal, never ``str(exc)``.
_AGENT_NOT_PUBLISHED = "agent product is no longer published"

# T11 (Amendment 1 / C9) — the MCP twin of the literal above, persisted by ``_apply_grant``
# when the MCP product behind a row is no longer offered. The MCP lane gained the SAME
# grant-time re-gate the agent lane has: since publish is now the only door into the
# marketplace for MCP servers too, an approve/retry must not write a real agent→MCP
# assignment for a product that was unpublished or delisted after the row was born.
# (Revoke stays UNGATED on purpose — the kill switch must work on a delisted product.)
_MCP_NOT_PUBLISHED = "MCP product is no longer published"

# T11 (Amendment 1 / C10) — the publish-time identity precondition, for BOTH product types.
# A product whose Entra identity is not provisioned cannot be granted on at all, so
# advertising it in the marketplace would publish a guaranteed dead end. Checked at
# publish-request CREATE and re-asserted at APPROVE. A FIXED literal, never ``str(exc)``;
# deliberately product-neutral (one text explains an agent and an MCP server), and distinct
# from ``_AGENT_NOT_PROVISIONED`` above, which belongs to the SUBSCRIPTION-grant seam.
_IDENTITY_NOT_PROVISIONED = "identity is not provisioned"

# E29×E33 seam — Databricks-governed agents are NOT publishable yet. A subscription
# approval applies the Entra Invoker assignment ONLY (``apply_user_agent_grant``), while
# E29's grant route holds the §3A invariant that a Databricks grant exists in BOTH places
# (Entra + the workspace app ACL) or in NEITHER (``grants._mirror_grant``). Until the
# subscription path speaks that mirror too, publishing would sell access the workspace's
# own door refuses — so publish is refused up front, with its own kind so the FE can say
# why. A FIXED literal, never ``str(exc)``.
_DATABRICKS_PUBLISH_UNSUPPORTED = (
    "Databricks-governed agents cannot be published to the marketplace yet"
)

# Product-neutral guard literals for the generalized publish path (C9). The route maps
# ``kind`` to its own fixed HTTP detail, so these are the service-side explanation; they say
# "product" rather than "agent" because one code path now serves both registries.
_UNKNOWN_PRODUCT = "Unknown product"
_PRODUCT_NOT_APPROVED = "Product is not approved for marketplace publication"
_PUBLISH_PENDING_CONFLICT = "A publish request for this product is already pending"
_PRODUCT_NOT_PUBLISHED = "Product is not published"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _new_id() -> str:
    return "mkt-" + uuid.uuid4().hex[:10]


def _new_publish_id() -> str:
    return "pub-" + uuid.uuid4().hex[:10]


def _listing_sk(product_type: str, product_id: str) -> str:
    return f"listing#{product_type}#{product_id}"


def _publish_sk(product_type, product_id: str) -> str:
    """ONE publish record per PRODUCT — a re-publish OVERWRITES it, and the decision
    history lives in the request's audit fields rather than in extra rows (C1).

    Amendment 1 / C9: the key carries the product TYPE as well, so the two registries get
    disjoint key spaces. Without it an agent and an MCP server that happen to share an id
    would collide on one record — the second publish would silently overwrite the first and
    ``approve_publish`` would dispatch the envelope write to the wrong registry.

    The type is normalized through ``ProductType`` (never interpolated raw) because a
    ``(str, Enum)`` member formats as ``"ProductType.AGENT"`` in an f-string, not as its
    value — the ``_listing_sk`` convention of keying on ``.value``.
    """
    return f"publish#{ProductType(product_type).value}#{product_id}"


def _identity_is_provisioned(product) -> bool:
    """Is this product's Entra identity ready to be granted on? (C10)

    ``identity_status == "provisioned"`` AND both ``entra_sp_id`` and ``invoker_role_id`` —
    the three fields a well-formed app-role assignment needs, and the SAME three field names
    on ``Agent`` and ``McpServer``, which is why ONE predicate serves both product types.
    ``getattr`` throughout so a record predating a field reads as not-provisioned rather than
    raising (fail closed).
    """
    return (
        getattr(product, "identity_status", None) == "provisioned"
        and bool(getattr(product, "entra_sp_id", None))
        and bool(getattr(product, "invoker_role_id", None))
    )


class MarketplaceError(Exception):
    """A marketplace operation failed (bad request / unknown product / state conflict /
    grant failure). Carries a SAFE message (no token/secret); routes map ``.kind``
    ('not_found' | 'conflict' | 'bad_request' | 'grant_failed') to 404/409/422/502.

    E33 publish kinds, same convention: 'agent_not_approved' | 'publish_conflict' |
    'illegal_publish_state' | 'identity_not_provisioned' → 409, 'publish_write_failed' → 502.
    ('agent_not_approved' keeps its name now that it covers MCP products too — the kind is a
    wire contract the route + frontend already map, and renaming it would buy nothing.)"""

    def __init__(self, message: str, *, kind: str = "bad_request") -> None:
        super().__init__(message)
        self.kind = kind


class MarketplaceService:
    def __init__(
        self,
        *,
        table_name: str = "",
        region: str = "us-east-1",
        mcp_registry=None,
        agent_registry=None,
        mcp_graph=None,
        grant_fn=None,
        revoke_fn=None,
        agent_grant_fn=None,
        agent_revoke_fn=None,
        tenant_service=None,
    ) -> None:
        self.table_name = table_name
        self.region = region
        self._mcp_registry = mcp_registry
        self._agent_registry = agent_registry
        self._mcp_graph = mcp_graph
        # E24/T8 — display-only tenant_name resolution on MCP cards (one cached
        # .list() per catalog read, never per-card). Optional: None ⇒ names omitted.
        self._tenant_service = tenant_service
        self._grant_fn = grant_fn or apply_agent_mcp_grant
        self._revoke_fn = revoke_fn or revoke_agent_mcp_grant
        # T3 — the user→agent grant pair, the AGENT-product twin of the MCP pair above.
        # Same injection contract (tests pass AsyncMocks; the route factory wires the real
        # shared functions), so an agent subscription confers REAL Entra access instead of
        # only flipping a status.
        #   agent_grant_fn(agent, user_oid: str) -> str    # the Entra assignment id
        #   agent_revoke_fn(agent, assignment_id: str) -> None
        self._agent_grant_fn = agent_grant_fn or apply_user_agent_grant
        self._agent_revoke_fn = agent_revoke_fn or revoke_user_agent_grant

        self._ddb = None
        self._table = None
        if table_name:
            try:
                self._ddb = boto3.resource("dynamodb", region_name=region)
                self._table = self._ddb.Table(table_name)
            except Exception:  # pragma: no cover
                self._table = None

        # local fallback caches (used when no DDB table is configured)
        self._local_subs: Dict[str, Subscription] = {}
        self._local_listings: Dict[str, Listing] = {}
        # E33 publish requests, keyed by _publish_sk(product_type, product_id) — ONE per
        # PRODUCT, exactly like the DDB layout, so the local path enforces the same
        # uniqueness (and the same agent/MCP key-space separation).
        self._local_publish: Dict[str, PublishRequest] = {}
        self._local_lock = threading.Lock()

    # -- mode helper --------------------------------------------------------

    @property
    def _has_ddb(self) -> bool:
        return bool(self.table_name) and self._table is not None

    # ===================================================================== #
    # Product catalog (read-models)
    # ===================================================================== #

    def list_agent_products(self, *, caller_oid: str) -> List[ProductCard]:
        """The agent catalog — REGISTRY-sourced (E33), one card per PUBLISHED agent.

        Replaces the static blueprint list: a product exists iff some agent carries an
        approved ``marketplace`` block with ``published=True``, and every datasheet value
        on the card is the publisher's DECLARATION as approved by an admin (never a
        curated literal, and never a measurement — see ``ProductCard``'s comment on the
        five deleted telemetry fields).

        There is deliberately **NO ``visible()`` filter** here: a published card is
        MARKETPLACE-WIDE, the analogue of an MCP record's ``shared=True`` (and
        ``list_mcp_products`` is now symmetric — it still accepts ``ctx`` for signature
        stability but no longer scopes by it). Publishing IS the act of making an
        agent cross-tenant discoverable, so re-filtering it by tenant would make the
        approval a no-op for every other tenant. The tenant badge still says whose it is.
        """
        listings = self._load_listings()
        my_subs = self._latest_sub_by_product(caller_oid, ProductType.AGENT)
        tenant_names = self._tenant_names()
        # LIVE consumer count per product: distinct requester teams (oids) with an
        # ACTIVE (pending/approved) subscription — the metrics() per-product-tally idiom,
        # scoped to agent products and deduped by requester so it reads as "N teams".
        consumers_live = self._agent_consumers_by_product()
        cards: List[ProductCard] = []
        for agent in self._agent_registry.list():
            publication = getattr(agent, "marketplace", None)
            # Unpublished (or never-published) → ABSENT entirely, not greyed. An unpublish
            # keeps the block with published=False, which must delist the card for real.
            #
            # The lifecycle clause is the ``list_mcp_products`` rule applied to agents (that
            # twin re-asserts ``lifecycle_state == "approved"`` on EVERY read): publication is
            # gated at REQUEST time only, and ``transition()`` writes the native status
            # WITHOUT touching the marketplace block, so a deprecated (or rejected) agent
            # would otherwise keep a live, subscribable card. This is the SAME condition
            # ``_agent_product_is_offered`` applies, so "visible as a card" and "subscribable"
            # stay the same set by construction and one deprecate delists at every gate.
            if publication is None or not publication.published:
                continue
            if getattr(agent, "lifecycle_state", None) != "approved":
                continue
            datasheet = publication.datasheet
            pid = agent.id
            listing = listings.get(_listing_sk(ProductType.AGENT.value, pid))
            mine = my_subs.get(pid)
            tenant_id = getattr(agent, "tenant_id", None)
            # E31F: LIVE ONLY — no seeded floor. A product nobody subscribed to reports 0
            # rather than a fabricated adoption number.
            consumers = consumers_live.get(pid, 0)
            cards.append(ProductCard(
                product_type=ProductType.AGENT,
                product_id=pid,
                name=agent.name,
                # The listing override still wins over the declared pitch (the
                # ``listing.X if listing and listing.X else …`` merge idiom, unchanged).
                pitch=(listing.pitch if listing and listing.pitch else datasheet.pitch),
                # Nothing derivable from a declaration: a datasheet has no capability
                # list, category or icon, and inventing one is what E31F removed.
                capabilities=[],
                available=(listing.available if listing else True),
                # E33: an agent product has NO auto-approve default of its own (a
                # declaration cannot waive the admin gate) — only a Listing turns it on.
                auto_approve=(listing.auto_approve if listing else False),
                # F4: governance metadata prefilling the agent Deploy mockup form. The
                # line of business comes off the registry record; the confidentiality is
                # the DECLARED product classification (a mandatory datasheet field), which
                # is what a consumer of the product acts on.
                business_unit=getattr(agent, "business_unit", None),
                data_classification=datasheet.data_classification,
                # Declared datasheet → card. Absent optional fields stay None / [] and the
                # UI omits them (no "—" placeholders).
                owner_team=datasheet.owner_team,
                support_contact=datasheet.support_contact,
                region=datasheet.region,
                version=datasheet.version,
                sla_tier=datasheet.sla_tier,
                support_hours=datasheet.support_hours,
                compliance=list(datasheet.compliance),
                guardrails=list(datasheet.guardrails),
                # Provenance: WHEN the declaration was attested. A card carrying this has a
                # declared datasheet behind it (what the FE "Preview" disclosure keys on).
                declared_at=publication.declared_at.isoformat(),
                consumers=consumers,
                # Tenant badge — id/flags from the record, name from the ONE cached tenant
                # list (None when unresolvable), exactly like the MCP cards. ``published``
                # is True by construction; agents have no ``shared`` flag.
                tenant_id=tenant_id,
                tenant_name=tenant_names.get(tenant_id) if tenant_id else None,
                published=True,
                my_status=(mine.status if mine else None),
                my_subscription_id=(mine.id if mine else None),
            ))
        return cards

    def _agent_consumers_by_product(self) -> Dict[str, int]:
        """Distinct requester teams (oids) with an ACTIVE subscription per agent product.

        Mirrors the per-``product_id`` tally idea in ``metrics()`` but counts UNIQUE
        requesters (a "team" proxy) and only pending/approved subs, so the card's
        consumer count reads as live adoption rather than raw request volume."""
        teams: Dict[str, set] = {}
        for s in self._load_subs():
            if s.product_type is not ProductType.AGENT:
                continue
            if s.status not in _ACTIVE_STATUSES:
                continue
            teams.setdefault(s.product_id, set()).add(s.requester_oid)
        return {pid: len(oids) for pid, oids in teams.items()}

    def list_mcp_products(self, *, caller_oid: str, ctx=None) -> List[ProductCard]:
        """The MCP catalog — one card per MARKETPLACE-PUBLISHED, approved MCP server.

        Amendment 1 / C9 retired the ``kind == "gateway"`` auto-listing filter: publish is
        now the only door into the marketplace for MCP servers exactly as it is for agents,
        so a product exists iff the record carries an approved ``marketplace`` block with
        ``published=True`` (and is still lifecycle-approved — re-asserted on every read for
        the ``list_agent_products`` reason: ``transition()`` never touches the block). ``kind``
        stays a DISPLAY field, so a standard-kind MCP lists once published and an unpublished
        gateway does not list at all.

        There is deliberately NO ``visible()`` filter left: every card here is
        marketplace-published, and publishing IS the act of making a product cross-tenant
        discoverable (the ``list_agent_products`` ruling, now symmetric). ``ctx`` is still
        accepted — the route threads it — but a published card is marketplace-wide, so
        scoping it would make the admin's approval a no-op for every other tenant. The tenant
        badge still says whose it is.
        """
        listings = self._load_listings()
        my_subs = self._latest_sub_by_product(caller_oid, ProductType.MCP)
        tenant_names = self._tenant_names()
        cards: List[ProductCard] = []
        for mcp in self._mcp_registry.list():
            publication = getattr(mcp, "marketplace", None)
            # Unpublished (or never-published) → ABSENT entirely, not greyed. An unpublish
            # keeps the block with published=False, which must delist the card for real.
            if publication is None or not publication.published:
                continue
            if getattr(mcp, "lifecycle_state", None) != "approved":
                continue
            datasheet = publication.datasheet
            # E24 tenant badge fields. ``shared`` stays the E24 cross-tenant share flag off the
            # record; the E24 ``published`` flag is deliberately NOT surfaced on a marketplace
            # card (see the ``published=True`` projection below).
            tenant_id = getattr(mcp, "tenant_id", None)
            shared = bool(getattr(mcp, "shared", False))
            pid = mcp.id
            listing = listings.get(_listing_sk(ProductType.MCP.value, pid))
            mine = my_subs.get(pid)
            cards.append(ProductCard(
                product_type=ProductType.MCP,
                product_id=pid,
                name=mcp.name,
                # The listing override still wins over the declared pitch — the agent
                # projection's merge idiom, now shared.
                pitch=(listing.pitch if listing and listing.pitch else datasheet.pitch),
                capabilities=[],
                available=(listing.available if listing else True),
                auto_approve=(listing.auto_approve if listing else False),
                # DISPLAY only since Amendment 1 (it no longer gates listing): the FE still
                # shows whether a published MCP is a gateway or a standard server.
                kind=getattr(mcp, "kind", None).value
                if hasattr(getattr(mcp, "kind", None), "value")
                else getattr(mcp, "kind", None),
                # ``owner_email`` is deliberately NOT projected onto a published card (fix
                # round 1): a published card is MARKETPLACE-WIDE, and the registry's owner
                # email is tenant-scoped contact PII — publishing a product must not broadcast
                # a named individual's address to every other tenant. The declared
                # ``support_contact`` (a mandatory datasheet field, typically a team mailbox
                # the publisher chose to expose) is its replacement below.
                business_unit=getattr(mcp, "business_unit", None),
                # F3 governance metadata — defensive (records may predate these
                # fields). data_classification is a DataClassification enum → its
                # .value; created_at/updated_at are datetimes → ISO strings.
                data_classification=(
                    getattr(mcp, "data_classification", None).value
                    if getattr(mcp, "data_classification", None) is not None
                    and hasattr(getattr(mcp, "data_classification"), "value")
                    else getattr(mcp, "data_classification", None)
                ),
                region=getattr(mcp, "region", None),
                version=getattr(mcp, "version", None),
                created_at=(mcp.created_at.isoformat() if getattr(mcp, "created_at", None) else None),
                updated_at=(mcp.updated_at.isoformat() if getattr(mcp, "updated_at", None) else None),
                # T11 — the DECLARED datasheet, projected exactly like the agent card. The
                # three fields the registry already owns as chips (data_classification /
                # region / version above) keep their REGISTRY value: those describe the
                # server record, and re-pointing them at the declaration would silently
                # replace a governed field with a publisher assertion.
                owner_team=datasheet.owner_team,
                support_contact=datasheet.support_contact,
                sla_tier=datasheet.sla_tier,
                support_hours=datasheet.support_hours,
                compliance=list(datasheet.compliance),
                guardrails=list(datasheet.guardrails),
                # Provenance: WHEN the declaration was attested (the FE's declared-datasheet
                # disclosure keys on this).
                declared_at=publication.declared_at.isoformat(),
                # E24/T8 tenant badge — id/flags from the record, name from the
                # ONE cached tenant list (None when unresolvable).
                tenant_id=tenant_id,
                tenant_name=tenant_names.get(tenant_id) if tenant_id else None,
                # ``published`` on a marketplace card means MARKETPLACE publication, for BOTH
                # product types (fix round 1) — True by construction, since the filter above is
                # what let this record become a card. It is NOT the E24 cross-tenant
                # ``published`` flag (a different feature with a different approver, Global
                # Constraints): surfacing that here made the same field mean two different
                # things on agent and MCP cards, which is exactly the conflation to avoid.
                published=True,
                shared=shared,
                my_status=(mine.status if mine else None),
                my_subscription_id=(mine.id if mine else None),
                my_agent_id=(mine.agent_id if mine else None),
            ))
        return cards

    def _tenant_names(self) -> Dict[str, str]:
        """tenant_id → tenant name via ONE tenant_service.list() per catalog read
        (display-only; never per-card). No service or a list() failure degrades to
        {} — the badge name is cosmetic and must never break the catalog."""
        if self._tenant_service is None:
            return {}
        try:
            return {t.id: t.name for t in self._tenant_service.list()}
        except Exception:  # noqa: BLE001 — display-only resolution, degrade.
            logger.warning("[marketplace] tenant name resolution failed", exc_info=True)
            return {}

    # ===================================================================== #
    # Subscription lifecycle
    # ===================================================================== #

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
        ptype = ProductType(product_type)

        # Resolve the product → 404 if unknown.
        product_name = self._resolve_product_name(ptype, product_id)

        # MCP: resolve + validate the agent (must be provisioned), capture its SP id + name.
        agent = None
        agent_name = None
        agent_sp_id = None
        if ptype is ProductType.MCP:
            if not agent_id:
                raise MarketplaceError("agent_id is required for an MCP subscription", kind="bad_request")
            agent = self._agent_registry.get(agent_id)
            if agent is None:
                raise MarketplaceError("Unknown agent", kind="not_found")
            if getattr(agent, "identity_status", None) != "provisioned" or not getattr(agent, "entra_sp_id", None):
                raise MarketplaceError("Agent is not provisioned for MCP access", kind="conflict")

            # F1 eligibility guard: the caller may only subscribe an MCP on behalf of
            # an agent they SPONSOR or have been GRANTED access to (direct user or
            # group). The sponsor check is a local field compare and short-circuits
            # BEFORE any Graph read (so a sponsor still succeeds when Graph is down);
            # the grant path reads list_assignments and FAILS CLOSED if that read
            # raises (the exception propagates → route maps it to 502).
            if not await self._is_agent_eligible(
                agent, caller_oid=requester_oid, caller_group_ids=caller_group_ids or []
            ):
                raise MarketplaceError(
                    "you may only subscribe on behalf of an agent you sponsor or are granted access to",
                    kind="conflict",
                )

            agent_name = getattr(agent, "name", None)
            agent_sp_id = getattr(agent, "entra_sp_id", None)

        # Idempotency: return an existing ACTIVE sub for the logical key.
        existing = self._active_sub_for(ptype, product_id, requester_oid, agent_id)
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

        auto = self._effective_auto_approve(ptype, product_id)
        if auto:
            if ptype is ProductType.AGENT:
                # T3: agent auto-approve applies the real user→agent grant inline, exactly
                # like the MCP branch below. It used to mark APPROVED with no grant, which
                # made an auto-approving product hand out rows that conferred no access.
                sub.auto_approved = True
                self._save(sub)
                sub = await self._apply_agent_grant(sub, decided_by="auto")
            else:
                # MCP auto-approve applies the real grant inline.
                sub.auto_approved = True
                self._save(sub)
                sub = await self._apply_grant(sub, decided_by="auto")
        else:
            self._save(sub)
        return sub

    # ===================================================================== #
    # Eligibility (F1) — agents the caller may subscribe an MCP on behalf of
    # ===================================================================== #

    async def eligible_agents(
        self, *, caller_oid: Optional[str], caller_group_ids: List[str]
    ) -> List:
        """Provisioned agents the caller may subscribe an MCP on behalf of.

        An agent is eligible when the caller is its SPONSOR
        (``agent.sponsor_oid == caller_oid``) OR is GRANTED on it — a direct user
        assignment (``principalType == "User"`` with ``principalId == caller_oid``)
        or a group assignment (``principalType == "Group"`` with ``principalId`` in
        ``caller_group_ids``). Only provisioned agents (with an SP id) are
        considered. Reads agents from ``self._agent_registry.list()``; for the
        non-sponsor grant check it reads ``self._mcp_graph.list_assignments`` on the
        agent's SP. A sponsor match short-circuits (NO Graph call). Deduped, sorted
        by name. Used by the picker endpoint; shares ``_is_agent_eligible`` with the
        subscribe guard (single source of truth).
        """
        agents = self._agent_registry.list()
        eligible: List = []
        seen: set = set()
        for agent in agents:
            if getattr(agent, "identity_status", None) != "provisioned" or not getattr(agent, "entra_sp_id", None):
                continue
            if await self._is_agent_eligible(
                agent, caller_oid=caller_oid, caller_group_ids=caller_group_ids
            ):
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
        """True iff the caller sponsors OR is granted (user/group) on the agent.

        Sponsor is a local field compare and is checked FIRST so it short-circuits
        before any Graph ``list_assignments`` read — that is what lets a sponsor
        succeed when Graph is down. For a non-sponsor, the Graph read is awaited and
        any error PROPAGATES (fail closed): the caller (subscribe guard) lets it
        surface as a 502; the listing endpoint wraps the call defensively itself.
        """
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
        subs = self._load_subs()
        if caller_oid is not None:
            subs = [s for s in subs if s.requester_oid == caller_oid]
        if status is not None:
            want = SubscriptionStatus(status)
            subs = [s for s in subs if s.status == want]
        if product_type is not None:
            want_t = ProductType(product_type)
            subs = [s for s in subs if s.product_type == want_t]
        # pending-first, then created_at desc.
        subs.sort(
            key=lambda s: (0 if s.status == SubscriptionStatus.PENDING else 1, _neg_ts(s.created_at)),
        )
        return subs

    async def approve(self, subscription_id: str, *, decided_by: str) -> Subscription:
        sub = self._get_sub(subscription_id)
        if sub is None:
            raise MarketplaceError("Unknown subscription", kind="not_found")
        if sub.status != SubscriptionStatus.PENDING:
            raise MarketplaceError("Subscription is not pending", kind="conflict")
        if sub.product_type is ProductType.AGENT:
            # T3: approving an agent subscription now applies the REAL E6 user→agent Entra
            # grant. It used to be a bare status flip, which meant an "approved" row conferred
            # no access at all — the subscriber still got a 403 from the agent.
            return await self._apply_agent_grant(sub, decided_by=decided_by)
        return await self._apply_grant(sub, decided_by=decided_by)

    def reject(self, subscription_id: str, *, decided_by: str, reason: Optional[str] = None) -> Subscription:
        sub = self._get_sub(subscription_id)
        if sub is None:
            raise MarketplaceError("Unknown subscription", kind="not_found")
        if sub.status != SubscriptionStatus.PENDING:
            raise MarketplaceError("Subscription is not pending", kind="conflict")
        sub.status = SubscriptionStatus.REJECTED
        sub.decided_by = decided_by
        sub.decided_at = _now()
        sub.decision_reason = reason
        sub.updated_at = _now()
        self._save(sub)
        return sub

    async def retry_grant(self, subscription_id: str, *, decided_by: str) -> Subscription:
        sub = self._get_sub(subscription_id)
        if sub is None:
            raise MarketplaceError("Unknown subscription", kind="not_found")
        if sub.status != SubscriptionStatus.FAILED:
            raise MarketplaceError("Subscription is not in a failed state", kind="conflict")
        # T3: a FAILED agent sub is retryable too — its grant is the user→agent one. Both
        # apply-paths are idempotent, so a retry after a partially-applied grant recovers
        # the existing assignment id rather than failing.
        if sub.product_type is ProductType.AGENT:
            return await self._apply_agent_grant(sub, decided_by=decided_by)
        return await self._apply_grant(sub, decided_by=decided_by)

    async def revoke_subscription(
        self, subscription_id: str, *, decided_by: str, reason: Optional[str] = None
    ) -> Subscription:
        """Admin-revoke an APPROVED subscription — the inverse of approve. For an MCP sub
        this drives the real E7 kill switch via the (injectable) ``revoke_fn`` (revoke the
        app-role assignment + tear down the agent→MCP OBO consent under the multiplicity
        guard); for an AGENT sub it drives the E6 kill switch via ``agent_revoke_fn``
        (delete the user's Invoker assignment on the agent SP — T3; before that an agent
        revoke was a bare status flip that left the access in place).

        On a real Graph failure the sub STAYS APPROVED (nothing persisted) and a
        ``grant_failed`` MarketplaceError is raised so the admin can simply retry (the route
        maps it to 502). A stale/already-gone grant (``GrantNotFoundError``) is treated as
        success. The revoke audit (``revoked_by``/``revoked_at``/``revoke_reason``) is set on
        the record WITHOUT overwriting the approval audit (``decided_by``/``decided_at``)."""
        sub = self._get_sub(subscription_id)
        if sub is None:
            raise MarketplaceError("Unknown subscription", kind="not_found")
        if sub.status != SubscriptionStatus.APPROVED:
            raise MarketplaceError("Subscription is not approved", kind="conflict")

        if sub.product_type is ProductType.AGENT:
            # T3: the agent-product kill switch — delete the user's Invoker assignment on the
            # agent SP. Same actionability guard as the MCP branch: only call Graph when there
            # is a stored assignment id AND a resolvable agent record; otherwise there is
            # nothing left to revoke, so just clean up the row.
            agent = self._agent_registry.get(sub.product_id)
            if sub.grant_assignment_id and agent is not None:
                try:
                    await self._agent_revoke_fn(agent, sub.grant_assignment_id)
                except UserGrantNotFoundError:
                    # Already gone in Graph (stale/double-click) → treat as success. The
                    # desired end state (no access) already holds.
                    pass
                except UserGrantError:
                    # A real teardown failure — leave the sub APPROVED (persist NOTHING) so
                    # the admin can retry by clicking Revoke again. Log the traceback only
                    # (never a token / Graph value) — the §12.9 no-leak failure seam.
                    logger.exception(
                        "[marketplace] user-grant revoke failed for subscription %s "
                        "(agent %s, assignment %s)",
                        sub.id, sub.product_id, sub.grant_assignment_id,
                    )
                    raise MarketplaceError("Grant revoke failed", kind="grant_failed") from None

        if sub.product_type is ProductType.MCP:
            mcp = self._mcp_registry.get(sub.product_id)
            # Only call the Graph teardown when there is something actionable: a stored
            # assignment id AND a resolvable MCP record. Otherwise best-effort removal —
            # there is nothing left in Graph to revoke, so just clean up the record.
            if sub.grant_assignment_id and mcp is not None:
                try:
                    await self._revoke_fn(mcp, sub.grant_assignment_id)
                except GrantNotFoundError:
                    # Already gone in Graph (stale/double-click) → treat as success.
                    pass
                except GrantRevokeError:
                    # A real teardown failure — leave the sub APPROVED (persist NOTHING) so
                    # the admin can retry by clicking Revoke again. Log the traceback only
                    # (never a token / Graph value) — the §12.9 no-leak failure seam.
                    logger.exception(
                        "[marketplace] revoke failed for subscription %s (MCP %s, assignment %s)",
                        sub.id, sub.product_id, sub.grant_assignment_id,
                    )
                    raise MarketplaceError("Grant revoke failed", kind="grant_failed") from None

        sub.status = SubscriptionStatus.REVOKED
        sub.revoked_by = decided_by
        sub.revoked_at = _now()
        sub.revoke_reason = reason
        sub.updated_at = _now()
        self._save(sub)
        return sub

    # ===================================================================== #
    # Listings
    # ===================================================================== #

    def set_listing(
        self,
        *,
        product_type,
        product_id: str,
        available: Optional[bool] = None,
        auto_approve: Optional[bool] = None,
        pitch: Optional[str] = None,
    ) -> Listing:
        ptype = ProductType(product_type)
        existing = self._load_listings().get(_listing_sk(ptype.value, product_id))
        listing = existing or Listing(product_type=ptype, product_id=product_id)
        if available is not None:
            listing.available = available
        if auto_approve is not None:
            listing.auto_approve = auto_approve
        if pitch is not None:
            listing.pitch = pitch
        self._save_listing(listing)
        return listing

    # ===================================================================== #
    # Publish requests (E33) — declared datasheet → admin approval → envelope
    # ===================================================================== #

    def create_publish_request(
        self,
        *,
        product_type,
        product_id: str,
        datasheet: Datasheet,
        requester_oid: str,
        requester_email: Optional[str],
        ctx=None,
    ) -> PublishRequest:
        """A publisher declares a datasheet for one of THEIR products (status PENDING).

        Amendment 1 / C9: ONE path for both product types — ``product_type`` selects the
        registry the product is read from, and is stamped on the record so ``approve_publish``
        knows which ``persist_marketplace`` to dispatch the envelope write to.

        Guards, in order:
          - the product exists AND is tenant-visible to the caller, else ``not_found``. A
            foreign-tenant product is byte-identical to a missing one, so the endpoint never
            confirms the existence of another tenant's agent or MCP server. ``visible()`` is
            called WITHOUT ``shared=`` deliberately: publishing is an OWNING-TENANT governance
            action, whereas E24's ``shared=true`` confers read + grant-target rights only,
            never write — so a foreign SHARED MCP is not publish-requestable (fail closed).
          - ``lifecycle_state == "approved"``, else ``agent_not_approved`` (the kind name is
            kept — see ``MarketplaceError``): the marketplace must not advertise a product the
            platform has not itself approved.
          - identity provisioned (C10), else ``identity_not_provisioned``: a product with no
            SP / Invoker role can never be granted on, so publishing it would advertise a
            guaranteed dead end.
          - no existing PENDING request for the product, else ``publish_conflict`` — a second
            declaration would silently overwrite the one an admin is reviewing. A
            REJECTED/APPROVED record is instead overwritten (ONE record per product, C1).

        Identity (``requester_oid``/``requester_email``) comes from the validated principal;
        ``product_name``/``tenant_id`` come from the REGISTRY, never the request body.
        """
        ptype = ProductType(product_type)
        product = self._get_product(ptype, product_id)
        # ctx=None (no tenant scoping wired / legacy caller) keeps the read unscoped, the
        # ``list_mcp_products`` convention. The route always passes a resolved ctx.
        if product is None or (
            ctx is not None and not visible(ctx, getattr(product, "tenant_id", None))
        ):
            raise MarketplaceError(_UNKNOWN_PRODUCT, kind="not_found")
        if getattr(product, "lifecycle_state", None) != "approved":
            raise MarketplaceError(_PRODUCT_NOT_APPROVED, kind="agent_not_approved")
        # Before the identity gate deliberately: for a Databricks agent "unsupported" is
        # the true reason — an identity answer would imply a provisioning retry could
        # unlock publish, which is false while the subscription grant lacks the ACL mirror.
        if ptype is ProductType.AGENT and getattr(
            product, "is_databricks_governed", False
        ):
            raise MarketplaceError(
                _DATABRICKS_PUBLISH_UNSUPPORTED, kind="databricks_publish_unsupported"
            )
        if not _identity_is_provisioned(product):
            raise MarketplaceError(
                _IDENTITY_NOT_PROVISIONED, kind="identity_not_provisioned"
            )

        existing = self._get_publish_for_product(ptype, product_id)
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
        self._save_publish(req)
        return req

    def list_publish_requests(self, status: Optional[str] = None) -> List[PublishRequest]:
        """Every publish request (ADMIN queue), optionally filtered by status.

        Sorted pending-first then created_at desc — the ``list_subscriptions`` ordering, so
        the approvals queue reads the same way for both kinds of decision."""
        reqs = list(self._load_publish_requests().values())
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
        self, product_type, product_id: str, ctx=None
    ) -> Optional[PublishRequest]:
        """The product's ONE publish record, or None when it was never requested.

        Keyed on the ``(product_type, product_id)`` PAIR (C9), so an agent and an MCP server
        that share an id read their own records rather than each other's.

        Tenant-scoped: when ``ctx`` is provided and the STORED request's ``tenant_id`` is
        not visible to the caller, this returns ``None`` — indistinguishable from "never
        requested", which is what makes the C5 route's 404 byte-identical for a foreign
        product and an unpublished one. Without that, a tenant-A OPERATOR polling this by
        product id would read tenant-B's declared datasheet, requester email and
        ``decision_reason``.

        The gate reads the request's OWN ``tenant_id`` (stamped from the registry record at
        create time) rather than re-reading the product, so a request stays scoped to the
        tenant it was filed under even if the record later moves or disappears.

        ``ctx=None`` keeps the read unscoped — the ``create_publish_request`` /
        ``list_mcp_products`` convention for callers with no tenant scoping wired. The
        route always passes a resolved ctx.
        """
        req = self._get_publish_for_product(ProductType(product_type), product_id)
        if req is None:
            return None
        if ctx is not None and not visible(ctx, req.tenant_id):
            return None
        return req

    async def approve_publish(self, request_id: str, decided_by: str) -> PublishRequest:
        """Approve a PENDING declaration: write the publication onto the PRODUCT's envelope,
        then flip the request APPROVED.

        The envelope write is dispatched by the REQUEST's ``product_type`` (C9) — the agent
        registry's ``persist_marketplace`` or the MCP registry's — so a request can only ever
        publish into the registry it was filed against.

        The create-time preconditions are RE-ASSERTED here (lifecycle approved + identity
        provisioned, C10), because a request outlives them: an agent may be deprecated or an
        MCP's identity torn down while the request sits in the queue, and an approve would
        otherwise advertise it. A product that no longer resolves at all is deliberately NOT
        pre-checked — it falls through to the registry write, whose not-found is the retryable
        ``publish_write_failed`` seam below rather than a state conflict.

        Two writes are unavoidable (the envelope write cannot ride a status transition), so
        the ORDER is what makes the failure safe: the registry write goes FIRST and the
        request only flips once it landed. A registry failure therefore leaves the request
        PENDING (retryable) with a safe error literal persisted, and raises
        ``publish_write_failed`` (→502) — the ``_apply_grant`` contract, inverted for a
        path whose safe state is "undecided" rather than "failed".

        The failure catch is deliberately BROAD, and covers BOTH registries' domain
        not-found errors (``AgentNotFoundError`` / ``McpServerNotFoundError``) plus the raw
        botocore ``ClientError`` from the TOCTOU seam: ``persist_marketplace`` raises the
        domain error when its own ``get()`` misses, but it then DELEGATES the write, so a
        record deleted between that read and the write surfaces a raw ``ClientError``
        instead. Catching only the domain errors would let that escape as a 500 with the
        request left silently undecided. (The errors are not imported here on purpose — the
        broad catch already handles them identically, and naming them would add an import
        edge from this service to both registry modules for no behavioural gain.)
        """
        req = self._get_publish_request(request_id)
        if req is None:
            raise MarketplaceError("Unknown publish request", kind="not_found")
        if req.status is not PublishRequestStatus.PENDING:
            raise MarketplaceError(
                "Publish request is not pending", kind="illegal_publish_state"
            )

        # Re-assert the create-time gates on the CURRENT record. A missing record is left to
        # the registry write below (see the docstring), so both branches only fire on a
        # product that still exists but has since gone stale.
        product = self._get_product(req.product_type, req.product_id)
        if product is not None:
            if getattr(product, "lifecycle_state", None) != "approved":
                raise MarketplaceError(_PRODUCT_NOT_APPROVED, kind="agent_not_approved")
            if req.product_type is ProductType.AGENT and getattr(
                product, "is_databricks_governed", False
            ):
                raise MarketplaceError(
                    _DATABRICKS_PUBLISH_UNSUPPORTED,
                    kind="databricks_publish_unsupported",
                )
            if not _identity_is_provisioned(product):
                raise MarketplaceError(
                    _IDENTITY_NOT_PROVISIONED, kind="identity_not_provisioned"
                )

        publication = MarketplacePublication(
            published=True,
            datasheet=req.datasheet,
            # The APPROVING admin attests the declaration — from the principal, never the body.
            declared_by=decided_by,
            declared_at=_now(),
        )
        try:
            self._registry_for(req.product_type).persist_marketplace(
                req.product_id, publication
            )
        except Exception:  # noqa: BLE001 — see the docstring: AgentNotFoundError /
            # McpServerNotFoundError OR a raw ClientError from the TOCTOU seam. Log the
            # traceback only (never the exception VALUE / any AWS detail) — the §12.9
            # no-leak failure seam.
            logger.exception(
                "[marketplace] publication write failed for request %s (%s %s)",
                req.id, req.product_type.value, req.product_id,
            )
            req.error = _PUBLISH_WRITE_ERROR
            req.updated_at = _now()
            self._save_publish(req)          # STAYS PENDING → the admin can just retry
            raise MarketplaceError(_PUBLISH_WRITE_RAISE, kind="publish_write_failed") from None

        req.status = PublishRequestStatus.APPROVED
        req.decided_by = decided_by
        req.decided_at = _now()
        req.error = None                     # clear a stale error from an earlier retry
        req.updated_at = _now()
        self._save_publish(req)
        return req

    def reject_publish(
        self, request_id: str, decided_by: str, reason: Optional[str]
    ) -> PublishRequest:
        """Reject a PENDING declaration. Writes NO envelope — the agent stays unpublished,
        so there is nothing to undo and no registry call to fail."""
        req = self._get_publish_request(request_id)
        if req is None:
            raise MarketplaceError("Unknown publish request", kind="not_found")
        if req.status is not PublishRequestStatus.PENDING:
            raise MarketplaceError(
                "Publish request is not pending", kind="illegal_publish_state"
            )
        req.status = PublishRequestStatus.REJECTED
        req.decided_by = decided_by
        req.decided_at = _now()
        req.decision_reason = reason
        req.updated_at = _now()
        self._save_publish(req)
        return req

    async def unpublish(self, product_type, product_id: str, decided_by: str) -> None:
        """Delist a published product (agent or MCP): keep the block, flip ``published`` to
        False, in the registry ``product_type`` selects (C9).

        The declared datasheet + its attestation (``declared_by``/``declared_at``) are
        RETAINED (C2/C8) — a delisting must not erase what was once declared, which is the
        record of what consumers were told. Absent-or-already-unpublished is ``not_found``
        so a double-click cannot produce a second envelope write.

        ``async`` for symmetry with ``approve_publish`` (the registry calls are sync)."""
        ptype = ProductType(product_type)
        product = self._get_product(ptype, product_id)
        publication = getattr(product, "marketplace", None) if product is not None else None
        if publication is None or not publication.published:
            raise MarketplaceError(_PRODUCT_NOT_PUBLISHED, kind="not_found")

        try:
            self._registry_for(ptype).persist_marketplace(
                product_id, publication.model_copy(update={"published": False})
            )
        except Exception:  # noqa: BLE001 — broad for the same reason as approve_publish.
            logger.exception(
                "[marketplace] unpublish write failed for %s %s (requested by %s)",
                ptype.value, product_id, decided_by,
            )
            raise MarketplaceError(_PUBLISH_WRITE_RAISE, kind="publish_write_failed") from None

    # -- product-type dispatch (the C9 seam) --------------------------------

    def _registry_for(self, product_type):
        """The registry that OWNS a product type — the one place the dispatch lives.

        Both registries expose the same ``get``/``list``/``persist_marketplace`` shape for the
        publish path, so every generalized method routes through here rather than branching
        on the type inline (which is how the two lanes would drift)."""
        return (
            self._agent_registry
            if ProductType(product_type) is ProductType.AGENT
            else self._mcp_registry
        )

    def _get_product(self, product_type, product_id: str):
        """The registry record behind a product id, or None. Type-dispatched (C9)."""
        return self._registry_for(product_type).get(product_id)

    # ===================================================================== #
    # Metrics
    # ===================================================================== #

    def metrics(self) -> MarketplaceMetrics:
        subs = self._load_subs()
        total = len(subs)
        pending = sum(1 for s in subs if s.status == SubscriptionStatus.PENDING)
        approved = sum(1 for s in subs if s.status == SubscriptionStatus.APPROVED)
        rejected = sum(1 for s in subs if s.status == SubscriptionStatus.REJECTED)
        failed = sum(1 for s in subs if s.status == SubscriptionStatus.FAILED)
        revoked = sum(1 for s in subs if s.status == SubscriptionStatus.REVOKED)
        denom = approved + rejected
        approval_rate = (approved / denom) if denom else 0.0

        by_type: Dict[str, int] = {ProductType.AGENT.value: 0, ProductType.MCP.value: 0}
        counts: Dict[str, dict] = {}
        for s in subs:
            by_type[s.product_type.value] = by_type.get(s.product_type.value, 0) + 1
            entry = counts.setdefault(s.product_id, {"product_id": s.product_id, "product_name": s.product_name, "count": 0})
            entry["count"] += 1
        top_products = sorted(counts.values(), key=lambda e: e["count"], reverse=True)[:5]

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

    # ===================================================================== #
    # Private helpers
    # ===================================================================== #

    def _resolve_product_name(self, ptype: ProductType, product_id: str) -> str:
        """Resolve a product id to its display name AND gate it: a subscription may only be
        born against something that really is an offered marketplace product. Every branch
        raises the SAME ``not_found`` kind with a byte-identical message, so a
        never-published / unpublished / delisted product is indistinguishable from a
        nonexistent id (the C5 no-enumeration invariant)."""
        if ptype is ProductType.AGENT:
            # E33: an agent product IS a registry record (the blueprint list is gone), so
            # the name comes from the record — the same source the card projects.
            agent = self._agent_registry.get(product_id)
            if agent is None:
                raise MarketplaceError("Unknown agent product", kind="not_found")
            # T3 gate: registry EXISTENCE is not productness. Since approving (or
            # auto-approving) an agent sub writes a REAL Entra Invoker assignment, the
            # record must carry a live marketplace publication — exactly the condition
            # ``list_agent_products`` uses to emit the card — and must not be delisted by
            # an admin Listing. Without this, any registry agent id could be subscribed and
            # then granted; worse, ``unpublish``/``set_listing(available=False)`` do not
            # clear ``listing.auto_approve``, so a delisted product with a stale
            # auto-approve Listing would keep self-granting with no admin in the loop.
            # Deliberately NO tenant/visibility check: a published card is
            # marketplace-wide by design (the ``list_agent_products`` ruling), so
            # cross-tenant subscribing is intended once published.
            if not self._product_is_offered(agent, ProductType.AGENT, self._load_listings()):
                raise MarketplaceError("Unknown agent product", kind="not_found")
            return agent.name
        mcp = self._mcp_registry.get(product_id)
        # Amendment 1 / C9: the ``kind == "gateway"`` filter is RETIRED — publish is the only
        # door for MCP servers too, so the same predicate that gates agents gates MCPs (a
        # published standard-kind server is subscribable; an unpublished gateway is not).
        if mcp is None or not self._product_is_offered(
            mcp, ProductType.MCP, self._load_listings()
        ):
            raise MarketplaceError("Unknown MCP product", kind="not_found")
        return mcp.name

    def _product_is_offered(self, product, product_type, listings) -> bool:
        """Is this registry record CURRENTLY an offered marketplace product? (C9)

        True iff it is PUBLISHED **and still lifecycle-APPROVED** (the very expression the
        catalog reads use to decide whether to emit a card) and not delisted by an admin
        Listing (absent listing = available, mirroring the card's
        ``available=(listing.available if listing else True)``). So "subscribable" and
        "visible as a card" are the same set by construction.

        The lifecycle clause matters because publication is gated at REQUEST time only and
        ``transition()`` never touches the marketplace block: without it a DEPRECATED product
        would stay subscribable AND grantable — an admin approve would write a REAL Entra
        assignment for something the platform has retired.

        ONE predicate, shared by the FOUR gates that must agree: the birth gates for both
        product types (``_resolve_product_name``) and the grant-time re-checks for both
        (``_apply_agent_grant`` / ``_apply_grant``). Publication is MUTABLE — a row outlives
        the check made at its birth — so the second pair is not redundant; keeping all four on
        this single expression is what stops them drifting, and a drift here would be a
        security hole rather than a cosmetic bug.

        ``listings`` is passed IN (rather than loaded here) so a caller that already holds the
        one full-partition read reuses it instead of re-scanning per product.

        Deliberately NO tenant/visibility filter: a published card is marketplace-wide by
        design (the ``list_agent_products`` ruling, symmetric since Amendment 1)."""
        publication = getattr(product, "marketplace", None)
        if publication is None or not publication.published:
            return False
        if getattr(product, "lifecycle_state", None) != "approved":
            return False
        listing = listings.get(
            _listing_sk(ProductType(product_type).value, getattr(product, "id", None))
        )
        return listing is None or bool(listing.available)

    def _effective_auto_approve(self, product_type, product_id: str) -> bool:
        ptype = ProductType(product_type)
        listing = self._load_listings().get(_listing_sk(ptype.value, product_id))
        if listing is not None:
            return bool(listing.auto_approve)
        # Both types default to admin-gated: E33 dropped the per-blueprint auto_approve
        # seed (a publisher's declaration cannot waive the subscription gate), so only an
        # admin's Listing turns auto-approve on — for agents and MCPs alike.
        return False

    async def _apply_grant(self, sub: Subscription, *, decided_by: str) -> Subscription:
        """Resolve the agent + MCP records, await the (injectable) grant fn, persist the
        outcome. On success → APPROVED + ``grant_assignment_id``. On failure → persist
        FAILED + a SAFE ``error`` THEN raise ``MarketplaceError(kind="grant_failed")`` so
        the route maps it to 502 and the row shows Retry."""
        agent = self._agent_registry.get(sub.agent_id) if sub.agent_id else None
        mcp = self._mcp_registry.get(sub.product_id)
        if agent is None or mcp is None:
            sub.status = SubscriptionStatus.FAILED
            sub.error = "Could not resolve agent or MCP for the grant"
            sub.decided_by = decided_by
            sub.decided_at = _now()
            sub.updated_at = _now()
            self._save(sub)
            raise MarketplaceError("Could not resolve agent or MCP for the grant", kind="grant_failed")

        # T11 — re-assert the marketplace-publication gate at GRANT time, the symmetric twin of
        # the agent lane's re-gate below. Publication is MUTABLE and a row outlives the birth
        # check in ``_resolve_product_name`` (and rows written before publish existed for MCPs
        # never passed one at all), so both lanes that write a REAL agent→MCP assignment
        # (``approve`` and ``retry_grant``) funnel through HERE. FAILED + ``grant_failed`` keeps
        # the row visible with a clear terminal reason and retryable once it is published again.
        # ``revoke_subscription`` deliberately does NOT gate: a kill switch must still work on
        # a product that has been delisted (that asymmetry is the point).
        if not self._product_is_offered(mcp, ProductType.MCP, self._load_listings()):
            sub.status = SubscriptionStatus.FAILED
            sub.error = _MCP_NOT_PUBLISHED
            sub.decided_by = decided_by
            sub.decided_at = _now()
            sub.updated_at = _now()
            self._save(sub)
            raise MarketplaceError(_MCP_NOT_PUBLISHED, kind="grant_failed")

        try:
            assignment_id = await self._grant_fn(agent, mcp)
        except Exception:  # noqa: BLE001 — persist FAILED before re-raising as grant_failed.
            # Log the traceback only (never the exception VALUE / any secret) — §12.9.
            logger.exception(
                "[marketplace] grant failed for subscription %s (agent %s -> MCP %s)",
                sub.id, sub.agent_id, sub.product_id,
            )
            sub.status = SubscriptionStatus.FAILED
            sub.error = "Grant application failed; see backend logs and retry."
            sub.decided_by = decided_by
            sub.decided_at = _now()
            sub.updated_at = _now()
            self._save(sub)
            raise MarketplaceError("Grant application failed", kind="grant_failed") from None

        sub.status = SubscriptionStatus.APPROVED
        sub.grant_assignment_id = assignment_id
        sub.error = None
        sub.decided_by = decided_by
        sub.decided_at = _now()
        sub.updated_at = _now()
        self._save(sub)
        return sub

    async def _apply_agent_grant(self, sub: Subscription, *, decided_by: str) -> Subscription:
        """The AGENT-product twin of ``_apply_grant`` (T3): resolve the agent BEHIND the
        product, await the (injectable) user→agent grant fn, persist the outcome.

        Since E33 an agent product IS a registry agent, so the product id is the agent id
        and the grant principal is the SUBSCRIBER (``sub.requester_oid``) — a human user, not
        an agent SP. That is the whole difference from the MCP path, whose principal is the
        subscribing agent's SP.

        Guards (all three fields are required for the Graph write to be well-formed — the
        E6 route's ``_is_provisioned`` check plus the role id it would look up):
        the agent must exist, be ``identity_status == "provisioned"``, and carry both
        ``entra_sp_id`` and ``invoker_role_id``. Otherwise the subscription is persisted
        FAILED with the SAFE literal "agent identity is not provisioned" (the E6 route's own
        wording) and ``grant_failed`` is raised → 502 with a Retry-able row, so an admin who
        provisions the agent can retry rather than re-subscribe.

        Then the product must still BE a product: the publication gate is re-asserted here
        (``_product_is_offered``) because a row outlives its birth-time check, and both
        real-grant lanes (``approve``/``retry_grant``) pass through this method. An unpublished
        or delisted product persists FAILED with ``_AGENT_NOT_PUBLISHED`` and raises
        ``grant_failed`` — retryable once it is published again.

        On success → APPROVED + ``grant_assignment_id`` (the handle revoke needs). On failure
        → FAILED + a SAFE ``error`` THEN raise ``grant_failed``, exactly the ``_apply_grant``
        contract."""
        agent = self._agent_registry.get(sub.product_id)
        if (
            agent is None
            or getattr(agent, "identity_status", None) != "provisioned"
            or not getattr(agent, "entra_sp_id", None)
            or not getattr(agent, "invoker_role_id", None)
        ):
            sub.status = SubscriptionStatus.FAILED
            sub.error = _AGENT_NOT_PROVISIONED
            sub.decided_by = decided_by
            sub.decided_at = _now()
            sub.updated_at = _now()
            self._save(sub)
            raise MarketplaceError(_AGENT_NOT_PROVISIONED, kind="grant_failed")

        # Re-assert the marketplace-publication gate at GRANT time. The birth gate in
        # ``_resolve_product_name`` only proves the product was offered when the row was
        # created; publication is mutable, so an admin may unpublish or delist between the
        # request and the decision — and rows written before that gate existed never passed
        # it at all. Both lanes that write a real Entra Invoker assignment (``approve`` and
        # ``retry_grant``) funnel through HERE, so this is the one place the check belongs.
        # FAILED + ``grant_failed`` (rather than ``not_found``) is the right shape: the row
        # exists and its requester can see it, so it must show a clear terminal reason and
        # stay retryable once the product is published again.
        if not self._product_is_offered(agent, ProductType.AGENT, self._load_listings()):
            sub.status = SubscriptionStatus.FAILED
            sub.error = _AGENT_NOT_PUBLISHED
            sub.decided_by = decided_by
            sub.decided_at = _now()
            sub.updated_at = _now()
            self._save(sub)
            raise MarketplaceError(_AGENT_NOT_PUBLISHED, kind="grant_failed")

        try:
            assignment_id = await self._agent_grant_fn(agent, sub.requester_oid)
        except Exception:  # noqa: BLE001 — persist FAILED before re-raising as grant_failed.
            # Log the traceback only (never the exception VALUE / any Graph detail) — §12.9.
            logger.exception(
                "[marketplace] user grant failed for subscription %s (user -> agent %s)",
                sub.id, sub.product_id,
            )
            sub.status = SubscriptionStatus.FAILED
            sub.error = "Grant application failed; see backend logs and retry."
            sub.decided_by = decided_by
            sub.decided_at = _now()
            sub.updated_at = _now()
            self._save(sub)
            raise MarketplaceError("Grant application failed", kind="grant_failed") from None

        sub.status = SubscriptionStatus.APPROVED
        sub.grant_assignment_id = assignment_id
        sub.error = None
        sub.decided_by = decided_by
        sub.decided_at = _now()
        sub.updated_at = _now()
        self._save(sub)
        return sub

    def _active_sub_for(
        self,
        ptype: ProductType,
        product_id: str,
        requester_oid: str,
        agent_id: Optional[str],
    ) -> Optional[Subscription]:
        """Idempotency lookup over pending/approved subs. Agent key = (oid, product_id);
        MCP key = (oid, agent_id, product_id)."""
        for s in self._load_subs():
            if s.status not in _ACTIVE_STATUSES:
                continue
            if s.product_type != ptype or s.product_id != product_id or s.requester_oid != requester_oid:
                continue
            if ptype is ProductType.MCP and s.agent_id != agent_id:
                continue
            return s
        return None

    def _latest_sub_by_product(self, caller_oid: str, ptype: ProductType) -> Dict[str, Subscription]:
        """The caller's latest subscription per product_id (for the card my_status merge)."""
        latest: Dict[str, Subscription] = {}
        for s in self._load_subs():
            if s.requester_oid != caller_oid or s.product_type != ptype:
                continue
            cur = latest.get(s.product_id)
            if cur is None or s.created_at > cur.created_at:
                latest[s.product_id] = s
        return latest

    # -- persistence (DDB-or-local) ----------------------------------------

    def _get_sub(self, subscription_id: str) -> Optional[Subscription]:
        if self._has_ddb:
            try:
                resp = self._table.get_item(Key={"pk": _PARTITION_KEY, "sk": subscription_id})
                item = resp.get("Item")
                if not item or item.get("_record_kind") != _KIND_SUBSCRIPTION:
                    return None
                return self._sub_from_item(item)
            except ClientError:
                logger.exception("Failed to fetch subscription %s from DDB", subscription_id)
                return None
        with self._local_lock:
            sub = self._local_subs.get(subscription_id)
            return sub.model_copy(deep=True) if sub else None

    def _load_subs(self) -> List[Subscription]:
        if self._has_ddb:
            try:
                items = self._scan_partition()
                return [self._sub_from_item(i) for i in items if i.get("_record_kind") == _KIND_SUBSCRIPTION]
            except ClientError:
                logger.exception("Failed to load subscriptions from DDB")
                return []
        with self._local_lock:
            return [s.model_copy(deep=True) for s in self._local_subs.values()]

    def _load_listings(self) -> Dict[str, Listing]:
        if self._has_ddb:
            try:
                items = self._scan_partition()
                out: Dict[str, Listing] = {}
                for i in items:
                    if i.get("_record_kind") != _KIND_LISTING:
                        continue
                    listing = self._listing_from_item(i)
                    out[_listing_sk(listing.product_type.value, listing.product_id)] = listing
                return out
            except ClientError:
                logger.exception("Failed to load listings from DDB")
                return {}
        with self._local_lock:
            return {k: v.model_copy(deep=True) for k, v in self._local_listings.items()}

    def _load_publish_requests(self) -> Dict[str, PublishRequest]:
        """Every publish record, keyed by ``_publish_sk(product_type, product_id)`` — the
        ``_load_listings`` shape (one full-partition read, kind-filtered)."""
        if self._has_ddb:
            try:
                items = self._scan_partition()
                out: Dict[str, PublishRequest] = {}
                for i in items:
                    if i.get("_record_kind") != _KIND_PUBLISH:
                        continue
                    req = self._publish_from_item(i)
                    out[_publish_sk(req.product_type, req.product_id)] = req
                return out
            except ClientError:
                logger.exception("Failed to load publish requests from DDB")
                return {}
        with self._local_lock:
            return {k: v.model_copy(deep=True) for k, v in self._local_publish.items()}

    def _get_publish_for_product(self, product_type, product_id: str) -> Optional[PublishRequest]:
        return self._load_publish_requests().get(_publish_sk(product_type, product_id))

    def _get_publish_request(self, request_id: str) -> Optional[PublishRequest]:
        """Lookup by request id. The sk is keyed by PRODUCT (one record per product), so an id
        lookup is a scan of the loaded records rather than a ``get_item``."""
        for req in self._load_publish_requests().values():
            if req.id == request_id:
                return req
        return None

    def _save_publish(self, req: PublishRequest) -> None:
        key = _publish_sk(req.product_type, req.product_id)
        if self._has_ddb:
            try:
                self._table.put_item(Item=self._publish_to_item(req))
                return
            except ClientError:
                logger.exception("Failed to save publish request %s to DDB", key)
        with self._local_lock:
            self._local_publish[key] = req.model_copy(deep=True)

    def _save(self, sub: Subscription) -> None:
        if self._has_ddb:
            try:
                self._table.put_item(Item=self._sub_to_item(sub))
                return
            except ClientError:
                logger.exception("Failed to save subscription %s to DDB", sub.id)
        with self._local_lock:
            self._local_subs[sub.id] = sub.model_copy(deep=True)

    def _save_listing(self, listing: Listing) -> None:
        key = _listing_sk(listing.product_type.value, listing.product_id)
        if self._has_ddb:
            try:
                self._table.put_item(Item=self._listing_to_item(listing))
                return
            except ClientError:
                logger.exception("Failed to save listing %s to DDB", key)
        with self._local_lock:
            self._local_listings[key] = listing.model_copy(deep=True)

    def _scan_partition(self) -> List[dict]:
        items: List[dict] = []
        kwargs = {"KeyConditionExpression": Key("pk").eq(_PARTITION_KEY)}
        while True:
            resp = self._table.query(**kwargs)
            items.extend(resp.get("Items", []))
            lek = resp.get("LastEvaluatedKey")
            if not lek:
                break
            kwargs["ExclusiveStartKey"] = lek
        return items

    def _sub_to_item(self, sub: Subscription) -> dict:
        return {
            "pk": _PARTITION_KEY,
            "sk": sub.id,
            "_record_kind": _KIND_SUBSCRIPTION,
            **json.loads(sub.model_dump_json()),
        }

    def _sub_from_item(self, item: dict) -> Subscription:
        clean = {k: v for k, v in item.items() if k not in ("pk", "sk", "_record_kind")}
        return Subscription.model_validate(clean)

    def _listing_to_item(self, listing: Listing) -> dict:
        return {
            "pk": _PARTITION_KEY,
            "sk": _listing_sk(listing.product_type.value, listing.product_id),
            "_record_kind": _KIND_LISTING,
            **json.loads(listing.model_dump_json()),
        }

    def _listing_from_item(self, item: dict) -> Listing:
        clean = {k: v for k, v in item.items() if k not in ("pk", "sk", "_record_kind")}
        return Listing.model_validate(clean)

    def _publish_to_item(self, req: PublishRequest) -> dict:
        return {
            "pk": _PARTITION_KEY,
            "sk": _publish_sk(req.product_type, req.product_id),
            "_record_kind": _KIND_PUBLISH,
            **json.loads(req.model_dump_json()),
        }

    def _publish_from_item(self, item: dict) -> PublishRequest:
        clean = {k: v for k, v in item.items() if k not in ("pk", "sk", "_record_kind")}
        return PublishRequest.model_validate(clean)


def _neg_ts(dt: datetime) -> float:
    """Negative epoch seconds — for a single descending sort key alongside the
    pending-first discriminator (created_at desc)."""
    return -dt.timestamp()
