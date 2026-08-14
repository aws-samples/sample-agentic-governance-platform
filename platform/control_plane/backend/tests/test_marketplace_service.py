"""Tests for the Epic 9 marketplace lifecycle orchestrator (T4).

Drives the in-memory local-fallback path (``table_name=""`` ⇒ ``_has_ddb`` false) and
injects doubles for the registries + the grant function (an ``AsyncMock``), so NO live
Graph / AWS is touched. Fakes are ``types.SimpleNamespace`` objects shaped like the
``Agent`` / ``McpServer`` records the service reads (``kind`` / ``lifecycle_state`` /
``identity_status`` / ``entra_sp_id`` / … — plain strings, matching the ``str, Enum``
``.value``s the service compares against).
"""

import asyncio
import types
from datetime import datetime, timezone

import pytest
from unittest.mock import AsyncMock

from models.agent import DataClassification
from services.agent_mcp_grant import GrantNotFoundError, GrantRevokeFailedError
from services.agent_user_grant import UserGrantError, UserGrantNotFoundError
from services.marketplace_service import MarketplaceError, MarketplaceService


# --------------------------------------------------------------------------- #
# Fakes / fixtures
# --------------------------------------------------------------------------- #

OID_A = "oid-alice"
OID_B = "oid-bob"
ADMIN = "oid-admin"


def _agent(agent_id, name, *, identity_status="provisioned", entra_sp_id="sp-a",
           invoker_role_id="role-invoker", marketplace=None, tenant_id=None,
           lifecycle_state="approved", business_unit=None, data_classification=None):
    """A fake shaped like an ``Agent`` record.

    E33: agent marketplace products are now REGISTRY-sourced, so the fake carries the
    fields the card projection + the publish guards read — ``marketplace`` (an
    ``Optional[MarketplacePublication]``), ``tenant_id``, ``lifecycle_state``, and the two
    governance fields the card reuses for agents (``business_unit`` /
    ``data_classification``).

    T3: ``invoker_role_id`` is the app role the user→agent grant assigns, so it sits
    alongside ``identity_status``/``entra_sp_id`` as one of the three fields the
    agent-subscription approve guard requires. Pass ``invoker_role_id=None`` to drive the
    "provisioned but role id missing" branch."""
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
    )


def _datasheet(**overrides):
    """A valid declared ``Datasheet`` (the 3 mandatory fields + whatever the test pins)."""
    from models.marketplace import Datasheet

    base = dict(
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


def _mcp(mcp_id, name, *, kind="gateway", lifecycle_state="approved",
         tenant_id=None, published=False, shared=False, marketplace=None,
         identity_status="provisioned", entra_sp_id="sp-m",
         invoker_role_id="role-invoker"):
    """A fake shaped like an ``McpServer`` record.

    Amendment 1 (T10/T11): MCP records now carry the same ``marketplace`` block agents do
    (``Optional[MarketplacePublication]`` — publish is the only door into the marketplace for
    both types), plus the three identity fields the C10 publish precondition reads
    (``identity_status``/``entra_sp_id``/``invoker_role_id`` — the SAME names as on ``Agent``,
    which is what lets one predicate serve both). ``published``/``shared`` below stay the E24
    cross-TENANT flags and are a DIFFERENT feature.
    """
    return types.SimpleNamespace(
        id=mcp_id,
        name=name,
        kind=kind,
        lifecycle_state=lifecycle_state,
        # E24/T8 tenant badge fields (real McpServer records carry these).
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
        # F3 governance metadata (surfaced on the MCP ProductCard). data_classification
        # is the DataClassification enum on a real record; the service reads its .value.
        data_classification=DataClassification.CONFIDENTIAL,
        region="EU",
        version="2.1.0",
        created_at=datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc),
        updated_at=datetime(2026, 6, 7, 8, 9, 10, tzinfo=timezone.utc),
    )


class _AgentRegistry:
    def __init__(self, agents):
        self._agents = {a.id: a for a in agents}
        # E33: every persist_marketplace call, as (agent_id, publication) — the publish
        # approve/unpublish assertions read this instead of a mock, so they can also see
        # the block land on the stored record.
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
    def __init__(self, mcps):
        self._mcps = {m.id: m for m in mcps}
        # T11: the MCP twin of ``_AgentRegistry.persist_calls`` — the publish
        # approve/unpublish assertions read this to prove the envelope write was dispatched
        # to the RIGHT registry.
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
# T11: a PUBLISHED standard-kind MCP is a product now (the ``kind == "gateway"`` filter is
# retired), and an UNPUBLISHED gateway is not — publish is the only door for both types.
STANDARD_MCP = "m-standard"
UNPUBLISHED_MCP = "m-unpublished-gw"

# E33: the three PUBLISHED agent products the catalog tests read. They are ordinary
# registry agents carrying an approved ``marketplace`` block — there is no blueprint list
# any more, so a product exists iff some agent's declaration was approved.
CC_AGENT = "agent-contact-center"
FNOL_AGENT = "agent-fnol"
ONB_AGENT = "agent-onboarding"


def _product_agents():
    """The published agent products in the default fixture (3 cards).

    Datasheet values are DECLARED (E33) and deliberately varied so the card-projection
    test can spot-check each field. None of them declares a measurement — uptime /
    latency / live status / rating have no home on ``Datasheet`` by design.

    T3: these are PROVISIONED with distinct SP ids, because subscribing to an agent product
    now applies a real user→agent Entra grant — an unprovisioned product could never be
    approved. (Before T3 they carried ``identity_status="none"``, which was harmless only
    while agent approval was a pure status flip.)
    """
    return [
        _agent(
            CC_AGENT, "Contact Center Agent",
            entra_sp_id="sp-cc", invoker_role_id="role-invoker-cc",
            business_unit="Customer Service",
            marketplace=_publication(datasheet=_datasheet(
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
            )),
        ),
        _agent(
            FNOL_AGENT, "First Notification of Loss (FNOL) Agent",
            entra_sp_id="sp-fnol", invoker_role_id="role-invoker-fnol",
            business_unit="Claims",
            marketplace=_publication(datasheet=_datasheet(
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
            )),
        ),
        # A THIN declaration: only the 3 mandatory fields. Every optional card field must
        # come back None / [] rather than a placeholder.
        _agent(
            ONB_AGENT, "Insurance Onboarding Agent",
            entra_sp_id="sp-onb", invoker_role_id="role-invoker-onb",
            business_unit="Onboarding",
            marketplace=_publication(datasheet=_datasheet(
                owner_team="Digital Onboarding",
                support_contact="onboarding@acme.com",
                data_classification="Confidential",
            )),
        ),
    ]


def _build_service(*, grant_return="asg-1", grant_side_effect=None, revoke_side_effect=None,
                   agent_grant_return="uasg-1", agent_grant_side_effect=None,
                   agent_revoke_side_effect=None,
                   mcps=None, agents=None, tenant_service=None):
    if agents is None:
        agents = [
            _agent(PROVISIONED_AGENT, "Provisioned Agent",
                   identity_status="provisioned", entra_sp_id="sp-a"),
            _agent(UNPROVISIONED_AGENT, "Draft Agent",
                   identity_status="none", entra_sp_id=None),
            # Published products (the agent catalog). The two agents above carry NO
            # marketplace block, so they are correctly absent from the product list.
            *_product_agents(),
        ]
    if mcps is None:
        # Amendment 1 / T11: the MCP catalog is PUBLICATION-gated, not kind-gated. So the
        # fixture pins all four corners of the retired ``kind == "gateway"`` filter:
        #   - a published gateway        → a product (every MCP subscription test uses it)
        #   - a published STANDARD       → also a product (the filter is really gone)
        #   - an UNPUBLISHED gateway     → NOT a product (publish is the only door)
        #   - a published, NON-approved  → NOT a product (lifecycle re-asserted on read)
        mcps = [
            _mcp(GATEWAY_MCP, "Gateway MCP", kind="gateway", lifecycle_state="approved",
                 marketplace=_publication(datasheet=_datasheet(
                     owner_team="MCP Platform",
                     support_contact="mcp-platform@acme.com",
                     data_classification="Confidential",
                     sla_tier="Gold",
                     support_hours="24/7",
                     compliance=["GDPR"],
                     guardrails=["Tool allow-list"],
                     pitch="Claims tooling over the governed gateway.",
                 ))),
            _mcp(STANDARD_MCP, "Standard MCP", kind="standard", lifecycle_state="approved",
                 marketplace=_publication()),
            _mcp(UNPUBLISHED_MCP, "Unpublished Gateway", kind="gateway",
                 lifecycle_state="approved", marketplace=None),
            _mcp("m-pending", "Pending Gateway", kind="gateway", lifecycle_state="proposed",
                 marketplace=_publication()),
        ]
    grant_fn = AsyncMock(return_value=grant_return)
    if grant_side_effect is not None:
        grant_fn.side_effect = grant_side_effect
    # The revoke twin of grant_fn — defaults to a no-op success (returns None, mirroring
    # the real revoke_agent_mcp_grant). Tests assert call args / inject side effects.
    revoke_fn = AsyncMock(return_value=None)
    if revoke_side_effect is not None:
        revoke_fn.side_effect = revoke_side_effect
    # T3: the user→agent grant pair (``services.agent_user_grant``), the AGENT-product twin
    # of grant_fn/revoke_fn. Injected the same way so no test touches live Graph. The return
    # value is a distinct literal ("uasg-1" vs the MCP "asg-1") so an assertion can prove
    # WHICH grant fn produced a stored assignment id. Kept OFF the returned tuple — the
    # 3-tuple shape is load-bearing for ~80 existing call sites — so agent-grant tests read
    # ``svc._agent_grant_fn`` / ``svc._agent_revoke_fn`` (the private-attribute idiom these
    # tests already use for ``svc._mcp_registry`` / ``svc._get_sub``).
    agent_grant_fn = AsyncMock(return_value=agent_grant_return)
    if agent_grant_side_effect is not None:
        agent_grant_fn.side_effect = agent_grant_side_effect
    agent_revoke_fn = AsyncMock(return_value=None)
    if agent_revoke_side_effect is not None:
        agent_revoke_fn.side_effect = agent_revoke_side_effect
    # F1: the eligibility guard reads list_assignments for the NON-sponsor path.
    # OID_A is the SPONSOR of PROVISIONED_AGENT (sp-a) → those subscribes
    # short-circuit before any Graph read. The default fake also reports OID_B as a
    # directly-granted (principalType User) principal on sp-a, so the few existing
    # tests that subscribe as OID_B on PROVISIONED_AGENT (to get a distinct
    # idempotency key) remain eligible under F1.
    async def _list_assignments(agent_sp_id):
        if agent_sp_id == "sp-a":
            return [{"principalId": OID_B, "principalType": "User"}]
        return []

    svc = MarketplaceService(
        table_name="",
        mcp_registry=_McpRegistry(mcps),
        agent_registry=_AgentRegistry(agents),
        mcp_graph=types.SimpleNamespace(
            list_assignments=AsyncMock(side_effect=_list_assignments)
        ),
        grant_fn=grant_fn,
        revoke_fn=revoke_fn,
        agent_grant_fn=agent_grant_fn,
        agent_revoke_fn=agent_revoke_fn,
        tenant_service=tenant_service,
    )
    return svc, grant_fn, revoke_fn


def _run(coro):
    return asyncio.run(coro)


# --------------------------------------------------------------------------- #
# Product listing
# --------------------------------------------------------------------------- #

def test_list_agent_products_merges_status():
    svc, _, _ = _build_service()
    cards = svc.list_agent_products(caller_oid=OID_A)
    assert len(cards) == 3
    # No subscription yet → my_status None on every card.
    assert all(c.my_status is None for c in cards)

    _run(svc.create_subscription(
        product_type="agent", product_id="agent-fnol",
        requester_oid=OID_A, requester_email="a@x",
    ))
    cards = svc.list_agent_products(caller_oid=OID_A)
    fnol = next(c for c in cards if c.product_id == "agent-fnol")
    assert fnol.my_status == "pending"
    # Another caller does not see Alice's status.
    cards_b = svc.list_agent_products(caller_oid=OID_B)
    fnol_b = next(c for c in cards_b if c.product_id == "agent-fnol")
    assert fnol_b.my_status is None


def test_list_agent_products_carries_business_unit_and_classification():
    # F4: agent ProductCards carry business_unit + data_classification (they prefill the
    # Deploy mockup form). E33 re-points the SOURCE: business_unit comes off the registry
    # record, data_classification off the DECLARED datasheet (the classification a
    # publisher attested for the PRODUCT, which is what a consumer acts on).
    svc, _, _ = _build_service()
    cards = svc.list_agent_products(caller_oid=OID_A)
    by_id = {c.product_id: c for c in cards}

    assert by_id["agent-fnol"].business_unit == "Claims"
    assert by_id["agent-fnol"].data_classification == "Confidential"
    assert by_id["agent-contact-center"].business_unit == "Customer Service"
    assert by_id["agent-contact-center"].data_classification == "Internal"
    assert by_id["agent-onboarding"].business_unit == "Onboarding"
    assert by_id["agent-onboarding"].data_classification == "Confidential"
    # The description used by the form is the declared pitch.
    assert by_id["agent-fnol"].pitch


def test_list_agent_products_come_from_published_registry_agents():
    """E33: a product exists iff an AGENT carries an approved ``marketplace`` block with
    ``published=True``. The blueprint list is gone, so an agent with no block (or an
    unpublished one) is ABSENT from the catalog entirely."""
    svc, _, _ = _build_service()
    ids = {c.product_id for c in svc.list_agent_products(caller_oid=OID_A)}

    assert ids == {CC_AGENT, FNOL_AGENT, ONB_AGENT}
    # The two plain registry agents carry no marketplace block → not products.
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
        _agent("agent-deprecated", "Retired Agent", lifecycle_state="deprecated",
               marketplace=_publication(published=True)),
        _agent("agent-rejected", "Rejected Agent", lifecycle_state="rejected",
               marketplace=_publication(published=True)),
    ]
    svc, _, _ = _build_service(agents=agents)
    ids = {c.product_id for c in svc.list_agent_products(caller_oid=OID_A)}
    assert ids == {"agent-live"}


def test_list_agent_products_published_card_is_marketplace_wide():
    """Published cards are MARKETPLACE-WIDE (the MCP ``shared=true`` analogue): a
    published agent from ANOTHER tenant IS visible, so there is deliberately NO
    ``visible()`` filter on this read path. The tenant badge still says whose it is."""
    agents = [
        _agent("agent-mine", "Mine", tenant_id="ten-1",
               marketplace=_publication(published=True)),
        _agent("agent-theirs", "Theirs", tenant_id="ten-2",
               marketplace=_publication(published=True)),
    ]
    svc, _, _ = _build_service(agents=agents, tenant_service=_tenant_names_service())
    by_id = {c.product_id: c for c in svc.list_agent_products(caller_oid=OID_A)}

    assert set(by_id) == {"agent-mine", "agent-theirs"}
    # Badged like the MCP cards: id from the record, name from the ONE cached list.
    assert by_id["agent-theirs"].tenant_id == "ten-2"
    assert by_id["agent-theirs"].tenant_name == "Marketing EU"
    # The card reports itself as published (that is why it is in the catalog).
    assert by_id["agent-theirs"].published is True


def test_list_agent_products_carries_datasheet_fields():
    """E33: the agent ProductCard projects the DECLARED datasheet off the agent's
    approved ``marketplace`` block — not a curated blueprint dict, and never a
    measurement (uptime / latency / live status / rating have no field to project into).
    ``declared_at`` is the card's provenance signal."""
    svc, _, _ = _build_service()
    by_id = {c.product_id: c for c in svc.list_agent_products(caller_oid=OID_A)}

    cc = by_id[CC_AGENT]
    assert cc.name == "Contact Center Agent"       # from the registry record, not a literal
    assert cc.owner_team == "Customer Service Platform"
    assert cc.support_contact == "cc-platform@acme.com"
    assert cc.sla_tier == "Gold"
    assert cc.support_hours == "24/7"
    assert cc.version == "2.4.1"
    assert cc.region == "EU (Frankfurt)"
    assert cc.data_classification == "Internal"
    assert "ISO 27001" in cc.compliance
    assert "PII redaction" in cc.guardrails
    assert cc.pitch == (
        "Customer profile lookup, interaction history, ticketing & callbacks."
    )
    # Provenance: the attestation timestamp, ISO-serialized.
    assert cc.declared_at == datetime(2026, 8, 1, 12, 0, 0, tzinfo=timezone.utc).isoformat()
    # Nothing derivable → empty, never a placeholder.
    assert cc.capabilities == []

    # A THIN declaration (3 mandatory fields only) leaves every optional field empty.
    onb = by_id[ONB_AGENT]
    assert onb.owner_team == "Digital Onboarding"
    assert onb.sla_tier is None
    assert onb.support_hours is None
    assert onb.version is None
    assert onb.region is None
    assert onb.compliance == []
    assert onb.guardrails == []
    assert onb.pitch is None
    # ``lifecycle`` is NOT a declarable field (a publisher cannot attest a lifecycle
    # stage), so it stays None on every agent card.
    assert all(c.lifecycle is None for c in by_id.values())

    # T11: MCP cards now carry the SAME declared datasheet (publish is one door), so the
    # projection is asserted for real in ``test_list_mcp_products_carries_declared_datasheet``.
    # What stays agent-only is the LIVE consumer tally.
    mcp = {c.product_id: c for c in svc.list_mcp_products(caller_oid=OID_A)}[GATEWAY_MCP]
    assert mcp.consumers is None


def test_list_mcp_products_carries_declared_datasheet():
    """T11 (C9): the declared datasheet is projected onto the MCP card exactly like the agent
    card — including ``support_contact`` and the ``declared_at`` provenance stamp. The three
    fields the REGISTRY owns as chips (data_classification / region / version) keep their
    registry value rather than being overwritten by the declaration."""
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
    # Registry chips win over the declaration for these three.
    assert gw.data_classification == DataClassification.CONFIDENTIAL.value
    assert gw.region == "EU"
    assert gw.version == "2.1.0"

    # A THIN declaration (the 3 mandatory fields) leaves every optional field empty.
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
    svc.set_listing(product_type="mcp", product_id=GATEWAY_MCP,
                    pitch="Admin-curated pitch", available=False, auto_approve=True)
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
    svc.set_listing(product_type="agent", product_id=CC_AGENT,
                    pitch="Admin-curated pitch", available=False, auto_approve=True)
    by_id = {c.product_id: c for c in svc.list_agent_products(caller_oid=OID_A)}

    assert by_id[CC_AGENT].pitch == "Admin-curated pitch"
    assert by_id[CC_AGENT].available is False
    assert by_id[CC_AGENT].auto_approve is True
    # An un-listed product keeps the declared pitch and the agent defaults.
    assert by_id[FNOL_AGENT].pitch.startswith("Start claims")
    assert by_id[FNOL_AGENT].available is True
    assert by_id[FNOL_AGENT].auto_approve is False


def test_list_agent_products_consumers_count_is_live():
    # E31F: the consumer count is LIVE ONLY — distinct requester teams (oids) with an
    # ACTIVE subscription. There is no seeded adoption floor, so a fresh service reads 0.
    svc, _, _ = _build_service()
    base = next(
        c for c in svc.list_agent_products(caller_oid=OID_A) if c.product_id == "agent-fnol"
    ).consumers
    assert base == 0  # no live subs yet → zero, not a seeded baseline

    # Two distinct teams subscribe to bp-fnol; bp-contact-center gets one.
    _run(svc.create_subscription(
        product_type="agent", product_id="agent-fnol",
        requester_oid=OID_A, requester_email="a@x",
    ))
    _run(svc.create_subscription(
        product_type="agent", product_id="agent-fnol",
        requester_oid=OID_B, requester_email="b@x",
    ))
    _run(svc.create_subscription(
        product_type="agent", product_id="agent-fnol",
        requester_oid=OID_A, requester_email="a@x",
    ))  # same team again → idempotent, no double count
    _run(svc.create_subscription(
        product_type="agent", product_id="agent-contact-center",
        requester_oid=OID_A, requester_email="a@x",
    ))

    by_id = {c.product_id: c for c in svc.list_agent_products(caller_oid=OID_A)}
    # 2 distinct teams subscribed (3 requests, one deduped) → 2.
    assert by_id["agent-fnol"].consumers == 2
    # 1 distinct team → 1 (previously masked by a seeded floor of 9).
    assert by_id["agent-contact-center"].consumers == 1
    # A product nobody subscribed to reads a truthful zero.
    assert by_id["agent-onboarding"].consumers == 0


def test_list_agent_products_consumers_count_counts_distinct_teams():
    # Live counting over an auto-approving product: a Listing turns auto-approve ON for
    # ONB_AGENT (E33: agents no longer carry a per-product auto_approve default), so
    # APPROVED subs count as active just like PENDING ones.
    svc, _, _ = _build_service()
    svc.set_listing(product_type="agent", product_id=ONB_AGENT, auto_approve=True)
    for oid in ("oid-t1", "oid-t2", "oid-t3"):
        _run(svc.create_subscription(
            product_type="agent", product_id="agent-onboarding",
            requester_oid=oid, requester_email=f"{oid}@x",
        ))
    card = next(
        c for c in svc.list_agent_products(caller_oid=OID_A) if c.product_id == "agent-onboarding"
    )
    assert card.consumers == 3  # 3 distinct teams


def test_list_mcp_products_lists_published_products_of_any_kind():
    """Amendment 1 / C9: the ``kind == "gateway"`` auto-listing filter is RETIRED. A card
    exists iff the record carries a published ``marketplace`` block AND is lifecycle-approved
    — so a published STANDARD server lists, an unpublished GATEWAY does not, and ``kind``
    survives only as a display field."""
    svc, _, _ = _build_service()
    cards = svc.list_mcp_products(caller_oid=OID_A)
    by_id = {c.product_id: c for c in cards}

    assert set(by_id) == {GATEWAY_MCP, STANDARD_MCP}
    assert UNPUBLISHED_MCP not in by_id        # publish is the only door
    assert "m-pending" not in by_id            # published but not lifecycle-approved
    assert by_id[GATEWAY_MCP].kind == "gateway"
    assert by_id[STANDARD_MCP].kind == "standard"     # display only, no longer a gate
    assert by_id[GATEWAY_MCP].business_unit == "Claims"


def test_published_mcp_card_omits_owner_email_and_uses_the_declared_support_contact():
    """Fix round 1: a published card is MARKETPLACE-WIDE, so the registry's tenant-scoped
    ``owner_email`` (contact PII for a named individual) is NOT projected onto it. The declared
    ``support_contact`` — a mandatory datasheet field the publisher chose to expose — is its
    replacement, and it is what a cross-tenant consumer acts on."""
    svc, _, _ = _build_service()
    by_id = {c.product_id: c for c in svc.list_mcp_products(caller_oid=OID_A)}

    # The registry record HAS an owner email...
    assert svc._mcp_registry.get(GATEWAY_MCP).owner_email == "owner@acme.com"
    # ...and the card does not carry it, for either declaration shape.
    assert by_id[GATEWAY_MCP].owner_email is None
    assert by_id[STANDARD_MCP].owner_email is None
    # The declared support contact is what the card exposes instead.
    assert by_id[GATEWAY_MCP].support_contact == "mcp-platform@acme.com"
    assert by_id[STANDARD_MCP].support_contact == "claims-platform@acme.com"


def test_marketplace_cards_report_published_for_both_product_types():
    """Fix round 1: ``ProductCard.published`` means MARKETPLACE publication on BOTH card
    types — True by construction, since publication is what makes a record a card. The E24
    cross-tenant ``published`` flag is deliberately NOT surfaced here: an MCP whose E24 flag is
    False still reports ``published=True`` once its declaration is approved, so the field
    cannot mean two different things depending on the product type."""
    mcps = [_mcp("m-unshared", "Unshared MCP", tenant_id="ten-1",
                 published=False, shared=False, marketplace=_publication())]
    svc, _, _ = _build_service(mcps=mcps)

    mcp_cards = svc.list_mcp_products(caller_oid=OID_A)
    agent_cards = svc.list_agent_products(caller_oid=OID_A)
    assert mcp_cards and agent_cards
    for card in [*mcp_cards, *agent_cards]:
        assert card.published is True
    # The E24 record flag is False — the card's ``published`` is not reading it.
    assert svc._mcp_registry.get("m-unshared").published is False
    # ``shared`` still mirrors the E24 share flag off the record.
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
    # F3: the MCP ProductCard now carries data_classification / region / version /
    # created_at / updated_at, sourced from the registry record. The
    # DataClassification enum is serialized to its string value; datetimes to ISO.
    # T11: these stay REGISTRY-sourced even though a declared datasheet also carries
    # classification/region/version — those three describe the server record.
    svc, _, _ = _build_service()
    card = {c.product_id: c for c in svc.list_mcp_products(caller_oid=OID_A)}[GATEWAY_MCP]
    assert card.data_classification == DataClassification.CONFIDENTIAL.value
    assert isinstance(card.data_classification, str)
    assert card.region == "EU"
    assert card.version == "2.1.0"
    assert card.created_at == datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc).isoformat()
    assert card.updated_at == datetime(2026, 6, 7, 8, 9, 10, tzinfo=timezone.utc).isoformat()


# --------------------------------------------------------------------------- #
# E24/T8 — tenant badge fields on the catalog cards
# --------------------------------------------------------------------------- #

def test_list_mcp_products_carries_tenant_badge_fields():
    """MCP cards carry tenant_id/published/shared from the registry record and
    tenant_name resolved via ONE tenant_service.list() (never per-card)."""
    mcps = [
        _mcp("m-own", "Own MCP", tenant_id="ten-1", published=False, shared=False,
             marketplace=_publication()),
        _mcp("m-pub", "Published MCP", tenant_id="ten-2", published=True, shared=False,
             marketplace=_publication()),
        _mcp("m-shared", "Shared MCP", tenant_id="ten-2", published=False, shared=True,
             marketplace=_publication()),
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
    # ``published`` is the MARKETPLACE flag on a card (fix round 1), so it is True for all
    # three regardless of their differing E24 record flags.
    assert all(c.published is True for c in by_id.values())


def test_list_mcp_products_tenant_name_degrades_without_tenant_service():
    """No tenant_service injected (or an unknown tenant id) → tenant_name None,
    the badge id/flags still stamped — display-only resolution never breaks the
    catalog."""
    mcps = [_mcp("m-own", "Own MCP", tenant_id="ten-x", published=True,
                 marketplace=_publication())]
    svc, _, _ = _build_service(mcps=mcps)  # tenant_service=None
    card = svc.list_mcp_products(caller_oid=OID_A)[0]
    assert card.tenant_id == "ten-x"
    assert card.tenant_name is None
    assert card.published is True


def test_list_mcp_products_tenant_service_error_degrades_to_no_names():
    """A tenant_service.list() failure degrades to tenant_name=None (never raises)."""
    def _boom():
        raise RuntimeError("ddb down")

    mcps = [_mcp("m-own", "Own MCP", tenant_id="ten-1", marketplace=_publication())]
    svc, _, _ = _build_service(
        mcps=mcps, tenant_service=types.SimpleNamespace(list=_boom)
    )
    card = svc.list_mcp_products(caller_oid=OID_A)[0]
    assert card.tenant_id == "ten-1"
    assert card.tenant_name is None


def test_list_agent_products_untagged_agents_carry_no_tenant_badge():
    """An agent record with no ``tenant_id`` (registered directly / pre-E24) yields a
    card with no tenant badge — but it is still a marketplace-wide published card, so
    ``published`` is True. ``shared`` has no agent-side source and stays False."""
    svc, _, _ = _build_service()
    cards = svc.list_agent_products(caller_oid=OID_A)
    assert len(cards) == 3  # tenant scoping never drops a published card
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
        _mcp("m-own", "Own MCP", tenant_id="ten-1", published=False, shared=False,
             marketplace=_publication()),
        _mcp("m-pub", "Foreign Published", tenant_id="ten-2", published=True, shared=False,
             marketplace=_publication()),
        _mcp("m-shared", "Foreign Shared", tenant_id="ten-2", published=False, shared=True,
             marketplace=_publication()),
        _mcp("m-unpublished", "Foreign Unpublished", tenant_id="ten-2",
             published=False, shared=False, marketplace=None),
    ]


def _tenant_names_service():
    return types.SimpleNamespace(
        list=lambda: [
            types.SimpleNamespace(id="ten-1", name="Claims DE"),
            types.SimpleNamespace(id="ten-2", name="Marketing EU"),
        ]
    )


def test_list_mcp_products_published_card_is_marketplace_wide():
    """T11 (C9): a marketplace-published MCP card is MARKETPLACE-WIDE, exactly like a
    published agent card — publishing IS the act of making a product cross-tenant
    discoverable, so re-filtering it by ``visible()`` would make the admin's approval a no-op
    for every other tenant. A foreign tenant's published MCP therefore APPEARS (badged with
    whose it is), and the only absent record is the one that never declared."""
    svc, _, _ = _build_service(
        mcps=_tenant_scoped_mcps(), tenant_service=_tenant_names_service()
    )
    cards = svc.list_mcp_products(caller_oid=OID_A, ctx=_ctx(tenant_ids=["ten-1"]))
    by_id = {c.product_id: c for c in cards}

    assert set(by_id) == {"m-own", "m-pub", "m-shared"}   # m-unpublished ABSENT
    assert by_id["m-own"].tenant_name == "Claims DE"
    # The foreign published card is badged so the FE can show whose it is.
    assert by_id["m-pub"].tenant_id == "ten-2"
    assert by_id["m-pub"].tenant_name == "Marketing EU"
    # ``shared`` remains the E24 cross-tenant share flag off the record (a DIFFERENT feature
    # from marketplace publication — Global Constraints), while ``published`` is the
    # MARKETPLACE flag and is True on every card (fix round 1).
    assert by_id["m-shared"].shared is True
    assert all(c.published is True for c in by_id.values())


def test_list_mcp_products_admin_sees_everything():
    svc, _, _ = _build_service(
        mcps=_tenant_scoped_mcps(), tenant_service=_tenant_names_service()
    )
    cards = svc.list_mcp_products(caller_oid=OID_A, ctx=_ctx(is_global=True))
    assert {c.product_id for c in cards} == {"m-own", "m-pub", "m-shared"}


def test_list_mcp_products_no_ctx_matches_the_scoped_read():
    """ctx=None (legacy callers / no tenant scoping wired) reads the SAME set as a scoped
    caller now that publication is the only filter — the ctx argument survives for the route's
    signature, not to narrow the catalog."""
    svc, _, _ = _build_service(mcps=_tenant_scoped_mcps())
    unscoped = {c.product_id for c in svc.list_mcp_products(caller_oid=OID_A)}
    scoped = {
        c.product_id
        for c in svc.list_mcp_products(caller_oid=OID_A, ctx=_ctx(tenant_ids=["ten-1"]))
    }
    assert unscoped == scoped == {"m-own", "m-pub", "m-shared"}


# --------------------------------------------------------------------------- #
# create_subscription — agent
# --------------------------------------------------------------------------- #

def test_create_agent_subscription_pending_when_not_auto():
    svc, grant_fn, _ = _build_service()
    sub = _run(svc.create_subscription(
        product_type="agent", product_id="agent-contact-center",
        requester_oid=OID_A, requester_email="a@x",
    ))
    assert sub.status == "pending"
    assert sub.auto_approved is False
    grant_fn.assert_not_called()


def test_create_agent_subscription_auto_approves():
    # E33: the agent default is auto_approve=False (a declaration carries no
    # auto-approve flag), so a Listing override is what turns it on.
    # T3: auto-approve APPLIES the user→agent grant inline (the MCP auto-approve
    # behaviour), so an auto-approved row confers real access.
    svc, grant_fn, _ = _build_service()
    svc.set_listing(product_type="agent", product_id=ONB_AGENT, auto_approve=True)
    sub = _run(svc.create_subscription(
        product_type="agent", product_id="agent-onboarding",
        requester_oid=OID_A, requester_email="a@x",
    ))
    assert sub.status == "approved"
    assert sub.auto_approved is True
    assert sub.decided_by == "auto"
    assert sub.grant_assignment_id == "uasg-1"
    # Granted to the SUBSCRIBER on the product's agent record.
    svc._agent_grant_fn.assert_awaited_once()
    call_args = svc._agent_grant_fn.await_args.args
    assert call_args[0] is svc._agent_registry.get(ONB_AGENT)
    assert call_args[1] == OID_A
    grant_fn.assert_not_called()


def test_agent_auto_approve_grant_failure_persists_failed_and_is_retryable():
    """T3 (amendment): an auto-approve whose grant FAILS must not report success. The row
    is persisted FAILED with a safe error and stays retryable — the same contract as the
    explicit-approve path, so the two entry points cannot disagree about access."""
    svc, _, _ = _build_service(agent_grant_side_effect=RuntimeError("graph 502"))
    svc.set_listing(product_type="agent", product_id=ONB_AGENT, auto_approve=True)

    with pytest.raises(MarketplaceError) as ei:
        _run(svc.create_subscription(
            product_type="agent", product_id="agent-onboarding",
            requester_oid=OID_A, requester_email="a@x",
        ))
    assert ei.value.kind == "grant_failed"

    # The row survives as FAILED (never "approved") with a safe, retryable error.
    failed = next(s for s in svc.list_subscriptions() if s.product_id == ONB_AGENT)
    assert failed.status == "failed"
    assert failed.error
    assert failed.auto_approved is True
    assert failed.grant_assignment_id is None

    # And Retry drives it to approved once Graph recovers.
    svc._agent_grant_fn.side_effect = None
    svc._agent_grant_fn.return_value = "uasg-2"
    retried = _run(svc.retry_grant(failed.id, decided_by=ADMIN))
    assert retried.status == "approved"
    assert retried.grant_assignment_id == "uasg-2"


# --------------------------------------------------------------------------- #
# create_subscription — MCP
# --------------------------------------------------------------------------- #

def test_create_mcp_subscription_requires_provisioned_agent():
    svc, _, _ = _build_service()
    with pytest.raises(MarketplaceError) as ei:
        _run(svc.create_subscription(
            product_type="mcp", product_id=GATEWAY_MCP, agent_id=UNPROVISIONED_AGENT,
            requester_oid=OID_A, requester_email="a@x",
        ))
    assert ei.value.kind == "conflict"


def test_create_mcp_subscription_captures_agent_sp_id():
    svc, _, _ = _build_service()
    sub = _run(svc.create_subscription(
        product_type="mcp", product_id=GATEWAY_MCP, agent_id=PROVISIONED_AGENT,
        requester_oid=OID_A, requester_email="a@x",
    ))
    assert sub.agent_sp_id == "sp-a"
    assert sub.agent_name == "Provisioned Agent"
    assert sub.agent_id == PROVISIONED_AGENT


def test_create_mcp_auto_approve_applies_grant_inline():
    svc, grant_fn, _ = _build_service()
    svc.set_listing(product_type="mcp", product_id=GATEWAY_MCP, auto_approve=True)
    sub = _run(svc.create_subscription(
        product_type="mcp", product_id=GATEWAY_MCP, agent_id=PROVISIONED_AGENT,
        requester_oid=OID_A, requester_email="a@x",
    ))
    assert grant_fn.call_count == 1
    assert sub.status == "approved"
    assert sub.grant_assignment_id == "asg-1"


def test_create_mcp_not_auto_is_pending_no_grant():
    svc, grant_fn, _ = _build_service()
    sub = _run(svc.create_subscription(
        product_type="mcp", product_id=GATEWAY_MCP, agent_id=PROVISIONED_AGENT,
        requester_oid=OID_A, requester_email="a@x",
    ))
    assert sub.status == "pending"
    grant_fn.assert_not_called()


# --------------------------------------------------------------------------- #
# Idempotency
# --------------------------------------------------------------------------- #

def test_idempotent_returns_active_sub():
    svc, _, _ = _build_service()
    first = _run(svc.create_subscription(
        product_type="agent", product_id="agent-fnol",
        requester_oid=OID_A, requester_email="a@x",
    ))
    second = _run(svc.create_subscription(
        product_type="agent", product_id="agent-fnol",
        requester_oid=OID_A, requester_email="a@x",
    ))
    assert first.id == second.id

    # For MCP the key includes agent_id: same agent → dedup, different agent → new sub.
    m1 = _run(svc.create_subscription(
        product_type="mcp", product_id=GATEWAY_MCP, agent_id=PROVISIONED_AGENT,
        requester_oid=OID_A, requester_email="a@x",
    ))
    m1_again = _run(svc.create_subscription(
        product_type="mcp", product_id=GATEWAY_MCP, agent_id=PROVISIONED_AGENT,
        requester_oid=OID_A, requester_email="a@x",
    ))
    assert m1.id == m1_again.id


def test_rejected_can_be_resubscribed():
    svc, _, _ = _build_service()
    first = _run(svc.create_subscription(
        product_type="agent", product_id="agent-fnol",
        requester_oid=OID_A, requester_email="a@x",
    ))
    svc.reject(first.id, decided_by=ADMIN, reason="nope")
    second = _run(svc.create_subscription(
        product_type="agent", product_id="agent-fnol",
        requester_oid=OID_A, requester_email="a@x",
    ))
    assert second.id != first.id
    # History preserved: both rows survive.
    all_subs = svc.list_subscriptions()
    assert {s.id for s in all_subs} >= {first.id, second.id}


# --------------------------------------------------------------------------- #
# approve / reject / retry
# --------------------------------------------------------------------------- #

def test_approve_mcp_applies_grant():
    svc, grant_fn, _ = _build_service()
    sub = _run(svc.create_subscription(
        product_type="mcp", product_id=GATEWAY_MCP, agent_id=PROVISIONED_AGENT,
        requester_oid=OID_A, requester_email="a@x",
    ))
    assert sub.status == "pending"
    approved = _run(svc.approve(sub.id, decided_by=ADMIN))
    assert grant_fn.call_count == 1
    assert approved.status == "approved"
    assert approved.grant_assignment_id == "asg-1"
    assert approved.decided_by == ADMIN


def test_approve_mcp_grant_failure_sets_failed():
    svc, grant_fn, _ = _build_service(grant_side_effect=RuntimeError("graph 502"))
    sub = _run(svc.create_subscription(
        product_type="mcp", product_id=GATEWAY_MCP, agent_id=PROVISIONED_AGENT,
        requester_oid=OID_A, requester_email="a@x",
    ))
    with pytest.raises(MarketplaceError) as ei:
        _run(svc.approve(sub.id, decided_by=ADMIN))
    assert ei.value.kind == "grant_failed"
    # The row is persisted as failed (with a safe error) and still retrievable.
    failed = next(s for s in svc.list_subscriptions() if s.id == sub.id)
    assert failed.status == "failed"
    assert failed.error


def test_retry_grant_only_on_failed():
    svc, grant_fn, _ = _build_service(grant_side_effect=RuntimeError("graph 502"))
    sub = _run(svc.create_subscription(
        product_type="mcp", product_id=GATEWAY_MCP, agent_id=PROVISIONED_AGENT,
        requester_oid=OID_A, requester_email="a@x",
    ))
    with pytest.raises(MarketplaceError):
        _run(svc.approve(sub.id, decided_by=ADMIN))
    # Now grant succeeds; retry → approved.
    grant_fn.side_effect = None
    grant_fn.return_value = "asg-1"
    retried = _run(svc.retry_grant(sub.id, decided_by=ADMIN))
    assert retried.status == "approved"
    assert retried.grant_assignment_id == "asg-1"

    # Retry on a pending sub → conflict. (OID_B is a distinct idempotency key so
    # this is a fresh pending sub; OID_B is directly granted on sp-a in the fake,
    # so the F1 eligibility guard allows it.)
    pending = _run(svc.create_subscription(
        product_type="mcp", product_id=GATEWAY_MCP, agent_id=PROVISIONED_AGENT,
        requester_oid=OID_B, requester_email="b@x",
    ))
    with pytest.raises(MarketplaceError) as ei:
        _run(svc.retry_grant(pending.id, decided_by=ADMIN))
    assert ei.value.kind == "conflict"


def test_approve_agent_applies_user_grant_not_the_mcp_grant():
    """T3: approving an AGENT sub applies the user→agent grant and stores its assignment
    id. The MCP ``grant_fn`` stays untouched — the two grant paths never cross."""
    svc, grant_fn, _ = _build_service()
    sub = _run(svc.create_subscription(
        product_type="agent", product_id="agent-contact-center",
        requester_oid=OID_A, requester_email="a@x",
    ))
    approved = _run(svc.approve(sub.id, decided_by=ADMIN))
    assert approved.status == "approved"
    assert approved.grant_assignment_id == "uasg-1"
    svc._agent_grant_fn.assert_awaited_once()
    grant_fn.assert_not_called()


def test_reject_sets_rejected_with_reason():
    svc, _, _ = _build_service()
    sub = _run(svc.create_subscription(
        product_type="agent", product_id="agent-contact-center",
        requester_oid=OID_A, requester_email="a@x",
    ))
    rejected = svc.reject(sub.id, decided_by=ADMIN, reason="nope")
    assert rejected.status == "rejected"
    assert rejected.decision_reason == "nope"


def test_approve_non_pending_conflicts():
    svc, _, _ = _build_service()
    sub = _run(svc.create_subscription(
        product_type="agent", product_id="agent-contact-center",
        requester_oid=OID_A, requester_email="a@x",
    ))
    _run(svc.approve(sub.id, decided_by=ADMIN))
    with pytest.raises(MarketplaceError) as ei:
        _run(svc.approve(sub.id, decided_by=ADMIN))
    assert ei.value.kind == "conflict"


# --------------------------------------------------------------------------- #
# list_subscriptions
# --------------------------------------------------------------------------- #

def test_list_subscriptions_caller_scoped_and_pending_first():
    svc, _, _ = _build_service()
    a1 = _run(svc.create_subscription(
        product_type="agent", product_id="agent-contact-center",
        requester_oid=OID_A, requester_email="a@x",
    ))
    b1 = _run(svc.create_subscription(
        product_type="agent", product_id="agent-fnol",
        requester_oid=OID_B, requester_email="b@x",
    ))
    # Caller-scoped.
    a_subs = svc.list_subscriptions(caller_oid=OID_A)
    assert {s.id for s in a_subs} == {a1.id}
    # Admin (no oid) sees all.
    all_subs = svc.list_subscriptions()
    assert {s.id for s in all_subs} >= {a1.id, b1.id}
    # Approve a1 → b1 (still pending) sorts before a1 (approved).
    _run(svc.approve(a1.id, decided_by=ADMIN))
    all_subs = svc.list_subscriptions()
    statuses = [s.status for s in all_subs]
    # The first pending appears before the first non-pending.
    first_pending = next(i for i, s in enumerate(statuses) if s == "pending")
    first_other = next((i for i, s in enumerate(statuses) if s != "pending"), len(statuses))
    assert first_pending < first_other


# --------------------------------------------------------------------------- #
# Listings + effective auto-approve
# --------------------------------------------------------------------------- #

def test_set_listing_overrides_auto_approve():
    svc, grant_fn, _ = _build_service()
    # MCP default → not auto.
    assert svc._effective_auto_approve("mcp", GATEWAY_MCP) is False
    svc.set_listing(product_type="mcp", product_id=GATEWAY_MCP, auto_approve=True)
    assert svc._effective_auto_approve("mcp", GATEWAY_MCP) is True
    # And it now changes create_subscription behavior.
    sub = _run(svc.create_subscription(
        product_type="mcp", product_id=GATEWAY_MCP, agent_id=PROVISIONED_AGENT,
        requester_oid=OID_A, requester_email="a@x",
    ))
    assert sub.status == "approved"
    assert grant_fn.call_count == 1


# --------------------------------------------------------------------------- #
# metrics
# --------------------------------------------------------------------------- #

# --------------------------------------------------------------------------- #
# F1 — eligible_agents (sponsor OR direct-user-grant OR group-grant)
# --------------------------------------------------------------------------- #

# In _build_service both agents have sponsor_oid=OID_A. For the eligibility tests
# we want a clean separation between sponsor and non-sponsor agents, so build a
# bespoke service here with explicit sponsor_oids + a fake graph whose
# list_assignments is an AsyncMock returning assignment dicts.


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
        _agent(PROVISIONED_AGENT, "Provisioned Agent",
               identity_status="provisioned", entra_sp_id="sp-a"),  # sponsor_oid=OID_A
        types.SimpleNamespace(
            id="agent-granted", name="Granted Agent",
            identity_status="provisioned", entra_sp_id="sp-granted",
            sponsor_oid=OID_B, entra_app_id="app-g",
            agent_arn="arn:aws:bedrock-agentcore:us-east-1:1:runtime/agent-g",
            oauth2_credential_provider_name=None,
        ),
        types.SimpleNamespace(
            id="agent-other", name="Other Agent",
            identity_status="provisioned", entra_sp_id="sp-other",
            sponsor_oid=OID_B, entra_app_id="app-o",
            agent_arn="arn:aws:bedrock-agentcore:us-east-1:1:runtime/agent-o",
            oauth2_credential_provider_name=None,
        ),
        _agent(UNPROVISIONED_AGENT, "Draft Agent",
               identity_status="none", entra_sp_id=None),  # sponsor_oid=OID_A, not provisioned
    ]
    # T11: the MCP must be marketplace-PUBLISHED to be subscribable at all (publish is the
    # only door), so the eligibility fixture carries a declaration too.
    mcps = [_mcp(GATEWAY_MCP, "Gateway MCP", kind="gateway", lifecycle_state="approved",
                 marketplace=_publication())]

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
    # OID_A sponsors PROVISIONED_AGENT → eligible WITHOUT any Graph call (short-circuit).
    svc, list_assignments, _ = _eligibility_service()
    agents = _run(svc.eligible_agents(caller_oid=OID_A, caller_group_ids=[]))
    ids = {a.id for a in agents}
    assert PROVISIONED_AGENT in ids
    # Sponsor match short-circuits → list_assignments NOT called for sp-a.
    called_sps = {c.args[0] for c in list_assignments.await_args_list}
    assert "sp-a" not in called_sps


def test_eligible_agents_includes_direct_user_grant():
    # OID_A is granted (principalType User) on agent-granted (sponsored by OID_B).
    svc, _, _ = _eligibility_service(
        assignments_by_sp={
            "sp-granted": [{"principalId": OID_A, "principalType": "User"}],
        }
    )
    agents = _run(svc.eligible_agents(caller_oid=OID_A, caller_group_ids=[]))
    ids = {a.id for a in agents}
    assert "agent-granted" in ids
    assert "agent-other" not in ids  # no assignment for OID_A there


def test_eligible_agents_includes_group_grant():
    # A Group assignment whose principalId is in the caller's group ids → eligible.
    svc, _, _ = _eligibility_service(
        assignments_by_sp={
            "sp-other": [{"principalId": "grp-claims", "principalType": "Group"}],
        }
    )
    agents = _run(svc.eligible_agents(caller_oid=OID_B, caller_group_ids=["grp-claims"]))
    ids = {a.id for a in agents}
    assert "agent-other" in ids


def test_eligible_agents_excludes_unrelated():
    # OID_A is neither sponsor nor granted on agent-other → excluded.
    svc, _, _ = _eligibility_service(
        assignments_by_sp={
            "sp-other": [{"principalId": "someone-else", "principalType": "User"}],
        }
    )
    agents = _run(svc.eligible_agents(caller_oid=OID_A, caller_group_ids=["grp-x"]))
    ids = {a.id for a in agents}
    assert "agent-other" not in ids


def test_eligible_agents_excludes_unprovisioned():
    # UNPROVISIONED_AGENT is sponsored by OID_A but not provisioned → excluded.
    svc, _, _ = _eligibility_service()
    agents = _run(svc.eligible_agents(caller_oid=OID_A, caller_group_ids=[]))
    ids = {a.id for a in agents}
    assert UNPROVISIONED_AGENT not in ids


# --------------------------------------------------------------------------- #
# F1 — create_subscription eligibility guard (MCP branch)
# --------------------------------------------------------------------------- #

def test_create_mcp_subscription_denies_ineligible_agent():
    # OID_A is NOT sponsor of agent-other and has no grant → denied (conflict);
    # the grant_fn must NOT be called.
    svc, _, grant_fn = _eligibility_service(
        assignments_by_sp={
            "sp-other": [{"principalId": "someone-else", "principalType": "User"}],
        }
    )
    with pytest.raises(MarketplaceError) as ei:
        _run(svc.create_subscription(
            product_type="mcp", product_id=GATEWAY_MCP, agent_id="agent-other",
            requester_oid=OID_A, requester_email="a@x", caller_group_ids=[],
        ))
    assert ei.value.kind == "conflict"
    grant_fn.assert_not_called()


def test_create_mcp_subscription_allows_granted_agent():
    # OID_B is group-granted on agent-other → eligible → pending sub (no grant yet,
    # not auto-approve), so grant_fn is NOT called but the sub is created.
    svc, _, grant_fn = _eligibility_service(
        assignments_by_sp={
            "sp-other": [{"principalId": "grp-claims", "principalType": "Group"}],
        }
    )
    sub = _run(svc.create_subscription(
        product_type="mcp", product_id=GATEWAY_MCP, agent_id="agent-other",
        requester_oid=OID_B, requester_email="b@x", caller_group_ids=["grp-claims"],
    ))
    assert sub.status == "pending"
    assert sub.agent_id == "agent-other"
    grant_fn.assert_not_called()


def test_create_mcp_subscription_allows_granted_agent_auto_approve_grants():
    # Same group-grant, but with auto-approve on → the grant path runs.
    svc, _, grant_fn = _eligibility_service(
        assignments_by_sp={
            "sp-other": [{"principalId": "grp-claims", "principalType": "Group"}],
        }
    )
    svc.set_listing(product_type="mcp", product_id=GATEWAY_MCP, auto_approve=True)
    sub = _run(svc.create_subscription(
        product_type="mcp", product_id=GATEWAY_MCP, agent_id="agent-other",
        requester_oid=OID_B, requester_email="b@x", caller_group_ids=["grp-claims"],
    ))
    assert sub.status == "approved"
    assert grant_fn.call_count == 1


def test_create_mcp_sponsor_allowed_when_graph_down():
    # Sponsor match must short-circuit BEFORE any Graph call, so a sponsor still
    # succeeds even if list_assignments raises (Graph down).
    svc, list_assignments, _ = _eligibility_service(
        list_assignments_side_effect=RuntimeError("graph 502"),
    )
    sub = _run(svc.create_subscription(
        product_type="mcp", product_id=GATEWAY_MCP, agent_id=PROVISIONED_AGENT,
        requester_oid=OID_A, requester_email="a@x", caller_group_ids=[],
    ))
    assert sub.status == "pending"
    assert sub.agent_id == PROVISIONED_AGENT
    list_assignments.assert_not_awaited()


def test_create_mcp_nonsponsor_graph_error_fails_closed():
    # Non-sponsor + list_assignments raises → propagates (fail closed); grant_fn NOT called.
    svc, _, grant_fn = _eligibility_service(
        list_assignments_side_effect=RuntimeError("graph 502"),
    )
    with pytest.raises(Exception):
        _run(svc.create_subscription(
            product_type="mcp", product_id=GATEWAY_MCP, agent_id="agent-other",
            requester_oid=OID_A, requester_email="a@x", caller_group_ids=[],
        ))
    grant_fn.assert_not_called()


def test_metrics_counts_and_rate():
    svc, _, _ = _build_service()
    svc.set_listing(product_type="agent", product_id=ONB_AGENT, auto_approve=True)
    # 1 approved agent (auto), 1 rejected agent, 1 pending mcp.
    auto = _run(svc.create_subscription(
        product_type="agent", product_id="agent-onboarding",
        requester_oid=OID_A, requester_email="a@x",
    ))
    assert auto.status == "approved"
    rej = _run(svc.create_subscription(
        product_type="agent", product_id="agent-contact-center",
        requester_oid=OID_A, requester_email="a@x",
    ))
    svc.reject(rej.id, decided_by=ADMIN, reason="x")
    _run(svc.create_subscription(
        product_type="mcp", product_id=GATEWAY_MCP, agent_id=PROVISIONED_AGENT,
        requester_oid=OID_B, requester_email="b@x",
    ))

    m = svc.metrics()
    assert m.total == 3
    assert m.pending == 1
    assert m.approved == 1
    assert m.rejected == 1
    assert m.failed == 0
    assert m.approval_rate == pytest.approx(1 / 2)  # approved / (approved + rejected)
    assert m.by_type["agent"] == 2
    assert m.by_type["mcp"] == 1
    assert m.top_products
    assert all({"product_id", "product_name", "count"} <= set(tp) for tp in m.top_products)


# --------------------------------------------------------------------------- #
# revoke_subscription (E9R T2)
# --------------------------------------------------------------------------- #

def _approved_mcp_sub(svc):
    """Create + approve an MCP sub for PROVISIONED_AGENT on GATEWAY_MCP → APPROVED with
    grant_assignment_id 'asg-1' (the grant_fn return). Returns the approved Subscription."""
    sub = _run(svc.create_subscription(
        product_type="mcp", product_id=GATEWAY_MCP, agent_id=PROVISIONED_AGENT,
        requester_oid=OID_A, requester_email="a@x",
    ))
    approved = _run(svc.approve(sub.id, decided_by=ADMIN))
    assert approved.status == "approved"
    assert approved.grant_assignment_id == "asg-1"
    return approved


def test_revoke_approved_mcp_calls_revoke_fn():
    svc, _, revoke_fn = _build_service()
    approved = _approved_mcp_sub(svc)
    decided_by_before = approved.decided_by
    decided_at_before = approved.decided_at

    revoked = _run(svc.revoke_subscription(
        approved.id, decided_by="oid-admin-2", reason="offboarding"
    ))
    # revoke_fn awaited once with (the MCP record from the fake registry, the assignment id).
    revoke_fn.assert_awaited_once()
    call_args = revoke_fn.await_args.args
    assert call_args[0] is svc._mcp_registry.get(GATEWAY_MCP)
    assert call_args[1] == "asg-1"

    assert revoked.status == "revoked"
    assert revoked.revoked_by == "oid-admin-2"
    assert revoked.revoked_at is not None
    assert revoked.revoke_reason == "offboarding"
    # Approval audit is NOT overwritten by the revoke.
    assert revoked.decided_by == decided_by_before == ADMIN
    assert revoked.decided_at == decided_at_before
    # Persisted.
    stored = next(s for s in svc.list_subscriptions() if s.id == approved.id)
    assert stored.status == "revoked"
    assert stored.revoked_by == "oid-admin-2"


def test_revoke_agent_sub_calls_agent_revoke_fn_not_the_mcp_one():
    """T3: revoking an AGENT sub tears down the user→agent assignment via
    ``agent_revoke_fn``; the MCP ``revoke_fn`` is never touched."""
    svc, _, revoke_fn = _build_service()
    sub = _run(svc.create_subscription(
        product_type="agent", product_id="agent-contact-center",
        requester_oid=OID_A, requester_email="a@x",
    ))
    approved = _run(svc.approve(sub.id, decided_by=ADMIN))
    assert approved.status == "approved"

    revoked = _run(svc.revoke_subscription(approved.id, decided_by=ADMIN))
    assert revoked.status == "revoked"
    assert revoked.revoked_by == ADMIN
    # Awaited with (the agent record behind the product, the stored assignment id).
    svc._agent_revoke_fn.assert_awaited_once()
    call_args = svc._agent_revoke_fn.await_args.args
    assert call_args[0] is svc._agent_registry.get(CC_AGENT)
    assert call_args[1] == "uasg-1"
    revoke_fn.assert_not_called()


def test_revoke_non_approved_conflicts():
    svc, _, revoke_fn = _build_service()
    # Pending sub → conflict.
    pending = _run(svc.create_subscription(
        product_type="agent", product_id="agent-contact-center",
        requester_oid=OID_A, requester_email="a@x",
    ))
    with pytest.raises(MarketplaceError) as ei:
        _run(svc.revoke_subscription(pending.id, decided_by=ADMIN))
    assert ei.value.kind == "conflict"

    # Rejected sub → conflict.
    rejected = _run(svc.create_subscription(
        product_type="agent", product_id="agent-fnol",
        requester_oid=OID_A, requester_email="a@x",
    ))
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
    # An approved MCP sub whose grant_assignment_id is None → nothing actionable in
    # Graph → mark REVOKED without calling revoke_fn.
    svc, _, revoke_fn = _build_service()
    approved = _approved_mcp_sub(svc)
    # Force the stored sub to have no assignment id (simulating an older / partial record).
    stored = svc._get_sub(approved.id)
    stored.grant_assignment_id = None
    svc._save(stored)

    revoked = _run(svc.revoke_subscription(approved.id, decided_by=ADMIN))
    assert revoked.status == "revoked"
    revoke_fn.assert_not_called()


def test_revoke_missing_mcp_record_marks_revoked_no_call():
    # mcp_registry.get returns None for the product → nothing actionable → mark REVOKED.
    svc, _, revoke_fn = _build_service()
    approved = _approved_mcp_sub(svc)
    # Drop the MCP from the registry so get() returns None.
    svc._mcp_registry._mcps.pop(GATEWAY_MCP)

    revoked = _run(svc.revoke_subscription(approved.id, decided_by=ADMIN))
    assert revoked.status == "revoked"
    revoke_fn.assert_not_called()


def test_revoke_stale_grant_marks_revoked():
    # revoke_fn raises GrantNotFoundError (already gone in Graph) → still mark REVOKED.
    svc, _, revoke_fn = _build_service(
        revoke_side_effect=GrantNotFoundError("gone")
    )
    approved = _approved_mcp_sub(svc)

    revoked = _run(svc.revoke_subscription(approved.id, decided_by=ADMIN))
    assert revoked.status == "revoked"
    revoke_fn.assert_awaited_once()


def test_revoke_graph_failure_keeps_approved_raises_grant_failed():
    # A real Graph failure (GrantRevokeFailedError) → sub STAYS APPROVED (nothing
    # persisted) and the service raises MarketplaceError(kind="grant_failed").
    svc, _, revoke_fn = _build_service(
        revoke_side_effect=GrantRevokeFailedError("boom")
    )
    approved = _approved_mcp_sub(svc)

    with pytest.raises(MarketplaceError) as ei:
        _run(svc.revoke_subscription(approved.id, decided_by=ADMIN))
    assert ei.value.kind == "grant_failed"

    # The stored sub is unchanged: still APPROVED, no revoked_* fields written.
    stored = next(s for s in svc.list_subscriptions() if s.id == approved.id)
    assert stored.status == "approved"
    assert stored.revoked_by is None
    assert stored.revoked_at is None
    assert stored.revoke_reason is None


def test_resubscribe_after_revoke_creates_new_sub():
    # After a revoke the sub is NOT active, so a fresh subscribe for the same
    # (oid, agent, product) creates a NEW sub id (history preserved).
    svc, _, _ = _build_service()
    approved = _approved_mcp_sub(svc)
    _run(svc.revoke_subscription(approved.id, decided_by=ADMIN))

    fresh = _run(svc.create_subscription(
        product_type="mcp", product_id=GATEWAY_MCP, agent_id=PROVISIONED_AGENT,
        requester_oid=OID_A, requester_email="a@x",
    ))
    assert fresh.id != approved.id
    assert fresh.status == "pending"
    # History preserved: both rows survive.
    all_ids = {s.id for s in svc.list_subscriptions()}
    assert {approved.id, fresh.id} <= all_ids


def test_metrics_counts_revoked():
    svc, _, _ = _build_service()
    svc.set_listing(product_type="agent", product_id=ONB_AGENT, auto_approve=True)
    # 1 auto-approved agent, then revoke it → revoked count 1, approved drops to 0.
    auto = _run(svc.create_subscription(
        product_type="agent", product_id="agent-onboarding",
        requester_oid=OID_A, requester_email="a@x",
    ))
    assert auto.status == "approved"
    _run(svc.revoke_subscription(auto.id, decided_by=ADMIN))

    m = svc.metrics()
    assert m.revoked == 1
    assert m.approved == 0
    assert m.total == 1


# --------------------------------------------------------------------------- #
# E33/T3 — the user→agent grant on agent subscriptions
# --------------------------------------------------------------------------- #

def _pending_agent_sub(svc, product_id=CC_AGENT, requester_oid=OID_A):
    sub = _run(svc.create_subscription(
        product_type="agent", product_id=product_id,
        requester_oid=requester_oid, requester_email="a@x",
    ))
    assert sub.status == "pending"
    return sub


def test_approve_agent_grants_the_subscriber_on_the_products_agent():
    """The grant's arguments are the contract: the AGENT record behind the product (since
    E33 a product IS a registry agent) and the SUBSCRIBER's oid as the principal — not the
    approving admin, and not an agent SP (that is the MCP path's principal)."""
    svc, _, _ = _build_service()
    sub = _pending_agent_sub(svc, requester_oid=OID_B)

    approved = _run(svc.approve(sub.id, decided_by=ADMIN))

    svc._agent_grant_fn.assert_awaited_once()
    call_args = svc._agent_grant_fn.await_args.args
    assert call_args[0] is svc._agent_registry.get(CC_AGENT)
    assert call_args[1] == OID_B            # the SUBSCRIBER, not ADMIN
    # The returned assignment id is persisted — it is the only handle revoke has.
    assert approved.grant_assignment_id == "uasg-1"
    stored = next(s for s in svc.list_subscriptions() if s.id == sub.id)
    assert stored.grant_assignment_id == "uasg-1"
    assert stored.decided_by == ADMIN       # the admin still owns the DECISION audit


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

    # No Graph write was attempted.
    svc._agent_grant_fn.assert_not_awaited()
    # Persisted FAILED with the fixed, safe literal (the E6 route's own wording).
    failed = next(s for s in svc.list_subscriptions() if s.id == sub.id)
    assert failed.status == "failed"
    assert failed.error == "agent identity is not provisioned"
    assert failed.grant_assignment_id is None


def test_approve_agent_grant_failure_sets_failed_with_a_safe_error():
    """A grant-fn raise → FAILED + a safe error literal + ``grant_failed``. The persisted
    error must NOT echo the underlying exception (the no-leak guard)."""
    svc, _, _ = _build_service(
        agent_grant_side_effect=UserGrantError("failed to grant the user access")
    )
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
    svc, _, _ = _build_service(
        agent_revoke_side_effect=UserGrantNotFoundError("grant not found")
    )
    sub = _pending_agent_sub(svc)
    approved = _run(svc.approve(sub.id, decided_by=ADMIN))

    revoked = _run(svc.revoke_subscription(approved.id, decided_by=ADMIN))

    assert revoked.status == "revoked"
    svc._agent_revoke_fn.assert_awaited_once()


def test_revoke_agent_sub_graph_failure_keeps_approved_and_persists_nothing():
    """A real teardown failure leaves the sub APPROVED with NOTHING persisted, so the admin
    can simply click Revoke again — the MCP revoke contract, exactly."""
    svc, _, _ = _build_service(
        agent_revoke_side_effect=UserGrantError("failed to revoke")
    )
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
    # The grant handle survives, so a later retry can still tear it down.
    assert stored.grant_assignment_id == "uasg-1"


def test_revoke_agent_sub_without_a_stored_assignment_id_skips_graph():
    """Nothing actionable in Graph (no stored assignment id) → mark REVOKED without calling
    the revoke fn, the MCP branch's best-effort-cleanup behaviour."""
    svc, _, _ = _build_service()
    sub = _pending_agent_sub(svc)
    approved = _run(svc.approve(sub.id, decided_by=ADMIN))
    stored = svc._get_sub(approved.id)
    stored.grant_assignment_id = None
    svc._save(stored)

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


# --------------------------------------------------------------------------- #
# E33/T3 — the marketplace-publication gate on agent subscriptions
#
# Since approving (or auto-approving) an agent sub writes a REAL Entra Invoker assignment,
# registry EXISTENCE is not enough: the product must be PUBLISHED and not delisted. Each
# failing case must be byte-identical to a nonexistent id (no enumeration) and must never
# reach the grant fn.
# --------------------------------------------------------------------------- #

def _unknown_agent_product_message(svc):
    """The literal a truly-nonexistent agent product raises — the byte-identical baseline
    every gated case must match."""
    with pytest.raises(MarketplaceError) as ei:
        _run(svc.create_subscription(
            product_type="agent", product_id="agent-does-not-exist",
            requester_oid=OID_A, requester_email="a@x",
        ))
    assert ei.value.kind == "not_found"
    return str(ei.value)


def test_subscribe_to_a_never_published_agent_is_not_found():
    """A registry agent with NO marketplace block is not a product — subscribing to it must
    be indistinguishable from subscribing to a nonexistent id, and no row may be born."""
    svc, _, _ = _build_service()
    baseline = _unknown_agent_product_message(svc)

    with pytest.raises(MarketplaceError) as ei:
        _run(svc.create_subscription(
            product_type="agent", product_id=PROVISIONED_AGENT,
            requester_oid=OID_A, requester_email="a@x",
        ))

    assert ei.value.kind == "not_found"
    assert str(ei.value) == baseline          # no enumeration oracle
    assert svc.list_subscriptions() == []     # nothing was persisted
    svc._agent_grant_fn.assert_not_awaited()


def test_subscribe_to_a_delisted_agent_product_is_not_found():
    """A published product an admin has set ``available=False`` is OFF the market: even
    with auto-approve left on, a subscribe cannot be born and no grant is applied."""
    svc, _, _ = _build_service()
    svc.set_listing(product_type="agent", product_id=CC_AGENT,
                    available=False, auto_approve=True)
    baseline = _unknown_agent_product_message(svc)

    with pytest.raises(MarketplaceError) as ei:
        _run(svc.create_subscription(
            product_type="agent", product_id=CC_AGENT,
            requester_oid=OID_A, requester_email="a@x",
        ))

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
    # The delisting did NOT clear auto-approve — the gate is what stops it.
    assert svc.set_listing(product_type="agent", product_id=CC_AGENT).auto_approve is True

    with pytest.raises(MarketplaceError) as ei:
        _run(svc.create_subscription(
            product_type="agent", product_id=CC_AGENT,
            requester_oid=OID_B, requester_email="b@x",
        ))

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
    # What ``AgentRegistryService.transition`` does: status only, block untouched.
    svc._agent_registry.get(CC_AGENT).lifecycle_state = "deprecated"
    assert svc._agent_registry.get(CC_AGENT).marketplace.published is True

    with pytest.raises(MarketplaceError) as ei:
        _run(svc.create_subscription(
            product_type="agent", product_id=CC_AGENT,
            requester_oid=OID_B, requester_email="b@x",
        ))

    assert ei.value.kind == "not_found"
    # Byte-identical to every other unknown-product answer (the C5 no-enumeration invariant).
    assert str(ei.value) == "Unknown agent product"
    assert svc.list_subscriptions() == []
    svc._agent_grant_fn.assert_not_awaited()
    # The card is gone in the same breath — one predicate, both gates.
    assert CC_AGENT not in {c.product_id for c in svc.list_agent_products(caller_oid=OID_B)}


def test_approve_after_deprecation_fails_and_grants_nothing():
    """The RE-GATE catches deprecation too: a PENDING row born while the agent was approved
    outlives that check, so an admin approving it after the agent was deprecated must NOT get
    a real Entra Invoker assignment on a retired agent. FAILED + the fixed literal, retryable
    if the agent is ever re-approved."""
    svc, _, _ = _build_service()
    sub = _pending_agent_sub(svc)                     # born while CC_AGENT was approved
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
    pending = _run(svc.create_subscription(
        product_type="agent", product_id=FNOL_AGENT,
        requester_oid=OID_A, requester_email="a@x",
    ))
    assert pending.status == "pending"

    svc.set_listing(product_type="agent", product_id=ONB_AGENT,
                    available=True, auto_approve=True)
    auto = _run(svc.create_subscription(
        product_type="agent", product_id=ONB_AGENT,
        requester_oid=OID_A, requester_email="a@x",
    ))
    assert auto.status == "approved"
    assert auto.grant_assignment_id == "uasg-1"
    svc._agent_grant_fn.assert_awaited_once()


def test_approve_after_unpublish_fails_and_republishing_makes_retry_succeed():
    """Publication is MUTABLE, so the birth gate can go stale: the row outlives it. An admin
    who unpublishes a product and then approves a still-open PENDING row for it must NOT get
    a real Entra Invoker grant on a product that is no longer offered — the row is persisted
    FAILED with the fixed literal instead, and stays retryable once it is re-published."""
    svc, _, _ = _build_service()
    sub = _pending_agent_sub(svc)                     # born while CC_AGENT was published
    _run(svc.unpublish("agent", CC_AGENT, ADMIN))

    with pytest.raises(MarketplaceError) as ei:
        _run(svc.approve(sub.id, decided_by=ADMIN))
    assert ei.value.kind == "grant_failed"
    assert str(ei.value) == "agent product is no longer published"

    # No Graph write was attempted.
    svc._agent_grant_fn.assert_not_awaited()
    failed = next(s for s in svc.list_subscriptions() if s.id == sub.id)
    assert failed.status == "failed"
    assert failed.error == "agent product is no longer published"
    assert failed.grant_assignment_id is None

    # Re-published → the SAME row retries to approved: a terminal REASON, not a dead end.
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
    assert svc._agent_grant_fn.await_count == 1        # the first attempt did reach Graph

    # Graph would now succeed, but the admin has delisted the product in the meantime.
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
    # The retry never reached Graph — the count is unchanged from the first attempt.
    assert svc._agent_grant_fn.await_count == 1


def test_mcp_subscription_path_never_touches_the_agent_grant_fns():
    """The MCP flow is byte-untouched by T3: approve + revoke drive the MCP pair ONLY."""
    svc, grant_fn, revoke_fn = _build_service()
    approved = _approved_mcp_sub(svc)
    _run(svc.revoke_subscription(approved.id, decided_by=ADMIN))

    grant_fn.assert_awaited_once()
    revoke_fn.assert_awaited_once()
    svc._agent_grant_fn.assert_not_awaited()
    svc._agent_revoke_fn.assert_not_awaited()


# --------------------------------------------------------------------------- #
# T11 (Amendment 1 / C9) — the MCP grant-time publication RE-GATE
#
# The MCP lane gained the same re-gate the agent lane has: a row outlives the birth check in
# ``_resolve_product_name``, so approve/retry must refuse a product that was unpublished or
# delisted since. Revoke stays UNGATED on purpose — a kill switch has to work on a delisted
# product, and that asymmetry is the whole point.
# --------------------------------------------------------------------------- #

def test_approve_mcp_after_unpublish_fails_and_republishing_makes_retry_succeed():
    svc, grant_fn, _ = _build_service()
    sub = _run(svc.create_subscription(
        product_type="mcp", product_id=GATEWAY_MCP, agent_id=PROVISIONED_AGENT,
        requester_oid=OID_A, requester_email="a@x",
    ))
    _run(svc.unpublish("mcp", GATEWAY_MCP, ADMIN))

    with pytest.raises(MarketplaceError) as ei:
        _run(svc.approve(sub.id, decided_by=ADMIN))
    assert ei.value.kind == "grant_failed"
    assert str(ei.value) == "MCP product is no longer published"

    # No Graph write was attempted, and the row carries the fixed, safe reason.
    grant_fn.assert_not_called()
    failed = next(s for s in svc.list_subscriptions() if s.id == sub.id)
    assert failed.status == "failed"
    assert failed.error == "MCP product is no longer published"
    assert failed.grant_assignment_id is None

    # Re-published → the SAME row retries to approved: a terminal REASON, not a dead end.
    svc._mcp_registry.persist_marketplace(GATEWAY_MCP, _publication())
    retried = _run(svc.retry_grant(sub.id, decided_by=ADMIN))
    assert retried.status == "approved"
    assert retried.grant_assignment_id == "asg-1"
    assert retried.error is None
    grant_fn.assert_awaited_once()


def test_retry_grant_after_mcp_delisting_fails_and_grants_nothing():
    """The delisted (``available=False``) condition gates the MCP retry lane too."""
    svc, grant_fn, _ = _build_service(grant_side_effect=RuntimeError("graph 502"))
    sub = _run(svc.create_subscription(
        product_type="mcp", product_id=GATEWAY_MCP, agent_id=PROVISIONED_AGENT,
        requester_oid=OID_A, requester_email="a@x",
    ))
    with pytest.raises(MarketplaceError):
        _run(svc.approve(sub.id, decided_by=ADMIN))
    assert grant_fn.await_count == 1                # the first attempt did reach Graph

    grant_fn.side_effect = None
    svc.set_listing(product_type="mcp", product_id=GATEWAY_MCP, available=False)

    with pytest.raises(MarketplaceError) as ei:
        _run(svc.retry_grant(sub.id, decided_by=ADMIN))
    assert ei.value.kind == "grant_failed"
    assert str(ei.value) == "MCP product is no longer published"
    failed = next(s for s in svc.list_subscriptions() if s.id == sub.id)
    assert failed.status == "failed"
    assert failed.error == "MCP product is no longer published"
    assert grant_fn.await_count == 1                # the retry never reached Graph


def test_approve_mcp_after_deprecation_fails_and_grants_nothing():
    """The re-gate catches lifecycle too: a ``transition`` writes the native status only and
    leaves the marketplace block untouched, so an admin must not grant on a retired server."""
    svc, grant_fn, _ = _build_service()
    sub = _run(svc.create_subscription(
        product_type="mcp", product_id=GATEWAY_MCP, agent_id=PROVISIONED_AGENT,
        requester_oid=OID_A, requester_email="a@x",
    ))
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
        _run(svc.create_subscription(
            product_type="mcp", product_id="m-does-not-exist", agent_id=PROVISIONED_AGENT,
            requester_oid=OID_A, requester_email="a@x",
        ))
    with pytest.raises(MarketplaceError) as unpublished:
        _run(svc.create_subscription(
            product_type="mcp", product_id=UNPUBLISHED_MCP, agent_id=PROVISIONED_AGENT,
            requester_oid=OID_A, requester_email="a@x",
        ))
    assert missing.value.kind == unpublished.value.kind == "not_found"
    assert str(unpublished.value) == str(missing.value) == "Unknown MCP product"
    assert svc.list_subscriptions() == []
    grant_fn.assert_not_called()


def test_subscribe_to_a_published_standard_mcp_works():
    """The retired ``kind == "gateway"`` filter, proven at the subscribe gate: a PUBLISHED
    standard-kind server is subscribable (and grantable) exactly like a gateway."""
    svc, grant_fn, _ = _build_service()
    svc.set_listing(product_type="mcp", product_id=STANDARD_MCP, auto_approve=True)
    sub = _run(svc.create_subscription(
        product_type="mcp", product_id=STANDARD_MCP, agent_id=PROVISIONED_AGENT,
        requester_oid=OID_A, requester_email="a@x",
    ))
    assert sub.status == "approved"
    assert sub.product_name == "Standard MCP"
    assert sub.grant_assignment_id == "asg-1"
    grant_fn.assert_awaited_once()


# --------------------------------------------------------------------------- #
# E33 + Amendment 1 — publish requests for BOTH product types
# (declared datasheet → admin approval → the right registry's envelope write)
#
# Every guard is exercised for agents AND MCP servers via ``PTYPES``, because the whole point
# of C9 is that ONE code path serves both: a test that only covered agents would let the MCP
# lane rot silently.
# --------------------------------------------------------------------------- #

# (product_type, ready product id, non-approved product id, foreign-tenant product id)
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
            _agent("agent-ready", "Ready Agent", tenant_id="ten-1",
                   lifecycle_state="approved"),
            _agent("agent-draft", "Draft Agent", tenant_id="ten-1",
                   lifecycle_state="proposed"),
            _agent("agent-foreign", "Foreign Agent", tenant_id="ten-2",
                   lifecycle_state="approved"),
        ]
    if mcps is None:
        mcps = [
            _mcp("mcp-ready", "Ready MCP", tenant_id="ten-1", lifecycle_state="approved"),
            _mcp("mcp-draft", "Draft MCP", tenant_id="ten-1", lifecycle_state="proposed"),
            _mcp("mcp-foreign", "Foreign MCP", tenant_id="ten-2",
                 lifecycle_state="approved"),
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
        product_type=product_type, product_id=product_id, datasheet=_datasheet(**ds),
        requester_oid=OID_A, requester_email="a@x", ctx=_ctx(is_global=True),
    )


@pytest.mark.parametrize("ptype,ready,draft,foreign", PTYPES)
def test_create_publish_request_persists_pending_with_requester_audit(
    ptype, ready, draft, foreign
):
    svc, _, _ = _publish_service()
    req = svc.create_publish_request(
        product_type=ptype, product_id=ready, datasheet=_datasheet(sla_tier="Gold"),
        requester_oid=OID_A, requester_email="a@x", ctx=_ctx(tenant_ids=["ten-1"]),
    )

    assert req.id.startswith("pub-")
    assert req.product_type == ptype
    assert req.product_id == ready
    # Resolved from the REGISTRY the product_type selects, never from the body.
    assert req.product_name == ("Ready Agent" if ptype == "agent" else "Ready MCP")
    assert req.tenant_id == "ten-1"
    assert req.status == "pending"
    assert req.requested_by == OID_A
    assert req.requested_by_email == "a@x"
    assert req.datasheet.sla_tier == "Gold"
    assert req.decided_by is None
    assert req.decided_at is None
    assert req.error is None
    # Persisted + retrievable by the (type, id) pair, and listed.
    assert svc.get_publish_request_for_product(ptype, ready).id == req.id
    assert [r.id for r in svc.list_publish_requests()] == [req.id]


@pytest.mark.parametrize("ptype,ready,draft,foreign", PTYPES)
def test_create_publish_request_requires_approved_lifecycle(ptype, ready, draft, foreign):
    svc, _, _ = _publish_service()
    with pytest.raises(MarketplaceError) as ei:
        svc.create_publish_request(
            product_type=ptype, product_id=draft, datasheet=_datasheet(),
            requester_oid=OID_A, requester_email="a@x", ctx=_ctx(tenant_ids=["ten-1"]),
        )
    # The kind name is kept from C3 even though it now covers MCPs (a wire contract).
    assert ei.value.kind == "agent_not_approved"
    assert svc.get_publish_request_for_product(ptype, draft) is None


@pytest.mark.parametrize("ptype,ready,draft,foreign", PTYPES)
@pytest.mark.parametrize(
    "identity",
    [
        pytest.param(dict(identity_status="none"), id="not-provisioned"),
        pytest.param(dict(entra_sp_id=None), id="no-sp-id"),
        pytest.param(dict(invoker_role_id=None), id="no-invoker-role-id"),
    ],
)
def test_create_publish_request_requires_a_provisioned_identity(
    identity, ptype, ready, draft, foreign
):
    """C10, for BOTH product types (NEW for agents as well as MCPs): a product whose Entra
    identity is not provisioned can never be granted on, so publishing it would advertise a
    guaranteed dead end. All three fields are required, and the detail is a FIXED literal."""
    svc, _, _ = _publish_service(
        agents=[_agent("agent-ready", "Ready Agent", tenant_id="ten-1",
                       lifecycle_state="approved", **identity)],
        mcps=[_mcp("mcp-ready", "Ready MCP", tenant_id="ten-1",
                   lifecycle_state="approved", **identity)],
    )

    with pytest.raises(MarketplaceError) as ei:
        svc.create_publish_request(
            product_type=ptype, product_id=ready, datasheet=_datasheet(),
            requester_oid=OID_A, requester_email="a@x", ctx=_ctx(tenant_ids=["ten-1"]),
        )
    assert ei.value.kind == "identity_not_provisioned"
    assert str(ei.value) == "identity is not provisioned"
    assert svc.get_publish_request_for_product(ptype, ready) is None


@pytest.mark.parametrize("ptype,ready,draft,foreign", PTYPES)
def test_create_publish_request_lifecycle_is_checked_before_identity(
    ptype, ready, draft, foreign
):
    """Guard ORDER (C9): lifecycle first, identity second — a not-yet-approved product reports
    the lifecycle block even when its identity is also missing, so the publisher is told the
    thing they have to fix first."""
    svc, _, _ = _publish_service(
        agents=[_agent("agent-draft", "Draft Agent", tenant_id="ten-1",
                       lifecycle_state="proposed", identity_status="none")],
        mcps=[_mcp("mcp-draft", "Draft MCP", tenant_id="ten-1",
                   lifecycle_state="proposed", identity_status="none")],
    )
    with pytest.raises(MarketplaceError) as ei:
        svc.create_publish_request(
            product_type=ptype, product_id=draft, datasheet=_datasheet(),
            requester_oid=OID_A, requester_email="a@x", ctx=_ctx(tenant_ids=["ten-1"]),
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
            product_type=ptype, product_id=foreign, datasheet=_datasheet(),
            requester_oid=OID_A, requester_email="a@x", ctx=ctx,
        )
    with pytest.raises(MarketplaceError) as missing:
        svc.create_publish_request(
            product_type=ptype, product_id="does-not-exist", datasheet=_datasheet(),
            requester_oid=OID_A, requester_email="a@x", ctx=ctx,
        )
    assert foreign_err.value.kind == missing.value.kind == "not_found"
    assert str(foreign_err.value) == str(missing.value)


def test_create_publish_request_refuses_a_foreign_shared_mcp():
    """Fix round 1 — the fail-closed intent, pinned: ``visible()`` is called WITHOUT
    ``shared=`` on purpose. Publishing is an OWNING-TENANT governance action, while E24's
    ``shared=true`` confers read + grant-target rights only, never write. So a tenant-A
    OPERATOR cannot file a publish request against tenant-B's SHARED MCP — and the refusal is
    byte-identical to a missing id, so it is not an existence oracle either."""
    svc, _, _ = _publish_service(
        mcps=[_mcp("mcp-foreign-shared", "Foreign Shared MCP", tenant_id="ten-2",
                   lifecycle_state="approved", shared=True)],
    )
    ctx = _ctx(tenant_ids=["ten-1"])

    with pytest.raises(MarketplaceError) as shared_err:
        svc.create_publish_request(
            product_type="mcp", product_id="mcp-foreign-shared", datasheet=_datasheet(),
            requester_oid=OID_A, requester_email="a@x", ctx=ctx,
        )
    with pytest.raises(MarketplaceError) as missing:
        svc.create_publish_request(
            product_type="mcp", product_id="does-not-exist", datasheet=_datasheet(),
            requester_oid=OID_A, requester_email="a@x", ctx=ctx,
        )

    assert shared_err.value.kind == missing.value.kind == "not_found"
    assert str(shared_err.value) == str(missing.value)
    assert svc.get_publish_request_for_product("mcp", "mcp-foreign-shared") is None
    # The owning tenant (and a global admin) can still publish it.
    assert svc.create_publish_request(
        product_type="mcp", product_id="mcp-foreign-shared", datasheet=_datasheet(),
        requester_oid=OID_B, requester_email="b@x", ctx=_ctx(tenant_ids=["ten-2"]),
    ).tenant_id == "ten-2"


@pytest.mark.parametrize("ptype,ready,draft,foreign", PTYPES)
def test_create_publish_request_admin_sees_every_tenant(ptype, ready, draft, foreign):
    svc, _, _ = _publish_service()
    req = svc.create_publish_request(
        product_type=ptype, product_id=foreign, datasheet=_datasheet(),
        requester_oid=ADMIN, requester_email="admin@x", ctx=_ctx(is_global=True),
    )
    assert req.tenant_id == "ten-2"


@pytest.mark.parametrize("ptype,ready,draft,foreign", PTYPES)
def test_create_publish_request_second_pending_conflicts(ptype, ready, draft, foreign):
    svc, _, _ = _publish_service()
    kwargs = dict(product_type=ptype, product_id=ready, requester_oid=OID_A,
                  requester_email="a@x", ctx=_ctx(tenant_ids=["ten-1"]))
    first = svc.create_publish_request(datasheet=_datasheet(), **kwargs)
    with pytest.raises(MarketplaceError) as ei:
        svc.create_publish_request(datasheet=_datasheet(sla_tier="Silver"), **kwargs)
    assert ei.value.kind == "publish_conflict"
    # The original PENDING request is untouched (no silent overwrite of the declaration).
    stored = svc.get_publish_request_for_product(ptype, ready)
    assert stored.id == first.id
    assert stored.datasheet.sla_tier is None


@pytest.mark.parametrize("ptype,ready,draft,foreign", PTYPES)
def test_create_publish_request_after_rejection_replaces_the_record(
    ptype, ready, draft, foreign
):
    """ONE record per PRODUCT: a re-publish after a REJECT overwrites it (the decision
    history lives in the audit fields, not in extra rows)."""
    svc, _, _ = _publish_service()
    kwargs = dict(product_type=ptype, product_id=ready, requester_oid=OID_A,
                  requester_email="a@x", ctx=_ctx(tenant_ids=["ten-1"]))
    first = svc.create_publish_request(datasheet=_datasheet(), **kwargs)
    svc.reject_publish(first.id, ADMIN, "thin datasheet")

    second = svc.create_publish_request(datasheet=_datasheet(sla_tier="Gold"), **kwargs)
    assert second.status == "pending"
    assert second.datasheet.sla_tier == "Gold"
    assert len(svc.list_publish_requests()) == 1
    assert svc.get_publish_request_for_product(ptype, ready).id == second.id


def test_publish_records_of_the_two_types_do_not_collide():
    """The C9 key-space separation: an AGENT and an MCP server sharing a product id keep
    DISTINCT records. Without the type in the sk the second publish would overwrite the first
    and ``approve_publish`` would dispatch the envelope write to the wrong registry."""
    same_id = "shared-id-1"
    svc, agent_registry, mcp_registry = _publish_service(
        agents=[_agent(same_id, "Agent With Shared Id", tenant_id="ten-1",
                       lifecycle_state="approved")],
        mcps=[_mcp(same_id, "MCP With Shared Id", tenant_id="ten-1",
                   lifecycle_state="approved")],
    )

    agent_req = svc.create_publish_request(
        product_type="agent", product_id=same_id, datasheet=_datasheet(owner_team="Team A"),
        requester_oid=OID_A, requester_email="a@x", ctx=_ctx(is_global=True),
    )
    mcp_req = svc.create_publish_request(
        product_type="mcp", product_id=same_id, datasheet=_datasheet(owner_team="Team M"),
        requester_oid=OID_B, requester_email="b@x", ctx=_ctx(is_global=True),
    )

    # Two records, not one overwritten — and each reads back its OWN declaration.
    assert agent_req.id != mcp_req.id
    assert len(svc.list_publish_requests()) == 2
    assert svc.get_publish_request_for_product("agent", same_id).id == agent_req.id
    assert svc.get_publish_request_for_product("agent", same_id).datasheet.owner_team == "Team A"
    assert svc.get_publish_request_for_product("mcp", same_id).id == mcp_req.id
    assert svc.get_publish_request_for_product("mcp", same_id).datasheet.owner_team == "Team M"

    # And each approval lands in ITS OWN registry.
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
    assert svc.get_publish_request_for_product(ptype, ready) is None


@pytest.mark.parametrize("ptype,ready,draft,foreign", PTYPES)
def test_get_publish_request_for_product_hides_a_foreign_tenants_request(
    ptype, ready, draft, foreign
):
    """A foreign tenant's publish record reads as None — INDISTINGUISHABLE from "never
    requested", which is what makes the route's 404 byte-identical for both cases.

    Without the gate, a tenant-A OPERATOR who guesses (or lists) a tenant-B product id reads
    B's declared datasheet, requester email and decision_reason straight out of the record.
    """
    svc, _, _ = _publish_service()
    filed = svc.create_publish_request(
        product_type=ptype, product_id=foreign,
        datasheet=_datasheet(owner_team="Marketing EU"),
        requester_oid=OID_B, requester_email="bob@tenant-two.example",
        ctx=_ctx(tenant_ids=["ten-2"]),
    )
    assert filed.tenant_id == "ten-2"

    ctx_one = _ctx(tenant_ids=["ten-1"])
    assert svc.get_publish_request_for_product(ptype, foreign, ctx_one) is None
    # ...and that is the SAME answer they get for a product that never requested one.
    assert svc.get_publish_request_for_product(ptype, ready, ctx_one) is None


@pytest.mark.parametrize("ptype,ready,draft,foreign", PTYPES)
def test_get_publish_request_for_product_returns_own_tenants_request(
    ptype, ready, draft, foreign
):
    svc, _, _ = _publish_service()
    req = svc.create_publish_request(
        product_type=ptype, product_id=ready, datasheet=_datasheet(),
        requester_oid=OID_A, requester_email="a@x", ctx=_ctx(tenant_ids=["ten-1"]),
    )
    got = svc.get_publish_request_for_product(ptype, ready, _ctx(tenant_ids=["ten-1"]))
    assert got is not None
    assert got.id == req.id


@pytest.mark.parametrize("ptype,ready,draft,foreign", PTYPES)
def test_get_publish_request_for_product_admin_sees_every_tenant(
    ptype, ready, draft, foreign
):
    """A global admin bypasses the gate — ``visible()``'s own semantics (is_global short-
    circuits), so the ADMIN approvals queue can read any tenant's request."""
    svc, _, _ = _publish_service()
    _pending_request(svc, ptype, foreign)
    got = svc.get_publish_request_for_product(ptype, foreign, _ctx(is_global=True))
    assert got is not None
    assert got.tenant_id == "ten-2"


@pytest.mark.parametrize("ptype,ready,draft,foreign", PTYPES)
def test_get_publish_request_for_product_no_ctx_is_unscoped(ptype, ready, draft, foreign):
    """ctx=None (no tenant scoping wired) → unfiltered, the create/list convention."""
    svc, _, _ = _publish_service()
    _pending_request(svc, ptype, foreign)
    assert svc.get_publish_request_for_product(ptype, foreign) is not None


def test_get_publish_request_for_product_untagged_request_hidden_from_scoped_caller():
    """An UNTAGGED request (a product with no tenant_id — registered directly / pre-E24) is
    invisible to a scoped caller and visible to an admin, exactly as ``visible()`` defines for
    a ``tenant_id is None`` resource. Fail-closed, not fail-open."""
    svc, _, _ = _publish_service(
        agents=[_agent("agent-untagged", "Untagged Agent", tenant_id=None,
                       lifecycle_state="approved")],
        mcps=[_mcp("mcp-untagged", "Untagged MCP", tenant_id=None,
                   lifecycle_state="approved")],
    )
    for ptype, pid in (("agent", "agent-untagged"), ("mcp", "mcp-untagged")):
        req = svc.create_publish_request(
            product_type=ptype, product_id=pid, datasheet=_datasheet(),
            requester_oid=OID_A, requester_email="a@x", ctx=None,
        )
        assert req.tenant_id is None
        assert svc.get_publish_request_for_product(ptype, pid, _ctx(tenant_ids=["ten-1"])) is None
        assert svc.get_publish_request_for_product(ptype, pid, _ctx(is_global=True)) is not None


@pytest.mark.parametrize("ptype,ready,draft,foreign", PTYPES)
def test_approve_publish_writes_the_publication_to_the_right_registry(
    ptype, ready, draft, foreign
):
    svc, _, _ = _publish_service()
    registry = _registry_of(svc, ptype)
    other = _other_registry_of(svc, ptype)
    req = _pending_request(svc, ptype, ready, sla_tier="Gold", compliance=["GDPR"])

    approved = _run(svc.approve_publish(req.id, ADMIN))

    assert approved.status == "approved"
    assert approved.decided_by == ADMIN
    assert approved.decided_at is not None
    assert approved.error is None
    # ONE registry write, carrying the declared datasheet + the approver's attestation —
    # dispatched by the REQUEST's product_type, so the other registry is never touched.
    assert len(registry.persist_calls) == 1
    assert other.persist_calls == []
    product_id, publication = registry.persist_calls[0]
    assert product_id == ready
    assert publication.published is True
    assert publication.declared_by == ADMIN          # the APPROVER, never the requester
    assert publication.declared_at is not None
    assert publication.datasheet.sla_tier == "Gold"
    assert publication.datasheet.compliance == ["GDPR"]
    # Persisted, and the product is now a marketplace card.
    assert svc.get_publish_request_for_product(ptype, ready).status == "approved"
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
    assert svc.get_publish_request_for_product(ptype, ready).status == "pending"


@pytest.mark.parametrize("ptype,ready,draft,foreign", PTYPES)
def test_approve_publish_re_asserts_the_identity_gate(ptype, ready, draft, foreign):
    """C10 at APPROVE: an identity torn down between the request and the decision blocks the
    publication with the FIXED literal, and writes no envelope."""
    svc, _, _ = _publish_service()
    registry = _registry_of(svc, ptype)
    req = _pending_request(svc, ptype, ready)
    registry.get(ready).identity_status = "failed"

    with pytest.raises(MarketplaceError) as ei:
        _run(svc.approve_publish(req.id, ADMIN))
    assert ei.value.kind == "identity_not_provisioned"
    assert str(ei.value) == "identity is not provisioned"
    assert registry.persist_calls == []
    assert svc.get_publish_request_for_product(ptype, ready).status == "pending"


def test_create_publish_request_refuses_a_databricks_governed_agent():
    """E29×E33 seam: the subscription grant path has no Databricks ACL mirror (grants.py's
    §3A both-or-neither invariant), so publish is refused up front. Checked BEFORE the
    identity gate deliberately — a provisioned federation agent must hear the true reason,
    not an identity answer that implies a provisioning retry could unlock publish."""
    agent = _agent("agent-dbx", "Databricks Agent", tenant_id="ten-1")
    agent.is_databricks_governed = True
    svc, _, _ = _publish_service(agents=[agent])

    with pytest.raises(MarketplaceError) as ei:
        svc.create_publish_request(
            product_type="agent", product_id="agent-dbx", datasheet=_datasheet(),
            requester_oid=OID_A, requester_email="a@x", ctx=_ctx(tenant_ids=["ten-1"]),
        )
    assert ei.value.kind == "databricks_publish_unsupported"
    assert svc.get_publish_request_for_product("agent", "agent-dbx") is None


def test_approve_publish_re_asserts_the_databricks_gate():
    """An agent that became Databricks-governed between the request and the decision blocks
    the publication with the seam kind, and writes no envelope."""
    svc, _, _ = _publish_service()
    registry = _registry_of(svc, "agent")
    req = _pending_request(svc, "agent", "agent-ready")
    registry.get("agent-ready").is_databricks_governed = True

    with pytest.raises(MarketplaceError) as ei:
        _run(svc.approve_publish(req.id, ADMIN))
    assert ei.value.kind == "databricks_publish_unsupported"
    assert registry.persist_calls == []
    assert svc.get_publish_request_for_product("agent", "agent-ready").status == "pending"


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
    assert registry.persist_calls == []      # no second envelope write


def test_approve_publish_unknown_request_is_not_found():
    svc, agent_registry, mcp_registry = _publish_service()
    with pytest.raises(MarketplaceError) as ei:
        _run(svc.approve_publish("pub-doesnotexist", ADMIN))
    assert ei.value.kind == "not_found"
    assert agent_registry.persist_calls == []
    assert mcp_registry.persist_calls == []


@pytest.mark.parametrize("ptype,ready,draft,foreign", PTYPES)
def test_approve_publish_registry_not_found_keeps_request_pending(
    ptype, ready, draft, foreign
):
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

    stored = svc.get_publish_request_for_product(ptype, ready)
    assert stored.status == "pending"        # retryable
    assert stored.error                      # a safe literal is persisted
    assert stored.decided_by is None
    assert stored.decided_at is None


@pytest.mark.parametrize("ptype,ready,draft,foreign", PTYPES)
def test_approve_publish_raw_client_error_never_escapes(ptype, ready, draft, foreign):
    """T1 carry-forward: ``persist_marketplace`` re-reads the record and then writes, so a
    TOCTOU delete surfaces a RAW botocore ``ClientError`` rather than the domain not-found.
    The approve path must catch registry failures BROADLY — a raw ClientError escaping would
    500 instead of 502 and leave the request undecided with no error recorded."""
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

    stored = svc.get_publish_request_for_product(ptype, ready)
    assert stored.status == "pending"
    assert stored.error


@pytest.mark.parametrize("ptype,ready,draft,foreign", PTYPES)
def test_approve_publish_error_is_a_fixed_literal_not_the_exception(
    ptype, ready, draft, foreign
):
    """The persisted/raised text is a FIXED literal — never ``str(exc)`` (no leak of a
    registry id, an ARN, or any AWS detail into a request record or a response)."""
    svc, _, _ = _publish_service()
    registry = _registry_of(svc, ptype)
    req = _pending_request(svc, ptype, ready)
    registry.persist_error = RuntimeError("arn:aws:secret-detail registry-id-1234")

    with pytest.raises(MarketplaceError) as ei:
        _run(svc.approve_publish(req.id, ADMIN))

    stored = svc.get_publish_request_for_product(ptype, ready)
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
    assert svc.get_publish_request_for_product(ptype, ready).error

    registry.persist_error = None
    approved = _run(svc.approve_publish(req.id, ADMIN))
    assert approved.status == "approved"
    assert approved.error is None


@pytest.mark.parametrize("ptype,ready,draft,foreign", PTYPES)
def test_reject_publish_records_reason_and_leaves_registry_untouched(
    ptype, ready, draft, foreign
):
    svc, _, _ = _publish_service()
    registry = _registry_of(svc, ptype)
    req = _pending_request(svc, ptype, ready)

    rejected = svc.reject_publish(req.id, ADMIN, "datasheet too thin")

    assert rejected.status == "rejected"
    assert rejected.decided_by == ADMIN
    assert rejected.decided_at is not None
    assert rejected.decision_reason == "datasheet too thin"
    assert registry.persist_calls == []      # a reject writes NO envelope
    assert svc.get_publish_request_for_product(ptype, ready).status == "rejected"
    # And the product is not a card.
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
    assert other.persist_calls == []         # dispatched to the right registry
    product_id, publication = registry.persist_calls[0]
    assert product_id == ready
    assert publication.published is False
    # The DECLARED history survives the delisting (C2/C8: never cleared to None).
    assert publication is not None
    assert publication.datasheet.sla_tier == "Gold"
    assert publication.declared_by == ADMIN
    assert publication.declared_at is not None
    # The card is gone.
    assert _cards_of(svc, ptype) == []


@pytest.mark.parametrize("ptype,ready,draft,foreign", PTYPES)
def test_unpublish_absent_or_already_unpublished_is_not_found(ptype, ready, draft, foreign):
    svc, _, _ = _publish_service()
    registry = _registry_of(svc, ptype)
    # No marketplace block at all.
    with pytest.raises(MarketplaceError) as ei:
        _run(svc.unpublish(ptype, ready, ADMIN))
    assert ei.value.kind == "not_found"

    # Already unpublished → same not_found (idempotent-looking, but no second write).
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

    item = svc._publish_to_item(req)
    assert item["pk"] == "MARKETPLACE"
    assert item["sk"] == expected_sk
    assert item["_record_kind"] == "publish"

    back = svc._publish_from_item(item)
    assert back == req
    assert back.datasheet.compliance == ["GDPR", "BaFin"]


def test_publish_sk_keys_on_the_product_type_and_id_pair():
    """The sk FORMAT is a contract (it is the record's identity), and the type comes from
    ``ProductType`` so a raw enum member can never leak ``"ProductType.AGENT"`` into a key."""
    from models.marketplace import ProductType
    from services.marketplace_service import _publish_sk

    assert _publish_sk("agent", "p-1") == "publish#agent#p-1"
    assert _publish_sk("mcp", "p-1") == "publish#mcp#p-1"
    assert _publish_sk(ProductType.AGENT, "p-1") == "publish#agent#p-1"
    assert _publish_sk(ProductType.MCP, "p-1") == "publish#mcp#p-1"
    # The two key spaces are disjoint for the SAME product id.
    assert _publish_sk("agent", "p-1") != _publish_sk("mcp", "p-1")


def test_publishing_an_mcp_makes_it_a_subscribable_marketplace_product():
    """End to end for the new MCP lane: a standard-kind server with no declaration is not a
    product, and one publish (declare → approve) turns it into a card AND a subscribable
    product — publish is the only door, for both types."""
    svc, grant_fn, _ = _publish_service()
    assert svc.list_mcp_products(caller_oid=OID_A) == []
    with pytest.raises(MarketplaceError):
        _run(svc.create_subscription(
            product_type="mcp", product_id="mcp-ready", agent_id="agent-ready",
            requester_oid=OID_A, requester_email="a@x",
        ))

    req = _pending_request(svc, "mcp", "mcp-ready", sla_tier="Silver")
    _run(svc.approve_publish(req.id, ADMIN))

    card = svc.list_mcp_products(caller_oid=OID_A)[0]
    assert card.product_id == "mcp-ready"
    assert card.sla_tier == "Silver"
    sub = _run(svc.create_subscription(
        product_type="mcp", product_id="mcp-ready", agent_id="agent-ready",
        requester_oid=OID_A, requester_email="a@x",
    ))
    assert sub.status == "pending"
    assert sub.product_name == "Ready MCP"
