"""Pydantic models for the Marketplace: subscriptions, listings, the declared datasheet and the
publication record.

DynamoDB serialization (``_to_item`` / ``_from_item`` via ``json.loads(model_dump_json())``) lives in the
marketplace service. No boto3, no I/O, no AWS imports here.
"""

from datetime import datetime
from enum import Enum
from typing import List, Optional

from pydantic import Field

from models.base import WireModel


class ProductType(str, Enum):
    AGENT = "agent"
    MCP = "mcp"


class SubscriptionStatus(str, Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    FAILED = "failed"
    REVOKED = "revoked"


class Subscription(WireModel):
    id: str = Field(description='"mkt-<uuid10>"')
    product_type: ProductType
    product_id: str
    product_name: str
    agent_id: Optional[str] = Field(default=None, description="MCP products only: the subscribing agent")
    agent_name: Optional[str] = Field(default=None, description="MCP products only")
    agent_sp_id: Optional[str] = Field(
        default=None, description="MCP products only: the grant principal, captured at request time"
    )
    requester_oid: str
    requester_email: Optional[str] = None
    message: Optional[str] = None
    status: SubscriptionStatus
    auto_approved: bool = False
    grant_assignment_id: Optional[str] = None
    decided_by: Optional[str] = Field(default=None, description='The admin oid or email, or "auto"')
    decided_at: Optional[datetime] = None
    decision_reason: Optional[str] = None
    revoked_by: Optional[str] = Field(
        default=None,
        description=(
            "The admin who revoked an approved subscription; a revoke audit distinct from the approval "
            "audit, which it does not overwrite"
        ),
    )
    revoked_at: Optional[datetime] = None
    revoke_reason: Optional[str] = None
    error: Optional[str] = None
    created_at: datetime
    updated_at: datetime


class Listing(WireModel):
    product_type: ProductType
    product_id: str
    available: bool = True
    auto_approve: bool = False
    pitch: Optional[str] = None


class Datasheet(WireModel):
    """Declared, publisher-asserted product metadata; never measured telemetry.

    A publisher declares these and an admin approves the declaration, so anything the platform would
    have to measure to be truthful (uptime, latency, live status, ratings) has no field here and must
    not gain one: a declaration cannot assert a measurement. The three mandatory fields are the ones a
    consumer cannot act without: who owns it, who to call, how the data is classified.
    ``support_contact`` is a plain string rather than ``EmailStr`` so the models take no extra
    validator dependency; the must-contain-``@`` rule lives in the form-side validator, where the
    message can name the field.
    """

    owner_team: str = Field(..., min_length=1)
    support_contact: str = Field(
        ...,
        min_length=3,
        description="Who to call about the product: an address or handle, at least three characters",
    )
    data_classification: str = Field(..., min_length=1)
    sla_tier: Optional[str] = None
    compliance: List[str] = Field(default_factory=list)
    support_hours: Optional[str] = None
    version: Optional[str] = None
    region: Optional[str] = None
    guardrails: List[str] = Field(default_factory=list)
    pitch: Optional[str] = None


class MarketplacePublication(WireModel):
    """What approve writes into the product envelope (agent or MCP server). Service-written only.

    Lives on ``Agent`` / ``McpServer`` and their envelopes, never on a create or update model, so no
    request body can forge the attestation: ``declared_by`` comes from the validated principal. An
    unpublish keeps this block with ``published=False`` rather than clearing it, so the declared history
    survives the delisting. One model for both product types: publish is the only door into the
    marketplace, so the two ``to_envelope()`` methods are the only write sites.
    """

    published: bool = False
    datasheet: Datasheet
    declared_by: str = Field(
        description="The approving admin's oid or email, from the principal, never a body"
    )
    declared_at: datetime


class PublishRequestStatus(str, Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


class PublishRequest(WireModel):
    """One product's marketplace-publication request, keyed by the ``(product_type, product_id)`` pair;
    a re-publish overwrites it and the decision history lives in the audit fields. ``product_type``
    has no default because it tells ``approve_publish`` which registry's ``persist_marketplace`` to
    dispatch the envelope write to. ``error`` is always one of the service's fixed sentences, never
    an exception's text."""

    id: str = Field(description='"pub-<uuid10>"')
    product_type: ProductType
    product_id: str
    product_name: str
    tenant_id: Optional[str] = None
    datasheet: Datasheet
    status: PublishRequestStatus
    requested_by: str = Field(description="The publisher's oid")
    requested_by_email: Optional[str] = None
    decided_by: Optional[str] = Field(default=None, description="The admin's oid or email")
    decided_at: Optional[datetime] = None
    decision_reason: Optional[str] = None
    error: Optional[str] = Field(
        default=None,
        description=(
            "A fixed sentence set when the registry envelope write fails on approve; the request stays "
            "pending and retryable"
        ),
    )
    created_at: datetime
    updated_at: datetime


class PublishRequestCreate(WireModel):
    """The publish-request body: which product, and the declaration. Never the requester (derived from
    the validated principal) and never a status or decision field. ``product_type`` is required rather
    than inferred: the two registries' id spaces are not distinguishable by shape, so guessing would let
    a publisher aim a request at the wrong registry."""

    product_type: ProductType
    product_id: str
    datasheet: Datasheet


class ProductCard(WireModel):
    """The catalog read model for the frontend: catalog plus listing plus the caller's own status.

    The datasheet fields are declared values projected from the approved ``Datasheet`` on the product's
    envelope, all optional so records lacking them validate; the UI omits an empty field. The measured
    fields a card once carried (``uptime_30d``, ``latency_p95_ms``, ``status``, ``rating``,
    ``rating_count``) are deleted, not re-pointed: a declared datasheet cannot truthfully state a
    measurement, and they belong to a telemetry read path that does not exist yet.
    """

    product_type: ProductType
    product_id: str
    name: str
    pitch: Optional[str] = None
    category: Optional[str] = None
    capabilities: List[str] = Field(default_factory=list)
    icon: Optional[str] = None
    available: bool = True
    auto_approve: bool = False
    kind: Optional[str] = Field(default=None, description="MCP products only, e.g. gateway")
    owner_email: Optional[str] = None
    business_unit: Optional[str] = None
    data_classification: Optional[str] = Field(default=None, description="A DataClassification value")
    region: Optional[str] = Field(default=None, description="Business or data region (DE, EU, ...)")
    version: Optional[str] = Field(default=None, description="server.json version for an MCP product")
    created_at: Optional[str] = Field(default=None, description="ISO string")
    updated_at: Optional[str] = Field(default=None, description="ISO string")
    owner_team: Optional[str] = None
    support_contact: Optional[str] = None
    sla_tier: Optional[str] = Field(default=None, description="Gold, Silver or Bronze")
    support_hours: Optional[str] = None
    lifecycle: Optional[str] = Field(default=None, description="Experimental, Beta, GA or Deprecated")
    consumers: Optional[int] = Field(
        default=None, description="Subscribing teams, computed live in the service"
    )
    declared_at: Optional[str] = Field(
        default=None,
        description=(
            "ISO string of the publication's declared_at: the card's provenance signal, which the Preview "
            "disclosure keys on"
        ),
    )
    compliance: List[str] = Field(default_factory=list)
    guardrails: List[str] = Field(default_factory=list)
    tenant_id: Optional[str] = Field(
        default=None, description="MCP cards only; agent blueprint cards stay global with None"
    )
    tenant_name: Optional[str] = Field(
        default=None, description="Display only, resolved from the cached tenant list; None when unresolvable"
    )
    published: bool = False
    shared: bool = False
    my_status: Optional[SubscriptionStatus] = Field(
        default=None, description="The caller's subscription state for this product; None if never subscribed"
    )
    my_subscription_id: Optional[str] = None
    my_agent_id: Optional[str] = Field(
        default=None, description="MCP products only: the agent the caller subscribed"
    )


class SubscriptionCreate(WireModel):
    product_type: ProductType
    product_id: str
    agent_id: Optional[str] = Field(default=None, description="Required when product_type is mcp")
    message: Optional[str] = None


class ListingUpdate(WireModel):
    available: Optional[bool] = None
    auto_approve: Optional[bool] = None
    pitch: Optional[str] = None


class RejectRequest(WireModel):
    reason: Optional[str] = None


class RevokeRequest(WireModel):
    reason: Optional[str] = None


class ProductTypeCounts(WireModel):
    agent: int
    mcp: int


class TopProduct(WireModel):
    product_id: str
    product_name: str
    count: int


class MarketplaceMetrics(WireModel):
    total: int
    pending: int
    approved: int
    rejected: int
    failed: int
    revoked: int = 0
    approval_rate: float = Field(
        description="approved / (approved + rejected); 0.0 when the denominator is 0"
    )
    by_type: ProductTypeCounts = Field(description='{"agent": n, "mcp": n}')
    top_products: List[TopProduct] = Field(
        description="[{product_id, product_name, count}], the top 5 by subscription count"
    )
