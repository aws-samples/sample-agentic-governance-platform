"""Pydantic models for the Marketplace (E9).

Plain ``BaseModel``s with ``str, Enum`` enums — the standard platform model idiom.
DynamoDB serialization (``_to_item``/``_from_item`` via ``json.loads(model_dump_json())``)
lives in the marketplace service, NOT here. No boto3, no I/O, no AWS imports — pure models.
"""

from datetime import datetime
from enum import Enum
from typing import List, Optional

from pydantic import BaseModel, Field


class ProductType(str, Enum):
    AGENT = "agent"
    MCP = "mcp"


class SubscriptionStatus(str, Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    FAILED = "failed"
    REVOKED = "revoked"


class Subscription(BaseModel):
    id: str                                  # "mkt-<uuid10>"
    product_type: ProductType
    product_id: str
    product_name: str
    agent_id: Optional[str] = None           # MCP-only
    agent_name: Optional[str] = None         # MCP-only
    agent_sp_id: Optional[str] = None        # MCP-only (grant principal, captured at request)
    requester_oid: str
    requester_email: Optional[str] = None
    message: Optional[str] = None
    status: SubscriptionStatus
    auto_approved: bool = False
    grant_assignment_id: Optional[str] = None
    decided_by: Optional[str] = None         # admin oid/email, or "auto"
    decided_at: Optional[datetime] = None
    decision_reason: Optional[str] = None
    # Revoke audit (E9R) — set when an admin revokes an approved subscription. Distinct
    # from the approval audit above (a revoke does NOT overwrite decided_by/decided_at).
    revoked_by: Optional[str] = None         # admin oid/email
    revoked_at: Optional[datetime] = None
    revoke_reason: Optional[str] = None
    error: Optional[str] = None
    created_at: datetime
    updated_at: datetime


class Listing(BaseModel):
    product_type: ProductType
    product_id: str
    available: bool = True
    auto_approve: bool = False
    pitch: Optional[str] = None


# ---------------------------------------------------------------------------
# Declared datasheet + marketplace publication (Epic 33)
# ---------------------------------------------------------------------------

class Datasheet(BaseModel):
    """Declared (publisher-asserted) product metadata. NO measured telemetry here ever.

    E31F removed the curated blueprint datasheet because it INVENTED values the platform
    could not observe; E33 brings the same field names back as an ATTESTATION — a publisher
    declares them and an admin approves the declaration. So anything the platform would have
    to MEASURE to be truthful (uptime, latency, live status, ratings) has no field here, and
    must not gain one: a declaration cannot assert a measurement. The three mandatory fields
    are the ones a consumer cannot act without — who owns it, who to call, how the data is
    classified.
    """

    owner_team: str = Field(..., min_length=1)
    # A plain ``str``, deliberately NOT pydantic's ``EmailStr``: the platform models take no
    # extra validator dependency (``email-validator``), and the "must contain @" rule lives in
    # the form-side validator where the message can name the field. min_length guards the
    # blank-string case that an all-Optional body would otherwise smuggle through.
    support_contact: str = Field(..., min_length=3)
    data_classification: str = Field(..., min_length=1)
    # Everything below is optional — a thin declaration is valid and the UI omits empty
    # fields (no "—" placeholders), exactly like the F3 governance chips.
    sla_tier: Optional[str] = None
    compliance: List[str] = Field(default_factory=list)
    support_hours: Optional[str] = None
    version: Optional[str] = None
    region: Optional[str] = None
    guardrails: List[str] = Field(default_factory=list)
    pitch: Optional[str] = None


class MarketplacePublication(BaseModel):
    """What approve writes into the PRODUCT envelope (agent or MCP). Service-written ONLY.

    Lives on ``Agent``/``McpServer`` + their envelopes only (the ``project_id`` convention —
    see ``models/agent.py``), so no request body can forge the attestation: ``declared_by``
    comes from the validated principal. An UNPUBLISH keeps this block with
    ``published=False`` rather than clearing it, so the declared history survives the
    delisting.

    ONE model for both product types (Amendment 1 / contract C8): publish is the only door
    into the marketplace for agents and MCP servers alike, so the two ``to_envelope()``
    methods are the only write sites and neither owns a private shape.
    """

    published: bool = False
    datasheet: Datasheet
    declared_by: str                         # approving admin oid/email — from principal, never body
    declared_at: datetime


class PublishRequestStatus(str, Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


class PublishRequest(BaseModel):
    """One PRODUCT's marketplace-publication request. ONE record per product (a re-publish
    overwrites it); the decision history lives in the audit fields below, not in extra rows.

    Amendment 1 (contract C9) generalized this from agents to both product types: the record
    is keyed by the ``(product_type, product_id)`` PAIR, and ``product_type`` is what tells
    ``approve_publish`` WHICH registry's ``persist_marketplace`` to dispatch the envelope
    write to. It therefore has no default — a request that does not say which registry holds
    its product is not actionable. (The old ``agent_id``/``agent_name`` names are gone;
    nothing is deployed, so there is no stored-record migration.)
    """

    id: str                                  # "pub-<uuid10>"
    product_type: ProductType
    product_id: str
    product_name: str
    tenant_id: Optional[str] = None
    datasheet: Datasheet
    status: PublishRequestStatus
    requested_by: str                        # publisher oid
    requested_by_email: Optional[str] = None
    decided_by: Optional[str] = None         # admin oid/email
    decided_at: Optional[datetime] = None
    decision_reason: Optional[str] = None
    # A FIXED safe literal set when the registry envelope write fails on approve (the
    # request stays PENDING and stays retryable) — never ``str(exc)``.
    error: Optional[str] = None
    created_at: datetime
    updated_at: datetime


class PublishRequestCreate(BaseModel):
    """The publish-request BODY: which product, and the declaration. Never the requester
    (derived from the validated principal) and never a status/decision field.

    ``product_type`` is required rather than inferred: the id spaces of the two registries
    are not distinguishable by shape, so guessing would let a publisher aim a request at the
    wrong registry."""

    product_type: ProductType
    product_id: str
    datasheet: Datasheet


class ProductCard(BaseModel):                # read-model for the FE (catalog ⊕ listing ⊕ caller status)
    product_type: ProductType
    product_id: str
    name: str
    pitch: Optional[str] = None
    category: Optional[str] = None
    capabilities: List[str] = Field(default_factory=list)
    icon: Optional[str] = None
    available: bool = True
    auto_approve: bool = False
    # MCP-only display:
    kind: Optional[str] = None               # "gateway"
    owner_email: Optional[str] = None
    business_unit: Optional[str] = None
    # MCP governance metadata (F3 — surfaced as chips on the card; click-through to
    # the registry detail page). All Optional + tolerant of records missing them.
    data_classification: Optional[str] = None  # DataClassification.value (e.g. "Confidential")
    region: Optional[str] = None               # business/data region (DE/EU/…)
    version: Optional[str] = None              # server.json version (e.g. "1.0.0")
    created_at: Optional[str] = None           # ISO string (serialized from the datetime)
    updated_at: Optional[str] = None           # ISO string
    # Agent "mesh product" datasheet — DECLARED values (E33), projected from the approved
    # ``Datasheet`` on the agent's envelope. All Optional + tolerant of records missing them —
    # the UI omits any empty field (no "—" placeholders), exactly like the F3 governance chips.
    #
    # The five MEASURED fields E31F stopped rendering (``uptime_30d``, ``latency_p95_ms``,
    # ``status``, ``rating``, ``rating_count``) are DELETED rather than re-pointed: a
    # publisher-asserted datasheet cannot truthfully declare a measurement, and leaving the
    # fields typed-but-empty is what let the invented values survive the first cleanup. Do not
    # re-add them here — they belong to a telemetry read path that does not exist yet.
    owner_team: Optional[str] = None           # owning team label (e.g. "Claims Automation")
    support_contact: Optional[str] = None      # declared support address (e.g. a team mailbox)
    # Service & reliability
    sla_tier: Optional[str] = None             # "Gold" | "Silver" | "Bronze"
    support_hours: Optional[str] = None        # e.g. "24/7" | "Business hours (CET)"
    lifecycle: Optional[str] = None            # "Experimental" | "Beta" | "GA" | "Deprecated"
    consumers: Optional[int] = None            # # subscribing teams — COMPUTED LIVE in the service
    # When the declaration was attested (E33) — ISO string, serialized from the publication's
    # ``declared_at``. This is the card's provenance signal: a card carrying it has a DECLARED
    # datasheet behind it, which is what the "Preview" disclosure keys on.
    declared_at: Optional[str] = None          # ISO string
    # Governance & compliance (data_classification + region above are reused for agents too)
    compliance: List[str] = Field(default_factory=list)   # e.g. ["GDPR", "BaFin", "SOC 2"]
    guardrails: List[str] = Field(default_factory=list)   # e.g. ["PII redaction", "Human-in-the-loop"]
    # Tenant badge (E24/T8 — MCP cards only; agent BLUEPRINT cards stay global with
    # tenant_id None). tenant_name is resolved display-only from the cached tenant
    # list (None when unresolvable); published/shared mirror the registry record.
    tenant_id: Optional[str] = None
    tenant_name: Optional[str] = None
    published: bool = False
    shared: bool = False
    # caller's own subscription state for THIS product (None if never subscribed):
    my_status: Optional[SubscriptionStatus] = None
    my_subscription_id: Optional[str] = None
    my_agent_id: Optional[str] = None        # MCP-only: which agent the caller subscribed


class SubscriptionCreate(BaseModel):
    product_type: ProductType
    product_id: str
    agent_id: Optional[str] = None           # required when product_type == mcp
    message: Optional[str] = None


class ListingUpdate(BaseModel):
    available: Optional[bool] = None
    auto_approve: Optional[bool] = None
    pitch: Optional[str] = None


class RejectRequest(BaseModel):
    reason: Optional[str] = None


class RevokeRequest(BaseModel):              # mirror RejectRequest
    reason: Optional[str] = None


class MarketplaceMetrics(BaseModel):
    total: int
    pending: int
    approved: int
    rejected: int
    failed: int
    revoked: int = 0
    approval_rate: float                     # approved / (approved+rejected), 0.0 when denom 0
    by_type: dict                            # {"agent": n, "mcp": n}
    top_products: List[dict]                 # [{product_id, product_name, count}], top 5 by sub count
