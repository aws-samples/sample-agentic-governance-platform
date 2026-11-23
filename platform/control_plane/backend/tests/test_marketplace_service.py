"""Tests for the marketplace lifecycle orchestrator.

Drives the in-memory path (``table_name=""``) and injects doubles for the registries and the grant
functions (``AsyncMock``), so no live Graph or AWS is touched. Fakes are ``types.SimpleNamespace``
objects shaped like the ``Agent`` / ``McpServer`` records the service reads, with plain strings where
the records carry ``str, Enum`` values.
"""

import asyncio
import inspect
import logging
import threading
import types
from datetime import datetime, timezone
from typing import Any
from unittest.mock import AsyncMock

import pytest
from botocore.exceptions import ClientError
from pydantic import ValidationError

from api.routes import grants as grants_routes
from core.state_store import StateStore
from models.agent import DataClassification
from models.marketplace import MarketplaceMetrics, ProductType, ProductTypeCounts, PublishRequest
from services import grant_mirror_service
from services.agent_mcp_grant import GrantNotFoundError, GrantRevokeFailedError
from services.agent_user_grant import UserGrantError, UserGrantNotFoundError
from services.marketplace_errors import MarketplaceError
from services.marketplace_grants import _AGENT_NOT_PROVISIONED
from services.marketplace_service import MarketplaceService
from services.platform_adapter import InvokeError

OID_A = "oid-alice"
OID_B = "oid-bob"
ADMIN = "oid-admin"


def _agent(
    agent_id,
    name,
    *,
    identity_status="provisioned",
    entra_sp_id: str | None = "sp-a",
    invoker_role_id="role-invoker",
    marketplace=None,
    tenant_id=None,
    lifecycle_state="approved",
    business_unit=None,
    data_classification=None,
    platform="aws_bedrock",
):
    """A fake shaped like an ``Agent`` record: the fields the card projection and the publish guards
    read (``marketplace``, ``tenant_id``, ``lifecycle_state``, ``business_unit``,
    ``data_classification``) and the three identity fields the grant guard requires. Pass
    ``invoker_role_id=None`` to drive the "provisioned but role id missing" branch."""
    return types.SimpleNamespace(
        id=agent_id,
        name=name,
        identity_status=identity_status,
        entra_sp_id=entra_sp_id,
        invoker_role_id=invoker_role_id,
        admin_role_id="role-admin",
        sponsor_oid=OID_A,
        entra_app_id="app-a",
        agent_arn="arn:aws:bedrock-agentcore:us-east-1:1:runtime/agent-a",
        oauth2_credential_provider_name=None,
        marketplace=marketplace,
        tenant_id=tenant_id,
        lifecycle_state=lifecycle_state,
        business_unit=business_unit,
        data_classification=data_classification,
        platform=platform,
    )


def _datasheet(**overrides):
    """A valid declared ``Datasheet`` (the 3 mandatory fields + whatever the test pins)."""
    from models.marketplace import Datasheet

    base: dict[str, Any] = dict(
        owner_team="Claims Automation",
        support_contact="claims-platform@acme.com",
        data_classification="Confidential",
    )
    base.update(overrides)
    return Datasheet(**base)


def _publication(*, published=True, declared_by=ADMIN, datasheet=None, declared_at=None):
    """A ``MarketplacePublication`` as the approve path would have written it."""
    from models.marketplace import MarketplacePublication

    return MarketplacePublication(
        published=published,
        datasheet=datasheet if datasheet is not None else _datasheet(),
        declared_by=declared_by,
        declared_at=declared_at or datetime(2026, 8, 1, 12, 0, 0, tzinfo=timezone.utc),
    )


def _mcp(
    mcp_id,
    name,
    *,
    kind="gateway",
    lifecycle_state="approved",
    tenant_id=None,
    published=False,
    shared=False,
    marketplace=None,
    identity_status="provisioned",
    entra_sp_id="sp-m",
    invoker_role_id="role-invoker",
):
    """A fake shaped like an ``McpServer`` record: the same ``marketplace`` block and identity fields
    an ``Agent`` carries (one predicate serves both types), plus the cross-tenant ``published`` /
    ``shared`` record flags, a different feature from marketplace publication.
    """
    return types.SimpleNamespace(
        id=mcp_id,
        name=name,
        kind=kind,
        lifecycle_state=lifecycle_state,
        tenant_id=tenant_id,
        published=published,
        shared=shared,
        marketplace=marketplace,
        identity_status=identity_status,
        entra_sp_id=entra_sp_id,
        invoker_role_id=invoker_role_id,
        admin_role_id="role-admin",
        gateway_url="https://gw.example/mcp",
        entra_app_audience="api://agp-mcp-m1",
        owner_email="owner@acme.com",
        business_unit="Claims",
        data_classification=DataClassification.CONFIDENTIAL,
        region="EU",
        version="2.1.0",
        created_at=datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc),
        updated_at=datetime(2026, 6, 7, 8, 9, 10, tzinfo=timezone.utc),
    )


class _AgentRegistry:
    """A registry fake; ``persist_calls`` records each ``persist_marketplace`` call, ``(id, publication)``."""

    def __init__(self, agents):
        self._agents = {a.id: a for a in agents}
        self.persist_calls = []
        self.persist_error = None

    def list(self, **_kwargs):
        return list(self._agents.values())

    def get(self, agent_id):
        return self._agents.get(agent_id)

    def persist_marketplace(self, agent_id, publication):
        """The real signature (``AgentRegistryService.persist_marketplace``): raises
        ``AgentNotFoundError`` on a missing id, otherwise writes the block and returns the
        agent. ``persist_error`` injects a failure (set it to a botocore ``ClientError`` to
        drive the TOCTOU seam where the real method's internal write raises)."""
        from services.agent_registry_service import AgentNotFoundError

        self.persist_calls.append((agent_id, publication))
        if self.persist_error is not None:
            raise self.persist_error
        agent = self._agents.get(agent_id)
        if agent is None:
            raise AgentNotFoundError(f"Agent {agent_id!r} not found")
        agent.marketplace = publication
        return agent


class _McpRegistry:
    """A registry fake; ``persist_calls`` records each ``persist_marketplace`` call, ``(id, publication)``."""

    def __init__(self, mcps):
        self._mcps = {m.id: m for m in mcps}
        self.persist_calls = []
        self.persist_error = None

    def list(self, **_kwargs):
        return list(self._mcps.values())

    def get(self, mcp_id):
        return self._mcps.get(mcp_id)

    def persist_marketplace(self, mcp_server_id, publication):
        """The real signature (``McpServerRegistryService.persist_marketplace``): raises
        ``McpServerNotFoundError`` on a missing id, otherwise writes the block and returns the
        record. ``persist_error`` injects a failure (a botocore ``ClientError`` drives the
        TOCTOU seam where the real method's internal write raises)."""
        from services.mcp_server_service import McpServerNotFoundError

        self.persist_calls.append((mcp_server_id, publication))
        if self.persist_error is not None:
            raise self.persist_error
        mcp = self._mcps.get(mcp_server_id)
        if mcp is None:
            raise McpServerNotFoundError(f"MCP server {mcp_server_id!r} not found")
        mcp.marketplace = publication
        return mcp


PROVISIONED_AGENT = "agent-prov"
UNPROVISIONED_AGENT = "agent-unprov"
GATEWAY_MCP = "m-gateway"
STANDARD_MCP = "m-standard"
UNPUBLISHED_MCP = "m-unpublished-gw"

CC_AGENT = "agent-contact-center"
FNOL_AGENT = "agent-fnol"
ONB_AGENT = "agent-onboarding"


def _product_agents():
    """The published agent products in the default fixture (3 cards).

    Datasheet values are declared and deliberately varied so the card-projection test can
    spot-check each field; none declares a measurement, because ``Datasheet`` has no field for one.
    All three are provisioned with distinct SP ids, because subscribing to an agent product applies
    a real user-to-agent grant.
    """
    return [
        _agent(
            CC_AGENT,
            "Contact Center Agent",
            entra_sp_id="sp-cc",
            invoker_role_id="role-invoker-cc",
            business_unit="Customer Service",
            marketplace=_publication(
                datasheet=_datasheet(
                    owner_team="Customer Service Platform",
                    support_contact="cc-platform@acme.com",
                    data_classification="Internal",
                    sla_tier="Gold",
                    support_hours="24/7",
                    version="2.4.1",
                    region="EU (Frankfurt)",
                    compliance=["GDPR", "SOC 2", "ISO 27001"],
                    guardrails=["PII redaction", "Human-in-the-loop"],
                    pitch="Customer profile lookup, interaction history, ticketing & callbacks.",
                )
            ),
        ),
        _agent(
            FNOL_AGENT,
            "First Notification of Loss (FNOL) Agent",
            entra_sp_id="sp-fnol",
            invoker_role_id="role-invoker-fnol",
            business_unit="Claims",
            marketplace=_publication(
                datasheet=_datasheet(
                    owner_team="Claims Automation",
                    support_contact="claims-automation@acme.com",
                    data_classification="Confidential",
                    sla_tier="Silver",
                    support_hours="Business hours (CET)",
                    version="1.8.0",
                    region="EU (Frankfurt)",
                    compliance=["GDPR", "BaFin"],
                    guardrails=["PII redaction"],
                    pitch="Start claims, check coverage, upload loss docs, estimate payout.",
                )
            ),
        ),
        _agent(
            ONB_AGENT,
            "Insurance Onboarding Agent",
            entra_sp_id="sp-onb",
            invoker_role_id="role-invoker-onb",
            business_unit="Onboarding",
            marketplace=_publication(
                datasheet=_datasheet(
                    owner_team="Digital Onboarding",
                    support_contact="onboarding@acme.com",
                    data_classification="Confidential",
                )
            ),
        ),
    ]


def _build_service(
    *,
    grant_return="asg-1",
    grant_side_effect=None,
    revoke_side_effect=None,
    agent_grant_return="uasg-1",
    agent_grant_side_effect=None,
    agent_revoke_side_effect=None,
    mcps=None,
    agents=None,
    tenant_service=None,
):
    """The in-memory service with fakes; returns ``(svc, grant_fn, revoke_fn)``.

    The default registries carry two plain agents, the three published agent products and four MCP
    servers pinning the corners of publication gating: a published gateway and a published standard
    server are products, an unpublished gateway and a published but not approved server are not.
    OID_A sponsors PROVISIONED_AGENT and OID_B is directly granted on it, so both subscribe without
    a Graph read failing closed. The agent grant pair is reached as ``svc._agent_grant_fn`` /
    ``svc._agent_revoke_fn``; its ids are distinct literals ("uasg-1" vs "asg-1") so an assertion
    can tell which pair produced a stored assignment id.
    """
    if agents is None:
        agents = [
            _agent(PROVISIONED_AGENT, "Provisioned Agent", identity_status="provisioned", entra_sp_id="sp-a"),
            _agent(UNPROVISIONED_AGENT, "Draft Agent", identity_status="none", entra_sp_id=None),
            *_product_agents(),
        ]
    if mcps is None:
        mcps = [
            _mcp(
                GATEWAY_MCP,
                "Gateway MCP",
                kind="gateway",
                lifecycle_state="approved",
                marketplace=_publication(
                    datasheet=_datasheet(
                        owner_team="MCP Platform",
                        support_contact="mcp-platform@acme.com",
                        data_classification="Confidential",
                        sla_tier="Gold",
                        support_hours="24/7",
                        compliance=["GDPR"],
                        guardrails=["Tool allow-list"],
                        pitch="Claims tooling over the governed gateway.",
                    )
                ),
            ),
            _mcp(
                STANDARD_MCP,
                "Standard MCP",
                kind="standard",
                lifecycle_state="approved",
                marketplace=_publication(),
            ),
            _mcp(
                UNPUBLISHED_MCP,
                "Unpublished Gateway",
                kind="gateway",
                lifecycle_state="approved",
                marketplace=None,
            ),
            _mcp(
                "m-pending",
                "Pending Gateway",
                kind="gateway",
                lifecycle_state="proposed",
                marketplace=_publication(),
            ),
        ]
    grant_fn = AsyncMock(return_value=grant_return)
    if grant_side_effect is not None:
        grant_fn.side_effect = grant_side_effect
    revoke_fn = AsyncMock(return_value=None)
    if revoke_side_effect is not None:
        revoke_fn.side_effect = revoke_side_effect
    agent_grant_fn = AsyncMock(return_value=agent_grant_return)
    if agent_grant_side_effect is not None:
        agent_grant_fn.side_effect = agent_grant_side_effect
    agent_revoke_fn = AsyncMock(return_value=None)
    if agent_revoke_side_effect is not None:
        agent_revoke_fn.side_effect = agent_revoke_side_effect

    async def _list_assignments(agent_sp_id):
        if agent_sp_id == "sp-a":
            return [{"principalId": OID_B, "principalType": "User"}]
        return []

    svc = MarketplaceService(
        table_name="",
        mcp_registry=_McpRegistry(mcps),
        agent_registry=_AgentRegistry(agents),
        mcp_graph=types.SimpleNamespace(list_assignments=AsyncMock(side_effect=_list_assignments)),
        grant_fn=grant_fn,
        revoke_fn=revoke_fn,
        agent_grant_fn=agent_grant_fn,
        agent_revoke_fn=agent_revoke_fn,
        tenant_service=tenant_service,
    )
    return svc, grant_fn, revoke_fn


def _run(coro):
    return asyncio.run(coro)


def test_list_agent_products_merges_status():
    svc, _, _ = _build_service()
    cards = svc.list_agent_products(caller_oid=OID_A)
    assert len(cards) == 3
    assert all(c.my_status is None for c in cards)

    _run(
        svc.create_subscription(
            product_type="agent",
            product_id="agent-fnol",
            requester_oid=OID_A,
            requester_email="a@x",
        )
    )
    cards = svc.list_agent_products(caller_oid=OID_A)
    fnol = next(c for c in cards if c.product_id == "agent-fnol")
    assert fnol.my_status == "pending"
    cards_b = svc.list_agent_products(caller_oid=OID_B)
    fnol_b = next(c for c in cards_b if c.product_id == "agent-fnol")
    assert fnol_b.my_status is None


def test_list_agent_products_carries_business_unit_and_classification():
    svc, _, _ = _build_service()
    cards = svc.list_agent_products(caller_oid=OID_A)
    by_id = {c.product_id: c for c in cards}

    assert by_id["agent-fnol"].business_unit == "Claims"
    assert by_id["agent-fnol"].data_classification == "Confidential"
    assert by_id["agent-contact-center"].business_unit == "Customer Service"
    assert by_id["agent-contact-center"].data_classification == "Internal"
    assert by_id["agent-onboarding"].business_unit == "Onboarding"
    assert by_id["agent-onboarding"].data_classification == "Confidential"
    assert by_id["agent-fnol"].pitch


def test_list_agent_products_come_from_published_registry_agents():
    """A product exists iff an agent carries an approved ``marketplace`` block with ``published=True``;
    an agent with no block, or an unpublished one, is absent from the catalog entirely."""
    svc, _, _ = _build_service()
    ids = {c.product_id for c in svc.list_agent_products(caller_oid=OID_A)}

    assert ids == {CC_AGENT, FNOL_AGENT, ONB_AGENT}
    assert PROVISIONED_AGENT not in ids
    assert UNPROVISIONED_AGENT not in ids


def test_list_agent_products_excludes_unpublished_declaration():
    """An UNPUBLISH keeps the declared block with ``published=False`` (history retained),
    and a card must not survive it — the delisting has to be real."""
    agents = [
        _agent("agent-live", "Live Agent", marketplace=_publication(published=True)),
        _agent("agent-delisted", "Delisted Agent", marketplace=_publication(published=False)),
    ]
    svc, _, _ = _build_service(agents=agents)
    ids = {c.product_id for c in svc.list_agent_products(caller_oid=OID_A)}
    assert ids == {"agent-live"}


def test_list_agent_products_excludes_a_non_approved_lifecycle():
    """LIFECYCLE is re-asserted on the READ, exactly as ``list_mcp_products`` does. Publication
    is gated at REQUEST time only and ``transition()`` writes the native status WITHOUT
    touching the marketplace block, so a DEPRECATED (or rejected) agent keeps a published
    block — and must NOT keep a card."""
    agents = [
        _agent("agent-live", "Live Agent", marketplace=_publication(published=True)),
        _agent(
            "agent-deprecated",
            "Retired Agent",
            lifecycle_state="deprecated",
            marketplace=_publication(published=True),
        ),
        _agent(
            "agent-rejected",
            "Rejected Agent",
            lifecycle_state="rejected",
            marketplace=_publication(published=True),
        ),
    ]
    svc, _, _ = _build_service(agents=agents)
    ids = {c.product_id for c in svc.list_agent_products(caller_oid=OID_A)}
    assert ids == {"agent-live"}


def test_list_agent_products_published_card_is_marketplace_wide():
    """Published cards are MARKETPLACE-WIDE (the MCP ``shared=true`` analogue): a
    published agent from ANOTHER tenant IS visible, so there is deliberately NO
    ``visible()`` filter on this read path. The tenant badge still says whose it is."""
    agents = [
        _agent("agent-mine", "Mine", tenant_id="ten-1", marketplace=_publication(published=True)),
        _agent("agent-theirs", "Theirs", tenant_id="ten-2", marketplace=_publication(published=True)),
    ]
    svc, _, _ = _build_service(agents=agents, tenant_service=_tenant_names_service())
    by_id = {c.product_id: c for c in svc.list_agent_products(caller_oid=OID_A)}

    assert set(by_id) == {"agent-mine", "agent-theirs"}
    assert by_id["agent-theirs"].tenant_id == "ten-2"
    assert by_id["agent-theirs"].tenant_name == "Marketing EU"
    assert by_id["agent-theirs"].published is True


def test_list_agent_products_carries_datasheet_fields():
    """The agent card projects the declared datasheet off the approved ``marketplace`` block, never a
    measurement (uptime, latency, live status and rating have no field to project into);
    ``declared_at`` is the card's provenance signal."""
    svc, _, _ = _build_service()
    by_id = {c.product_id: c for c in svc.list_agent_products(caller_oid=OID_A)}

    cc = by_id[CC_AGENT]
    assert cc.name == "Contact Center Agent"
    assert cc.owner_team == "Customer Service Platform"
    assert cc.support_contact == "cc-platform@acme.com"
    assert cc.sla_tier == "Gold"
    assert cc.support_hours == "24/7"
    assert cc.version == "2.4.1"
    assert cc.region == "EU (Frankfurt)"
    assert cc.data_classification == "Internal"
    assert "ISO 27001" in cc.compliance
    assert "PII redaction" in cc.guardrails
    assert cc.pitch == ("Customer profile lookup, interaction history, ticketing & callbacks.")
    assert cc.declared_at == datetime(2026, 8, 1, 12, 0, 0, tzinfo=timezone.utc).isoformat()
    assert cc.capabilities == []

    onb = by_id[ONB_AGENT]
    assert onb.owner_team == "Digital Onboarding"
    assert onb.sla_tier is None
    assert onb.support_hours is None
    assert onb.version is None
    assert onb.region is None
    assert onb.compliance == []
    assert onb.guardrails == []
    assert onb.pitch is None
    assert all(c.lifecycle is None for c in by_id.values())

    mcp = {c.product_id: c for c in svc.list_mcp_products(caller_oid=OID_A)}[GATEWAY_MCP]
    assert mcp.consumers is None


def test_list_mcp_products_carries_declared_datasheet():
    """The declared datasheet is projected onto the MCP card exactly like the agent card, including
    ``support_contact`` and the ``declared_at`` stamp; the three fields the registry owns as chips
    (data_classification, region, version) keep their registry value."""
    svc, _, _ = _build_service()
    by_id = {c.product_id: c for c in svc.list_mcp_products(caller_oid=OID_A)}

    gw = by_id[GATEWAY_MCP]
    assert gw.owner_team == "MCP Platform"
    assert gw.support_contact == "mcp-platform@acme.com"
    assert gw.sla_tier == "Gold"
    assert gw.support_hours == "24/7"
    assert gw.compliance == ["GDPR"]
    assert gw.guardrails == ["Tool allow-list"]
    assert gw.pitch == "Claims tooling over the governed gateway."
    assert gw.declared_at == datetime(2026, 8, 1, 12, 0, 0, tzinfo=timezone.utc).isoformat()
    assert gw.data_classification == DataClassification.CONFIDENTIAL.value
    assert gw.region == "EU"
    assert gw.version == "2.1.0"

    thin = by_id[STANDARD_MCP]
    assert thin.owner_team == "Claims Automation"
    assert thin.sla_tier is None
    assert thin.support_hours is None
    assert thin.compliance == []
    assert thin.guardrails == []
    assert thin.pitch is None


def test_list_mcp_products_listing_override_wins_over_the_declared_pitch():
    """The listing merge is shared with the agent projection: an admin's ``set_listing``
    overrides the declared pitch and owns available/auto_approve."""
    svc, _, _ = _build_service()
    svc.set_listing(
        product_type="mcp",
        product_id=GATEWAY_MCP,
        pitch="Admin-curated pitch",
        available=False,
        auto_approve=True,
    )
    by_id = {c.product_id: c for c in svc.list_mcp_products(caller_oid=OID_A)}

    assert by_id[GATEWAY_MCP].pitch == "Admin-curated pitch"
    assert by_id[GATEWAY_MCP].available is False
    assert by_id[GATEWAY_MCP].auto_approve is True
    assert by_id[STANDARD_MCP].available is True
    assert by_id[STANDARD_MCP].auto_approve is False


def test_list_agent_products_listing_overrides_still_win():
    """The listing merge is unchanged by the re-source: an admin's ``set_listing`` still
    overrides the declared pitch and owns available/auto_approve."""
    svc, _, _ = _build_service()
    svc.set_listing(
        product_type="agent",
        product_id=CC_AGENT,
        pitch="Admin-curated pitch",
        available=False,
        auto_approve=True,
    )
    by_id = {c.product_id: c for c in svc.list_agent_products(caller_oid=OID_A)}

    assert by_id[CC_AGENT].pitch == "Admin-curated pitch"
    assert by_id[CC_AGENT].available is False
    assert by_id[CC_AGENT].auto_approve is True
    pitch = by_id[FNOL_AGENT].pitch
    assert pitch is not None
    assert pitch.startswith("Start claims")
    assert by_id[FNOL_AGENT].available is True
    assert by_id[FNOL_AGENT].auto_approve is False


def test_list_agent_products_consumers_count_is_live():
    svc, _, _ = _build_service()
    base = next(
        c for c in svc.list_agent_products(caller_oid=OID_A) if c.product_id == "agent-fnol"
    ).consumers
    assert base == 0

    _run(
        svc.create_subscription(
            product_type="agent",
            product_id="agent-fnol",
            requester_oid=OID_A,
            requester_email="a@x",
        )
    )
    _run(
        svc.create_subscription(
            product_type="agent",
            product_id="agent-fnol",
            requester_oid=OID_B,
            requester_email="b@x",
        )
    )
    _run(
        svc.create_subscription(
            product_type="agent",
            product_id="agent-fnol",
            requester_oid=OID_A,
            requester_email="a@x",
        )
    )
    _run(
        svc.create_subscription(
            product_type="agent",
            product_id="agent-contact-center",
            requester_oid=OID_A,
            requester_email="a@x",
        )
    )

    by_id = {c.product_id: c for c in svc.list_agent_products(caller_oid=OID_A)}
    assert by_id["agent-fnol"].consumers == 2
    assert by_id["agent-contact-center"].consumers == 1
    assert by_id["agent-onboarding"].consumers == 0


def test_list_agent_products_consumers_count_counts_distinct_teams():
    svc, _, _ = _build_service()
    svc.set_listing(product_type="agent", product_id=ONB_AGENT, auto_approve=True)
    for oid in ("oid-t1", "oid-t2", "oid-t3"):
        _run(
            svc.create_subscription(
                product_type="agent",
                product_id="agent-onboarding",
                requester_oid=oid,
                requester_email=f"{oid}@x",
            )
        )
    card = next(c for c in svc.list_agent_products(caller_oid=OID_A) if c.product_id == "agent-onboarding")
    assert card.consumers == 3


def test_list_mcp_products_lists_published_products_of_any_kind():
    """A card exists iff the record carries a published ``marketplace`` block and is lifecycle-approved:
    a published standard server lists, an unpublished gateway does not, and ``kind`` is a display
    field only."""
    svc, _, _ = _build_service()
    cards = svc.list_mcp_products(caller_oid=OID_A)
    by_id = {c.product_id: c for c in cards}

    assert set(by_id) == {GATEWAY_MCP, STANDARD_MCP}
    assert UNPUBLISHED_MCP not in by_id
    assert "m-pending" not in by_id
    assert by_id[GATEWAY_MCP].kind == "gateway"
    assert by_id[STANDARD_MCP].kind == "standard"
    assert by_id[GATEWAY_MCP].business_unit == "Claims"


def test_published_mcp_card_omits_owner_email_and_uses_the_declared_support_contact():
    """A published card is MARKETPLACE-WIDE, so the registry's tenant-scoped
    ``owner_email`` (contact PII for a named individual) is NOT projected onto it. The declared
    ``support_contact`` — a mandatory datasheet field the publisher chose to expose — is its
    replacement, and it is what a cross-tenant consumer acts on."""
    svc, _, _ = _build_service()
    by_id = {c.product_id: c for c in svc.list_mcp_products(caller_oid=OID_A)}

    assert svc._mcp_registry.get(GATEWAY_MCP).owner_email == "owner@acme.com"
    assert by_id[GATEWAY_MCP].owner_email is None
    assert by_id[STANDARD_MCP].owner_email is None
    assert by_id[GATEWAY_MCP].support_contact == "mcp-platform@acme.com"
    assert by_id[STANDARD_MCP].support_contact == "claims-platform@acme.com"


def test_marketplace_cards_report_published_for_both_product_types():
    """``ProductCard.published`` means marketplace publication on both card types, True by
    construction since publication is what makes a record a card. The record's cross-tenant
    ``published`` flag is not surfaced: an MCP whose record flag is False still reports
    ``published=True`` once its declaration is approved."""
    mcps = [
        _mcp(
            "m-unshared",
            "Unshared MCP",
            tenant_id="ten-1",
            published=False,
            shared=False,
            marketplace=_publication(),
        )
    ]
    svc, _, _ = _build_service(mcps=mcps)

    mcp_cards = svc.list_mcp_products(caller_oid=OID_A)
    agent_cards = svc.list_agent_products(caller_oid=OID_A)
    assert mcp_cards and agent_cards
    for card in [*mcp_cards, *agent_cards]:
        assert card.published is True
    assert svc._mcp_registry.get("m-unshared").published is False
    assert mcp_cards[0].shared is False


def test_list_mcp_products_excludes_an_unpublished_declaration():
    """An UNPUBLISH keeps the block with ``published=False`` (declared history retained) and
    the card must not survive it — the delisting has to be real, exactly as for agents."""
    mcps = [
        _mcp("m-live", "Live MCP", marketplace=_publication(published=True)),
        _mcp("m-delisted", "Delisted MCP", marketplace=_publication(published=False)),
    ]
    svc, _, _ = _build_service(mcps=mcps)
    assert {c.product_id for c in svc.list_mcp_products(caller_oid=OID_A)} == {"m-live"}


def test_list_mcp_products_carries_governance_metadata():
    svc, _, _ = _build_service()
    card = {c.product_id: c for c in svc.list_mcp_products(caller_oid=OID_A)}[GATEWAY_MCP]
    assert card.data_classification == DataClassification.CONFIDENTIAL.value
    assert isinstance(card.data_classification, str)
    assert card.region == "EU"
    assert card.version == "2.1.0"
    assert card.created_at == datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc).isoformat()
    assert card.updated_at == datetime(2026, 6, 7, 8, 9, 10, tzinfo=timezone.utc).isoformat()


def test_list_mcp_products_carries_tenant_badge_fields():
    """MCP cards carry tenant_id/published/shared from the registry record and
    tenant_name resolved via ONE tenant_service.list() (never per-card)."""
    mcps = [
        _mcp(
            "m-own", "Own MCP", tenant_id="ten-1", published=False, shared=False, marketplace=_publication()
        ),
        _mcp(
            "m-pub",
            "Published MCP",
            tenant_id="ten-2",
            published=True,
            shared=False,
            marketplace=_publication(),
        ),
        _mcp(
            "m-shared",
            "Shared MCP",
            tenant_id="ten-2",
            published=False,
            shared=True,
            marketplace=_publication(),
        ),
    ]
    tenant_service = types.SimpleNamespace(
        list=lambda: [
            types.SimpleNamespace(id="ten-1", name="Claims DE"),
            types.SimpleNamespace(id="ten-2", name="Marketing EU"),
        ]
    )
    svc, _, _ = _build_service(mcps=mcps, tenant_service=tenant_service)
    by_id = {c.product_id: c for c in svc.list_mcp_products(caller_oid=OID_A)}

    assert by_id["m-own"].tenant_id == "ten-1"
    assert by_id["m-own"].tenant_name == "Claims DE"
    assert by_id["m-own"].shared is False
    assert by_id["m-pub"].tenant_id == "ten-2"
    assert by_id["m-pub"].tenant_name == "Marketing EU"
    assert by_id["m-shared"].shared is True
    assert all(c.published is True for c in by_id.values())


def test_list_mcp_products_tenant_name_degrades_without_tenant_service():
    """No tenant_service injected (or an unknown tenant id) → tenant_name None,
    the badge id/flags still stamped — display-only resolution never breaks the
    catalog."""
    mcps = [_mcp("m-own", "Own MCP", tenant_id="ten-x", published=True, marketplace=_publication())]
    svc, _, _ = _build_service(mcps=mcps)
    card = svc.list_mcp_products(caller_oid=OID_A)[0]
    assert card.tenant_id == "ten-x"
    assert card.tenant_name is None
    assert card.published is True


def test_list_mcp_products_tenant_service_error_degrades_to_no_names():
    """A tenant_service.list() failure degrades to tenant_name=None (never raises)."""

    def _boom():
        raise RuntimeError("ddb down")

    mcps = [_mcp("m-own", "Own MCP", tenant_id="ten-1", marketplace=_publication())]
    svc, _, _ = _build_service(mcps=mcps, tenant_service=types.SimpleNamespace(list=_boom))
    card = svc.list_mcp_products(caller_oid=OID_A)[0]
    assert card.tenant_id == "ten-1"
    assert card.tenant_name is None


def test_list_agent_products_untagged_agents_carry_no_tenant_badge():
    """An agent record with no ``tenant_id`` yields a card with no tenant badge that is still a
    marketplace-wide published card, so ``published`` is True; ``shared`` has no agent-side source
    and stays False."""
    svc, _, _ = _build_service()
    cards = svc.list_agent_products(caller_oid=OID_A)
    assert len(cards) == 3
    for card in cards:
        assert card.tenant_id is None
        assert card.tenant_name is None
        assert card.published is True
        assert card.shared is False


def _ctx(*, is_global=False, tenant_ids=()):
    from services.tenant_resolver import TenantContext

    return TenantContext(is_global=is_global, tenant_ids=frozenset(tenant_ids), tenants=())


def _tenant_scoped_mcps():
    """Tenant-varied MCP records. The first three are MARKETPLACE-published (so they are
    products); ``m-unpublished`` is a foreign record with no declaration at all, which is the
    only reason a record is now absent from the catalog."""
    return [
        _mcp(
            "m-own", "Own MCP", tenant_id="ten-1", published=False, shared=False, marketplace=_publication()
        ),
        _mcp(
            "m-pub",
            "Foreign Published",
            tenant_id="ten-2",
            published=True,
            shared=False,
            marketplace=_publication(),
        ),
        _mcp(
            "m-shared",
            "Foreign Shared",
            tenant_id="ten-2",
            published=False,
            shared=True,
            marketplace=_publication(),
        ),
        _mcp(
            "m-unpublished",
            "Foreign Unpublished",
            tenant_id="ten-2",
            published=False,
            shared=False,
            marketplace=None,
        ),
    ]


def _tenant_names_service():
    return types.SimpleNamespace(
        list=lambda: [
            types.SimpleNamespace(id="ten-1", name="Claims DE"),
            types.SimpleNamespace(id="ten-2", name="Marketing EU"),
        ]
    )


def test_list_mcp_products_published_card_is_marketplace_wide():
    """A marketplace-published MCP card is marketplace-wide, like a published agent card: publishing
    is the act of making a product cross-tenant discoverable, so filtering it by ``visible()`` would
    make the admin's approval a no-op for every other tenant. A foreign tenant's published MCP
    appears, badged with whose it is; the only absent record is the one that never declared."""
    svc, _, _ = _build_service(mcps=_tenant_scoped_mcps(), tenant_service=_tenant_names_service())
    cards = svc.list_mcp_products(caller_oid=OID_A)
    by_id = {c.product_id: c for c in cards}

    assert set(by_id) == {"m-own", "m-pub", "m-shared"}
    assert by_id["m-own"].tenant_name == "Claims DE"
    assert by_id["m-pub"].tenant_id == "ten-2"
    assert by_id["m-pub"].tenant_name == "Marketing EU"
    assert by_id["m-shared"].shared is True
    assert all(c.published is True for c in by_id.values())


def test_list_mcp_products_takes_no_tenant_context():
    """The MCP catalog is marketplace-wide, so its read takes no scope at all: a ``ctx`` is refused."""
    svc, _, _ = _build_service(mcps=_tenant_scoped_mcps())
    assert "ctx" not in inspect.signature(svc.list_mcp_products).parameters
    untyped: Any = svc.list_mcp_products
    with pytest.raises(TypeError):
        untyped(caller_oid=OID_A, ctx=_ctx(is_global=True))


def test_create_agent_subscription_pending_when_not_auto():
    svc, grant_fn, _ = _build_service()
    sub = _run(
        svc.create_subscription(
            product_type="agent",
            product_id="agent-contact-center",
            requester_oid=OID_A,
            requester_email="a@x",
        )
    )
    assert sub.status == "pending"
    assert sub.auto_approved is False
    grant_fn.assert_not_called()


def test_create_agent_subscription_auto_approves():
    svc, grant_fn, _ = _build_service()
    svc.set_listing(product_type="agent", product_id=ONB_AGENT, auto_approve=True)
    sub = _run(
        svc.create_subscription(
            product_type="agent",
            product_id="agent-onboarding",
            requester_oid=OID_A,
            requester_email="a@x",
        )
    )
    assert sub.status == "approved"
    assert sub.auto_approved is True
    assert sub.decided_by == "auto"
    assert sub.grant_assignment_id == "uasg-1"
    svc._agent_grant_fn.assert_awaited_once()
    call_args = svc._agent_grant_fn.await_args.args
    assert call_args[0] is svc._agent_registry.get(ONB_AGENT)
    assert call_args[1] == OID_A
    grant_fn.assert_not_called()


def test_agent_auto_approve_grant_failure_persists_failed_and_is_retryable():
    """An auto-approve whose grant fails must not report success: the row is persisted FAILED with a
    safe error and stays retryable, the same contract as the explicit-approve path."""
    svc, _, _ = _build_service(agent_grant_side_effect=RuntimeError("graph 502"))
    svc.set_listing(product_type="agent", product_id=ONB_AGENT, auto_approve=True)

    with pytest.raises(MarketplaceError) as ei:
        _run(
            svc.create_subscription(
                product_type="agent",
                product_id="agent-onboarding",
                requester_oid=OID_A,
                requester_email="a@x",
            )
        )
    assert ei.value.kind == "grant_failed"

    failed = next(s for s in svc.list_subscriptions() if s.product_id == ONB_AGENT)
    assert failed.status == "failed"
    assert failed.error
    assert failed.auto_approved is True
    assert failed.grant_assignment_id is None

    svc._agent_grant_fn.side_effect = None
    svc._agent_grant_fn.return_value = "uasg-2"
    retried = _run(svc.retry_grant(failed.id, decided_by=ADMIN))
    assert retried.status == "approved"
    assert retried.grant_assignment_id == "uasg-2"


def test_create_mcp_subscription_requires_provisioned_agent():
    svc, _, _ = _build_service()
    with pytest.raises(MarketplaceError) as ei:
        _run(
            svc.create_subscription(
                product_type="mcp",
                product_id=GATEWAY_MCP,
                agent_id=UNPROVISIONED_AGENT,
                requester_oid=OID_A,
                requester_email="a@x",
            )
        )
    assert ei.value.kind == "conflict"


def test_create_mcp_subscription_captures_agent_sp_id():
    svc, _, _ = _build_service()
    sub = _run(
        svc.create_subscription(
            product_type="mcp",
            product_id=GATEWAY_MCP,
            agent_id=PROVISIONED_AGENT,
            requester_oid=OID_A,
            requester_email="a@x",
        )
    )
    assert sub.agent_sp_id == "sp-a"
    assert sub.agent_name == "Provisioned Agent"
    assert sub.agent_id == PROVISIONED_AGENT


def test_create_mcp_auto_approve_applies_grant_inline():
    svc, grant_fn, _ = _build_service()
    svc.set_listing(product_type="mcp", product_id=GATEWAY_MCP, auto_approve=True)
    sub = _run(
        svc.create_subscription(
            product_type="mcp",
            product_id=GATEWAY_MCP,
            agent_id=PROVISIONED_AGENT,
            requester_oid=OID_A,
            requester_email="a@x",
        )
    )
    assert grant_fn.call_count == 1
    assert sub.status == "approved"
    assert sub.grant_assignment_id == "asg-1"


def test_create_mcp_not_auto_is_pending_no_grant():
    svc, grant_fn, _ = _build_service()
    sub = _run(
        svc.create_subscription(
            product_type="mcp",
            product_id=GATEWAY_MCP,
            agent_id=PROVISIONED_AGENT,
            requester_oid=OID_A,
            requester_email="a@x",
        )
    )
    assert sub.status == "pending"
    grant_fn.assert_not_called()


def test_idempotent_returns_active_sub():
    svc, _, _ = _build_service()
    first = _run(
        svc.create_subscription(
            product_type="agent",
            product_id="agent-fnol",
            requester_oid=OID_A,
            requester_email="a@x",
        )
    )
    second = _run(
        svc.create_subscription(
            product_type="agent",
            product_id="agent-fnol",
            requester_oid=OID_A,
            requester_email="a@x",
        )
    )
    assert first.id == second.id

    m1 = _run(
        svc.create_subscription(
            product_type="mcp",
            product_id=GATEWAY_MCP,
            agent_id=PROVISIONED_AGENT,
            requester_oid=OID_A,
            requester_email="a@x",
        )
    )
    m1_again = _run(
        svc.create_subscription(
            product_type="mcp",
            product_id=GATEWAY_MCP,
            agent_id=PROVISIONED_AGENT,
            requester_oid=OID_A,
            requester_email="a@x",
        )
    )
    assert m1.id == m1_again.id


def test_rejected_can_be_resubscribed():
    svc, _, _ = _build_service()
    first = _run(
        svc.create_subscription(
            product_type="agent",
            product_id="agent-fnol",
            requester_oid=OID_A,
            requester_email="a@x",
        )
    )
    svc.reject(first.id, decided_by=ADMIN, reason="nope")
    second = _run(
        svc.create_subscription(
            product_type="agent",
            product_id="agent-fnol",
            requester_oid=OID_A,
            requester_email="a@x",
        )
    )
    assert second.id != first.id
    all_subs = svc.list_subscriptions()
    assert {s.id for s in all_subs} >= {first.id, second.id}


def test_approve_mcp_applies_grant():
    svc, grant_fn, _ = _build_service()
    sub = _run(
        svc.create_subscription(
            product_type="mcp",
            product_id=GATEWAY_MCP,
            agent_id=PROVISIONED_AGENT,
            requester_oid=OID_A,
            requester_email="a@x",
        )
    )
    assert sub.status == "pending"
    approved = _run(svc.approve(sub.id, decided_by=ADMIN))
    assert grant_fn.call_count == 1
    assert approved.status == "approved"
    assert approved.grant_assignment_id == "asg-1"
    assert approved.decided_by == ADMIN


def test_approve_mcp_grant_failure_sets_failed():
    svc, _grant_fn, _ = _build_service(grant_side_effect=RuntimeError("graph 502"))
    sub = _run(
        svc.create_subscription(
            product_type="mcp",
            product_id=GATEWAY_MCP,
            agent_id=PROVISIONED_AGENT,
            requester_oid=OID_A,
            requester_email="a@x",
        )
    )
    with pytest.raises(MarketplaceError) as ei:
        _run(svc.approve(sub.id, decided_by=ADMIN))
    assert ei.value.kind == "grant_failed"
    failed = next(s for s in svc.list_subscriptions() if s.id == sub.id)
    assert failed.status == "failed"
    assert failed.error


def test_retry_grant_only_on_failed():
    svc, grant_fn, _ = _build_service(grant_side_effect=RuntimeError("graph 502"))
    sub = _run(
        svc.create_subscription(
            product_type="mcp",
            product_id=GATEWAY_MCP,
            agent_id=PROVISIONED_AGENT,
            requester_oid=OID_A,
            requester_email="a@x",
        )
    )
    with pytest.raises(MarketplaceError):
        _run(svc.approve(sub.id, decided_by=ADMIN))
    grant_fn.side_effect = None
    grant_fn.return_value = "asg-1"
    retried = _run(svc.retry_grant(sub.id, decided_by=ADMIN))
    assert retried.status == "approved"
    assert retried.grant_assignment_id == "asg-1"

    pending = _run(
        svc.create_subscription(
            product_type="mcp",
            product_id=GATEWAY_MCP,
            agent_id=PROVISIONED_AGENT,
            requester_oid=OID_B,
            requester_email="b@x",
        )
    )
    with pytest.raises(MarketplaceError) as ei:
        _run(svc.retry_grant(pending.id, decided_by=ADMIN))
    assert ei.value.kind == "conflict"


def test_approve_agent_applies_user_grant_not_the_mcp_grant():
    """Approving an agent subscription applies the user-to-agent grant and stores its assignment id;
    the MCP ``grant_fn`` stays untouched."""
    svc, grant_fn, _ = _build_service()
    sub = _run(
        svc.create_subscription(
            product_type="agent",
            product_id="agent-contact-center",
            requester_oid=OID_A,
            requester_email="a@x",
        )
    )
    approved = _run(svc.approve(sub.id, decided_by=ADMIN))
    assert approved.status == "approved"
    assert approved.grant_assignment_id == "uasg-1"
    svc._agent_grant_fn.assert_awaited_once()
    grant_fn.assert_not_called()


def test_reject_sets_rejected_with_reason():
    svc, _, _ = _build_service()
    sub = _run(
        svc.create_subscription(
            product_type="agent",
            product_id="agent-contact-center",
            requester_oid=OID_A,
            requester_email="a@x",
        )
    )
    rejected = svc.reject(sub.id, decided_by=ADMIN, reason="nope")
    assert rejected.status == "rejected"
    assert rejected.decision_reason == "nope"


def test_approve_non_pending_conflicts():
    svc, _, _ = _build_service()
    sub = _run(
        svc.create_subscription(
            product_type="agent",
            product_id="agent-contact-center",
            requester_oid=OID_A,
            requester_email="a@x",
        )
    )
    _run(svc.approve(sub.id, decided_by=ADMIN))
    with pytest.raises(MarketplaceError) as ei:
        _run(svc.approve(sub.id, decided_by=ADMIN))
    assert ei.value.kind == "conflict"


def test_list_subscriptions_caller_scoped_and_pending_first():
    svc, _, _ = _build_service()
    a1 = _run(
        svc.create_subscription(
            product_type="agent",
            product_id="agent-contact-center",
            requester_oid=OID_A,
            requester_email="a@x",
        )
    )
    b1 = _run(
        svc.create_subscription(
            product_type="agent",
            product_id="agent-fnol",
            requester_oid=OID_B,
            requester_email="b@x",
        )
    )
    a_subs = svc.list_subscriptions(caller_oid=OID_A)
    assert {s.id for s in a_subs} == {a1.id}
    all_subs = svc.list_subscriptions()
    assert {s.id for s in all_subs} >= {a1.id, b1.id}
    _run(svc.approve(a1.id, decided_by=ADMIN))
    all_subs = svc.list_subscriptions()
    statuses = [s.status for s in all_subs]
    first_pending = next(i for i, s in enumerate(statuses) if s == "pending")
    first_other = next((i for i, s in enumerate(statuses) if s != "pending"), len(statuses))
    assert first_pending < first_other


def test_set_listing_overrides_auto_approve():
    svc, grant_fn, _ = _build_service()
    assert svc._effective_auto_approve("mcp", GATEWAY_MCP) is False
    svc.set_listing(product_type="mcp", product_id=GATEWAY_MCP, auto_approve=True)
    assert svc._effective_auto_approve("mcp", GATEWAY_MCP) is True
    sub = _run(
        svc.create_subscription(
            product_type="mcp",
            product_id=GATEWAY_MCP,
            agent_id=PROVISIONED_AGENT,
            requester_oid=OID_A,
            requester_email="a@x",
        )
    )
    assert sub.status == "approved"
    assert grant_fn.call_count == 1


def _eligibility_service(*, assignments_by_sp=None, list_assignments_side_effect=None):
    """A service tuned for eligibility tests.

    - ``PROVISIONED_AGENT`` is sponsored by OID_A (sp-a).
    - ``agent-granted`` is provisioned (sp-granted), sponsored by OID_B.
    - ``agent-other`` is provisioned (sp-other), sponsored by OID_B.
    - ``UNPROVISIONED_AGENT`` is sponsored by OID_A but NOT provisioned.

    ``mcp_graph.list_assignments`` is an AsyncMock; ``assignments_by_sp`` maps an
    agent SP id → the assignment-dict list it returns.
    """
    agents = [
        _agent(PROVISIONED_AGENT, "Provisioned Agent", identity_status="provisioned", entra_sp_id="sp-a"),
        types.SimpleNamespace(
            id="agent-granted",
            name="Granted Agent",
            identity_status="provisioned",
            entra_sp_id="sp-granted",
            sponsor_oid=OID_B,
            entra_app_id="app-g",
            agent_arn="arn:aws:bedrock-agentcore:us-east-1:1:runtime/agent-g",
            oauth2_credential_provider_name=None,
        ),
        types.SimpleNamespace(
            id="agent-other",
            name="Other Agent",
            identity_status="provisioned",
            entra_sp_id="sp-other",
            sponsor_oid=OID_B,
            entra_app_id="app-o",
            agent_arn="arn:aws:bedrock-agentcore:us-east-1:1:runtime/agent-o",
            oauth2_credential_provider_name=None,
        ),
        _agent(UNPROVISIONED_AGENT, "Draft Agent", identity_status="none", entra_sp_id=None),
    ]
    mcps = [
        _mcp(
            GATEWAY_MCP, "Gateway MCP", kind="gateway", lifecycle_state="approved", marketplace=_publication()
        )
    ]

    assignments_by_sp = assignments_by_sp or {}

    async def _list_assignments(agent_sp_id):
        return assignments_by_sp.get(agent_sp_id, [])

    list_assignments = AsyncMock(side_effect=_list_assignments)
    if list_assignments_side_effect is not None:
        list_assignments = AsyncMock(side_effect=list_assignments_side_effect)

    grant_fn = AsyncMock(return_value="asg-1")
    svc = MarketplaceService(
        table_name="",
        mcp_registry=_McpRegistry(mcps),
        agent_registry=_AgentRegistry(agents),
        mcp_graph=types.SimpleNamespace(list_assignments=list_assignments),
        grant_fn=grant_fn,
    )
    return svc, list_assignments, grant_fn


def test_eligible_agents_includes_sponsor():
    svc, list_assignments, _ = _eligibility_service()
    agents = _run(svc.eligible_agents(caller_oid=OID_A, caller_group_ids=[]))
    ids = {a.id for a in agents}
    assert PROVISIONED_AGENT in ids
    called_sps = {c.args[0] for c in list_assignments.await_args_list}
    assert "sp-a" not in called_sps


def test_eligible_agents_includes_direct_user_grant():
    svc, _, _ = _eligibility_service(
        assignments_by_sp={
            "sp-granted": [{"principalId": OID_A, "principalType": "User"}],
        }
    )
    agents = _run(svc.eligible_agents(caller_oid=OID_A, caller_group_ids=[]))
    ids = {a.id for a in agents}
    assert "agent-granted" in ids
    assert "agent-other" not in ids


def test_eligible_agents_includes_group_grant():
    svc, _, _ = _eligibility_service(
        assignments_by_sp={
            "sp-other": [{"principalId": "grp-claims", "principalType": "Group"}],
        }
    )
    agents = _run(svc.eligible_agents(caller_oid=OID_B, caller_group_ids=["grp-claims"]))
    ids = {a.id for a in agents}
    assert "agent-other" in ids


def test_eligible_agents_excludes_unrelated():
    svc, _, _ = _eligibility_service(
        assignments_by_sp={
            "sp-other": [{"principalId": "someone-else", "principalType": "User"}],
        }
    )
    agents = _run(svc.eligible_agents(caller_oid=OID_A, caller_group_ids=["grp-x"]))
    ids = {a.id for a in agents}
    assert "agent-other" not in ids


def test_eligible_agents_excludes_unprovisioned():
    svc, _, _ = _eligibility_service()
    agents = _run(svc.eligible_agents(caller_oid=OID_A, caller_group_ids=[]))
    ids = {a.id for a in agents}
    assert UNPROVISIONED_AGENT not in ids


def test_create_mcp_subscription_denies_ineligible_agent():
    svc, _, grant_fn = _eligibility_service(
        assignments_by_sp={
            "sp-other": [{"principalId": "someone-else", "principalType": "User"}],
        }
    )
    with pytest.raises(MarketplaceError) as ei:
        _run(
            svc.create_subscription(
                product_type="mcp",
                product_id=GATEWAY_MCP,
                agent_id="agent-other",
                requester_oid=OID_A,
                requester_email="a@x",
                caller_group_ids=[],
            )
        )
    assert ei.value.kind == "conflict"
    grant_fn.assert_not_called()


def test_create_mcp_subscription_allows_granted_agent():
    svc, _, grant_fn = _eligibility_service(
        assignments_by_sp={
            "sp-other": [{"principalId": "grp-claims", "principalType": "Group"}],
        }
    )
    sub = _run(
        svc.create_subscription(
            product_type="mcp",
            product_id=GATEWAY_MCP,
            agent_id="agent-other",
            requester_oid=OID_B,
            requester_email="b@x",
            caller_group_ids=["grp-claims"],
        )
    )
    assert sub.status == "pending"
    assert sub.agent_id == "agent-other"
    grant_fn.assert_not_called()


def test_create_mcp_subscription_allows_granted_agent_auto_approve_grants():
    svc, _, grant_fn = _eligibility_service(
        assignments_by_sp={
            "sp-other": [{"principalId": "grp-claims", "principalType": "Group"}],
        }
    )
    svc.set_listing(product_type="mcp", product_id=GATEWAY_MCP, auto_approve=True)
    sub = _run(
        svc.create_subscription(
            product_type="mcp",
            product_id=GATEWAY_MCP,
            agent_id="agent-other",
            requester_oid=OID_B,
            requester_email="b@x",
            caller_group_ids=["grp-claims"],
        )
    )
    assert sub.status == "approved"
    assert grant_fn.call_count == 1


def test_create_mcp_sponsor_allowed_when_graph_down():
    svc, list_assignments, _ = _eligibility_service(
        list_assignments_side_effect=RuntimeError("graph 502"),
    )
    sub = _run(
        svc.create_subscription(
            product_type="mcp",
            product_id=GATEWAY_MCP,
            agent_id=PROVISIONED_AGENT,
            requester_oid=OID_A,
            requester_email="a@x",
            caller_group_ids=[],
        )
    )
    assert sub.status == "pending"
    assert sub.agent_id == PROVISIONED_AGENT
    list_assignments.assert_not_awaited()


def test_create_mcp_nonsponsor_graph_error_fails_closed():
    svc, _, grant_fn = _eligibility_service(
        list_assignments_side_effect=RuntimeError("graph 502"),
    )
    with pytest.raises(RuntimeError):
        _run(
            svc.create_subscription(
                product_type="mcp",
                product_id=GATEWAY_MCP,
                agent_id="agent-other",
                requester_oid=OID_A,
                requester_email="a@x",
                caller_group_ids=[],
            )
        )
    grant_fn.assert_not_called()


def test_metrics_counts_and_rate():
    svc, _, _ = _build_service()
    svc.set_listing(product_type="agent", product_id=ONB_AGENT, auto_approve=True)
    auto = _run(
        svc.create_subscription(
            product_type="agent",
            product_id="agent-onboarding",
            requester_oid=OID_A,
            requester_email="a@x",
        )
    )
    assert auto.status == "approved"
    rej = _run(
        svc.create_subscription(
            product_type="agent",
            product_id="agent-contact-center",
            requester_oid=OID_A,
            requester_email="a@x",
        )
    )
    svc.reject(rej.id, decided_by=ADMIN, reason="x")
    _run(
        svc.create_subscription(
            product_type="mcp",
            product_id=GATEWAY_MCP,
            agent_id=PROVISIONED_AGENT,
            requester_oid=OID_B,
            requester_email="b@x",
        )
    )

    m = svc.metrics()
    assert m.total == 3
    assert m.pending == 1
    assert m.approved == 1
    assert m.rejected == 1
    assert m.failed == 0
    assert m.approval_rate == pytest.approx(1 / 2)
    dumped = m.model_dump(mode="json")
    assert list(dumped["by_type"].items()) == [("agent", 2), ("mcp", 1)]
    assert sorted((tp["product_id"], tp["count"]) for tp in dumped["top_products"]) == [
        (CC_AGENT, 1),
        (ONB_AGENT, 1),
        (GATEWAY_MCP, 1),
    ]
    assert all(list(tp) == ["product_id", "product_name", "count"] for tp in dumped["top_products"])


def test_metrics_close_the_type_counts_and_the_top_product_rows():
    assert list(ProductTypeCounts.model_fields) == [product_type.value for product_type in ProductType]
    counts = {"total": 0, "pending": 0, "approved": 0, "rejected": 0, "failed": 0, "approval_rate": 0.0}
    top = {"product_id": "p", "product_name": "P", "count": 1}
    metrics = MarketplaceMetrics.model_validate(
        {**counts, "by_type": {"agent": 1, "mcp": 0}, "top_products": [top]}
    )
    assert metrics.model_dump(mode="json")["top_products"] == [top]
    with pytest.raises(ValidationError):
        MarketplaceMetrics.model_validate({**counts, "by_type": {"agent": 1}, "top_products": []})
    with pytest.raises(ValidationError):
        MarketplaceMetrics.model_validate(
            {**counts, "by_type": {"agent": 1, "mcp": 0}, "top_products": [{"product_id": "p"}]}
        )


def _approved_mcp_sub(svc):
    """Create + approve an MCP sub for PROVISIONED_AGENT on GATEWAY_MCP → APPROVED with
    grant_assignment_id 'asg-1' (the grant_fn return). Returns the approved Subscription."""
    sub = _run(
        svc.create_subscription(
            product_type="mcp",
            product_id=GATEWAY_MCP,
            agent_id=PROVISIONED_AGENT,
            requester_oid=OID_A,
            requester_email="a@x",
        )
    )
    approved = _run(svc.approve(sub.id, decided_by=ADMIN))
    assert approved.status == "approved"
    assert approved.grant_assignment_id == "asg-1"
    return approved


def test_revoke_approved_mcp_calls_revoke_fn():
    svc, _, revoke_fn = _build_service()
    approved = _approved_mcp_sub(svc)
    decided_by_before = approved.decided_by
    decided_at_before = approved.decided_at

    revoked = _run(svc.revoke_subscription(approved.id, decided_by="oid-admin-2", reason="offboarding"))
    revoke_fn.assert_awaited_once()
    assert revoke_fn.await_args is not None
    call_args = revoke_fn.await_args.args
    assert call_args[0] is svc._mcp_registry.get(GATEWAY_MCP)
    assert call_args[1] == "asg-1"

    assert revoked.status == "revoked"
    assert revoked.revoked_by == "oid-admin-2"
    assert revoked.revoked_at is not None
    assert revoked.revoke_reason == "offboarding"
    assert revoked.decided_by == decided_by_before == ADMIN
    assert revoked.decided_at == decided_at_before
    stored = next(s for s in svc.list_subscriptions() if s.id == approved.id)
    assert stored.status == "revoked"
    assert stored.revoked_by == "oid-admin-2"


def test_revoke_agent_sub_calls_agent_revoke_fn_not_the_mcp_one():
    """Revoking an agent subscription tears down the user-to-agent assignment via ``agent_revoke_fn``;
    the MCP ``revoke_fn`` is never touched."""
    svc, _, revoke_fn = _build_service()
    sub = _run(
        svc.create_subscription(
            product_type="agent",
            product_id="agent-contact-center",
            requester_oid=OID_A,
            requester_email="a@x",
        )
    )
    approved = _run(svc.approve(sub.id, decided_by=ADMIN))
    assert approved.status == "approved"

    revoked = _run(svc.revoke_subscription(approved.id, decided_by=ADMIN))
    assert revoked.status == "revoked"
    assert revoked.revoked_by == ADMIN
    svc._agent_revoke_fn.assert_awaited_once()
    call_args = svc._agent_revoke_fn.await_args.args
    assert call_args[0] is svc._agent_registry.get(CC_AGENT)
    assert call_args[1] == "uasg-1"
    revoke_fn.assert_not_called()


def test_revoke_non_approved_conflicts():
    svc, _, revoke_fn = _build_service()
    pending = _run(
        svc.create_subscription(
            product_type="agent",
            product_id="agent-contact-center",
            requester_oid=OID_A,
            requester_email="a@x",
        )
    )
    with pytest.raises(MarketplaceError) as ei:
        _run(svc.revoke_subscription(pending.id, decided_by=ADMIN))
    assert ei.value.kind == "conflict"

    rejected = _run(
        svc.create_subscription(
            product_type="agent",
            product_id="agent-fnol",
            requester_oid=OID_A,
            requester_email="a@x",
        )
    )
    svc.reject(rejected.id, decided_by=ADMIN, reason="nope")
    with pytest.raises(MarketplaceError) as ei:
        _run(svc.revoke_subscription(rejected.id, decided_by=ADMIN))
    assert ei.value.kind == "conflict"

    revoke_fn.assert_not_called()


def test_revoke_unknown_not_found():
    svc, _, revoke_fn = _build_service()
    with pytest.raises(MarketplaceError) as ei:
        _run(svc.revoke_subscription("mkt-doesnotexist", decided_by=ADMIN))
    assert ei.value.kind == "not_found"
    revoke_fn.assert_not_called()


def test_revoke_missing_assignment_id_marks_revoked_no_call():
    svc, _, revoke_fn = _build_service()
    approved = _approved_mcp_sub(svc)
    stored = svc._rows.get_sub(approved.id)
    assert stored is not None
    stored.grant_assignment_id = None
    svc._rows.save(stored, after={"approved"})

    revoked = _run(svc.revoke_subscription(approved.id, decided_by=ADMIN))
    assert revoked.status == "revoked"
    revoke_fn.assert_not_called()


def test_revoke_missing_mcp_record_marks_revoked_no_call():
    svc, _, revoke_fn = _build_service()
    approved = _approved_mcp_sub(svc)
    svc._mcp_registry._mcps.pop(GATEWAY_MCP)

    revoked = _run(svc.revoke_subscription(approved.id, decided_by=ADMIN))
    assert revoked.status == "revoked"
    revoke_fn.assert_not_called()


def test_revoke_stale_grant_marks_revoked():
    svc, _, revoke_fn = _build_service(revoke_side_effect=GrantNotFoundError("gone"))
    approved = _approved_mcp_sub(svc)

    revoked = _run(svc.revoke_subscription(approved.id, decided_by=ADMIN))
    assert revoked.status == "revoked"
    revoke_fn.assert_awaited_once()


def test_revoke_graph_failure_keeps_approved_raises_grant_failed():
    svc, _, _revoke_fn = _build_service(revoke_side_effect=GrantRevokeFailedError("boom"))
    approved = _approved_mcp_sub(svc)

    with pytest.raises(MarketplaceError) as ei:
        _run(svc.revoke_subscription(approved.id, decided_by=ADMIN))
    assert ei.value.kind == "grant_failed"

    stored = next(s for s in svc.list_subscriptions() if s.id == approved.id)
    assert stored.status == "approved"
    assert stored.revoked_by is None
    assert stored.revoked_at is None
    assert stored.revoke_reason is None


def test_resubscribe_after_revoke_creates_new_sub():
    svc, _, _ = _build_service()
    approved = _approved_mcp_sub(svc)
    _run(svc.revoke_subscription(approved.id, decided_by=ADMIN))

    fresh = _run(
        svc.create_subscription(
            product_type="mcp",
            product_id=GATEWAY_MCP,
            agent_id=PROVISIONED_AGENT,
            requester_oid=OID_A,
            requester_email="a@x",
        )
    )
    assert fresh.id != approved.id
    assert fresh.status == "pending"
    all_ids = {s.id for s in svc.list_subscriptions()}
    assert {approved.id, fresh.id} <= all_ids


def test_metrics_counts_revoked():
    svc, _, _ = _build_service()
    svc.set_listing(product_type="agent", product_id=ONB_AGENT, auto_approve=True)
    auto = _run(
        svc.create_subscription(
            product_type="agent",
            product_id="agent-onboarding",
            requester_oid=OID_A,
            requester_email="a@x",
        )
    )
    assert auto.status == "approved"
    _run(svc.revoke_subscription(auto.id, decided_by=ADMIN))

    m = svc.metrics()
    assert m.revoked == 1
    assert m.approved == 0
    assert m.total == 1


def _pending_agent_sub(svc, product_id=CC_AGENT, requester_oid=OID_A):
    sub = _run(
        svc.create_subscription(
            product_type="agent",
            product_id=product_id,
            requester_oid=requester_oid,
            requester_email="a@x",
        )
    )
    assert sub.status == "pending"
    return sub


def test_approve_agent_grants_the_subscriber_on_the_products_agent():
    """The grant's arguments are the contract: the agent record behind the product (a product is a
    registry agent) and the subscriber's oid as the principal, not the approving admin and not an
    agent SP."""
    svc, _, _ = _build_service()
    sub = _pending_agent_sub(svc, requester_oid=OID_B)

    approved = _run(svc.approve(sub.id, decided_by=ADMIN))

    svc._agent_grant_fn.assert_awaited_once()
    call_args = svc._agent_grant_fn.await_args.args
    assert call_args[0] is svc._agent_registry.get(CC_AGENT)
    assert call_args[1] == OID_B
    assert approved.grant_assignment_id == "uasg-1"
    stored = next(s for s in svc.list_subscriptions() if s.id == sub.id)
    assert stored.grant_assignment_id == "uasg-1"
    assert stored.decided_by == ADMIN


@pytest.mark.parametrize(
    "kwargs",
    [
        pytest.param(dict(identity_status="none"), id="not-provisioned"),
        pytest.param(dict(entra_sp_id=None), id="no-sp-id"),
        pytest.param(dict(invoker_role_id=None), id="no-invoker-role-id"),
    ],
)
def test_approve_agent_unprovisioned_identity_fails_with_a_safe_error(kwargs):
    """All three fields are required for a well-formed Graph write, so each missing one
    persists FAILED with the SAFE literal and raises ``grant_failed`` — never a raw Graph
    call against a malformed ``/servicePrincipals/None/...`` path."""
    agents = [_agent("agent-x", "Product X", marketplace=_publication(), **kwargs)]
    svc, _, _ = _build_service(agents=agents)
    sub = _pending_agent_sub(svc, product_id="agent-x")

    with pytest.raises(MarketplaceError) as ei:
        _run(svc.approve(sub.id, decided_by=ADMIN))
    assert ei.value.kind == "grant_failed"

    svc._agent_grant_fn.assert_not_awaited()
    failed = next(s for s in svc.list_subscriptions() if s.id == sub.id)
    assert failed.status == "failed"
    assert failed.error == "agent identity is not provisioned"
    assert failed.grant_assignment_id is None


def test_approve_agent_grant_failure_sets_failed_with_a_safe_error():
    """A grant-fn raise → FAILED + a safe error literal + ``grant_failed``. The persisted
    error must NOT echo the underlying exception (the no-leak guard)."""
    svc, _, _ = _build_service(agent_grant_side_effect=UserGrantError("failed to grant the user access"))
    sub = _pending_agent_sub(svc)

    with pytest.raises(MarketplaceError) as ei:
        _run(svc.approve(sub.id, decided_by=ADMIN))
    assert ei.value.kind == "grant_failed"

    failed = next(s for s in svc.list_subscriptions() if s.id == sub.id)
    assert failed.status == "failed"
    assert failed.error == "Grant application failed; see backend logs and retry."


def test_retry_grant_reruns_the_user_grant_for_a_failed_agent_sub():
    """``retry_grant`` covers FAILED AGENT subs (not just MCP ones): once Graph recovers,
    Retry drives the row to approved with the new assignment id."""
    svc, _, _ = _build_service(agent_grant_side_effect=UserGrantError("boom"))
    sub = _pending_agent_sub(svc)
    with pytest.raises(MarketplaceError):
        _run(svc.approve(sub.id, decided_by=ADMIN))

    svc._agent_grant_fn.side_effect = None
    svc._agent_grant_fn.return_value = "uasg-9"
    retried = _run(svc.retry_grant(sub.id, decided_by=ADMIN))

    assert retried.status == "approved"
    assert retried.grant_assignment_id == "uasg-9"
    assert retried.error is None
    assert svc._agent_grant_fn.await_count == 2


def test_revoke_agent_sub_stale_grant_is_success():
    """``UserGrantNotFoundError`` = the assignment is already gone = SUCCESS: the desired
    end state (no access) holds, so the row still flips to REVOKED."""
    svc, _, _ = _build_service(agent_revoke_side_effect=UserGrantNotFoundError("grant not found"))
    sub = _pending_agent_sub(svc)
    approved = _run(svc.approve(sub.id, decided_by=ADMIN))

    revoked = _run(svc.revoke_subscription(approved.id, decided_by=ADMIN))

    assert revoked.status == "revoked"
    svc._agent_revoke_fn.assert_awaited_once()


def test_revoke_agent_sub_graph_failure_keeps_approved_and_persists_nothing():
    """A real teardown failure leaves the sub APPROVED with NOTHING persisted, so the admin
    can simply click Revoke again — the MCP revoke contract, exactly."""
    svc, _, _ = _build_service(agent_revoke_side_effect=UserGrantError("failed to revoke"))
    sub = _pending_agent_sub(svc)
    approved = _run(svc.approve(sub.id, decided_by=ADMIN))

    with pytest.raises(MarketplaceError) as ei:
        _run(svc.revoke_subscription(approved.id, decided_by=ADMIN))
    assert ei.value.kind == "grant_failed"

    stored = next(s for s in svc.list_subscriptions() if s.id == approved.id)
    assert stored.status == "approved"
    assert stored.revoked_by is None
    assert stored.revoked_at is None
    assert stored.revoke_reason is None
    assert stored.grant_assignment_id == "uasg-1"


def test_revoke_agent_sub_without_a_stored_assignment_id_skips_graph():
    """Nothing actionable in Graph (no stored assignment id) → mark REVOKED without calling
    the revoke fn, the MCP branch's best-effort-cleanup behaviour."""
    svc, _, _ = _build_service()
    sub = _pending_agent_sub(svc)
    approved = _run(svc.approve(sub.id, decided_by=ADMIN))
    stored = svc._rows.get_sub(approved.id)
    assert stored is not None
    stored.grant_assignment_id = None
    svc._rows.save(stored, after={"approved"})

    revoked = _run(svc.revoke_subscription(approved.id, decided_by=ADMIN))

    assert revoked.status == "revoked"
    svc._agent_revoke_fn.assert_not_awaited()


def test_revoke_agent_sub_with_a_deleted_agent_record_skips_graph():
    """The agent record is gone (deregistered between approve and revoke) → nothing to
    revoke on, so clean up the row rather than raising."""
    svc, _, _ = _build_service()
    sub = _pending_agent_sub(svc)
    approved = _run(svc.approve(sub.id, decided_by=ADMIN))
    svc._agent_registry._agents.pop(CC_AGENT)

    revoked = _run(svc.revoke_subscription(approved.id, decided_by=ADMIN))

    assert revoked.status == "revoked"
    svc._agent_revoke_fn.assert_not_awaited()


def _unknown_agent_product_message(svc):
    """The literal a truly-nonexistent agent product raises — the byte-identical baseline
    every gated case must match."""
    with pytest.raises(MarketplaceError) as ei:
        _run(
            svc.create_subscription(
                product_type="agent",
                product_id="agent-does-not-exist",
                requester_oid=OID_A,
                requester_email="a@x",
            )
        )
    assert ei.value.kind == "not_found"
    return str(ei.value)


def test_subscribe_to_a_never_published_agent_is_not_found():
    """A registry agent with NO marketplace block is not a product — subscribing to it must
    be indistinguishable from subscribing to a nonexistent id, and no row may be born."""
    svc, _, _ = _build_service()
    baseline = _unknown_agent_product_message(svc)

    with pytest.raises(MarketplaceError) as ei:
        _run(
            svc.create_subscription(
                product_type="agent",
                product_id=PROVISIONED_AGENT,
                requester_oid=OID_A,
                requester_email="a@x",
            )
        )

    assert ei.value.kind == "not_found"
    assert str(ei.value) == baseline
    assert svc.list_subscriptions() == []
    svc._agent_grant_fn.assert_not_awaited()


def test_subscribe_to_a_delisted_agent_product_is_not_found():
    """A published product an admin has set ``available=False`` is OFF the market: even
    with auto-approve left on, a subscribe cannot be born and no grant is applied."""
    svc, _, _ = _build_service()
    svc.set_listing(product_type="agent", product_id=CC_AGENT, available=False, auto_approve=True)
    baseline = _unknown_agent_product_message(svc)

    with pytest.raises(MarketplaceError) as ei:
        _run(
            svc.create_subscription(
                product_type="agent",
                product_id=CC_AGENT,
                requester_oid=OID_A,
                requester_email="a@x",
            )
        )

    assert ei.value.kind == "not_found"
    assert str(ei.value) == baseline
    assert svc.list_subscriptions() == []
    svc._agent_grant_fn.assert_not_awaited()


def test_subscribe_after_unpublish_is_not_found_and_grants_nothing():
    """The stale-auto-approve exploit path: an admin turns auto-approve on, then UNPUBLISHES
    the product. ``unpublish`` keeps the block with ``published=False`` but does NOT clear
    ``listing.auto_approve``, so without the publication gate a VIEWER's own subscribe would
    self-serve a real Entra Invoker grant on a delisted agent with no admin in the loop."""
    svc, _, _ = _build_service()
    svc.set_listing(product_type="agent", product_id=CC_AGENT, auto_approve=True)
    _run(svc.unpublish("agent", CC_AGENT, ADMIN))
    assert svc.set_listing(product_type="agent", product_id=CC_AGENT).auto_approve is True

    with pytest.raises(MarketplaceError) as ei:
        _run(
            svc.create_subscription(
                product_type="agent",
                product_id=CC_AGENT,
                requester_oid=OID_B,
                requester_email="b@x",
            )
        )

    assert ei.value.kind == "not_found"
    assert svc.list_subscriptions() == []
    svc._agent_grant_fn.assert_not_awaited()


def test_subscribe_to_a_deprecated_agent_product_is_not_found():
    """Deprecating an agent delists it at the BIRTH gate too, not just on the card. A
    ``transition`` writes the native status only — the marketplace block survives untouched —
    so the same predicate that hides the card (``_agent_product_is_offered``) is what stops a
    new subscription being born against a retired agent (auto-approve on, to prove the
    self-serve grant lane is closed as well)."""
    svc, _, _ = _build_service()
    svc.set_listing(product_type="agent", product_id=CC_AGENT, auto_approve=True)
    svc._agent_registry.get(CC_AGENT).lifecycle_state = "deprecated"
    assert svc._agent_registry.get(CC_AGENT).marketplace.published is True

    with pytest.raises(MarketplaceError) as ei:
        _run(
            svc.create_subscription(
                product_type="agent",
                product_id=CC_AGENT,
                requester_oid=OID_B,
                requester_email="b@x",
            )
        )

    assert ei.value.kind == "not_found"
    assert str(ei.value) == "Unknown agent product"
    assert svc.list_subscriptions() == []
    svc._agent_grant_fn.assert_not_awaited()
    assert CC_AGENT not in {c.product_id for c in svc.list_agent_products(caller_oid=OID_B)}


def test_approve_after_deprecation_fails_and_grants_nothing():
    """The RE-GATE catches deprecation too: a PENDING row born while the agent was approved
    outlives that check, so an admin approving it after the agent was deprecated must NOT get
    a real Entra Invoker assignment on a retired agent. FAILED + the fixed literal, retryable
    if the agent is ever re-approved."""
    svc, _, _ = _build_service()
    sub = _pending_agent_sub(svc)
    svc._agent_registry.get(CC_AGENT).lifecycle_state = "deprecated"

    with pytest.raises(MarketplaceError) as ei:
        _run(svc.approve(sub.id, decided_by=ADMIN))
    assert ei.value.kind == "grant_failed"
    assert str(ei.value) == "agent product is no longer published"

    svc._agent_grant_fn.assert_not_awaited()
    failed = next(s for s in svc.list_subscriptions() if s.id == sub.id)
    assert failed.status == "failed"
    assert failed.error == "agent product is no longer published"
    assert failed.grant_assignment_id is None


def test_subscribe_to_a_published_available_agent_product_still_works():
    """The gate is a gate, not a wall: a published product with no delisting subscribes and
    (with auto-approve on) applies the grant exactly as before."""
    svc, _, _ = _build_service()
    pending = _run(
        svc.create_subscription(
            product_type="agent",
            product_id=FNOL_AGENT,
            requester_oid=OID_A,
            requester_email="a@x",
        )
    )
    assert pending.status == "pending"

    svc.set_listing(product_type="agent", product_id=ONB_AGENT, available=True, auto_approve=True)
    auto = _run(
        svc.create_subscription(
            product_type="agent",
            product_id=ONB_AGENT,
            requester_oid=OID_A,
            requester_email="a@x",
        )
    )
    assert auto.status == "approved"
    assert auto.grant_assignment_id == "uasg-1"
    svc._agent_grant_fn.assert_awaited_once()


def test_approve_after_unpublish_fails_and_republishing_makes_retry_succeed():
    """Publication is MUTABLE, so the birth gate can go stale: the row outlives it. An admin
    who unpublishes a product and then approves a still-open PENDING row for it must NOT get
    a real Entra Invoker grant on a product that is no longer offered — the row is persisted
    FAILED with the fixed literal instead, and stays retryable once it is re-published."""
    svc, _, _ = _build_service()
    sub = _pending_agent_sub(svc)
    _run(svc.unpublish("agent", CC_AGENT, ADMIN))

    with pytest.raises(MarketplaceError) as ei:
        _run(svc.approve(sub.id, decided_by=ADMIN))
    assert ei.value.kind == "grant_failed"
    assert str(ei.value) == "agent product is no longer published"

    svc._agent_grant_fn.assert_not_awaited()
    failed = next(s for s in svc.list_subscriptions() if s.id == sub.id)
    assert failed.status == "failed"
    assert failed.error == "agent product is no longer published"
    assert failed.grant_assignment_id is None

    svc._agent_registry.persist_marketplace(CC_AGENT, _publication())
    retried = _run(svc.retry_grant(sub.id, decided_by=ADMIN))

    assert retried.status == "approved"
    assert retried.grant_assignment_id == "uasg-1"
    assert retried.error is None
    svc._agent_grant_fn.assert_awaited_once()


def test_retry_grant_after_delisting_fails_and_grants_nothing():
    """The retry lane is gated too, and on the delisted (``available=False``) condition as
    well as the unpublished one: a FAILED row whose product was taken off the market since
    cannot be retried into a real grant."""
    svc, _, _ = _build_service(agent_grant_side_effect=UserGrantError("boom"))
    sub = _pending_agent_sub(svc)
    with pytest.raises(MarketplaceError):
        _run(svc.approve(sub.id, decided_by=ADMIN))
    assert svc._agent_grant_fn.await_count == 1

    svc._agent_grant_fn.side_effect = None
    svc.set_listing(product_type="agent", product_id=CC_AGENT, available=False)

    with pytest.raises(MarketplaceError) as ei:
        _run(svc.retry_grant(sub.id, decided_by=ADMIN))
    assert ei.value.kind == "grant_failed"
    assert str(ei.value) == "agent product is no longer published"

    failed = next(s for s in svc.list_subscriptions() if s.id == sub.id)
    assert failed.status == "failed"
    assert failed.error == "agent product is no longer published"
    assert failed.grant_assignment_id is None
    assert svc._agent_grant_fn.await_count == 1


def test_mcp_subscription_path_never_touches_the_agent_grant_fns():
    """Approve and revoke of an MCP subscription drive the MCP grant pair only."""
    svc, grant_fn, revoke_fn = _build_service()
    approved = _approved_mcp_sub(svc)
    _run(svc.revoke_subscription(approved.id, decided_by=ADMIN))

    grant_fn.assert_awaited_once()
    revoke_fn.assert_awaited_once()
    svc._agent_grant_fn.assert_not_awaited()
    svc._agent_revoke_fn.assert_not_awaited()


def test_approve_mcp_after_unpublish_fails_and_republishing_makes_retry_succeed():
    svc, grant_fn, _ = _build_service()
    sub = _run(
        svc.create_subscription(
            product_type="mcp",
            product_id=GATEWAY_MCP,
            agent_id=PROVISIONED_AGENT,
            requester_oid=OID_A,
            requester_email="a@x",
        )
    )
    _run(svc.unpublish("mcp", GATEWAY_MCP, ADMIN))

    with pytest.raises(MarketplaceError) as ei:
        _run(svc.approve(sub.id, decided_by=ADMIN))
    assert ei.value.kind == "grant_failed"
    assert str(ei.value) == "MCP product is no longer published"

    grant_fn.assert_not_called()
    failed = next(s for s in svc.list_subscriptions() if s.id == sub.id)
    assert failed.status == "failed"
    assert failed.error == "MCP product is no longer published"
    assert failed.grant_assignment_id is None

    svc._mcp_registry.persist_marketplace(GATEWAY_MCP, _publication())
    retried = _run(svc.retry_grant(sub.id, decided_by=ADMIN))
    assert retried.status == "approved"
    assert retried.grant_assignment_id == "asg-1"
    assert retried.error is None
    grant_fn.assert_awaited_once()


def test_retry_grant_after_mcp_delisting_fails_and_grants_nothing():
    """The delisted (``available=False``) condition gates the MCP retry lane too."""
    svc, grant_fn, _ = _build_service(grant_side_effect=RuntimeError("graph 502"))
    sub = _run(
        svc.create_subscription(
            product_type="mcp",
            product_id=GATEWAY_MCP,
            agent_id=PROVISIONED_AGENT,
            requester_oid=OID_A,
            requester_email="a@x",
        )
    )
    with pytest.raises(MarketplaceError):
        _run(svc.approve(sub.id, decided_by=ADMIN))
    assert grant_fn.await_count == 1

    grant_fn.side_effect = None
    svc.set_listing(product_type="mcp", product_id=GATEWAY_MCP, available=False)

    with pytest.raises(MarketplaceError) as ei:
        _run(svc.retry_grant(sub.id, decided_by=ADMIN))
    assert ei.value.kind == "grant_failed"
    assert str(ei.value) == "MCP product is no longer published"
    failed = next(s for s in svc.list_subscriptions() if s.id == sub.id)
    assert failed.status == "failed"
    assert failed.error == "MCP product is no longer published"
    assert grant_fn.await_count == 1


def test_approve_mcp_after_deprecation_fails_and_grants_nothing():
    """The re-gate catches lifecycle too: a ``transition`` writes the native status only and
    leaves the marketplace block untouched, so an admin must not grant on a retired server."""
    svc, grant_fn, _ = _build_service()
    sub = _run(
        svc.create_subscription(
            product_type="mcp",
            product_id=GATEWAY_MCP,
            agent_id=PROVISIONED_AGENT,
            requester_oid=OID_A,
            requester_email="a@x",
        )
    )
    svc._mcp_registry.get(GATEWAY_MCP).lifecycle_state = "deprecated"

    with pytest.raises(MarketplaceError) as ei:
        _run(svc.approve(sub.id, decided_by=ADMIN))
    assert ei.value.kind == "grant_failed"
    grant_fn.assert_not_called()


def test_revoke_mcp_sub_still_works_after_unpublish():
    """The kill switch is deliberately UNGATED: an admin must be able to tear down access to a
    product that has just been delisted — that is exactly when they most need to."""
    svc, _, revoke_fn = _build_service()
    approved = _approved_mcp_sub(svc)
    _run(svc.unpublish("mcp", GATEWAY_MCP, ADMIN))

    revoked = _run(svc.revoke_subscription(approved.id, decided_by=ADMIN))
    assert revoked.status == "revoked"
    revoke_fn.assert_awaited_once()


def test_subscribe_to_an_unpublished_mcp_is_not_found():
    """The MCP BIRTH gate: an approved gateway with no declaration is not a product, and the
    answer is byte-identical to a nonexistent id (no enumeration oracle)."""
    svc, grant_fn, _ = _build_service()
    with pytest.raises(MarketplaceError) as missing:
        _run(
            svc.create_subscription(
                product_type="mcp",
                product_id="m-does-not-exist",
                agent_id=PROVISIONED_AGENT,
                requester_oid=OID_A,
                requester_email="a@x",
            )
        )
    with pytest.raises(MarketplaceError) as unpublished:
        _run(
            svc.create_subscription(
                product_type="mcp",
                product_id=UNPUBLISHED_MCP,
                agent_id=PROVISIONED_AGENT,
                requester_oid=OID_A,
                requester_email="a@x",
            )
        )
    assert missing.value.kind == unpublished.value.kind == "not_found"
    assert str(unpublished.value) == str(missing.value) == "Unknown MCP product"
    assert svc.list_subscriptions() == []
    grant_fn.assert_not_called()


def test_subscribe_to_a_published_standard_mcp_works():
    """The retired ``kind == "gateway"`` filter, proven at the subscribe gate: a PUBLISHED
    standard-kind server is subscribable (and grantable) exactly like a gateway."""
    svc, grant_fn, _ = _build_service()
    svc.set_listing(product_type="mcp", product_id=STANDARD_MCP, auto_approve=True)
    sub = _run(
        svc.create_subscription(
            product_type="mcp",
            product_id=STANDARD_MCP,
            agent_id=PROVISIONED_AGENT,
            requester_oid=OID_A,
            requester_email="a@x",
        )
    )
    assert sub.status == "approved"
    assert sub.product_name == "Standard MCP"
    assert sub.grant_assignment_id == "asg-1"
    grant_fn.assert_awaited_once()


PTYPES = [
    pytest.param("agent", "agent-ready", "agent-draft", "agent-foreign", id="agent"),
    pytest.param("mcp", "mcp-ready", "mcp-draft", "mcp-foreign", id="mcp"),
]


def _publish_service(*, agents=None, mcps=None):
    """A service whose registries each hold ONE approved, provisioned, tenant-tagged product
    ready to publish (plus a not-yet-approved one and a foreign-tenant one), and no published
    products at all yet. Returns ``(svc, agent_registry, mcp_registry)``."""
    if agents is None:
        agents = [
            _agent("agent-ready", "Ready Agent", tenant_id="ten-1", lifecycle_state="approved"),
            _agent("agent-draft", "Draft Agent", tenant_id="ten-1", lifecycle_state="proposed"),
            _agent("agent-foreign", "Foreign Agent", tenant_id="ten-2", lifecycle_state="approved"),
        ]
    if mcps is None:
        mcps = [
            _mcp("mcp-ready", "Ready MCP", tenant_id="ten-1", lifecycle_state="approved"),
            _mcp("mcp-draft", "Draft MCP", tenant_id="ten-1", lifecycle_state="proposed"),
            _mcp("mcp-foreign", "Foreign MCP", tenant_id="ten-2", lifecycle_state="approved"),
        ]
    svc, _, _ = _build_service(agents=agents, mcps=mcps)
    return svc, svc._agent_registry, svc._mcp_registry


def _registry_of(svc, product_type):
    """The registry that should have received the envelope write — the assertion twin of the
    service's own ``_registry_for`` dispatch."""
    return svc._agent_registry if product_type == "agent" else svc._mcp_registry


def _other_registry_of(svc, product_type):
    return svc._mcp_registry if product_type == "agent" else svc._agent_registry


def _cards_of(svc, product_type, caller_oid=OID_A):
    return (
        svc.list_agent_products(caller_oid=caller_oid)
        if product_type == "agent"
        else svc.list_mcp_products(caller_oid=caller_oid)
    )


def _pending_request(svc, product_type="agent", product_id="agent-ready", **ds):
    return svc.create_publish_request(
        product_type=product_type,
        product_id=product_id,
        datasheet=_datasheet(**ds),
        requester_oid=OID_A,
        requester_email="a@x",
        ctx=_ctx(is_global=True),
    )


def _request_for(svc: MarketplaceService, product_type: str, product_id: str) -> PublishRequest:
    req = svc.get_publish_request_for_product(product_type, product_id, ctx=_ctx(is_global=True))
    assert req is not None
    return req


@pytest.mark.parametrize("ptype,ready,draft,foreign", PTYPES)
def test_create_publish_request_persists_pending_with_requester_audit(ptype, ready, draft, foreign):
    svc, _, _ = _publish_service()
    req = svc.create_publish_request(
        product_type=ptype,
        product_id=ready,
        datasheet=_datasheet(sla_tier="Gold"),
        requester_oid=OID_A,
        requester_email="a@x",
        ctx=_ctx(tenant_ids=["ten-1"]),
    )

    assert req.id.startswith("pub-")
    assert req.product_type == ptype
    assert req.product_id == ready
    assert req.product_name == ("Ready Agent" if ptype == "agent" else "Ready MCP")
    assert req.tenant_id == "ten-1"
    assert req.status == "pending"
    assert req.requested_by == OID_A
    assert req.requested_by_email == "a@x"
    assert req.datasheet.sla_tier == "Gold"
    assert req.decided_by is None
    assert req.decided_at is None
    assert req.error is None
    assert _request_for(svc, ptype, ready).id == req.id
    assert [r.id for r in svc.list_publish_requests()] == [req.id]


@pytest.mark.parametrize("ptype,ready,draft,foreign", PTYPES)
def test_create_publish_request_requires_approved_lifecycle(ptype, ready, draft, foreign):
    svc, _, _ = _publish_service()
    with pytest.raises(MarketplaceError) as ei:
        svc.create_publish_request(
            product_type=ptype,
            product_id=draft,
            datasheet=_datasheet(),
            requester_oid=OID_A,
            requester_email="a@x",
            ctx=_ctx(tenant_ids=["ten-1"]),
        )
    assert ei.value.kind == "agent_not_approved"
    assert svc.get_publish_request_for_product(ptype, draft, ctx=_ctx(is_global=True)) is None


@pytest.mark.parametrize("ptype,ready,draft,foreign", PTYPES)
@pytest.mark.parametrize(
    "identity",
    [
        pytest.param(dict(identity_status="none"), id="not-provisioned"),
        pytest.param(dict(entra_sp_id=None), id="no-sp-id"),
        pytest.param(dict(invoker_role_id=None), id="no-invoker-role-id"),
    ],
)
def test_create_publish_request_requires_a_provisioned_identity(identity, ptype, ready, draft, foreign):
    """For both product types, a product whose Entra identity is not provisioned can never be granted
    on, so publishing it would advertise a dead end; all three identity fields are required."""
    svc, _, _ = _publish_service(
        agents=[
            _agent("agent-ready", "Ready Agent", tenant_id="ten-1", lifecycle_state="approved", **identity)
        ],
        mcps=[_mcp("mcp-ready", "Ready MCP", tenant_id="ten-1", lifecycle_state="approved", **identity)],
    )

    with pytest.raises(MarketplaceError) as ei:
        svc.create_publish_request(
            product_type=ptype,
            product_id=ready,
            datasheet=_datasheet(),
            requester_oid=OID_A,
            requester_email="a@x",
            ctx=_ctx(tenant_ids=["ten-1"]),
        )
    assert ei.value.kind == "identity_not_provisioned"
    assert str(ei.value) == "identity is not provisioned"
    assert svc.get_publish_request_for_product(ptype, ready, ctx=_ctx(is_global=True)) is None


@pytest.mark.parametrize("ptype,ready,draft,foreign", PTYPES)
def test_create_publish_request_lifecycle_is_checked_before_identity(ptype, ready, draft, foreign):
    """Lifecycle first, identity second: a not-yet-approved product reports the lifecycle block even
    when its identity is also missing, so the publisher is told the thing to fix first."""
    svc, _, _ = _publish_service(
        agents=[
            _agent(
                "agent-draft",
                "Draft Agent",
                tenant_id="ten-1",
                lifecycle_state="proposed",
                identity_status="none",
            )
        ],
        mcps=[
            _mcp(
                "mcp-draft",
                "Draft MCP",
                tenant_id="ten-1",
                lifecycle_state="proposed",
                identity_status="none",
            )
        ],
    )
    with pytest.raises(MarketplaceError) as ei:
        svc.create_publish_request(
            product_type=ptype,
            product_id=draft,
            datasheet=_datasheet(),
            requester_oid=OID_A,
            requester_email="a@x",
            ctx=_ctx(tenant_ids=["ten-1"]),
        )
    assert ei.value.kind == "agent_not_approved"


@pytest.mark.parametrize("ptype,ready,draft,foreign", PTYPES)
def test_create_publish_request_foreign_tenant_is_not_found(ptype, ready, draft, foreign):
    """A foreign-tenant product is INDISTINGUISHABLE from a missing one (byte-identical
    not_found) — the publish path must not confirm a foreign product's existence."""
    svc, _, _ = _publish_service()
    ctx = _ctx(tenant_ids=["ten-1"])
    with pytest.raises(MarketplaceError) as foreign_err:
        svc.create_publish_request(
            product_type=ptype,
            product_id=foreign,
            datasheet=_datasheet(),
            requester_oid=OID_A,
            requester_email="a@x",
            ctx=ctx,
        )
    with pytest.raises(MarketplaceError) as missing:
        svc.create_publish_request(
            product_type=ptype,
            product_id="does-not-exist",
            datasheet=_datasheet(),
            requester_oid=OID_A,
            requester_email="a@x",
            ctx=ctx,
        )
    assert foreign_err.value.kind == missing.value.kind == "not_found"
    assert str(foreign_err.value) == str(missing.value)


def test_create_publish_request_refuses_a_foreign_shared_mcp():
    """``visible()`` is called without ``shared=`` on purpose: publishing is an owning-tenant action,
    while ``shared=true`` confers read and grant-target rights only. A tenant-A operator cannot file
    a publish request against tenant-B's shared MCP, and the refusal is byte-identical to a missing
    id, so it is not an existence oracle either."""
    svc, _, _ = _publish_service(
        mcps=[
            _mcp(
                "mcp-foreign-shared",
                "Foreign Shared MCP",
                tenant_id="ten-2",
                lifecycle_state="approved",
                shared=True,
            )
        ],
    )
    ctx = _ctx(tenant_ids=["ten-1"])

    with pytest.raises(MarketplaceError) as shared_err:
        svc.create_publish_request(
            product_type="mcp",
            product_id="mcp-foreign-shared",
            datasheet=_datasheet(),
            requester_oid=OID_A,
            requester_email="a@x",
            ctx=ctx,
        )
    with pytest.raises(MarketplaceError) as missing:
        svc.create_publish_request(
            product_type="mcp",
            product_id="does-not-exist",
            datasheet=_datasheet(),
            requester_oid=OID_A,
            requester_email="a@x",
            ctx=ctx,
        )

    assert shared_err.value.kind == missing.value.kind == "not_found"
    assert str(shared_err.value) == str(missing.value)
    assert svc.get_publish_request_for_product("mcp", "mcp-foreign-shared", ctx=_ctx(is_global=True)) is None
    assert (
        svc.create_publish_request(
            product_type="mcp",
            product_id="mcp-foreign-shared",
            datasheet=_datasheet(),
            requester_oid=OID_B,
            requester_email="b@x",
            ctx=_ctx(tenant_ids=["ten-2"]),
        ).tenant_id
        == "ten-2"
    )


@pytest.mark.parametrize("ptype,ready,draft,foreign", PTYPES)
def test_create_publish_request_admin_sees_every_tenant(ptype, ready, draft, foreign):
    svc, _, _ = _publish_service()
    req = svc.create_publish_request(
        product_type=ptype,
        product_id=foreign,
        datasheet=_datasheet(),
        requester_oid=ADMIN,
        requester_email="admin@x",
        ctx=_ctx(is_global=True),
    )
    assert req.tenant_id == "ten-2"


@pytest.mark.parametrize("ptype,ready,draft,foreign", PTYPES)
def test_create_publish_request_second_pending_conflicts(ptype, ready, draft, foreign):
    svc, _, _ = _publish_service()
    kwargs: dict[str, Any] = dict(
        product_type=ptype,
        product_id=ready,
        requester_oid=OID_A,
        requester_email="a@x",
        ctx=_ctx(tenant_ids=["ten-1"]),
    )
    first = svc.create_publish_request(datasheet=_datasheet(), **kwargs)
    with pytest.raises(MarketplaceError) as ei:
        svc.create_publish_request(datasheet=_datasheet(sla_tier="Silver"), **kwargs)
    assert ei.value.kind == "publish_conflict"
    stored = _request_for(svc, ptype, ready)
    assert stored.id == first.id
    assert stored.datasheet.sla_tier is None


@pytest.mark.parametrize("ptype,ready,draft,foreign", PTYPES)
def test_create_publish_request_after_rejection_replaces_the_record(ptype, ready, draft, foreign):
    """ONE record per PRODUCT: a re-publish after a REJECT overwrites it (the decision
    history lives in the audit fields, not in extra rows)."""
    svc, _, _ = _publish_service()
    kwargs: dict[str, Any] = dict(
        product_type=ptype,
        product_id=ready,
        requester_oid=OID_A,
        requester_email="a@x",
        ctx=_ctx(tenant_ids=["ten-1"]),
    )
    first = svc.create_publish_request(datasheet=_datasheet(), **kwargs)
    svc.reject_publish(first.id, ADMIN, "thin datasheet")

    second = svc.create_publish_request(datasheet=_datasheet(sla_tier="Gold"), **kwargs)
    assert second.status == "pending"
    assert second.datasheet.sla_tier == "Gold"
    assert len(svc.list_publish_requests()) == 1
    assert _request_for(svc, ptype, ready).id == second.id


def test_publish_records_of_the_two_types_do_not_collide():
    """An agent and an MCP server sharing a product id keep distinct publish records; without the type
    in the sort key the second publish would overwrite the first and ``approve_publish`` would write
    the envelope into the wrong registry."""
    same_id = "shared-id-1"
    svc, agent_registry, mcp_registry = _publish_service(
        agents=[_agent(same_id, "Agent With Shared Id", tenant_id="ten-1", lifecycle_state="approved")],
        mcps=[_mcp(same_id, "MCP With Shared Id", tenant_id="ten-1", lifecycle_state="approved")],
    )

    agent_req = svc.create_publish_request(
        product_type="agent",
        product_id=same_id,
        datasheet=_datasheet(owner_team="Team A"),
        requester_oid=OID_A,
        requester_email="a@x",
        ctx=_ctx(is_global=True),
    )
    mcp_req = svc.create_publish_request(
        product_type="mcp",
        product_id=same_id,
        datasheet=_datasheet(owner_team="Team M"),
        requester_oid=OID_B,
        requester_email="b@x",
        ctx=_ctx(is_global=True),
    )

    assert agent_req.id != mcp_req.id
    assert len(svc.list_publish_requests()) == 2
    assert _request_for(svc, "agent", same_id).id == agent_req.id
    assert _request_for(svc, "agent", same_id).datasheet.owner_team == "Team A"
    assert _request_for(svc, "mcp", same_id).id == mcp_req.id
    assert _request_for(svc, "mcp", same_id).datasheet.owner_team == "Team M"

    _run(svc.approve_publish(agent_req.id, ADMIN))
    assert [c[0] for c in agent_registry.persist_calls] == [same_id]
    assert mcp_registry.persist_calls == []
    _run(svc.approve_publish(mcp_req.id, ADMIN))
    assert [c[0] for c in mcp_registry.persist_calls] == [same_id]


def test_list_publish_requests_filters_by_status():
    svc, _, _ = _publish_service()
    pending = _pending_request(svc, "agent", "agent-ready")
    rejected = _pending_request(svc, "mcp", "mcp-ready")
    svc.reject_publish(rejected.id, ADMIN, "no")

    assert [r.id for r in svc.list_publish_requests(status="pending")] == [pending.id]
    assert [r.id for r in svc.list_publish_requests(status="rejected")] == [rejected.id]
    assert len(svc.list_publish_requests()) == 2


@pytest.mark.parametrize("ptype,ready,draft,foreign", PTYPES)
def test_get_publish_request_for_product_none_when_absent(ptype, ready, draft, foreign):
    svc, _, _ = _publish_service()
    assert svc.get_publish_request_for_product(ptype, ready, ctx=_ctx(is_global=True)) is None


@pytest.mark.parametrize("ptype,ready,draft,foreign", PTYPES)
def test_get_publish_request_for_product_hides_a_foreign_tenants_request(ptype, ready, draft, foreign):
    """A foreign tenant's publish record reads as None — INDISTINGUISHABLE from "never
    requested", which is what makes the route's 404 byte-identical for both cases.

    Without the gate, a tenant-A OPERATOR who guesses (or lists) a tenant-B product id reads
    B's declared datasheet, requester email and decision_reason straight out of the record.
    """
    svc, _, _ = _publish_service()
    filed = svc.create_publish_request(
        product_type=ptype,
        product_id=foreign,
        datasheet=_datasheet(owner_team="Marketing EU"),
        requester_oid=OID_B,
        requester_email="bob@tenant-two.example",
        ctx=_ctx(tenant_ids=["ten-2"]),
    )
    assert filed.tenant_id == "ten-2"

    ctx_one = _ctx(tenant_ids=["ten-1"])
    assert svc.get_publish_request_for_product(ptype, foreign, ctx=ctx_one) is None
    assert svc.get_publish_request_for_product(ptype, ready, ctx=ctx_one) is None


@pytest.mark.parametrize("ptype,ready,draft,foreign", PTYPES)
def test_get_publish_request_for_product_returns_own_tenants_request(ptype, ready, draft, foreign):
    svc, _, _ = _publish_service()
    req = svc.create_publish_request(
        product_type=ptype,
        product_id=ready,
        datasheet=_datasheet(),
        requester_oid=OID_A,
        requester_email="a@x",
        ctx=_ctx(tenant_ids=["ten-1"]),
    )
    got = svc.get_publish_request_for_product(ptype, ready, ctx=_ctx(tenant_ids=["ten-1"]))
    assert got is not None
    assert got.id == req.id


@pytest.mark.parametrize("ptype,ready,draft,foreign", PTYPES)
def test_get_publish_request_for_product_admin_sees_every_tenant(ptype, ready, draft, foreign):
    """A global admin bypasses the gate — ``visible()``'s own semantics (is_global short-
    circuits), so the ADMIN approvals queue can read any tenant's request."""
    svc, _, _ = _publish_service()
    _pending_request(svc, ptype, foreign)
    got = svc.get_publish_request_for_product(ptype, foreign, ctx=_ctx(is_global=True))
    assert got is not None
    assert got.tenant_id == "ten-2"


def test_every_tenant_scoped_method_requires_ctx_as_a_keyword():
    """Every public method that takes a tenant scope takes it as a required keyword, so a call that
    omits it is a TypeError and a pyright error, never an unscoped read."""
    scoped = {}
    for name, method in inspect.getmembers(MarketplaceService, inspect.isfunction):
        param = inspect.signature(method).parameters.get("ctx")
        if not name.startswith("_") and param is not None:
            scoped[name] = param
    assert {"create_publish_request", "get_publish_request_for_product"} <= set(scoped)
    loose = {
        name: f"{param.kind.name}, default {param.default!r}"
        for name, param in scoped.items()
        if param.kind is not inspect.Parameter.KEYWORD_ONLY or param.default is not inspect.Parameter.empty
    }
    assert loose == {}


@pytest.mark.parametrize("ptype,ready,draft,foreign", PTYPES)
def test_get_publish_request_for_product_without_ctx_is_a_type_error(ptype, ready, draft, foreign):
    """A foreign tenant's record is on file and a call that omits the scope reads nothing."""
    svc, _, _ = _publish_service()
    _pending_request(svc, ptype, foreign)
    untyped: Any = svc.get_publish_request_for_product
    with pytest.raises(TypeError):
        untyped(ptype, foreign)


@pytest.mark.parametrize("ptype,ready,draft,foreign", PTYPES)
def test_get_publish_request_for_product_with_ctx_none_raises(ptype, ready, draft, foreign):
    """``ctx=None`` is not a scope: the call raises before a record is returned."""
    svc, _, _ = _publish_service()
    _pending_request(svc, ptype, foreign)
    untyped: Any = svc.get_publish_request_for_product
    with pytest.raises((TypeError, AttributeError)):
        untyped(ptype, foreign, ctx=None)


@pytest.mark.parametrize("ptype,ready,draft,foreign", PTYPES)
def test_create_publish_request_with_ctx_none_raises(ptype, ready, draft, foreign):
    """``ctx=None`` is not a scope: publishing a foreign tenant's product raises and files nothing."""
    svc, _, _ = _publish_service()
    untyped: Any = svc.create_publish_request
    with pytest.raises((TypeError, AttributeError)):
        untyped(
            product_type=ptype,
            product_id=foreign,
            datasheet=_datasheet(),
            requester_oid=OID_A,
            requester_email="a@x",
            ctx=None,
        )
    assert svc.get_publish_request_for_product(ptype, foreign, ctx=_ctx(is_global=True)) is None


def test_get_publish_request_for_product_untagged_request_hidden_from_scoped_caller():
    """An untagged request (a product with no tenant_id) is invisible to a scoped caller and visible
    to an admin, exactly as ``visible()`` defines for a ``tenant_id is None`` resource."""
    svc, _, _ = _publish_service(
        agents=[_agent("agent-untagged", "Untagged Agent", tenant_id=None, lifecycle_state="approved")],
        mcps=[_mcp("mcp-untagged", "Untagged MCP", tenant_id=None, lifecycle_state="approved")],
    )
    for ptype, pid in (("agent", "agent-untagged"), ("mcp", "mcp-untagged")):
        req = svc.create_publish_request(
            product_type=ptype,
            product_id=pid,
            datasheet=_datasheet(),
            requester_oid=OID_A,
            requester_email="a@x",
            ctx=_ctx(is_global=True),
        )
        assert req.tenant_id is None
        assert svc.get_publish_request_for_product(ptype, pid, ctx=_ctx(tenant_ids=["ten-1"])) is None
        assert svc.get_publish_request_for_product(ptype, pid, ctx=_ctx(is_global=True)) is not None


@pytest.mark.parametrize("ptype,ready,draft,foreign", PTYPES)
def test_approve_publish_writes_the_publication_to_the_right_registry(ptype, ready, draft, foreign):
    svc, _, _ = _publish_service()
    registry = _registry_of(svc, ptype)
    other = _other_registry_of(svc, ptype)
    req = _pending_request(svc, ptype, ready, sla_tier="Gold", compliance=["GDPR"])

    approved = _run(svc.approve_publish(req.id, ADMIN))

    assert approved.status == "approved"
    assert approved.decided_by == ADMIN
    assert approved.decided_at is not None
    assert approved.error is None
    assert len(registry.persist_calls) == 1
    assert other.persist_calls == []
    product_id, publication = registry.persist_calls[0]
    assert product_id == ready
    assert publication.published is True
    assert publication.declared_by == ADMIN
    assert publication.declared_at is not None
    assert publication.datasheet.sla_tier == "Gold"
    assert publication.datasheet.compliance == ["GDPR"]
    assert _request_for(svc, ptype, ready).status == "approved"
    assert [c.product_id for c in _cards_of(svc, ptype)] == [ready]


@pytest.mark.parametrize("ptype,ready,draft,foreign", PTYPES)
def test_approve_publish_re_asserts_the_lifecycle_gate(ptype, ready, draft, foreign):
    """A request outlives its create-time gates: the product may be DEPRECATED while it sits
    in the queue (``transition()`` leaves the marketplace block alone), so approve re-checks
    rather than advertising a retired product. No envelope write happens."""
    svc, _, _ = _publish_service()
    registry = _registry_of(svc, ptype)
    req = _pending_request(svc, ptype, ready)
    registry.get(ready).lifecycle_state = "deprecated"

    with pytest.raises(MarketplaceError) as ei:
        _run(svc.approve_publish(req.id, ADMIN))
    assert ei.value.kind == "agent_not_approved"
    assert registry.persist_calls == []
    assert _request_for(svc, ptype, ready).status == "pending"


@pytest.mark.parametrize("ptype,ready,draft,foreign", PTYPES)
def test_approve_publish_re_asserts_the_identity_gate(ptype, ready, draft, foreign):
    """An identity torn down between the request and the decision blocks the publication with the
    fixed literal and writes no envelope."""
    svc, _, _ = _publish_service()
    registry = _registry_of(svc, ptype)
    req = _pending_request(svc, ptype, ready)
    registry.get(ready).identity_status = "failed"

    with pytest.raises(MarketplaceError) as ei:
        _run(svc.approve_publish(req.id, ADMIN))
    assert ei.value.kind == "identity_not_provisioned"
    assert str(ei.value) == "identity is not provisioned"
    assert registry.persist_calls == []
    assert _request_for(svc, ptype, ready).status == "pending"


def test_create_publish_request_refuses_a_databricks_governed_agent():
    """The subscription grant path has no Databricks ACL mirror, so publishing a Databricks-governed
    agent is refused up front, before the identity gate: a provisioned agent must hear the true
    reason, not an identity answer that implies a provisioning retry could unlock publish."""
    agent = _agent("agent-dbx", "Databricks Agent", tenant_id="ten-1", platform="databricks")
    svc, _, _ = _publish_service(agents=[agent])

    with pytest.raises(MarketplaceError) as ei:
        svc.create_publish_request(
            product_type="agent",
            product_id="agent-dbx",
            datasheet=_datasheet(),
            requester_oid=OID_A,
            requester_email="a@x",
            ctx=_ctx(tenant_ids=["ten-1"]),
        )
    assert ei.value.kind == "databricks_publish_unsupported"
    assert svc.get_publish_request_for_product("agent", "agent-dbx", ctx=_ctx(is_global=True)) is None


def test_publish_is_refused_for_ANY_platform_whose_declaration_withholds_it(monkeypatch):
    """The gate is an allowlist read from the platform declaration, not a Databricks denylist: a
    platform whose declaration says ``marketplace_publish=False`` is refused with the generic kind,
    and only Databricks keeps its own kind.

    The non-publish platform is faked at the declaration seam because ``TenantPlatform`` is a closed
    enum with two members: substituting ``declaration_for`` exercises the only thing this gate reads.
    """
    from services import marketplace_service as ms

    monkeypatch.setattr(
        ms,
        "declaration_for",
        lambda platform: types.SimpleNamespace(marketplace_publish=False),
    )
    agent = _agent("agent-aws", "AWS Agent", tenant_id="ten-1")
    svc, _, _ = _publish_service(agents=[agent])

    with pytest.raises(MarketplaceError) as ei:
        svc.create_publish_request(
            product_type="agent",
            product_id="agent-aws",
            datasheet=_datasheet(),
            requester_oid=OID_A,
            requester_email="a@x",
            ctx=_ctx(tenant_ids=["ten-1"]),
        )
    assert ei.value.kind == "publish_unsupported_platform"
    assert "platform" in str(ei.value)
    assert svc.get_publish_request_for_product("agent", "agent-aws", ctx=_ctx(is_global=True)) is None


def test_an_inventory_only_platform_label_is_left_publishable():
    """The six labels AGP does not govern have no governed publish path to refuse, and refusing
    here would silently retire a shipped catalog behavior. Unchanged by the flip."""
    agent = _agent("agent-sap", "SAP Agent", tenant_id="ten-1", platform="sap")
    svc, _, _ = _publish_service(agents=[agent])

    req = svc.create_publish_request(
        product_type="agent",
        product_id="agent-sap",
        datasheet=_datasheet(),
        requester_oid=OID_A,
        requester_email="a@x",
        ctx=_ctx(tenant_ids=["ten-1"]),
    )
    assert req.status == "pending"


def test_approve_publish_re_asserts_the_databricks_gate():
    """An agent that became Databricks-governed between the request and the decision blocks
    the publication with the seam kind, and writes no envelope."""
    svc, _, _ = _publish_service()
    registry = _registry_of(svc, "agent")
    req = _pending_request(svc, "agent", "agent-ready")
    registry.get("agent-ready").platform = "databricks"

    with pytest.raises(MarketplaceError) as ei:
        _run(svc.approve_publish(req.id, ADMIN))
    assert ei.value.kind == "databricks_publish_unsupported"
    assert registry.persist_calls == []
    assert _request_for(svc, "agent", "agent-ready").status == "pending"


@pytest.mark.parametrize("ptype,ready,draft,foreign", PTYPES)
def test_approve_publish_non_pending_is_illegal_state(ptype, ready, draft, foreign):
    svc, _, _ = _publish_service()
    registry = _registry_of(svc, ptype)
    req = _pending_request(svc, ptype, ready)
    _run(svc.approve_publish(req.id, ADMIN))
    registry.persist_calls.clear()

    with pytest.raises(MarketplaceError) as ei:
        _run(svc.approve_publish(req.id, ADMIN))
    assert ei.value.kind == "illegal_publish_state"
    assert registry.persist_calls == []


def test_approve_publish_unknown_request_is_not_found():
    svc, agent_registry, mcp_registry = _publish_service()
    with pytest.raises(MarketplaceError) as ei:
        _run(svc.approve_publish("pub-doesnotexist", ADMIN))
    assert ei.value.kind == "not_found"
    assert agent_registry.persist_calls == []
    assert mcp_registry.persist_calls == []


@pytest.mark.parametrize("ptype,ready,draft,foreign", PTYPES)
def test_approve_publish_registry_not_found_keeps_request_pending(ptype, ready, draft, foreign):
    """The product was deleted between the request and the approval: the request STAYS
    PENDING with a safe error so the admin can retry, and the kind is
    ``publish_write_failed`` (→502), never a silent success. This is the seam where BOTH
    registries' domain errors land — ``AgentNotFoundError`` and ``McpServerNotFoundError``."""
    svc, _, _ = _publish_service()
    registry = _registry_of(svc, ptype)
    req = _pending_request(svc, ptype, ready)
    registry._agents.pop(ready) if ptype == "agent" else registry._mcps.pop(ready)

    with pytest.raises(MarketplaceError) as ei:
        _run(svc.approve_publish(req.id, ADMIN))
    assert ei.value.kind == "publish_write_failed"

    stored = _request_for(svc, ptype, ready)
    assert stored.status == "pending"
    assert stored.error
    assert stored.decided_by is None
    assert stored.decided_at is None


@pytest.mark.parametrize("ptype,ready,draft,foreign", PTYPES)
def test_approve_publish_raw_client_error_never_escapes(ptype, ready, draft, foreign):
    """``persist_marketplace`` re-reads the record and then writes, so a delete in between surfaces a
    raw botocore ``ClientError`` rather than the domain not-found; the approve path catches registry
    failures broadly, so the request is left pending with its error recorded instead of undecided
    behind a 500."""
    from botocore.exceptions import ClientError

    svc, _, _ = _publish_service()
    registry = _registry_of(svc, ptype)
    req = _pending_request(svc, ptype, ready)
    registry.persist_error = ClientError(
        {"Error": {"Code": "ResourceNotFoundException", "Message": "gone"}},
        "UpdateRegistryRecord",
    )

    with pytest.raises(MarketplaceError) as ei:
        _run(svc.approve_publish(req.id, ADMIN))
    assert ei.value.kind == "publish_write_failed"

    stored = _request_for(svc, ptype, ready)
    assert stored.status == "pending"
    assert stored.error


@pytest.mark.parametrize("ptype,ready,draft,foreign", PTYPES)
def test_approve_publish_error_is_a_fixed_literal_not_the_exception(ptype, ready, draft, foreign):
    """The persisted/raised text is a FIXED literal — never ``str(exc)`` (no leak of a
    registry id, an ARN, or any AWS detail into a request record or a response)."""
    svc, _, _ = _publish_service()
    registry = _registry_of(svc, ptype)
    req = _pending_request(svc, ptype, ready)
    registry.persist_error = RuntimeError("arn:aws:secret-detail registry-id-1234")

    with pytest.raises(MarketplaceError) as ei:
        _run(svc.approve_publish(req.id, ADMIN))

    stored = _request_for(svc, ptype, ready)
    assert stored.error is not None
    for text in (stored.error, str(ei.value)):
        assert "arn:aws" not in text
        assert "registry-id-1234" not in text


@pytest.mark.parametrize("ptype,ready,draft,foreign", PTYPES)
def test_approve_publish_clears_a_stale_error_on_retry(ptype, ready, draft, foreign):
    svc, _, _ = _publish_service()
    registry = _registry_of(svc, ptype)
    req = _pending_request(svc, ptype, ready)
    registry.persist_error = RuntimeError("boom")
    with pytest.raises(MarketplaceError):
        _run(svc.approve_publish(req.id, ADMIN))
    assert _request_for(svc, ptype, ready).error

    registry.persist_error = None
    approved = _run(svc.approve_publish(req.id, ADMIN))
    assert approved.status == "approved"
    assert approved.error is None


@pytest.mark.parametrize("ptype,ready,draft,foreign", PTYPES)
def test_approve_publish_reports_a_failed_lifecycle_reassert(ptype, ready, draft, foreign):
    """The envelope write landed but the record could not be put back to APPROVED, so the card stays
    delisted: not a successful approve. The request stays PENDING with a fixed literal naming the
    re-approval, and the call fails."""
    svc, _, _ = _publish_service()
    registry = _registry_of(svc, ptype)
    req = _pending_request(svc, ptype, ready)
    real = registry.persist_marketplace

    def _reset_and_not_reasserted(product_id, publication):
        product = real(product_id, publication)
        product.lifecycle_state = "proposed"
        return product

    registry.persist_marketplace = _reset_and_not_reasserted

    with pytest.raises(MarketplaceError) as ei:
        _run(svc.approve_publish(req.id, ADMIN))
    assert ei.value.kind == "publish_needs_reapproval"

    stored = _request_for(svc, ptype, ready)
    assert stored.status == "pending"
    assert stored.error is not None
    assert "re-approv" in stored.error
    assert stored.decided_by is None


@pytest.mark.parametrize("ptype,ready,draft,foreign", PTYPES)
def test_approve_publish_writes_off_the_event_loop(ptype, ready, draft, foreign):
    """The registry write and the lifecycle re-assert poll the registry, so they run in a worker
    thread, never on the event loop."""
    import threading

    svc, _, _ = _publish_service()
    registry = _registry_of(svc, ptype)
    req = _pending_request(svc, ptype, ready)
    real = registry.persist_marketplace
    seen = {}

    def _record_thread(product_id, publication):
        seen["thread"] = threading.get_ident()
        return real(product_id, publication)

    registry.persist_marketplace = _record_thread
    loop_thread = threading.get_ident()

    _run(svc.approve_publish(req.id, ADMIN))

    assert seen["thread"] != loop_thread


@pytest.mark.parametrize("ptype,ready,draft,foreign", PTYPES)
def test_reject_publish_records_reason_and_leaves_registry_untouched(ptype, ready, draft, foreign):
    svc, _, _ = _publish_service()
    registry = _registry_of(svc, ptype)
    req = _pending_request(svc, ptype, ready)

    rejected = svc.reject_publish(req.id, ADMIN, "datasheet too thin")

    assert rejected.status == "rejected"
    assert rejected.decided_by == ADMIN
    assert rejected.decided_at is not None
    assert rejected.decision_reason == "datasheet too thin"
    assert registry.persist_calls == []
    assert _request_for(svc, ptype, ready).status == "rejected"
    assert _cards_of(svc, ptype) == []


@pytest.mark.parametrize("ptype,ready,draft,foreign", PTYPES)
def test_reject_publish_non_pending_is_illegal_state(ptype, ready, draft, foreign):
    svc, _, _ = _publish_service()
    req = _pending_request(svc, ptype, ready)
    svc.reject_publish(req.id, ADMIN, "no")
    with pytest.raises(MarketplaceError) as ei:
        svc.reject_publish(req.id, ADMIN, "no again")
    assert ei.value.kind == "illegal_publish_state"


def test_reject_publish_unknown_request_is_not_found():
    svc, _, _ = _publish_service()
    with pytest.raises(MarketplaceError) as ei:
        svc.reject_publish("pub-doesnotexist", ADMIN, None)
    assert ei.value.kind == "not_found"


@pytest.mark.parametrize("ptype,ready,draft,foreign", PTYPES)
def test_unpublish_flips_published_false_keeping_the_datasheet(ptype, ready, draft, foreign):
    svc, _, _ = _publish_service()
    registry = _registry_of(svc, ptype)
    other = _other_registry_of(svc, ptype)
    req = _pending_request(svc, ptype, ready, sla_tier="Gold")
    _run(svc.approve_publish(req.id, ADMIN))
    registry.persist_calls.clear()

    assert _run(svc.unpublish(ptype, ready, "oid-admin-2")) is None

    assert len(registry.persist_calls) == 1
    assert other.persist_calls == []
    product_id, publication = registry.persist_calls[0]
    assert product_id == ready
    assert publication.published is False
    assert publication is not None
    assert publication.datasheet.sla_tier == "Gold"
    assert publication.declared_by == ADMIN
    assert publication.declared_at is not None
    assert _cards_of(svc, ptype) == []


@pytest.mark.parametrize("ptype,ready,draft,foreign", PTYPES)
def test_unpublish_absent_or_already_unpublished_is_not_found(ptype, ready, draft, foreign):
    svc, _, _ = _publish_service()
    registry = _registry_of(svc, ptype)
    with pytest.raises(MarketplaceError) as ei:
        _run(svc.unpublish(ptype, ready, ADMIN))
    assert ei.value.kind == "not_found"

    req = _pending_request(svc, ptype, ready)
    _run(svc.approve_publish(req.id, ADMIN))
    _run(svc.unpublish(ptype, ready, ADMIN))
    registry.persist_calls.clear()
    with pytest.raises(MarketplaceError) as ei:
        _run(svc.unpublish(ptype, ready, ADMIN))
    assert ei.value.kind == "not_found"
    assert registry.persist_calls == []


@pytest.mark.parametrize("ptype,ready,draft,foreign", PTYPES)
def test_unpublish_unknown_product_is_not_found(ptype, ready, draft, foreign):
    svc, agent_registry, mcp_registry = _publish_service()
    with pytest.raises(MarketplaceError) as ei:
        _run(svc.unpublish(ptype, "does-not-exist", ADMIN))
    assert ei.value.kind == "not_found"
    assert agent_registry.persist_calls == []
    assert mcp_registry.persist_calls == []


@pytest.mark.parametrize("ptype,ready,draft,foreign", PTYPES)
def test_unpublish_registry_failure_raises_publish_write_failed(ptype, ready, draft, foreign):
    svc, _, _ = _publish_service()
    registry = _registry_of(svc, ptype)
    req = _pending_request(svc, ptype, ready)
    _run(svc.approve_publish(req.id, ADMIN))
    registry.persist_error = RuntimeError("arn:aws:leaky detail")

    with pytest.raises(MarketplaceError) as ei:
        _run(svc.unpublish(ptype, ready, ADMIN))
    assert ei.value.kind == "publish_write_failed"
    assert "arn:aws" not in str(ei.value)


@pytest.mark.parametrize(
    "ptype,pid,expected_sk",
    [
        pytest.param("agent", "agent-ready", "publish#agent#agent-ready", id="agent"),
        pytest.param("mcp", "mcp-ready", "publish#mcp#mcp-ready", id="mcp"),
    ],
)
def test_publish_requests_survive_the_ddb_item_roundtrip(ptype, pid, expected_sk):
    """The publish record uses the generic ``model_dump_json`` item idiom under
    ``sk = publish#<product_type>#<product_id>`` / ``_record_kind = "publish"`` — one record
    per PRODUCT, with the type in the key so the two registries get disjoint key spaces."""
    svc, _, _ = _publish_service()
    req = _pending_request(svc, ptype, pid, compliance=["GDPR", "BaFin"], sla_tier="Gold")

    item = svc._rows._publish_to_item(req)
    assert item["pk"] == "MARKETPLACE"
    assert item["sk"] == expected_sk
    assert item["_record_kind"] == "publish"

    back = svc._rows._publish_from_item(item)
    assert back == req
    assert back.datasheet.compliance == ["GDPR", "BaFin"]


def test_publish_sk_keys_on_the_product_type_and_id_pair():
    """The sk FORMAT is a contract (it is the record's identity), and the type comes from
    ``ProductType`` so a raw enum member can never leak ``"ProductType.AGENT"`` into a key."""
    from models.marketplace import ProductType
    from services.marketplace_store import _publish_sk

    assert _publish_sk("agent", "p-1") == "publish#agent#p-1"
    assert _publish_sk("mcp", "p-1") == "publish#mcp#p-1"
    assert _publish_sk(ProductType.AGENT, "p-1") == "publish#agent#p-1"
    assert _publish_sk(ProductType.MCP, "p-1") == "publish#mcp#p-1"
    assert _publish_sk("agent", "p-1") != _publish_sk("mcp", "p-1")


def test_publishing_an_mcp_makes_it_a_subscribable_marketplace_product():
    """End to end for the new MCP lane: a standard-kind server with no declaration is not a
    product, and one publish (declare → approve) turns it into a card AND a subscribable
    product — publish is the only door, for both types."""
    svc, _grant_fn, _ = _publish_service()
    assert svc.list_mcp_products(caller_oid=OID_A) == []
    with pytest.raises(MarketplaceError):
        _run(
            svc.create_subscription(
                product_type="mcp",
                product_id="mcp-ready",
                agent_id="agent-ready",
                requester_oid=OID_A,
                requester_email="a@x",
            )
        )

    req = _pending_request(svc, "mcp", "mcp-ready", sla_tier="Silver")
    _run(svc.approve_publish(req.id, ADMIN))

    card = svc.list_mcp_products(caller_oid=OID_A)[0]
    assert card.product_id == "mcp-ready"
    assert card.sla_tier == "Silver"
    sub = _run(
        svc.create_subscription(
            product_type="mcp",
            product_id="mcp-ready",
            agent_id="agent-ready",
            requester_oid=OID_A,
            requester_email="a@x",
        )
    )
    assert sub.status == "pending"
    assert sub.product_name == "Ready MCP"


def test_the_not_provisioned_sentence_is_the_same_on_every_grant_surface():
    """The marketplace approval, the grant mirror whose row the grant routes raise, and the invoke
    refusal answer an unprovisioned agent identity with one sentence, held in three copies that
    this test alone ties together."""
    assert _AGENT_NOT_PROVISIONED == grant_mirror_service._NOT_PROVISIONED
    assert 'GrantMirrorError(kind="not_provisioned")' in inspect.getsource(grants_routes)
    assert InvokeError.kinds["not_provisioned"].detail == grant_mirror_service._NOT_PROVISIONED


def test_two_admins_approving_one_request_yield_one_approval_and_one_conflict():
    """Both admins read the request PENDING before either writes: each registry write waits for
    the other's read, so the write's condition, not the read-side guard, decides who approved."""
    svc, _, _ = _publish_service()
    registry = _registry_of(svc, "agent")
    req = _pending_request(svc, "agent", "agent-ready")
    both_read = threading.Barrier(2)
    persist = registry.persist_marketplace

    def persist_after_both_read(product_id, publication):
        both_read.wait(timeout=5)
        return persist(product_id, publication)

    registry.persist_marketplace = persist_after_both_read

    async def race():
        return await asyncio.gather(
            svc.approve_publish(req.id, "admin-a"),
            svc.approve_publish(req.id, "admin-b"),
            return_exceptions=True,
        )

    outcomes = _run(race())
    approved = [o for o in outcomes if not isinstance(o, BaseException)]
    refused = [o for o in outcomes if isinstance(o, BaseException)]
    assert len(approved) == 1 and len(refused) == 1
    assert isinstance(refused[0], MarketplaceError)
    assert refused[0].kind == "illegal_publish_state"
    stored = _request_for(svc, "agent", "agent-ready")
    assert stored.status == "approved"
    assert stored.decided_by == approved[0].decided_by


class _ReadsAsTheyStood:
    def __init__(self, live):
        self._live = live
        self._then = StateStore("")._table
        for row in live.scan()["Items"]:
            self._then.put_item(Item=row)

    def __getattr__(self, name):
        return getattr(self._live, name)

    def get_item(self, **kwargs):
        return self._then.get_item(**kwargs)

    def query(self, **kwargs):
        return self._then.query(**kwargs)


def test_two_admins_setting_different_listing_fields_both_land():
    svc, _, _ = _build_service()
    pitch = "Admin-curated pitch"
    live = svc._store
    read_before_a = StateStore("marketplace", table=_ReadsAsTheyStood(live._table))
    svc.set_listing(product_type="agent", product_id=CC_AGENT, available=False)

    svc._store = read_before_a
    answered = svc.set_listing(product_type="agent", product_id=CC_AGENT, pitch=pitch)
    svc._store = live

    assert (answered.available, answered.auto_approve, answered.pitch) == (False, False, pitch)
    card = next(c for c in svc.list_agent_products(caller_oid=OID_A) if c.product_id == CC_AGENT)
    assert (card.available, card.pitch) == (False, pitch)


class _WritesRefused:
    """A table whose reads are the memory double's and whose every write raises."""

    def __init__(self, inner):
        self._inner = inner

    def __getattr__(self, name):
        return getattr(self._inner, name)

    def put_item(self, **_kwargs):
        raise ClientError(
            {"Error": {"Code": "ProvisionedThroughputExceededException", "Message": "throttled"}},
            "PutItem",
        )

    def update_item(self, **_kwargs):
        raise ClientError(
            {"Error": {"Code": "ProvisionedThroughputExceededException", "Message": "throttled"}},
            "UpdateItem",
        )


def test_a_failed_marketplace_write_propagates_instead_of_reporting_success():
    svc, _, _ = _build_service()
    svc._store = StateStore("marketplace", table=_WritesRefused(StateStore("")._table))

    with pytest.raises(ClientError):
        _run(
            svc.create_subscription(
                product_type="agent", product_id=FNOL_AGENT, requester_oid=OID_A, requester_email="a@x"
            )
        )
    assert svc.list_subscriptions() == []

    with pytest.raises(ClientError):
        svc.set_listing(product_type="agent", product_id=FNOL_AGENT, auto_approve=True)
    fnol = next(c for c in svc.list_agent_products(caller_oid=OID_A) if c.product_id == FNOL_AGENT)
    assert fnol.my_status is None
    assert fnol.auto_approve is False


class _ReadsRefused:
    """A table whose writes are the memory double's and whose every read raises."""

    def __init__(self, inner):
        self._inner = inner

    def __getattr__(self, name):
        return getattr(self._inner, name)

    def get_item(self, **_kwargs):
        raise ClientError(
            {"Error": {"Code": "ProvisionedThroughputExceededException", "Message": "throttled"}},
            "GetItem",
        )

    def query(self, **_kwargs):
        raise ClientError(
            {"Error": {"Code": "ProvisionedThroughputExceededException", "Message": "throttled"}},
            "Query",
        )


def test_a_failed_marketplace_read_answers_absent_as_before(caplog):
    """A store fault on a read is logged and answers "nothing here", the shape every caller read
    before the store; only a write fault propagates."""
    svc, _, _ = _build_service()
    sub = _run(
        svc.create_subscription(
            product_type="agent", product_id=FNOL_AGENT, requester_oid=OID_A, requester_email="a@x"
        )
    )
    svc.set_listing(product_type="agent", product_id=FNOL_AGENT, auto_approve=True)
    svc.create_publish_request(
        product_type="agent",
        product_id=FNOL_AGENT,
        datasheet=_datasheet(),
        requester_oid=OID_A,
        requester_email="a@x",
        ctx=_ctx(is_global=True),
    )
    svc._store = StateStore("marketplace", table=_ReadsRefused(svc._store._table))

    with caplog.at_level(logging.ERROR, logger="services.marketplace_store"):
        assert svc.list_subscriptions() == []
        with pytest.raises(MarketplaceError) as refused:
            svc.reject(sub.id, decided_by="admin")
        assert svc.list_publish_requests() == []
        assert svc.get_publish_request_for_product("agent", FNOL_AGENT, ctx=_ctx(is_global=True)) is None
        fnol = next(c for c in svc.list_agent_products(caller_oid=OID_A) if c.product_id == FNOL_AGENT)

    assert refused.value.kind == "not_found"
    assert fnol.auto_approve is False
    assert fnol.my_status is None
    assert {record.getMessage() for record in caplog.records} == {
        "Failed to load subscriptions from DDB",
        f"Failed to fetch subscription {sub.id} from DDB",
        "Failed to load publish requests from DDB",
        "Failed to load listings from DDB",
    }
