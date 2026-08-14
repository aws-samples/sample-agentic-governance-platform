"""Tests for the marketplace Pydantic models (E9 T1)."""

import json
from datetime import datetime, timezone

import pytest

from models.marketplace import (
    Datasheet,
    MarketplaceMetrics,
    MarketplacePublication,
    ProductCard,
    ProductType,
    PublishRequest,
    PublishRequestCreate,
    PublishRequestStatus,
    RevokeRequest,
    Subscription,
    SubscriptionCreate,
    SubscriptionStatus,
)

FIXED_NOW = datetime(2026, 6, 7, 12, 0, 0, tzinfo=timezone.utc)


def test_subscription_round_trips_through_json():
    agent_sub = Subscription(
        id="mkt-abc1234567",
        product_type=ProductType.AGENT,
        product_id="bp-fnol",
        product_name="First Notification of Loss (FNOL) Agent",
        requester_oid="oid-user-1",
        requester_email="user1@acme.com",
        status=SubscriptionStatus.PENDING,
        created_at=FIXED_NOW,
        updated_at=FIXED_NOW,
    )
    agent_round_tripped = Subscription.model_validate(
        json.loads(agent_sub.model_dump_json())
    )
    assert agent_round_tripped == agent_sub

    mcp_sub = Subscription(
        id="mkt-def8901234",
        product_type=ProductType.MCP,
        product_id="mcp-data-product",
        product_name="Data Product Gateway",
        agent_id="agt-1",
        agent_name="Contact Center Agent",
        agent_sp_id="sp-a",
        requester_oid="oid-user-2",
        requester_email="user2@acme.com",
        message="Need access for the claims flow",
        status=SubscriptionStatus.APPROVED,
        grant_assignment_id="asg-1",
        decided_by="admin@acme.com",
        decided_at=FIXED_NOW,
        created_at=FIXED_NOW,
        updated_at=FIXED_NOW,
    )
    mcp_round_tripped = Subscription.model_validate(
        json.loads(mcp_sub.model_dump_json())
    )
    assert mcp_round_tripped == mcp_sub


def test_subscription_status_enum_values():
    assert SubscriptionStatus.PENDING.value == "pending"
    assert SubscriptionStatus.APPROVED.value == "approved"
    assert SubscriptionStatus.REJECTED.value == "rejected"
    assert SubscriptionStatus.FAILED.value == "failed"
    assert SubscriptionStatus.REVOKED.value == "revoked"
    assert ProductType.AGENT.value == "agent"
    assert ProductType.MCP.value == "mcp"


def test_revoked_subscription_round_trips_with_revoke_fields():
    # A revoked MCP sub carries the 3 new revoke audit fields AND keeps its original
    # approval audit (decided_by/decided_at) — a revoke does NOT overwrite them.
    sub = Subscription(
        id="mkt-rev0000001",
        product_type=ProductType.MCP,
        product_id="mcp-data-product",
        product_name="Data Product Gateway",
        agent_id="agt-1",
        agent_name="Contact Center Agent",
        agent_sp_id="sp-a",
        requester_oid="oid-user-2",
        requester_email="user2@acme.com",
        status=SubscriptionStatus.REVOKED,
        grant_assignment_id="asg-1",
        decided_by="admin@acme.com",
        decided_at=FIXED_NOW,
        revoked_by="admin2@acme.com",
        revoked_at=FIXED_NOW,
        revoke_reason="offboarding the agent",
        created_at=FIXED_NOW,
        updated_at=FIXED_NOW,
    )
    round_tripped = Subscription.model_validate(json.loads(sub.model_dump_json()))
    assert round_tripped == sub
    assert round_tripped.status == SubscriptionStatus.REVOKED
    assert round_tripped.revoked_by == "admin2@acme.com"
    assert round_tripped.revoked_at == FIXED_NOW
    assert round_tripped.revoke_reason == "offboarding the agent"
    # Approval audit untouched by the revoke.
    assert round_tripped.decided_by == "admin@acme.com"
    assert round_tripped.decided_at == FIXED_NOW


def test_old_shape_subscription_without_revoke_fields_still_validates():
    # An OLD DDB record (predating the revoke fields) must validate with None defaults.
    old_item = {
        "id": "mkt-old0000001",
        "product_type": "agent",
        "product_id": "bp-fnol",
        "product_name": "FNOL Agent",
        "requester_oid": "oid-user-1",
        "status": "approved",
        "created_at": FIXED_NOW.isoformat(),
        "updated_at": FIXED_NOW.isoformat(),
    }
    sub = Subscription.model_validate(old_item)
    assert sub.revoked_by is None
    assert sub.revoked_at is None
    assert sub.revoke_reason is None


def test_revoke_request_validates_empty():
    assert RevokeRequest().reason is None
    assert RevokeRequest(reason="cleanup").reason == "cleanup"


def test_subscription_create_requires_product_fields():
    created = SubscriptionCreate(product_type="agent", product_id="bp-x")
    assert created.product_type == ProductType.AGENT
    assert created.product_id == "bp-x"

    with pytest.raises(pytest.importorskip("pydantic").ValidationError):
        SubscriptionCreate(product_type="agent")


def test_product_card_defaults():
    card = ProductCard(product_type="mcp", product_id="m1", name="N")
    assert card.capabilities == []
    assert card.available is True
    assert card.my_status is None
    # Agent datasheet fields are optional → None / empty-list by default, so records
    # (and MCP cards) lacking them validate fine.
    assert card.owner_team is None
    assert card.sla_tier is None
    assert card.consumers is None
    assert card.compliance == []
    assert card.guardrails == []


def test_product_card_datasheet_round_trips():
    """The agent DECLARED-datasheet fields survive a JSON round-trip with types (E33)."""
    card = ProductCard(
        product_type="agent",
        product_id="agt-fnol",
        name="FNOL Agent",
        owner_team="Claims Automation",
        support_contact="claims-automation@acme.com",
        sla_tier="Silver",
        support_hours="Business hours (CET)",
        lifecycle="GA",
        consumers=5,
        region="EU (Frankfurt)",
        version="1.8.0",
        data_classification="Confidential",
        declared_at="2026-08-10T12:00:00+00:00",
        compliance=["GDPR", "BaFin"],
        guardrails=["PII redaction"],
    )
    again = ProductCard.model_validate(json.loads(card.model_dump_json()))
    assert again == card
    assert again.support_contact == "claims-automation@acme.com"
    assert again.declared_at == "2026-08-10T12:00:00+00:00"
    assert again.consumers == 5
    assert again.compliance == ["GDPR", "BaFin"]


def test_product_card_drops_measured_telemetry_fields():
    """E33 deleted the five INVENTED/measured fields — a declared datasheet cannot assert
    uptime, latency, live status or ratings, so the card must not carry a home for them.
    Asserted via ``hasattr`` (not a ValidationError) because pydantic's default
    ``extra="ignore"`` silently DROPS an unknown kwarg rather than raising."""
    card = ProductCard(
        product_type="agent",
        product_id="agt-fnol",
        name="FNOL Agent",
        uptime_30d="99.9%",
        latency_p95_ms=540,
        status="Operational",
        rating=4.3,
        rating_count=27,
    )
    for gone in ("uptime_30d", "latency_p95_ms", "status", "rating", "rating_count"):
        assert not hasattr(card, gone), f"ProductCard still carries {gone}"
    # And they are absent from the serialized read-model the frontend types.
    dumped = json.loads(card.model_dump_json())
    for gone in ("uptime_30d", "latency_p95_ms", "status", "rating", "rating_count"):
        assert gone not in dumped


# ---------------------------------------------------------------------------
# E33 — declared datasheet + publish request (contract C1)
# ---------------------------------------------------------------------------

def _datasheet(**overrides) -> Datasheet:
    """A minimal VALID datasheet (the 3 mandatory fields), overridable."""
    fields = {
        "owner_team": "Claims Automation",
        "support_contact": "claims-automation@acme.com",
        "data_classification": "Confidential",
    }
    fields.update(overrides)
    return Datasheet(**fields)


def test_datasheet_requires_the_three_mandatory_fields():
    """owner_team / support_contact / data_classification are mandatory — a publisher
    cannot declare a datasheet that omits who owns it, who to call, or how the data
    is classified."""
    from pydantic import ValidationError

    assert _datasheet().owner_team == "Claims Automation"

    for missing in ("owner_team", "support_contact", "data_classification"):
        payload = {
            "owner_team": "Claims Automation",
            "support_contact": "claims-automation@acme.com",
            "data_classification": "Confidential",
        }
        payload.pop(missing)
        with pytest.raises(ValidationError):
            Datasheet(**payload)


def test_datasheet_optionals_default():
    """Everything beyond the 3 mandatory fields is optional → None / empty list, so a
    thin declaration validates (the UI omits empty fields, no "—" placeholders)."""
    sheet = _datasheet()
    assert sheet.sla_tier is None
    assert sheet.support_hours is None
    assert sheet.version is None
    assert sheet.region is None
    assert sheet.pitch is None
    assert sheet.compliance == []
    assert sheet.guardrails == []


def test_datasheet_support_contact_min_length():
    """``support_contact`` is a plain ``str`` with a min length (NOT pydantic ``EmailStr``
    — the platform models take no extra validator dependency), so it cannot be blank."""
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        _datasheet(support_contact="")


def test_publish_request_status_enum_values():
    assert PublishRequestStatus.PENDING.value == "pending"
    assert PublishRequestStatus.APPROVED.value == "approved"
    assert PublishRequestStatus.REJECTED.value == "rejected"


def test_publish_request_round_trips_through_json():
    """The DDB serialization idiom (``json.loads(model_dump_json())`` →
    ``model_validate``) must round-trip a PublishRequest, nested datasheet included."""
    req = PublishRequest(
        id="pub-abc1234567",
        product_type=ProductType.AGENT,
        product_id="agt-1",
        product_name="FNOL Agent",
        tenant_id="ten-claims",
        datasheet=_datasheet(
            sla_tier="Gold",
            compliance=["GDPR", "BaFin"],
            support_hours="24/7",
            version="1.8.0",
            region="EU (Frankfurt)",
            guardrails=["PII redaction"],
            pitch="Automates first notification of loss intake.",
        ),
        status=PublishRequestStatus.PENDING,
        requested_by="oid-publisher-1",
        requested_by_email="publisher@acme.com",
        created_at=FIXED_NOW,
        updated_at=FIXED_NOW,
    )
    again = PublishRequest.model_validate(json.loads(req.model_dump_json()))
    assert again == req
    assert again.datasheet.compliance == ["GDPR", "BaFin"]
    assert again.status == PublishRequestStatus.PENDING
    assert again.product_type == ProductType.AGENT
    # Decision audit + the safe-literal error field default to None on a PENDING record.
    assert again.decided_by is None
    assert again.decided_at is None
    assert again.decision_reason is None
    assert again.error is None


def test_publish_request_carries_product_type_for_mcp():
    """Amendment 1 (C9): publish is the ONE door for BOTH product types, so the record is
    keyed by the (product_type, product_id) PAIR — an MCP request is indistinguishable from
    an agent one except for ``product_type``. The old agent-only ``agent_id``/``agent_name``
    names are gone (pydantic's default ``extra="ignore"`` would silently DROP them, which is
    why this asserts on the model fields, not on a ValidationError)."""
    req = PublishRequest(
        id="pub-mcp0000001",
        product_type=ProductType.MCP,
        product_id="mcp-rec-123",
        product_name="internal-claims-mcp",
        tenant_id="ten-claims",
        datasheet=_datasheet(),
        status=PublishRequestStatus.PENDING,
        requested_by="oid-publisher-3",
        created_at=FIXED_NOW,
        updated_at=FIXED_NOW,
    )
    again = PublishRequest.model_validate(json.loads(req.model_dump_json()))
    assert again == req
    assert again.product_type == ProductType.MCP
    assert again.product_id == "mcp-rec-123"
    assert again.product_name == "internal-claims-mcp"
    for gone in ("agent_id", "agent_name"):
        assert gone not in PublishRequest.model_fields
        assert not hasattr(again, gone)


def test_publish_request_requires_product_type():
    """``product_type`` has no default — a record that does not say WHICH registry holds the
    product cannot be dispatched to the right ``persist_marketplace`` on approve."""
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        PublishRequest(
            id="pub-nopt00001",
            product_id="agt-1",
            product_name="FNOL Agent",
            datasheet=_datasheet(),
            status=PublishRequestStatus.PENDING,
            requested_by="oid-publisher-1",
            created_at=FIXED_NOW,
            updated_at=FIXED_NOW,
        )


def test_publish_request_decided_round_trips_with_audit_fields():
    req = PublishRequest(
        id="pub-def8901234",
        product_type=ProductType.AGENT,
        product_id="agt-2",
        product_name="Contact Center Agent",
        datasheet=_datasheet(),
        status=PublishRequestStatus.REJECTED,
        requested_by="oid-publisher-2",
        decided_by="admin@acme.com",
        decided_at=FIXED_NOW,
        decision_reason="datasheet incomplete",
        created_at=FIXED_NOW,
        updated_at=FIXED_NOW,
    )
    again = PublishRequest.model_validate(json.loads(req.model_dump_json()))
    assert again == req
    assert again.status == PublishRequestStatus.REJECTED
    assert again.decided_by == "admin@acme.com"
    assert again.tenant_id is None


def test_publish_request_create_shape():
    """The request BODY carries only the product (type + id) + the datasheet — never the
    requester (which is derived from the validated principal) and never a status."""
    body = PublishRequestCreate.model_validate(
        {
            "product_type": "agent",
            "product_id": "agt-1",
            "datasheet": {
                "owner_team": "Claims Automation",
                "support_contact": "claims-automation@acme.com",
                "data_classification": "Confidential",
            },
        }
    )
    assert body.product_type == ProductType.AGENT
    assert body.product_id == "agt-1"
    assert body.datasheet.support_contact == "claims-automation@acme.com"

    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        PublishRequestCreate(product_type="agent", product_id="agt-1")  # no datasheet
    with pytest.raises(ValidationError):
        # product_type is REQUIRED — the route must never have to guess the registry.
        PublishRequestCreate(product_id="agt-1", datasheet=_datasheet())
    assert "agent_id" not in PublishRequestCreate.model_fields


def test_publish_request_create_accepts_mcp_product_type():
    body = PublishRequestCreate.model_validate(
        {
            "product_type": "mcp",
            "product_id": "mcp-rec-123",
            "datasheet": {
                "owner_team": "Platform",
                "support_contact": "platform@acme.com",
                "data_classification": "Internal",
            },
        }
    )
    assert body.product_type == ProductType.MCP
    assert body.product_id == "mcp-rec-123"


def test_marketplace_publication_round_trips_through_json():
    """What ``approve`` writes into the agent envelope. Service-written ONLY: the
    attestation fields (``declared_by``/``declared_at``) come from the principal."""
    pub = MarketplacePublication(
        published=True,
        datasheet=_datasheet(sla_tier="Gold"),
        declared_by="admin@acme.com",
        declared_at=FIXED_NOW,
    )
    again = MarketplacePublication.model_validate(json.loads(pub.model_dump_json()))
    assert again == pub
    assert again.published is True
    assert again.datasheet.sla_tier == "Gold"


def test_marketplace_publication_published_defaults_false():
    """``published`` defaults False so an unpublish (which KEEPS the block, retaining the
    declared history) is expressible without a datasheet rewrite."""
    pub = MarketplacePublication(
        datasheet=_datasheet(),
        declared_by="admin@acme.com",
        declared_at=FIXED_NOW,
    )
    assert pub.published is False


def test_metrics_shape():
    metrics = MarketplaceMetrics(
        total=0,
        pending=0,
        approved=0,
        rejected=0,
        failed=0,
        approval_rate=0.0,
        by_type={},
        top_products=[],
    )
    assert metrics.total == 0
    assert metrics.approval_rate == 0.0
    assert metrics.by_type == {}
    assert metrics.top_products == []
    # `revoked` defaults to 0 so metrics built without it (old callers) still validate.
    assert metrics.revoked == 0


def test_metrics_accepts_revoked_count():
    metrics = MarketplaceMetrics(
        total=1,
        pending=0,
        approved=0,
        rejected=0,
        failed=0,
        revoked=1,
        approval_rate=0.0,
        by_type={},
        top_products=[],
    )
    assert metrics.revoked == 1


def test_config_has_marketplace_table_name():
    from core.config import Settings

    assert Settings().MARKETPLACE_TABLE_NAME == ""
