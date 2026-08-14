"""Tests for AgentRegistryService (Epic 4, Task 2).

Strategy (research §10): no moto — inject ``unittest.mock.MagicMock`` boto3 clients
via the service constructor. Fixtures live in ``conftest.py``.
"""

from __future__ import annotations

import json

import pytest
from botocore.exceptions import ClientError

from datetime import datetime, timezone
from types import SimpleNamespace

from models.agent import Agent, AgentCreate, AuthType, AgentUpdate, LifecycleState, Platform
from services.agent_registry_service import (
    AgentRegistryService,
    NameTakenError,
    apply_creator_sponsor,
)

from conftest import REGISTRY_ID, RECORD_ID


def _not_found(op: str = "GetRegistryRecord") -> ClientError:
    return ClientError(
        {"Error": {"Code": "ResourceNotFoundException", "Message": "x"}}, op
    )


# --- apply_creator_sponsor (E22/T3 shared back-fill) -----------------------

def _principal(oid: str, email: str) -> SimpleNamespace:
    """A minimal stand-in for core.rbac.Principal (only .oid / .email are read)."""
    return SimpleNamespace(oid=oid, email=email)


def test_apply_creator_sponsor_backfills_when_blank():
    out = apply_creator_sponsor(
        AgentCreate(name="a", tenant_id="default", sponsor_oid=None, sponsor_email=None),
        _principal(oid="O1", email="e@x"),
    )
    assert out.sponsor_oid == "O1" and out.sponsor_email == "e@x"


def test_apply_creator_sponsor_preserves_explicit():
    out = apply_creator_sponsor(
        AgentCreate(name="a", tenant_id="default", sponsor_oid="EXPLICIT", sponsor_email="owner@x"),
        _principal(oid="O1", email="e@x"),
    )
    assert out.sponsor_oid == "EXPLICIT" and out.sponsor_email == "owner@x"


# --- create ----------------------------------------------------------------

def test_create_calls_create_registry_record_with_custom_descriptor(service, mock_registry_clients):
    ctl, _ = mock_registry_clients
    req = AgentCreate(
        name="claims-triage-de",
        purpose="Triage motor claims",
        sponsor_oid="maria-oid",
        sponsor_email="maria.bauer@example.com",
        business_unit="Claims",
        region="DE",
        platform=Platform.AWS_BEDROCK,
        tenant_id="default",
    )

    agent = service.create(req, created_by="maria.bauer@example.com")

    assert ctl.create_registry_record.called
    kwargs = ctl.create_registry_record.call_args.kwargs
    assert kwargs["registryId"] == REGISTRY_ID
    assert kwargs["name"] == "claims-triage-de"
    assert kwargs["recordType"] == "CUSTOM"

    inline = kwargs["descriptors"]["custom"]["data"]
    envelope = json.loads(inline)
    assert envelope["sponsor_oid"] == "maria-oid"
    assert envelope["business_unit"] == "Claims"
    assert envelope["platform"] == "aws_bedrock"
    assert envelope["created_by"] == "maria.bauer@example.com"
    assert envelope["schema_version"] == 1

    # recordId parsed from recordArn (create output has NO recordId — research §6).
    assert agent.id == RECORD_ID
    # DRAFT -> PROPOSED.
    assert agent.lifecycle_state == LifecycleState.PROPOSED
    assert agent.created_by == "maria.bauer@example.com"


def test_create_without_purpose_sends_nonempty_description(service, mock_registry_clients):
    """Regression: CreateRegistryRecord requires description min length 1. A create with
    no purpose (e.g. the add_repo template flow) must fall back to the name, not send ""
    (which botocore rejects with ParamValidationError -> a raw 500)."""
    ctl, _ = mock_registry_clients

    service.create(AgentCreate(name="tmpl-materialized-agent", tenant_id="default"))

    kwargs = ctl.create_registry_record.call_args.kwargs
    assert kwargs["description"] == "tmpl-materialized-agent"
    assert len(kwargs["description"]) >= 1


def test_create_name_precheck_raises_name_taken(service, mock_registry_clients):
    ctl, _ = mock_registry_clients
    # Name pre-check finds an existing record with that name.
    ctl.list_registry_records.return_value = {
        "registryRecords": [
            {"recordId": "rec-existing", "name": "claims-triage-de", "status": "DRAFT"}
        ],
        "nextToken": None,
    }

    with pytest.raises(NameTakenError):
        service.create(AgentCreate(name="claims-triage-de", tenant_id="default"))

    # Must NOT have attempted to create the record.
    ctl.create_registry_record.assert_not_called()


def test_create_name_precheck_queries_by_name(service, mock_registry_clients):
    ctl, _ = mock_registry_clients
    service.create(AgentCreate(name="fraud-watch-eu", tenant_id="default"))
    # The pre-check narrowed by name via the structured `filters` list (E32: the discrete
    # name= query param is gone).
    precheck = ctl.list_registry_records.call_args_list[0]
    assert precheck.kwargs["filters"] == [{"name": "name", "values": ["fraud-watch-eu"]}]


def test_create_polls_to_draft_before_returning(service, mock_registry_clients, sample_record, monkeypatch):
    """create() must poll the freshly-created (CREATING) record to DRAFT before
    returning, and derive lifecycle from the freshly-observed DRAFT status (→ PROPOSED),
    NOT the stale CREATING create response. Mirrors the MCP service's poll."""
    # Don't actually sleep between the CREATING and DRAFT polls.
    monkeypatch.setattr("services.agent_registry_service.time.sleep", lambda _s: None)

    ctl, _ = mock_registry_clients
    # create returns CREATING (override the fixture's DRAFT default for the create resp).
    ctl.create_registry_record.return_value = {"recordArn": ctl.create_registry_record.return_value["recordArn"], "status": "CREATING"}
    # Poll sees CREATING first, then DRAFT.
    ctl.get_registry_record.side_effect = [
        sample_record(status="CREATING"),
        sample_record(status="DRAFT"),
    ]

    agent = service.create(AgentCreate(name="claims-triage-de", tenant_id="default"))

    # The poll ran (get_registry_record called at least twice: CREATING then DRAFT).
    assert ctl.get_registry_record.call_count >= 2
    # Lifecycle derived from the freshly-observed DRAFT status, not the stale CREATING.
    assert agent.lifecycle_state == LifecycleState.PROPOSED


def test_create_stamps_pending_identity_status_for_agentcore_agent(service, mock_registry_clients):
    """An AgentCore agent (arn + entra + aws_bedrock) → the CREATE envelope's
    custom `data` blob carries identity_status='pending' (stamped INTO create, no
    update-after-create). A metadata agent (no arn) → envelope identity_status='none'."""
    ctl, _ = mock_registry_clients

    # --- agentcore agent: identity_status stamped 'pending' in the create envelope ---
    service.create(
        AgentCreate(
            name="claims-triage-de",
            platform=Platform.AWS_BEDROCK,
            auth_type=AuthType.ENTRA,
            agent_arn="arn:aws:bedrock-agentcore:us-east-1:111122223333:runtime/agent-abc123",
            tenant_id="default",
        )
    )
    inline = ctl.create_registry_record.call_args.kwargs["descriptors"]["custom"]["data"]
    envelope = json.loads(inline)
    assert envelope["identity_status"] == "pending"

    # --- metadata agent (no arn): identity_status stays 'none' ---
    ctl.create_registry_record.reset_mock()
    service.create(AgentCreate(name="metadata-only-agent", tenant_id="default"))
    inline = ctl.create_registry_record.call_args.kwargs["descriptors"]["custom"]["data"]
    envelope = json.loads(inline)
    assert envelope["identity_status"] == "none"


def test_agent_poll_conflict_during_creating_keeps_polling(service, mock_registry_clients, sample_record):
    """A ConflictException while the record is still CREATING is expected — the poll
    swallows it and keeps polling until DRAFT (mirror the MCP service)."""
    ctl, _ = mock_registry_clients
    conflict = ClientError(
        {"Error": {"Code": "ConflictException", "Message": "record is CREATING"}},
        "GetRegistryRecord",
    )
    ctl.get_registry_record.side_effect = [conflict, sample_record(status="DRAFT")]
    status = service._poll_record_to_draft(RECORD_ID, attempts=3, delay=0)
    assert status == "DRAFT"
    assert ctl.get_registry_record.call_count == 2


def test_agent_poll_times_out_raises(service, mock_registry_clients, sample_record):
    """Never reaches DRAFT -> the bounded loop exhausts and raises RuntimeError."""
    ctl, _ = mock_registry_clients
    ctl.get_registry_record.return_value = sample_record(status="CREATING")
    with pytest.raises(RuntimeError):
        service._poll_record_to_draft(RECORD_ID, attempts=2, delay=0)


# --- get -------------------------------------------------------------------

def test_get_parses_envelope_and_maps_status(service, mock_registry_clients, sample_record):
    ctl, _ = mock_registry_clients
    ctl.get_registry_record.return_value = sample_record(status="APPROVED")

    agent = service.get(RECORD_ID)

    assert agent is not None
    assert agent.id == RECORD_ID
    assert agent.purpose == "Triage inbound motor claims for the DE market"
    assert agent.lifecycle_state == LifecycleState.APPROVED
    assert agent.business_unit == "Claims"
    assert agent.platform == Platform.AWS_BEDROCK
    ctl.get_registry_record.assert_called_with(registryId=REGISTRY_ID, recordId=RECORD_ID)


def test_get_returns_none_on_not_found(service, mock_registry_clients):
    ctl, _ = mock_registry_clients
    ctl.get_registry_record.side_effect = _not_found()
    assert service.get("rec-missing") is None


def test_get_raises_on_malformed_envelope(service, mock_registry_clients, sample_record):
    """A record present but without a well-formed CUSTOM envelope fails loudly."""
    from services.agent_registry_service import MalformedAgentRecordError

    ctl, _ = mock_registry_clients
    ctl.get_registry_record.return_value = sample_record(descriptors={})
    with pytest.raises(MalformedAgentRecordError):
        service.get(RECORD_ID)


def test_get_reraises_non_not_found_client_error(service, mock_registry_clients):
    """A non-ResourceNotFound ClientError must propagate, not be swallowed as None."""
    ctl, _ = mock_registry_clients
    ctl.get_registry_record.side_effect = ClientError(
        {"Error": {"Code": "ThrottlingException", "Message": "slow down"}}, "GetRegistryRecord"
    )
    with pytest.raises(ClientError):
        service.get(RECORD_ID)


# --- list ------------------------------------------------------------------

def test_list_fans_out_and_filters_by_business_unit(service, mock_registry_clients, sample_record, sample_envelope):
    ctl, _ = mock_registry_clients
    ctl.list_registry_records.return_value = {
        "registryRecords": [
            {"recordId": "rec-1", "name": "a", "status": "DRAFT"},
            {"recordId": "rec-2", "name": "b", "status": "DRAFT"},
        ],
        "nextToken": None,
    }

    def _get(registryId, recordId):
        if recordId == "rec-1":
            return sample_record(recordId="rec-1", _envelope=sample_envelope(business_unit="Claims"))
        return sample_record(recordId="rec-2", _envelope=sample_envelope(business_unit="Underwriting"))

    ctl.get_registry_record.side_effect = _get

    # N+1 fan-out: one GetRegistryRecord per summary.
    all_agents = service.list()
    assert len(all_agents) == 2
    assert ctl.get_registry_record.call_count == 2

    # Client-side business_unit filter narrows the set.
    claims = service.list(business_unit="Claims")
    assert [a.id for a in claims] == ["rec-1"]


def test_list_passes_record_type_custom(service, mock_registry_clients):
    """Agents are CUSTOM records, narrowed by the structured recordType filter (E32)."""
    ctl, _ = mock_registry_clients
    service.list()
    kwargs = ctl.list_registry_records.call_args.kwargs
    assert kwargs["filters"] == [{"name": "recordType", "values": ["CUSTOM"]}]


def test_list_server_side_status_filter_for_one_to_one_lifecycle(service, mock_registry_clients):
    """APPROVED maps 1:1 to native status -> server-side `status=APPROVED` filter."""
    ctl, _ = mock_registry_clients
    service.list(lifecycle_state=LifecycleState.APPROVED)
    kwargs = ctl.list_registry_records.call_args.kwargs
    assert {"name": "status", "values": ["APPROVED"]} in kwargs["filters"]


def test_list_proposed_is_client_side_not_server_filtered(service, mock_registry_clients):
    """PROPOSED maps from DRAFT/CREATING/*_FAILED, so NO server-side status filter
    is sent (a server `status=DRAFT` would wrongly drop transient records)."""
    ctl, _ = mock_registry_clients
    service.list(lifecycle_state=LifecycleState.PROPOSED)
    kwargs = ctl.list_registry_records.call_args.kwargs
    assert [f["name"] for f in kwargs["filters"]] == ["recordType"]


def test_list_skips_malformed_record_and_continues(service, mock_registry_clients, sample_record, sample_envelope):
    """A record with a malformed/missing envelope must NOT abort the whole fan-out."""
    ctl, _ = mock_registry_clients
    ctl.list_registry_records.return_value = {
        "registryRecords": [
            {"recordId": "rec-bad", "name": "bad", "status": "DRAFT"},
            {"recordId": "rec-good", "name": "good", "status": "DRAFT"},
        ],
        "nextToken": None,
    }

    def _get(registryId, recordId):
        if recordId == "rec-bad":
            # Missing the custom envelope entirely.
            return sample_record(recordId="rec-bad", descriptors={})
        return sample_record(recordId="rec-good")

    ctl.get_registry_record.side_effect = _get

    agents = service.list()
    assert [a.id for a in agents] == ["rec-good"]


def test_list_skips_record_with_malformed_marketplace_block(
    service, mock_registry_clients, sample_record, sample_envelope
):
    """E33: a well-formed JSON envelope can still hold a structurally invalid ``marketplace``
    block, which ``from_record`` parses with ``MarketplacePublication.model_validate``. That
    pydantic ``ValidationError`` must reach ``list()`` as ``MalformedAgentRecordError`` —
    otherwise ONE bad record 500s the whole catalog (``/agents``, the marketplace product list
    and eligible-agents) instead of being skipped-and-warned."""
    ctl, _ = mock_registry_clients
    ctl.list_registry_records.return_value = {
        "registryRecords": [
            {"recordId": "rec-bad", "name": "bad", "status": "DRAFT"},
            {"recordId": "rec-good", "name": "good", "status": "DRAFT"},
        ],
        "nextToken": None,
    }

    def _get(registryId, recordId):
        if recordId == "rec-bad":
            # Envelope parses as JSON; the marketplace block does not validate.
            return sample_record(
                recordId="rec-bad",
                _envelope=sample_envelope(marketplace={"published": True}),
            )
        return sample_record(recordId="rec-good")

    ctl.get_registry_record.side_effect = _get

    agents = service.list()
    assert [a.id for a in agents] == ["rec-good"]


def test_get_raises_malformed_on_invalid_marketplace_block(
    service, mock_registry_clients, sample_record, sample_envelope
):
    """The same fault on a single-record read surfaces as the malformed-record error (which
    the caller maps), never as a raw pydantic ``ValidationError`` escaping the service."""
    from services.agent_registry_service import MalformedAgentRecordError

    ctl, _ = mock_registry_clients
    ctl.get_registry_record.return_value = sample_record(
        _envelope=sample_envelope(marketplace="not-a-publication")
    )
    with pytest.raises(MalformedAgentRecordError):
        service.get(RECORD_ID)


def test_list_paginates_across_pages(service, mock_registry_clients, sample_record):
    """The pagination loop follows nextToken until exhausted."""
    ctl, _ = mock_registry_clients
    ctl.list_registry_records.side_effect = [
        {"registryRecords": [{"recordId": "rec-1", "name": "a", "status": "DRAFT"}], "nextToken": "tok"},
        {"registryRecords": [{"recordId": "rec-2", "name": "b", "status": "DRAFT"}], "nextToken": None},
    ]
    ctl.get_registry_record.side_effect = lambda registryId, recordId: sample_record(recordId=recordId)

    agents = service.list()
    assert {a.id for a in agents} == {"rec-1", "rec-2"}
    assert ctl.list_registry_records.call_count == 2


# --- update ----------------------------------------------------------------

def test_update_read_modify_write(service, mock_registry_clients, sample_record):
    ctl, _ = mock_registry_clients
    ctl.get_registry_record.return_value = sample_record(status="DRAFT")

    updated = service.update(RECORD_ID, AgentUpdate(business_unit="Finance"))

    assert updated is not None
    assert updated.business_unit == "Finance"
    assert ctl.update_registry_record.called
    kwargs = ctl.update_registry_record.call_args.kwargs
    # UpdateRegistryRecord is a PATCH API: descriptors are wrapped in the
    # optionalValue envelope (custom nests two levels). See test_registry_update_param_shape.
    inline = kwargs["descriptors"]["optionalValue"]["custom"]["optionalValue"]["data"]["optionalValue"]
    envelope = json.loads(inline)
    assert envelope["business_unit"] == "Finance"


def test_update_preserves_untouched_fields_and_sends_description(service, mock_registry_clients, sample_record):
    """Read-modify-write must not drop untouched fields, and must send the
    (record) description from purpose."""
    ctl, _ = mock_registry_clients
    ctl.get_registry_record.return_value = sample_record(status="DRAFT")

    service.update(RECORD_ID, AgentUpdate(business_unit="Finance"))

    kwargs = ctl.update_registry_record.call_args.kwargs
    # description sent from the existing purpose, wrapped in the PATCH optionalValue
    # envelope (UpdateRegistryRecord is a PATCH API; create sends it flat).
    assert kwargs["description"] == {"optionalValue": "Triage inbound motor claims for the DE market"}
    # name preserved — and sent as a plain string (NOT wrapped).
    assert kwargs["name"] == "claims-triage-de"
    # untouched governance fields survive in the regenerated envelope.
    inline = kwargs["descriptors"]["optionalValue"]["custom"]["optionalValue"]["data"]["optionalValue"]
    envelope = json.loads(inline)
    assert envelope["sponsor_oid"] == "maria-oid"
    assert envelope["platform"] == "aws_bedrock"
    assert envelope["region"] == "DE"


def test_update_without_purpose_sends_name_as_description(service, mock_registry_clients, sample_record):
    """Regression (mirror of ``test_create_without_purpose_sends_nonempty_description``):
    UpdateRegistryRecord's ``description`` is min length 1, so a blank-purpose record must
    patch the name, not "". Sending "" raises ``ParamValidationError`` — which is NOT a
    ``ClientError``, so ``update()``'s handler cannot map it and it escapes as a raw 500."""
    ctl, _ = mock_registry_clients
    ctl.get_registry_record.return_value = sample_record(status="DRAFT", description="")

    service.update(RECORD_ID, AgentUpdate(business_unit="Finance"))

    kwargs = ctl.update_registry_record.call_args.kwargs
    assert kwargs["description"] == {"optionalValue": "claims-triage-de"}


def test_persist_identity_without_purpose_sends_name_as_description(service, mock_registry_clients, sample_record, sample_envelope):
    """The same guard on the second update call site. Tested separately from ``update()``
    because they are two independent literals: fixing one and not the other leaves the
    provisioning hook — the path with no ``except`` at all — still 500-ing."""
    ctl, _ = mock_registry_clients
    agent = Agent.from_record(sample_record(status="DRAFT", description=""), sample_envelope())
    assert agent.purpose == ""  # the precondition this test exists for

    service.persist_identity(agent)

    kwargs = ctl.update_registry_record.call_args.kwargs
    assert kwargs["description"] == {"optionalValue": "claims-triage-de"}


def test_update_returns_none_when_missing(service, mock_registry_clients):
    ctl, _ = mock_registry_clients
    ctl.get_registry_record.side_effect = _not_found()
    assert service.update("rec-missing", AgentUpdate(business_unit="Finance")) is None


def test_update_with_empty_purpose_falls_back_to_name_for_description(
    service, mock_registry_clients, sample_record
):
    """UpdateRegistryRecord rejects an empty description (min length 1), so a
    purpose-less agent must PATCH the same name fallback create() writes —
    the E29 live-test failure: {"optionalValue": ""} → ParamValidationError."""
    ctl, _ = mock_registry_clients
    ctl.get_registry_record.return_value = sample_record(description="")

    service.update(RECORD_ID, AgentUpdate(business_unit="Finance"))

    kwargs = ctl.update_registry_record.call_args.kwargs
    assert kwargs["description"] == {"optionalValue": "claims-triage-de"}


def test_persist_identity_with_empty_purpose_falls_back_to_name_for_description(
    service, mock_registry_clients, sample_record
):
    """The provisioning orchestrator hands persist_identity the IN-MEMORY agent
    from the create request, whose purpose can be "" even though create() wrote
    the name fallback into the record — the empty PATCH killed provisioning AND
    the failed-status persist (agent stuck 'pending'). E29 live fix."""
    ctl, _ = mock_registry_clients
    ctl.get_registry_record.return_value = sample_record(description="")
    agent = service.get(RECORD_ID)
    assert agent.purpose == ""  # the shape the live incident produced

    service.persist_identity(agent)

    kwargs = ctl.update_registry_record.call_args.kwargs
    assert kwargs["description"] == {"optionalValue": "claims-triage-de"}


# --- delete ----------------------------------------------------------------

def test_delete_returns_prior_agent(service, mock_registry_clients, sample_record):
    ctl, _ = mock_registry_clients
    ctl.get_registry_record.return_value = sample_record()
    prior = service.delete(RECORD_ID)
    assert prior is not None and prior.id == RECORD_ID
    ctl.delete_registry_record.assert_called_with(registryId=REGISTRY_ID, recordId=RECORD_ID)


def test_delete_returns_none_when_missing(service, mock_registry_clients):
    ctl, _ = mock_registry_clients
    ctl.get_registry_record.side_effect = _not_found()
    assert service.delete("rec-missing") is None
    ctl.delete_registry_record.assert_not_called()


# --- submit_for_approval ----------------------------------------------------

def test_submit_for_approval_calls_submit(service, mock_registry_clients, sample_record):
    ctl, _ = mock_registry_clients
    ctl.get_registry_record.return_value = sample_record(status="PENDING_APPROVAL")
    agent = service.submit_for_approval(RECORD_ID)
    ctl.submit_registry_record_for_approval.assert_called_with(
        registryId=REGISTRY_ID, recordId=RECORD_ID
    )
    assert agent is not None
    assert agent.lifecycle_state == LifecycleState.PENDING_APPROVAL


def test_submit_for_approval_returns_none_when_missing(service, mock_registry_clients):
    ctl, _ = mock_registry_clients
    ctl.submit_registry_record_for_approval.side_effect = _not_found("SubmitRegistryRecordForApproval")
    assert service.submit_for_approval("rec-missing") is None


# --- transition -------------------------------------------------------------

def test_transition_approve_calls_update_status(service, mock_registry_clients, sample_record):
    ctl, _ = mock_registry_clients
    ctl.get_registry_record.return_value = sample_record(status="APPROVED")
    agent = service.transition(RECORD_ID, "approve", reason="ok")
    ctl.update_registry_record_status.assert_called_with(
        registryId=REGISTRY_ID, recordId=RECORD_ID, status="APPROVED", statusReason="ok"
    )
    assert agent is not None
    assert agent.lifecycle_state == LifecycleState.APPROVED


def test_transition_bad_action_raises_value_error(service):
    with pytest.raises(ValueError):
        service.transition(RECORD_ID, "bogus", reason="x")


def test_transition_empty_reason_raises_value_error(service):
    with pytest.raises(ValueError):
        service.transition(RECORD_ID, "approve", reason="")


def test_transition_returns_none_when_missing(service, mock_registry_clients):
    ctl, _ = mock_registry_clients
    ctl.update_registry_record_status.side_effect = _not_found("UpdateRegistryRecordStatus")
    assert service.transition("rec-missing", "approve", reason="ok") is None


def test_transition_illegal_edge_raises_illegal_transition_error(service, mock_registry_clients):
    """A boto3 ValidationException for an illegal status edge (e.g. DRAFT->APPROVED,
    the live bug) is mapped to IllegalTransitionError, not a raw ClientError (research §5)."""
    from services.agent_registry_service import IllegalTransitionError

    ctl, _ = mock_registry_clients
    msg = (
        "Invalid status transition from DRAFT to APPROVED. Valid transitions from "
        "DRAFT: PENDING_APPROVAL, DEPRECATED, DRAFT, UPDATING"
    )
    ctl.update_registry_record_status.side_effect = ClientError(
        {"Error": {"Code": "ValidationException", "Message": msg}},
        "UpdateRegistryRecordStatus",
    )

    with pytest.raises(IllegalTransitionError) as exc:
        service.transition("rec-1", "approve", "ok")
    assert "Invalid status transition" in str(exc.value)


def test_transition_reraises_other_client_errors(service, mock_registry_clients):
    """A non-ValidationException, non-not-found ClientError still propagates raw."""
    ctl, _ = mock_registry_clients
    ctl.update_registry_record_status.side_effect = ClientError(
        {"Error": {"Code": "ThrottlingException", "Message": "slow down"}},
        "UpdateRegistryRecordStatus",
    )
    with pytest.raises(ClientError):
        service.transition("rec-1", "approve", "ok")


# --- envelope round-trip guard (Epic 12, Task 2) ---------------------------

def test_mcp_server_ids_round_trips_through_envelope():
    """A mutated ``mcp_server_ids`` survives ``to_envelope()`` -> ``from_record()``.

    Tasks 3/4 mutate this set on grant/revoke, so a persist->reload round-trip
    must preserve it. ``Agent`` requires ``lifecycle_state``/``created_at``/
    ``updated_at`` (no defaults); ``from_record`` reads the native ``recordId``/
    ``name``/``status``/``createdAt``/``updatedAt`` keys off the record dict.
    """
    from datetime import datetime

    from models.agent import Agent

    dt = datetime(2026, 6, 1, 10, 0, 0)
    a = Agent(
        id="x",
        name="n",
        mcp_server_ids=["m1", "m2"],
        lifecycle_state=LifecycleState.PROPOSED,
        created_at=dt,
        updated_at=dt,
    )

    env = a.to_envelope()
    assert env["mcp_server_ids"] == ["m1", "m2"]

    rebuilt = Agent.from_record(
        {
            "recordId": "x",
            "name": "n",
            "status": "DRAFT",
            "createdAt": dt,
            "updatedAt": dt,
        },
        env,
    )
    assert rebuilt.mcp_server_ids == ["m1", "m2"]


def test_model_id_round_trips_through_envelope():
    """model_id (E21) survives ``to_envelope()`` -> ``from_record()`` so the runtime
    build reads a non-empty model from the record envelope."""
    from datetime import datetime

    from models.agent import Agent

    dt = datetime(2026, 6, 1, 10, 0, 0)
    a = Agent(
        id="x",
        name="n",
        model_id="us.anthropic.claude-sonnet-4-6",
        lifecycle_state=LifecycleState.PROPOSED,
        created_at=dt,
        updated_at=dt,
    )

    env = a.to_envelope()
    assert env["model_id"] == "us.anthropic.claude-sonnet-4-6"

    rebuilt = Agent.from_record(
        {
            "recordId": "x",
            "name": "n",
            "status": "DRAFT",
            "createdAt": dt,
            "updatedAt": dt,
        },
        env,
    )
    assert rebuilt.model_id == "us.anthropic.claude-sonnet-4-6"


def test_from_record_tolerates_pre_e21_envelope_without_model_id():
    """A pre-E21 envelope lacking model_id hydrates as None (purely additive)."""
    from datetime import datetime

    from models.agent import Agent

    dt = datetime(2026, 6, 1, 10, 0, 0)
    rebuilt = Agent.from_record(
        {
            "recordId": "x",
            "name": "n",
            "status": "DRAFT",
            "createdAt": dt,
            "updatedAt": dt,
        },
        {},  # legacy envelope: no model_id key
    )
    assert rebuilt.model_id is None


# --- tenant fields round-trip (Epic 24, Task 4) -----------------------------

def test_tenant_fields_round_trip_through_envelope():
    """tenant_id + published (E24) survive ``to_envelope()`` -> ``from_record()``."""
    from datetime import datetime

    from models.agent import Agent

    dt = datetime(2026, 6, 1, 10, 0, 0)
    a = Agent(
        id="x",
        name="n",
        tenant_id="ten-1",
        published=True,
        lifecycle_state=LifecycleState.PROPOSED,
        created_at=dt,
        updated_at=dt,
    )

    env = a.to_envelope()
    assert env["tenant_id"] == "ten-1"
    assert env["published"] is True

    rebuilt = Agent.from_record(
        {
            "recordId": "x",
            "name": "n",
            "status": "DRAFT",
            "createdAt": dt,
            "updatedAt": dt,
        },
        env,
    )
    assert rebuilt.tenant_id == "ten-1"
    assert rebuilt.published is True


def test_from_record_tolerates_pre_e23_envelope_without_tenant_fields():
    """A pre-E24 envelope lacking tenant_id/published hydrates to None/False
    (migration tolerance — ENVELOPE_SCHEMA_VERSION stays 1)."""
    from datetime import datetime

    from models.agent import Agent

    dt = datetime(2026, 6, 1, 10, 0, 0)
    rebuilt = Agent.from_record(
        {
            "recordId": "x",
            "name": "n",
            "status": "DRAFT",
            "createdAt": dt,
            "updatedAt": dt,
        },
        {},  # legacy envelope: no tenant keys
    )
    assert rebuilt.tenant_id is None
    assert rebuilt.published is False


def test_create_envelope_carries_tenant_fields(service, mock_registry_clients):
    """The CREATE envelope's custom `data` blob carries the caller-picked tenant_id
    (AgentCreate → Agent → to_envelope, no extra service wiring)."""
    ctl, _ = mock_registry_clients
    service.create(AgentCreate(tenant_id="ten-claims", name="claims-triage-de"))
    inline = ctl.create_registry_record.call_args.kwargs["descriptors"]["custom"]["data"]
    envelope = json.loads(inline)
    assert envelope["tenant_id"] == "ten-claims"
    assert envelope["published"] is False


def test_create_envelope_carries_databricks_fields(service, mock_registry_clients):
    """E29/T5, C-4: the CREATE envelope's inlineContent carries the caller-picked
    Databricks runtime fields (AgentCreate → Agent → to_envelope). This is the write
    side of the additive fence — a registration that names a Databricks app must not
    lose its handle between the request body and the stored record."""
    ctl, _ = mock_registry_clients
    service.create(
        AgentCreate(
            tenant_id="ten-claims",
            name="claims-triage-db",
            platform=Platform.DATABRICKS,
            auth_type=AuthType.ENTRA,
            runtime_handle="https://dbc-test.cloud.databricks.com/apps/claims-triage",
            runtime_kind="app",
            binding_mode="federation",
        )
    )
    inline = ctl.create_registry_record.call_args.kwargs["descriptors"]["custom"]["data"]
    envelope = json.loads(inline)
    assert envelope["runtime_handle"] == "https://dbc-test.cloud.databricks.com/apps/claims-triage"
    assert envelope["runtime_kind"] == "app"
    assert envelope["binding_mode"] == "federation"
    # the sp_secret-mode fields are SERVICE-written (T6), so a create leaves them null
    assert envelope["databricks_sp_id"] is None
    assert envelope["databricks_sp_secret_arn"] is None
    assert envelope["oauth2_app_client_id"] is None


# --- create-hook provisioning dispatch (E29/T5, contract C-4) ----------------
#
# The route's `if is_agentcore / elif is_databricks_governed / else nothing` dispatch.
# Exercised by calling the `create_agent` coroutine DIRECTLY with a real
# `BackgroundTasks()` and inspecting what got SCHEDULED — "schedule provision" is
# literally a `background_tasks.add_task` entry, and asserting on it needs no HTTP
# scaffolding. (The TestClient-driven agentcore-path test lives in
# tests/test_grants_routes.py and stays as it is.)

def _dispatch_agent(**overrides):
    """A registry-shaped Agent for the create hook to dispatch on."""
    from datetime import datetime, timezone

    from models.agent import Agent, Origin

    now = datetime.now(timezone.utc)
    base = dict(
        id="rec-dispatch",
        name="claims-triage",
        purpose="Triage claims",
        lifecycle_state=LifecycleState.PROPOSED,
        origin=Origin.REGISTERED,
        tenant_id="ten-1",
        identity_status="pending",
        created_at=now,
        updated_at=now,
    )
    base.update(overrides)
    return Agent(**base)


def _agentcore_agent():
    return _dispatch_agent(
        platform=Platform.AWS_BEDROCK,
        auth_type=AuthType.ENTRA,
        agent_arn="arn:aws:bedrock-agentcore:eu-central-1:123456789012:runtime/claims-triage",
    )


def _databricks_agent():
    return _dispatch_agent(
        platform=Platform.DATABRICKS,
        auth_type=AuthType.ENTRA,
        runtime_handle="https://dbc-test.cloud.databricks.com/apps/claims-triage",
        runtime_kind="app",
        binding_mode="federation",
    )


async def _run_create_hook(created_agent):
    """Drive `agents.create_agent` with mocked singletons; return the scheduled tasks.

    Returns ``(tasks, identity_svc)`` where ``tasks`` is the BackgroundTasks list the
    route built. Every dependency is passed explicitly (the route is a plain coroutine
    once you supply its Depends-injected args), so no auth/HTTP layer is involved.
    """
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, MagicMock, patch

    from fastapi import BackgroundTasks

    import api.routes.agents as agents_module
    import api.routes.tenants as tenants_module
    from core.rbac import Principal, Role
    from services.tenant_resolver import TenantContext

    registry = MagicMock()
    registry.create.return_value = created_agent
    identity = MagicMock()
    identity.provision = AsyncMock(return_value=created_agent)
    langfuse = MagicMock()

    tenants_module._svc = SimpleNamespace(get=lambda tid: SimpleNamespace(id=tid))
    tasks = BackgroundTasks()
    principal = Principal(
        oid="oid-1", email="maria.bauer@example.com", role=Role.OPERATOR, raw_claims={}
    )
    ctx = TenantContext(is_global=True, tenant_ids=frozenset(), tenants=())

    with patch.object(agents_module, "_svc", registry), \
            patch.object(agents_module, "_identity_svc", identity), \
            patch.object(agents_module, "_langfuse_svc", langfuse):
        returned = await agents_module.create_agent(
            AgentCreate(name=created_agent.name, tenant_id="ten-1"),
            tasks,
            principal=principal,
            ctx=ctx,
        )

    assert returned is created_agent
    return tasks, identity


def _scheduled_provision_calls(tasks, identity):
    """The subset of scheduled background tasks that are the identity provision call."""
    return [t for t in tasks.tasks if t.func is identity.provision]


@pytest.mark.asyncio
async def test_create_hook_schedules_provision_for_agentcore_agent():
    """Today's behavior, unchanged: an AgentCore agent (arn + ENTRA + AWS_BEDROCK)
    gets exactly one background provision, with the created agent as its argument."""
    agent = _agentcore_agent()
    tasks, identity = await _run_create_hook(agent)
    calls = _scheduled_provision_calls(tasks, identity)
    assert len(calls) == 1
    assert calls[0].args == (agent,)


@pytest.mark.asyncio
async def test_create_hook_schedules_provision_for_databricks_governed_agent():
    """E29/T5: a Databricks-governed agent (handle + ENTRA + DATABRICKS) gets the SAME
    background provision call shape. provision() runs the platform-neutral
    provision_identity first; T6 adds the Databricks runtime half behind it."""
    agent = _databricks_agent()
    tasks, identity = await _run_create_hook(agent)
    calls = _scheduled_provision_calls(tasks, identity)
    assert len(calls) == 1
    assert calls[0].args == (agent,)


@pytest.mark.asyncio
async def test_create_hook_schedules_no_provision_for_ungoverned_agent():
    """Neither gate → NO provisioning (today's behavior for a metadata-only record).
    A Databricks record with no runtime_handle, and an AWS record with no ARN, both
    land here — being on a governed PLATFORM is not itself a licence to provision."""
    for agent in (
        _dispatch_agent(),  # no platform, no auth, no handle
        _dispatch_agent(platform=Platform.DATABRICKS, auth_type=AuthType.ENTRA),  # no handle
        _dispatch_agent(platform=Platform.AWS_BEDROCK, auth_type=AuthType.ENTRA),  # no arn
        _databricks_agent().model_copy(update={"auth_type": AuthType.API_KEY}),  # not Entra
    ):
        tasks, identity = await _run_create_hook(agent)
        assert _scheduled_provision_calls(tasks, identity) == []
        identity.provision.assert_not_called()

# ---------------------------------------------------------------------------
# E32 — agent-registry namespace + record-schema migration
# ---------------------------------------------------------------------------

def test_create_sends_new_namespace_shape(service, mock_registry_clients):
    """CreateRegistryRecord uses recordType + descriptors.custom.data (E32)."""
    ctl, _ = mock_registry_clients
    service.create(
        AgentCreate(name="a1", purpose="p", platform=Platform.AWS_BEDROCK, tenant_id="default")
    )

    kwargs = ctl.create_registry_record.call_args.kwargs
    assert kwargs["recordType"] == "CUSTOM"
    assert kwargs["name"] == "a1"
    assert kwargs["displayName"] == "a1"
    assert "descriptorType" not in kwargs
    assert "inlineContent" not in json.dumps(kwargs["descriptors"])
    envelope = json.loads(kwargs["descriptors"]["custom"]["data"])
    assert envelope["agent_id"] == ""


def test_list_uses_structured_filters(service, mock_registry_clients):
    """ListRegistryRecords filters via filters=[{name,values}] (E32)."""
    ctl, _ = mock_registry_clients
    list(service._iter_record_summaries(status="APPROVED"))

    kwargs = ctl.list_registry_records.call_args.kwargs
    assert kwargs["filters"] == [
        {"name": "recordType", "values": ["CUSTOM"]},
        {"name": "status", "values": ["APPROVED"]},
    ]
    assert "descriptorType" not in kwargs
    assert "status" not in kwargs


def test_from_record_reads_display_name(sample_record, sample_envelope):
    """Native displayName WINS over the record's name (E32).

    The two values must DIFFER here or the test is a tautology: the shared conftest
    record sets ``name == displayName``, so an assertion against that fixture passes
    whether ``from_record`` reads ``displayName`` or ``name`` — it would not have failed
    had the feature never been implemented. Overridden per-test rather than in
    ``conftest`` because the ``name == displayName`` record is the realistic post-create
    shape that the rest of the suite (and Task 3) relies on.

    ``name`` is the registry's unique dedup key; ``displayName`` is the human-facing
    label. The fall-back direction (no ``displayName`` at all → use ``name``, for records
    written before the migration) is covered by ``test_agent_lifecycle``'s ``_fake_record``.
    """
    record = sample_record(name="dedup-key", displayName="Human Label")
    assert Agent.from_record(record, sample_envelope()).name == "Human Label"


def test_update_sends_display_name(service, mock_registry_clients, sample_record):
    """A rename must patch displayName too, or reads silently resolve the stale one (E32).

    Regression guard: reads prefer displayName, so updating `name` alone makes a
    rename revert on the next read.
    """
    ctl, _ = mock_registry_clients
    ctl.get_registry_record.return_value = sample_record(status="DRAFT")

    service.update(RECORD_ID, AgentUpdate(name="new-name"))

    kwargs = ctl.update_registry_record.call_args.kwargs
    # name stays a PLAIN string; displayName takes the one-level PATCH envelope.
    assert kwargs["name"] == "new-name"
    assert kwargs["displayName"] == {"optionalValue": "new-name"}


def test_persist_identity_sends_display_name(
    service, mock_registry_clients, sample_record, sample_envelope
):
    """persist_identity envelope-writes the whole agent, so it owns displayName too (E32)."""
    ctl, _ = mock_registry_clients
    agent = Agent.from_record(sample_record(status="DRAFT"), sample_envelope())

    service.persist_identity(agent)

    kwargs = ctl.update_registry_record.call_args.kwargs
    assert kwargs["name"] == agent.name
    assert kwargs["displayName"] == {"optionalValue": agent.name}


# ---------------------------------------------------------------------------
# E33 — marketplace publication block (contract C2)
# ---------------------------------------------------------------------------

DECLARED_AT = datetime(2026, 8, 10, 12, 0, 0, tzinfo=timezone.utc)


def _publication(published: bool = True):
    """A MarketplacePublication with a fully-populated declared datasheet."""
    from models.marketplace import Datasheet, MarketplacePublication

    return MarketplacePublication(
        published=published,
        datasheet=Datasheet(
            owner_team="Claims Automation",
            support_contact="claims-automation@acme.com",
            data_classification="Confidential",
            sla_tier="Gold",
            compliance=["GDPR", "BaFin"],
            support_hours="24/7",
            version="1.8.0",
            region="EU (Frankfurt)",
            guardrails=["PII redaction"],
            pitch="Automates first notification of loss intake.",
        ),
        declared_by="admin@acme.com",
        declared_at=DECLARED_AT,
    )


def _envelope_from_update(ctl) -> dict:
    """Parse the envelope out of the LAST UpdateRegistryRecord call's three-level
    ``optionalValue`` descriptors wrap (see test_registry_update_param_shape)."""
    kwargs = ctl.update_registry_record.call_args.kwargs
    inline = kwargs["descriptors"]["optionalValue"]["custom"]["optionalValue"]["data"]["optionalValue"]
    return json.loads(inline)


def test_marketplace_block_round_trips_through_envelope():
    """The declared publication (E33) survives ``to_envelope()`` -> ``from_record()``,
    nested datasheet and attestation fields included."""
    dt = datetime(2026, 6, 1, 10, 0, 0)
    a = Agent(
        id="x",
        name="n",
        marketplace=_publication(),
        lifecycle_state=LifecycleState.PROPOSED,
        created_at=dt,
        updated_at=dt,
    )

    env = a.to_envelope()
    # Serialized JSON-safe (mode="json"), so the nested datetime is a string.
    assert env["marketplace"]["published"] is True
    assert env["marketplace"]["datasheet"]["owner_team"] == "Claims Automation"
    assert env["marketplace"]["declared_by"] == "admin@acme.com"
    assert isinstance(env["marketplace"]["declared_at"], str)
    json.dumps(env)  # the envelope must stay json.dumps-able

    rebuilt = Agent.from_record(
        {
            "recordId": "x",
            "name": "n",
            "status": "DRAFT",
            "createdAt": dt,
            "updatedAt": dt,
        },
        env,
    )
    assert rebuilt.marketplace is not None
    assert rebuilt.marketplace.published is True
    assert rebuilt.marketplace.datasheet.compliance == ["GDPR", "BaFin"]
    assert rebuilt.marketplace.declared_by == "admin@acme.com"
    assert rebuilt.marketplace.declared_at == DECLARED_AT


def test_from_record_tolerates_pre_e33_envelope_without_marketplace():
    """A pre-E33 envelope lacking the key hydrates to None (migration tolerance —
    ENVELOPE_SCHEMA_VERSION stays 1). An explicit stored ``null`` is tolerated too."""
    dt = datetime(2026, 6, 1, 10, 0, 0)
    record = {
        "recordId": "x",
        "name": "n",
        "status": "DRAFT",
        "createdAt": dt,
        "updatedAt": dt,
    }
    assert Agent.from_record(record, {}).marketplace is None
    assert Agent.from_record(record, {"marketplace": None}).marketplace is None


def test_marketplace_is_not_settable_by_a_request_body():
    """The block is SERVICE-WRITTEN (the ``project_id`` convention): it lives on ``Agent``
    + the envelope ONLY, so a body key of that name is dropped by pydantic's default
    ``extra="ignore"`` on both create and update — a caller can never forge an attestation."""
    from models.agent import AgentCreate, AgentUpdate

    created = AgentCreate.model_validate(
        {"name": "a", "tenant_id": "default", "marketplace": {"published": True}}
    )
    assert not hasattr(created, "marketplace")
    updated = AgentUpdate.model_validate({"marketplace": {"published": True}})
    assert not hasattr(updated, "marketplace")


def test_update_preserves_marketplace_block(service, mock_registry_clients, sample_record, sample_envelope):
    """The silently-destroyed-key trap: an ordinary ``update()`` on an UNRELATED field
    re-serializes the whole envelope, so the marketplace key must survive it. A key absent
    from ``to_envelope()`` would be discarded here with no error at all."""
    ctl, _ = mock_registry_clients
    env = sample_envelope(
        marketplace=json.loads(_publication().model_dump_json())
    )
    ctl.get_registry_record.return_value = sample_record(status="DRAFT", _envelope=env)

    updated = service.update(RECORD_ID, AgentUpdate(business_unit="Finance"))

    assert updated is not None and updated.business_unit == "Finance"
    envelope = _envelope_from_update(ctl)
    assert envelope["marketplace"]["published"] is True
    assert envelope["marketplace"]["datasheet"]["owner_team"] == "Claims Automation"
    assert envelope["marketplace"]["declared_by"] == "admin@acme.com"


def test_persist_marketplace_writes_block_through_patch_envelope(
    service, mock_registry_clients, sample_record, sample_envelope
):
    """``persist_marketplace`` is the service-only write (mirrors ``persist_identity``):
    read-modify-write through the three-level descriptors wrap."""
    ctl, _ = mock_registry_clients
    ctl.get_registry_record.return_value = sample_record(status="DRAFT")

    result = service.persist_marketplace(RECORD_ID, _publication())

    assert result.marketplace is not None and result.marketplace.published is True
    kwargs = ctl.update_registry_record.call_args.kwargs
    # name stays a PLAIN string; displayName + description take the one-level wrap.
    assert kwargs["name"] == "claims-triage-de"
    assert kwargs["displayName"] == {"optionalValue": "claims-triage-de"}
    assert kwargs["description"] == {
        "optionalValue": "Triage inbound motor claims for the DE market"
    }
    envelope = _envelope_from_update(ctl)
    assert envelope["marketplace"]["published"] is True
    assert envelope["marketplace"]["datasheet"]["support_contact"] == "claims-automation@acme.com"
    # Untouched governance fields survive the regenerated envelope.
    assert envelope["sponsor_oid"] == "maria-oid"
    assert envelope["region"] == "DE"


def test_persist_marketplace_unpublish_keeps_block_with_published_false(
    service, mock_registry_clients, sample_record, sample_envelope
):
    """Unpublish KEEPS the declared block (history retained) with ``published=False`` —
    it does NOT clear it to None."""
    ctl, _ = mock_registry_clients
    env = sample_envelope(marketplace=json.loads(_publication().model_dump_json()))
    ctl.get_registry_record.return_value = sample_record(status="APPROVED", _envelope=env)

    result = service.persist_marketplace(RECORD_ID, _publication(published=False))

    assert result.marketplace is not None and result.marketplace.published is False
    envelope = _envelope_from_update(ctl)
    assert envelope["marketplace"]["published"] is False
    assert envelope["marketplace"]["datasheet"]["owner_team"] == "Claims Automation"


def test_persist_marketplace_none_clears_the_block(
    service, mock_registry_clients, sample_record, sample_envelope
):
    """``None`` clears the block outright (the key is written as ``null``, not omitted —
    omitting it would leave the stale block in place on a later read)."""
    ctl, _ = mock_registry_clients
    env = sample_envelope(marketplace=json.loads(_publication().model_dump_json()))
    ctl.get_registry_record.return_value = sample_record(status="APPROVED", _envelope=env)

    result = service.persist_marketplace(RECORD_ID, None)

    assert result.marketplace is None
    envelope = _envelope_from_update(ctl)
    assert "marketplace" in envelope
    assert envelope["marketplace"] is None


def test_persist_marketplace_raises_when_agent_missing(service, mock_registry_clients):
    """A missing agent is an AgentNotFoundError, not a silent None: the marketplace
    service maps it to a fixed-literal 404/502 rather than persisting an APPROVED
    publish request whose registry write never landed."""
    from services.agent_registry_service import AgentNotFoundError

    ctl, _ = mock_registry_clients
    ctl.get_registry_record.side_effect = _not_found()

    with pytest.raises(AgentNotFoundError):
        service.persist_marketplace("rec-missing", _publication())

    ctl.update_registry_record.assert_not_called()


def test_persist_marketplace_bumps_updated_at(service, mock_registry_clients, sample_record):
    ctl, _ = mock_registry_clients
    ctl.get_registry_record.return_value = sample_record(status="DRAFT")
    before = service.get(RECORD_ID).updated_at

    result = service.persist_marketplace(RECORD_ID, _publication())

    assert result.updated_at > before


# ---------------------------------------------------------------------------
# E33 Amendment 2 — deprecate ⇒ unlist (contract C12)
# ---------------------------------------------------------------------------

def _method_call_names(ctl) -> list[str]:
    """The ordered method names called on the control client (for ordering pins)."""
    return [call[0] for call in ctl.method_calls]


def test_transition_deprecate_unlists_a_published_marketplace_product(
    service, mock_registry_clients, sample_record, sample_envelope
):
    """C12: deprecating a PUBLISHED product unlists it — one write per concern, lifecycle
    status FIRST and the unlist SECOND, with the declared datasheet kept as history."""
    ctl, _ = mock_registry_clients
    env = sample_envelope(marketplace=json.loads(_publication().model_dump_json()))
    ctl.get_registry_record.return_value = sample_record(status="DEPRECATED", _envelope=env)

    agent = service.transition(RECORD_ID, "deprecate", reason="superseded by v2")

    ctl.update_registry_record_status.assert_called_with(
        registryId=REGISTRY_ID,
        recordId=RECORD_ID,
        status="DEPRECATED",
        statusReason="superseded by v2",
    )
    # Ordering pin: the lifecycle write lands BEFORE the unlist envelope write.
    names = _method_call_names(ctl)
    assert names.index("update_registry_record_status") < names.index("update_registry_record")

    envelope = _envelope_from_update(ctl)
    assert envelope["marketplace"]["published"] is False
    # The declared datasheet is HISTORY: the block is kept, never cleared to None.
    assert envelope["marketplace"]["datasheet"]["owner_team"] == "Claims Automation"
    assert envelope["marketplace"]["datasheet"]["support_contact"] == "claims-automation@acme.com"
    assert envelope["marketplace"]["datasheet"]["compliance"] == ["GDPR", "BaFin"]
    assert envelope["marketplace"]["declared_by"] == "admin@acme.com"
    assert envelope["marketplace"]["declared_at"] is not None

    # The returned record reflects the unlist (the route hands this straight back).
    assert agent is not None
    assert agent.lifecycle_state == LifecycleState.DEPRECATED
    assert agent.marketplace is not None and agent.marketplace.published is False
    assert agent.marketplace.datasheet.owner_team == "Claims Automation"


def test_transition_deprecate_never_published_writes_no_marketplace_block(
    service, mock_registry_clients, sample_record
):
    """No marketplace block at all → nothing to unlist, so no envelope write happens
    (a spurious write would invent an attestation the publisher never made)."""
    ctl, _ = mock_registry_clients
    ctl.get_registry_record.return_value = sample_record(status="DEPRECATED")

    agent = service.transition(RECORD_ID, "deprecate", reason="eol")

    ctl.update_registry_record.assert_not_called()
    assert agent is not None and agent.marketplace is None


def test_transition_deprecate_already_unpublished_writes_no_marketplace_block(
    service, mock_registry_clients, sample_record, sample_envelope
):
    """Already ``published=False`` → idempotent no-op: the unlist is skipped entirely."""
    ctl, _ = mock_registry_clients
    env = sample_envelope(
        marketplace=json.loads(_publication(published=False).model_dump_json())
    )
    ctl.get_registry_record.return_value = sample_record(status="DEPRECATED", _envelope=env)

    agent = service.transition(RECORD_ID, "deprecate", reason="eol")

    ctl.update_registry_record.assert_not_called()
    assert agent is not None and agent.marketplace is not None
    assert agent.marketplace.published is False


@pytest.mark.parametrize("action,status", [("approve", "APPROVED"), ("reject", "REJECTED")])
def test_transition_approve_and_reject_never_touch_the_marketplace_block(
    service, mock_registry_clients, sample_record, sample_envelope, action, status
):
    """ONLY a DEPRECATED target unlists. This is also what makes a deprecation STICK: a
    later lifecycle re-approve must NOT re-list the product — only a fresh publish
    request can."""
    ctl, _ = mock_registry_clients
    env = sample_envelope(
        marketplace=json.loads(_publication(published=False).model_dump_json())
    )
    ctl.get_registry_record.return_value = sample_record(status=status, _envelope=env)

    agent = service.transition(RECORD_ID, action, reason="ok")

    ctl.update_registry_record.assert_not_called()
    assert agent is not None and agent.marketplace is not None
    assert agent.marketplace.published is False


def test_transition_deprecate_raises_when_the_unlist_write_fails(
    service, mock_registry_clients, sample_record, sample_envelope
):
    """An unlist failure RAISES (the existing route error mapping surfaces it) rather than
    being swallowed. The lifecycle write already landed, so the record is deprecated but
    still flagged published — which the read-path lifecycle gate refuses to list anyway
    (defense in depth); the retry path is transition-again or an admin unpublish."""
    ctl, _ = mock_registry_clients
    env = sample_envelope(marketplace=json.loads(_publication().model_dump_json()))
    ctl.get_registry_record.return_value = sample_record(status="DEPRECATED", _envelope=env)
    ctl.update_registry_record.side_effect = ClientError(
        {"Error": {"Code": "ThrottlingException", "Message": "slow down"}},
        "UpdateRegistryRecord",
    )

    with pytest.raises(ClientError):
        service.transition(RECORD_ID, "deprecate", reason="superseded by v2")

    ctl.update_registry_record_status.assert_called_once()
    ctl.update_registry_record.assert_called_once()


def test_transition_deprecate_returns_none_when_the_record_vanished(
    service, mock_registry_clients
):
    """The status write succeeded but the re-read 404s: still ``None`` (the pre-C12
    contract), and no unlist is attempted on a record we could not hydrate."""
    ctl, _ = mock_registry_clients
    ctl.get_registry_record.side_effect = _not_found()

    assert service.transition(RECORD_ID, "deprecate", reason="eol") is None
    ctl.update_registry_record.assert_not_called()
