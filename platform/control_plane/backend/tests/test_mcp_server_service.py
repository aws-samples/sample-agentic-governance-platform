"""Tests for McpServerRegistryService (Epic 5, Task 3).

Strategy (research §10): no moto — inject ``unittest.mock.MagicMock`` boto3
clients via the service constructor. Fixtures live in ``conftest.py``. This is a
structural clone of ``test_agent_registry_service.py`` with the MCP payload, the
``kind`` client-side filter, the new ``McpValidationError`` (schema-validation
→ 422), and the poll-to-DRAFT (research §6.2).
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone

import pytest
from botocore.exceptions import ClientError

from models.marketplace import Datasheet, MarketplacePublication
from models.mcp_server import Kind, McpServerCreate, McpServerUpdate, McpTool, LifecycleState
from services.mcp_server_service import (
    IllegalTransitionError,
    MalformedMcpRecordError,
    McpServerNotFoundError,
    McpServerRegistryService,
    McpValidationError,
    NameTakenError,
)

from conftest import REGISTRY_ID, MCP_RECORD_ID


def _not_found(op: str = "GetRegistryRecord") -> ClientError:
    return ClientError(
        {"Error": {"Code": "ResourceNotFoundException", "Message": "x"}}, op
    )


def _schema_validation(op: str) -> ClientError:
    return ClientError(
        {
            "Error": {
                "Code": "ValidationException",
                "Message": (
                    "Schema validation failed: content is not in compliance with "
                    "schema version '2025-12-11' for descriptor type 'mcp'."
                ),
            }
        },
        op,
    )


# --- create ----------------------------------------------------------------

def test_create_calls_create_registry_record_with_mcp_descriptor(mcp_service, mcp_mock_registry_clients):
    ctl, _ = mcp_mock_registry_clients
    req = McpServerCreate(
        name="internal-claims-mcp",
        description="Read-only claims access",
        kind=Kind.STANDARD,
        owner_oid="maria-oid",
        owner_email="maria.bauer@example.com",
        business_unit="Claims",
        region="DE",
        endpoint_url="https://mcp.claims.acme.internal/mcp",
        tenant_id="default",
    )

    mcp = mcp_service.create(req, created_by="lars.svensson@example.com")

    assert ctl.create_registry_record.called
    kwargs = ctl.create_registry_record.call_args.kwargs
    assert kwargs["registryId"] == REGISTRY_ID
    assert kwargs["name"] == "internal-claims-mcp"
    assert kwargs["recordType"] == "MCP"

    server_descriptor = kwargs["descriptors"]["mcpServer"]
    assert server_descriptor["dataSchemaVersion"] == "2025-12-11"
    server_json = json.loads(server_descriptor["data"])
    assert server_json["name"] == "agp/internal-claims-mcp"
    gov = server_json["_meta"]["com.agp/governance"]
    assert gov["business_unit"] == "Claims"
    assert gov["created_by"] == "lars.svensson@example.com"
    assert gov["schema_version"] == 1

    assert mcp.id == MCP_RECORD_ID
    assert mcp.lifecycle_state == LifecycleState.PROPOSED  # DRAFT after poll
    assert mcp.created_by == "lars.svensson@example.com"


def test_create_omits_tools_branch_when_no_tools(mcp_service, mcp_mock_registry_clients):
    ctl, _ = mcp_mock_registry_clients
    mcp_service.create(McpServerCreate(name="server-only-mcp", tenant_id="default", available_tools=[]))
    kwargs = ctl.create_registry_record.call_args.kwargs
    # E32: tools live under mcpServer.additionalData.tools, so a server-only record omits
    # the whole additionalData branch.
    assert "additionalData" not in kwargs["descriptors"]["mcpServer"]


def test_create_includes_tools_branch_when_present(mcp_service, mcp_mock_registry_clients):
    ctl, _ = mcp_mock_registry_clients
    mcp_service.create(
        McpServerCreate(
            name="claims-mcp",
            tenant_id="default",
            available_tools=[
                McpTool(name="get_claim", description="Fetch", input_schema={"type": "object"})
            ],
        )
    )
    kwargs = ctl.create_registry_record.call_args.kwargs
    tools_descriptor = kwargs["descriptors"]["mcpServer"]["additionalData"]["tools"]
    assert tools_descriptor["dataSchemaVersion"] == "2025-11-25"
    payload = json.loads(tools_descriptor["data"])
    assert payload["tools"][0]["name"] == "get_claim"
    assert payload["tools"][0]["inputSchema"] == {"type": "object"}


def test_create_polls_record_to_draft(mcp_service, mcp_mock_registry_clients, mcp_sample_record, monkeypatch):
    ctl, _ = mcp_mock_registry_clients
    # Don't actually sleep between the CREATING and DRAFT polls.
    monkeypatch.setattr(time, "sleep", lambda *_: None)
    # create returns CREATING (fixture default); poll sees CREATING then DRAFT.
    ctl.get_registry_record.side_effect = [
        mcp_sample_record(status="CREATING"),
        mcp_sample_record(status="DRAFT"),
    ]
    mcp = mcp_service.create(McpServerCreate(name="claims-mcp", tenant_id="default"))
    assert mcp.id == MCP_RECORD_ID
    assert ctl.get_registry_record.call_count >= 2


def test_poll_conflict_during_creating_keeps_polling(mcp_service, mcp_mock_registry_clients, mcp_sample_record):
    ctl, _ = mcp_mock_registry_clients
    # A ConflictException while the record is still CREATING is expected — keep polling.
    conflict = ClientError(
        {"Error": {"Code": "ConflictException", "Message": "record is CREATING"}},
        "GetRegistryRecord",
    )
    ctl.get_registry_record.side_effect = [conflict, mcp_sample_record(status="DRAFT")]
    status = mcp_service._poll_record_to_draft(MCP_RECORD_ID, attempts=3, delay=0)
    assert status == "DRAFT"
    assert ctl.get_registry_record.call_count == 2


def test_poll_times_out_raises(mcp_service, mcp_mock_registry_clients, mcp_sample_record):
    ctl, _ = mcp_mock_registry_clients
    # Never reaches DRAFT -> bounded loop exhausts and raises.
    ctl.get_registry_record.return_value = mcp_sample_record(status="CREATING")
    with pytest.raises(RuntimeError):
        mcp_service._poll_record_to_draft(MCP_RECORD_ID, attempts=2, delay=0)


def test_create_name_precheck_raises_name_taken(mcp_service, mcp_mock_registry_clients):
    ctl, _ = mcp_mock_registry_clients
    ctl.list_registry_records.return_value = {
        "registryRecords": [
            {"recordId": "mcp-existing", "name": "claims-mcp", "status": "DRAFT"}
        ],
        "nextToken": None,
    }
    with pytest.raises(NameTakenError):
        mcp_service.create(McpServerCreate(name="claims-mcp", tenant_id="default"))
    ctl.create_registry_record.assert_not_called()


def test_create_schema_validation_raises_mcp_validation_error(mcp_service, mcp_mock_registry_clients):
    ctl, _ = mcp_mock_registry_clients
    ctl.create_registry_record.side_effect = _schema_validation("CreateRegistryRecord")
    with pytest.raises(McpValidationError) as exc:
        mcp_service.create(McpServerCreate(name="bad-mcp", tenant_id="default"))
    # A schema-validation rejection on create must NOT be mapped to the transition error.
    assert not isinstance(exc.value, IllegalTransitionError)


def test_create_name_precheck_queries_by_name_and_mcp_type(mcp_service, mcp_mock_registry_clients):
    """E32: the discrete name/descriptorType query params became one structured filters list."""
    ctl, _ = mcp_mock_registry_clients
    mcp_service.create(McpServerCreate(name="fraud-mcp", tenant_id="default"))
    precheck = ctl.list_registry_records.call_args_list[0]
    assert precheck.kwargs["filters"] == [
        {"name": "name", "values": ["fraud-mcp"]},
        {"name": "recordType", "values": ["MCP"]},
    ]
    assert "descriptorType" not in precheck.kwargs
    assert "name" not in precheck.kwargs


# --- get -------------------------------------------------------------------

def test_get_parses_server_json_and_maps_status(mcp_service, mcp_mock_registry_clients, mcp_sample_record):
    ctl, _ = mcp_mock_registry_clients
    ctl.get_registry_record.return_value = mcp_sample_record(status="APPROVED")

    mcp = mcp_service.get(MCP_RECORD_ID)

    assert mcp is not None
    assert mcp.id == MCP_RECORD_ID
    assert mcp.description == "Read-only access to motor and property claims records for the DE market."
    assert mcp.version == "1.0.0"
    assert mcp.endpoint_url == "https://mcp.claims.acme.internal/mcp"
    assert mcp.kind == Kind.STANDARD
    assert mcp.lifecycle_state == LifecycleState.APPROVED
    ctl.get_registry_record.assert_called_with(registryId=REGISTRY_ID, recordId=MCP_RECORD_ID)


def test_get_returns_none_on_not_found(mcp_service, mcp_mock_registry_clients):
    ctl, _ = mcp_mock_registry_clients
    ctl.get_registry_record.side_effect = _not_found()
    assert mcp_service.get("mcp-missing") is None


def test_get_raises_on_malformed_record(mcp_service, mcp_mock_registry_clients, mcp_sample_record):
    ctl, _ = mcp_mock_registry_clients
    bad = mcp_sample_record()
    bad["descriptors"]["mcpServer"]["data"] = "{not valid json"
    ctl.get_registry_record.return_value = bad
    with pytest.raises(MalformedMcpRecordError):
        mcp_service.get(MCP_RECORD_ID)


def test_get_reraises_non_not_found_client_error(mcp_service, mcp_mock_registry_clients):
    ctl, _ = mcp_mock_registry_clients
    ctl.get_registry_record.side_effect = ClientError(
        {"Error": {"Code": "ThrottlingException", "Message": "slow down"}}, "GetRegistryRecord"
    )
    with pytest.raises(ClientError):
        mcp_service.get(MCP_RECORD_ID)


# --- list ------------------------------------------------------------------

def test_list_fans_out_and_filters_by_kind(mcp_service, mcp_mock_registry_clients, mcp_sample_record, mcp_sample_envelope):
    ctl, _ = mcp_mock_registry_clients
    ctl.list_registry_records.return_value = {
        "registryRecords": [
            {"recordId": "mcp-1", "name": "a", "status": "DRAFT"},
            {"recordId": "mcp-2", "name": "b", "status": "DRAFT"},
        ],
        "nextToken": None,
    }

    def _get(registryId, recordId):
        if recordId == "mcp-1":
            return mcp_sample_record(recordId="mcp-1", _envelope=mcp_sample_envelope(kind="gateway"))
        return mcp_sample_record(recordId="mcp-2", _envelope=mcp_sample_envelope(kind="standard"))

    ctl.get_registry_record.side_effect = _get

    all_mcp = mcp_service.list()
    assert len(all_mcp) == 2
    assert ctl.get_registry_record.call_count == 2

    gateways = mcp_service.list(kind="gateway")
    assert [m.id for m in gateways] == ["mcp-1"]


def test_list_filters_by_business_unit(mcp_service, mcp_mock_registry_clients, mcp_sample_record, mcp_sample_envelope):
    ctl, _ = mcp_mock_registry_clients
    ctl.list_registry_records.return_value = {
        "registryRecords": [
            {"recordId": "mcp-1", "name": "a", "status": "DRAFT"},
            {"recordId": "mcp-2", "name": "b", "status": "DRAFT"},
        ],
        "nextToken": None,
    }

    def _get(registryId, recordId):
        if recordId == "mcp-1":
            return mcp_sample_record(recordId="mcp-1", _envelope=mcp_sample_envelope(business_unit="Claims"))
        return mcp_sample_record(recordId="mcp-2", _envelope=mcp_sample_envelope(business_unit="Underwriting"))

    ctl.get_registry_record.side_effect = _get

    claims = mcp_service.list(business_unit="Claims")
    assert [m.id for m in claims] == ["mcp-1"]


def test_list_passes_descriptor_type_mcp(mcp_service, mcp_mock_registry_clients):
    """E32: the record-type filter moved into the structured filters list."""
    ctl, _ = mcp_mock_registry_clients
    mcp_service.list()
    kwargs = ctl.list_registry_records.call_args.kwargs
    assert kwargs["filters"] == [{"name": "recordType", "values": ["MCP"]}]
    assert "descriptorType" not in kwargs


def test_list_server_side_status_filter_for_one_to_one_lifecycle(mcp_service, mcp_mock_registry_clients):
    ctl, _ = mcp_mock_registry_clients
    mcp_service.list(lifecycle_state=LifecycleState.APPROVED)
    kwargs = ctl.list_registry_records.call_args.kwargs
    assert kwargs["filters"] == [
        {"name": "recordType", "values": ["MCP"]},
        {"name": "status", "values": ["APPROVED"]},
    ]
    assert "status" not in kwargs


def test_list_proposed_is_client_side_not_server_filtered(mcp_service, mcp_mock_registry_clients):
    ctl, _ = mcp_mock_registry_clients
    mcp_service.list(lifecycle_state=LifecycleState.PROPOSED)
    kwargs = ctl.list_registry_records.call_args.kwargs
    # No status leg in the filters list — PROPOSED maps from several native states, so it
    # is filtered client-side.
    assert kwargs["filters"] == [{"name": "recordType", "values": ["MCP"]}]
    assert "status" not in kwargs


def test_list_skips_malformed_record_and_continues(mcp_service, mcp_mock_registry_clients, mcp_sample_record):
    ctl, _ = mcp_mock_registry_clients
    ctl.list_registry_records.return_value = {
        "registryRecords": [
            {"recordId": "mcp-bad", "name": "bad", "status": "DRAFT"},
            {"recordId": "mcp-good", "name": "good", "status": "DRAFT"},
        ],
        "nextToken": None,
    }

    def _get(registryId, recordId):
        if recordId == "mcp-bad":
            bad = mcp_sample_record(recordId="mcp-bad")
            bad["descriptors"]["mcpServer"]["data"] = "{broken"
            return bad
        return mcp_sample_record(recordId="mcp-good")

    ctl.get_registry_record.side_effect = _get

    result = mcp_service.list()
    assert [m.id for m in result] == ["mcp-good"]


def test_list_paginates_across_pages(mcp_service, mcp_mock_registry_clients, mcp_sample_record):
    ctl, _ = mcp_mock_registry_clients
    ctl.list_registry_records.side_effect = [
        {"registryRecords": [{"recordId": "mcp-1", "name": "a", "status": "DRAFT"}], "nextToken": "tok"},
        {"registryRecords": [{"recordId": "mcp-2", "name": "b", "status": "DRAFT"}], "nextToken": None},
    ]
    ctl.get_registry_record.side_effect = lambda registryId, recordId: mcp_sample_record(recordId=recordId)

    result = mcp_service.list()
    assert {m.id for m in result} == {"mcp-1", "mcp-2"}
    assert ctl.list_registry_records.call_count == 2


# --- update ----------------------------------------------------------------

def test_update_read_modify_write(mcp_service, mcp_mock_registry_clients, mcp_sample_record):
    ctl, _ = mcp_mock_registry_clients
    ctl.get_registry_record.return_value = mcp_sample_record(status="DRAFT")

    updated = mcp_service.update(MCP_RECORD_ID, McpServerUpdate(description="New description"))

    assert updated is not None
    assert updated.description == "New description"
    assert ctl.update_registry_record.called
    kwargs = ctl.update_registry_record.call_args.kwargs
    # UpdateRegistryRecord is a PATCH API: descriptors are wrapped in the optionalValue
    # envelope at every level, and E32 made every LEAF its own wrapper too — so the server
    # blob sits three optionalValues deep. See test_registry_update_param_shape.
    server_descriptor = kwargs["descriptors"]["optionalValue"]["mcpServer"]["optionalValue"]
    server_json = json.loads(server_descriptor["data"]["optionalValue"])
    assert server_json["description"] == "New description"


def test_update_schema_validation_raises_mcp_validation_error(mcp_service, mcp_mock_registry_clients, mcp_sample_record):
    ctl, _ = mcp_mock_registry_clients
    ctl.get_registry_record.return_value = mcp_sample_record(status="DRAFT")
    ctl.update_registry_record.side_effect = _schema_validation("UpdateRegistryRecord")
    with pytest.raises(McpValidationError):
        mcp_service.update(MCP_RECORD_ID, McpServerUpdate(description="bad"))


def test_update_returns_none_when_missing(mcp_service, mcp_mock_registry_clients):
    ctl, _ = mcp_mock_registry_clients
    ctl.get_registry_record.side_effect = _not_found()
    assert mcp_service.update("mcp-missing", McpServerUpdate(description="x")) is None


# --- delete ----------------------------------------------------------------

def test_delete_returns_prior(mcp_service, mcp_mock_registry_clients, mcp_sample_record):
    ctl, _ = mcp_mock_registry_clients
    ctl.get_registry_record.return_value = mcp_sample_record()
    prior = mcp_service.delete(MCP_RECORD_ID)
    assert prior is not None and prior.id == MCP_RECORD_ID
    ctl.delete_registry_record.assert_called_with(registryId=REGISTRY_ID, recordId=MCP_RECORD_ID)


def test_delete_returns_none_when_missing(mcp_service, mcp_mock_registry_clients):
    ctl, _ = mcp_mock_registry_clients
    ctl.get_registry_record.side_effect = _not_found()
    assert mcp_service.delete("mcp-missing") is None
    ctl.delete_registry_record.assert_not_called()


# --- submit_for_approval ----------------------------------------------------

def test_submit_for_approval_calls_submit(mcp_service, mcp_mock_registry_clients, mcp_sample_record):
    ctl, _ = mcp_mock_registry_clients
    ctl.get_registry_record.return_value = mcp_sample_record(status="PENDING_APPROVAL")
    mcp = mcp_service.submit_for_approval(MCP_RECORD_ID)
    ctl.submit_registry_record_for_approval.assert_called_with(
        registryId=REGISTRY_ID, recordId=MCP_RECORD_ID
    )
    assert mcp is not None
    assert mcp.lifecycle_state == LifecycleState.PENDING_APPROVAL


def test_submit_for_approval_returns_none_when_missing(mcp_service, mcp_mock_registry_clients):
    ctl, _ = mcp_mock_registry_clients
    ctl.submit_registry_record_for_approval.side_effect = _not_found("SubmitRegistryRecordForApproval")
    assert mcp_service.submit_for_approval("mcp-missing") is None


# --- transition -------------------------------------------------------------

def test_transition_approve_calls_update_status(mcp_service, mcp_mock_registry_clients, mcp_sample_record):
    ctl, _ = mcp_mock_registry_clients
    ctl.get_registry_record.return_value = mcp_sample_record(status="APPROVED")
    mcp = mcp_service.transition(MCP_RECORD_ID, "approve", reason="ok")
    ctl.update_registry_record_status.assert_called_with(
        registryId=REGISTRY_ID, recordId=MCP_RECORD_ID, status="APPROVED", statusReason="ok"
    )
    assert mcp is not None
    assert mcp.lifecycle_state == LifecycleState.APPROVED


def test_transition_bad_action_raises_value_error(mcp_service):
    with pytest.raises(ValueError):
        mcp_service.transition(MCP_RECORD_ID, "bogus", reason="x")


def test_transition_empty_reason_raises_value_error(mcp_service):
    with pytest.raises(ValueError):
        mcp_service.transition(MCP_RECORD_ID, "approve", reason="")


def test_transition_illegal_edge_raises_illegal_transition_error(mcp_service, mcp_mock_registry_clients):
    ctl, _ = mcp_mock_registry_clients
    msg = (
        "Invalid status transition from DRAFT to APPROVED. Valid transitions from "
        "DRAFT: PENDING_APPROVAL, DEPRECATED, DRAFT, UPDATING"
    )
    ctl.update_registry_record_status.side_effect = ClientError(
        {"Error": {"Code": "ValidationException", "Message": msg}},
        "UpdateRegistryRecordStatus",
    )
    with pytest.raises(IllegalTransitionError) as exc:
        mcp_service.transition("mcp-1", "approve", "ok")
    assert "Invalid status transition" in str(exc.value)


def test_transition_returns_none_when_missing(mcp_service, mcp_mock_registry_clients):
    ctl, _ = mcp_mock_registry_clients
    ctl.update_registry_record_status.side_effect = _not_found("UpdateRegistryRecordStatus")
    assert mcp_service.transition("mcp-missing", "approve", reason="ok") is None


def test_transition_reraises_other_client_errors(mcp_service, mcp_mock_registry_clients):
    ctl, _ = mcp_mock_registry_clients
    ctl.update_registry_record_status.side_effect = ClientError(
        {"Error": {"Code": "ThrottlingException", "Message": "slow down"}},
        "UpdateRegistryRecordStatus",
    )
    with pytest.raises(ClientError):
        mcp_service.transition("mcp-1", "approve", "ok")


# --- persist_identity (Epic 7, T-IDENTITY) ----------------------------------


def _hydrated_mcp(mcp_service, mcp_mock_registry_clients, mcp_sample_record) -> "object":
    """Hydrate a real McpServer via get() so persist_identity has an in-hand record."""
    ctl, _ = mcp_mock_registry_clients
    ctl.get_registry_record.return_value = mcp_sample_record(status="DRAFT")
    return mcp_service.get(MCP_RECORD_ID)


def test_persist_identity_writes_mcp_server_descriptor_not_custom(
    mcp_service, mcp_mock_registry_clients, mcp_sample_record
):
    # CRITIC-I3 (clone trap): persist_identity MUST mirror update()'s mcpServer
    # descriptor rebuild — NOT the agent CUSTOM helper. The written descriptor must
    # carry the `mcpServer` branch (PATCH optionalValue shape), never a `custom` branch.
    ctl, _ = mcp_mock_registry_clients
    mcp = _hydrated_mcp(mcp_service, mcp_mock_registry_clients, mcp_sample_record)

    mcp.entra_sp_id = "sp-obj-id-xyz"
    mcp.identity_status = "provisioned"

    returned = mcp_service.persist_identity(mcp)

    assert returned is mcp
    assert ctl.update_registry_record.called
    kwargs = ctl.update_registry_record.call_args.kwargs
    descriptors = kwargs["descriptors"]
    # The PATCH envelope wraps the mcpServer branch (NOT custom).
    assert "optionalValue" in descriptors
    inner = descriptors["optionalValue"]
    assert "mcpServer" in inner
    assert "custom" not in inner
    # And it is the same mcpServer PATCH shape that update() produces.
    server_descriptor = inner["mcpServer"]["optionalValue"]
    assert "data" in server_descriptor
    # name is a plain string (not wrapped) — matches update().
    assert kwargs["name"] == mcp.name


def test_persist_identity_round_trips_identity_fields(
    mcp_service, mcp_mock_registry_clients, mcp_sample_record
):
    # The identity fields ride in the governance envelope inside server.json (via
    # to_server_json → to_envelope), so the T-MODEL round-trip carries them.
    ctl, _ = mcp_mock_registry_clients
    mcp = _hydrated_mcp(mcp_service, mcp_mock_registry_clients, mcp_sample_record)

    mcp.entra_app_id = "app-client-guid-xyz"
    mcp.entra_sp_id = "sp-obj-id-xyz"
    mcp.entra_app_audience = "api://agp-mcp-mcp-rec-123"
    mcp.invoker_role_id = "invoker-xyz"
    mcp.admin_role_id = "admin-xyz"
    mcp.gateway_id = "demo-gw-aBcDeFgHiJ"
    mcp.gateway_url = "https://demo-gw.gateway.bedrock-agentcore.us-east-1.amazonaws.com/mcp"
    mcp.identity_status = "provisioned"

    mcp_service.persist_identity(mcp)

    kwargs = ctl.update_registry_record.call_args.kwargs
    server_descriptor = kwargs["descriptors"]["optionalValue"]["mcpServer"]["optionalValue"]
    server_json = json.loads(server_descriptor["data"]["optionalValue"])
    gov = server_json["_meta"]["com.agp/governance"]

    assert gov["entra_app_id"] == "app-client-guid-xyz"
    assert gov["entra_sp_id"] == "sp-obj-id-xyz"
    assert gov["entra_app_audience"] == "api://agp-mcp-mcp-rec-123"
    assert gov["invoker_role_id"] == "invoker-xyz"
    assert gov["admin_role_id"] == "admin-xyz"
    assert gov["gateway_id"] == "demo-gw-aBcDeFgHiJ"
    assert gov["gateway_url"] == "https://demo-gw.gateway.bedrock-agentcore.us-east-1.amazonaws.com/mcp"
    assert gov["identity_status"] == "provisioned"


def test_persist_identity_preserves_tools_branch(
    mcp_service, mcp_mock_registry_clients, mcp_sample_record
):
    # The sample record carries tools; persist_identity rebuilds the descriptor via
    # tools_as_mcp() exactly like update(), so the tools branch survives the write.
    ctl, _ = mcp_mock_registry_clients
    mcp = _hydrated_mcp(mcp_service, mcp_mock_registry_clients, mcp_sample_record)
    mcp.identity_status = "provisioned"

    mcp_service.persist_identity(mcp)

    kwargs = ctl.update_registry_record.call_args.kwargs
    server_branch = kwargs["descriptors"]["optionalValue"]["mcpServer"]["optionalValue"]
    tools_branch = server_branch["additionalData"]["optionalValue"]["tools"]
    tools_payload = json.loads(tools_branch["optionalValue"]["data"]["optionalValue"])
    assert tools_payload["tools"][0]["name"] == "get_claim"


# --- persist_identity ConflictException retry (T-PERSIST-RETRY) -------------


def _conflict(op: str = "UpdateRegistryRecord") -> ClientError:
    """A ConflictException — the record is briefly UPDATING after a prior write/create
    (the E6 live seam: "Registry record cannot be modified while in UPDATING state")."""
    return ClientError(
        {
            "Error": {
                "Code": "ConflictException",
                "Message": (
                    "Registry record cannot be modified while in UPDATING state."
                ),
            }
        },
        op,
    )


def test_persist_identity_retries_on_conflict_then_succeeds(
    mcp_service, mcp_mock_registry_clients, mcp_sample_record, monkeypatch
):
    # The UpdateRegistryRecord write races a prior write's UPDATING transition: it
    # ConflictExceptions twice, then succeeds. persist_identity must retry and return
    # normally. Patch time.sleep so the test is instant.
    import services.mcp_server_service as mod

    monkeypatch.setattr(mod.time, "sleep", lambda *_a, **_k: None)
    ctl, _ = mcp_mock_registry_clients
    mcp = _hydrated_mcp(mcp_service, mcp_mock_registry_clients, mcp_sample_record)
    mcp.identity_status = "provisioned"

    ctl.update_registry_record.side_effect = [
        _conflict(),
        _conflict(),
        {"recordArn": MCP_RECORD_ID, "status": "DRAFT"},
    ]

    returned = mcp_service.persist_identity(mcp)

    assert returned is mcp
    assert ctl.update_registry_record.call_count == 3


def test_persist_identity_reraises_non_conflict_error(
    mcp_service, mcp_mock_registry_clients, mcp_sample_record, monkeypatch
):
    # A NON-Conflict ClientError (e.g. ValidationException) must propagate immediately,
    # NOT be retried — the retry only swallows ConflictException.
    import services.mcp_server_service as mod

    monkeypatch.setattr(mod.time, "sleep", lambda *_a, **_k: None)
    ctl, _ = mcp_mock_registry_clients
    mcp = _hydrated_mcp(mcp_service, mcp_mock_registry_clients, mcp_sample_record)
    mcp.identity_status = "provisioned"

    ctl.update_registry_record.side_effect = _schema_validation("UpdateRegistryRecord")

    with pytest.raises(ClientError) as exc:
        mcp_service.persist_identity(mcp)
    assert exc.value.response["Error"]["Code"] == "ValidationException"
    # Not retried — surfaced on the first attempt.
    assert ctl.update_registry_record.call_count == 1


def test_persist_identity_raises_after_exhausting_conflict_retries(
    mcp_service, mcp_mock_registry_clients, mcp_sample_record, monkeypatch
):
    # The record never leaves UPDATING: after `attempts` ConflictExceptions the bounded
    # retry exhausts and surfaces the last ConflictException.
    import services.mcp_server_service as mod

    monkeypatch.setattr(mod.time, "sleep", lambda *_a, **_k: None)
    ctl, _ = mcp_mock_registry_clients
    mcp = _hydrated_mcp(mcp_service, mcp_mock_registry_clients, mcp_sample_record)
    mcp.identity_status = "provisioned"

    ctl.update_registry_record.side_effect = _conflict()

    # Use a tiny attempts budget via the helper to keep the test quick.
    with pytest.raises(ClientError) as exc:
        mcp_service._update_registry_record_with_retry(
            recordId=mcp.id,
            name=mcp.name,
            displayName={"optionalValue": mcp.name},
            description={"optionalValue": mcp.description or ""},
            descriptors={"optionalValue": {"mcpServer": {"optionalValue": {}}}},
            recordVersion="1.0.0",
            attempts=3,
            delay=0,
        )
    assert exc.value.response["Error"]["Code"] == "ConflictException"
    assert ctl.update_registry_record.call_count == 3


def test_update_still_maps_validation_through_retry_helper(
    mcp_service, mcp_mock_registry_clients, mcp_sample_record, monkeypatch
):
    # update() routes its write through the same retry helper; a ValidationException
    # must still map to McpValidationError (its 422 behavior), NOT be retried.
    import services.mcp_server_service as mod

    monkeypatch.setattr(mod.time, "sleep", lambda *_a, **_k: None)
    ctl, _ = mcp_mock_registry_clients
    ctl.get_registry_record.return_value = mcp_sample_record(status="DRAFT")
    ctl.update_registry_record.side_effect = _schema_validation("UpdateRegistryRecord")

    with pytest.raises(McpValidationError):
        mcp_service.update(MCP_RECORD_ID, McpServerUpdate(description="bad"))
    # Not retried — the validation error surfaced on the first attempt.
    assert ctl.update_registry_record.call_count == 1


# ---------------------------------------------------------------------------
# E32 — agent-registry namespace + mcpServer descriptor migration
# ---------------------------------------------------------------------------

def test_create_sends_new_mcp_descriptor_shape(mcp_service, mcp_mock_registry_clients):
    """descriptors.mcpServer.data replaces descriptors.mcp.server (E32)."""
    ctl, _ = mcp_mock_registry_clients
    mcp_service.create(McpServerCreate(name="m1", description="d", kind="standard",
                                       endpoint_url="https://example.invalid/mcp",
                                       tenant_id="default"))

    kwargs = ctl.create_registry_record.call_args.kwargs
    assert kwargs["recordType"] == "MCP"
    assert "descriptorType" not in kwargs
    server = kwargs["descriptors"]["mcpServer"]
    assert json.loads(server["data"])["name"]
    assert server["dataSchemaVersion"] == "2025-12-11"
    assert "mcp" not in kwargs["descriptors"]


def test_create_nests_tools_under_additional_data(mcp_service, mcp_mock_registry_clients):
    """tools move to mcpServer.additionalData.tools with dataSchemaVersion (E32)."""
    ctl, _ = mcp_mock_registry_clients
    mcp_service.create(McpServerCreate(name="m2", description="d", kind="standard",
                                       endpoint_url="https://example.invalid/mcp",
                                       tenant_id="default",
                                       available_tools=[McpTool(name="t1", description="dt")]))

    server = ctl.create_registry_record.call_args.kwargs["descriptors"]["mcpServer"]
    tools = server["additionalData"]["tools"]
    assert json.loads(tools["data"])["tools"][0]["name"] == "t1"
    assert tools["dataSchemaVersion"] == "2025-11-25"


def test_create_sends_display_name(mcp_service, mcp_mock_registry_clients):
    """create writes both name fields, so the record is readable by a displayName-first read."""
    ctl, _ = mcp_mock_registry_clients
    mcp_service.create(McpServerCreate(name="m3", tenant_id="default"))

    kwargs = ctl.create_registry_record.call_args.kwargs
    assert kwargs["name"] == "m3"
    assert kwargs["displayName"] == "m3"


def test_create_falls_back_to_name_when_description_blank(mcp_service, mcp_mock_registry_clients):
    """A blank description must never reach the API: Description has min length 1.

    ``McpServerCreate.description`` defaults to ``""``, and CreateRegistryRecord rejects a
    zero-length description with a ParamValidationError — which surfaces as a raw 500, not a
    422. Falling back to the always-present name keeps a description-less create working
    (same guard the agent side uses with ``purpose or name``).
    """
    ctl, _ = mcp_mock_registry_clients
    mcp_service.create(McpServerCreate(name="no-description-mcp", tenant_id="default"))

    assert ctl.create_registry_record.call_args.kwargs["description"] == "no-description-mcp"


def test_update_sends_display_name(mcp_service, mcp_mock_registry_clients, mcp_sample_record):
    """A rename must patch displayName too, or reads silently resolve the stale one (E32).

    Regression guard: reads prefer displayName, so updating `name` alone makes a rename
    revert on the next read.
    """
    ctl, _ = mcp_mock_registry_clients
    ctl.get_registry_record.return_value = mcp_sample_record(status="DRAFT")

    mcp_service.update(MCP_RECORD_ID, McpServerUpdate(name="new-name"))

    kwargs = ctl.update_registry_record.call_args.kwargs
    # name stays a PLAIN string; displayName takes the one-level PATCH envelope.
    assert kwargs["name"] == "new-name"
    assert kwargs["displayName"] == {"optionalValue": "new-name"}


def test_persist_identity_sends_display_name(
    mcp_service, mcp_mock_registry_clients, mcp_sample_record
):
    """persist_identity envelope-writes the whole server, so it owns displayName too (E32)."""
    ctl, _ = mcp_mock_registry_clients
    mcp = _hydrated_mcp(mcp_service, mcp_mock_registry_clients, mcp_sample_record)

    mcp_service.persist_identity(mcp)

    kwargs = ctl.update_registry_record.call_args.kwargs
    assert kwargs["name"] == mcp.name
    assert kwargs["displayName"] == {"optionalValue": mcp.name}


def test_get_reads_display_name(mcp_service, mcp_mock_registry_clients, mcp_sample_record):
    """Native displayName WINS over the record's name (E32).

    The two values must DIFFER here or the test is a tautology: the shared conftest record
    sets ``name == displayName``, so an assertion against that fixture passes whether
    ``from_record`` reads ``displayName`` or ``name``. Overridden per-test rather than in
    ``conftest`` because the ``name == displayName`` record is the realistic post-create shape
    the rest of the suite relies on. The fall-back direction (no ``displayName`` at all → use
    ``name``) is covered by ``test_mcp_server_model``'s ``_native_record``.
    """
    ctl, _ = mcp_mock_registry_clients
    ctl.get_registry_record.return_value = mcp_sample_record(
        name="dedup-key", displayName="Human Label"
    )

    assert mcp_service.get(MCP_RECORD_ID).name == "Human Label"


# ---------------------------------------------------------------------------
# E33 Amendment 1 — marketplace publication block (contract C8)
# ---------------------------------------------------------------------------

DECLARED_AT = datetime(2026, 8, 11, 12, 0, 0, tzinfo=timezone.utc)


def _publication(published: bool = True):
    """A MarketplacePublication with a fully-populated declared datasheet."""
    return MarketplacePublication(
        published=published,
        datasheet=Datasheet(
            owner_team="Platform Engineering",
            support_contact="mcp-platform@acme.com",
            data_classification="Confidential",
            sla_tier="Gold",
            compliance=["GDPR", "BaFin"],
            support_hours="24/7",
            version="1.0.0",
            region="EU (Frankfurt)",
            guardrails=["Tool allow-list"],
            pitch="Read-only claims access for downstream agents.",
        ),
        declared_by="admin@acme.com",
        declared_at=DECLARED_AT,
    )


def _envelope_from_update(ctl) -> dict:
    """Parse the governance envelope out of the LAST UpdateRegistryRecord call's
    three-level ``optionalValue`` descriptors wrap (see test_registry_update_param_shape)."""
    kwargs = ctl.update_registry_record.call_args.kwargs
    server_descriptor = kwargs["descriptors"]["optionalValue"]["mcpServer"]["optionalValue"]
    server_json = json.loads(server_descriptor["data"]["optionalValue"])
    return server_json["_meta"]["com.agp/governance"]


def test_update_preserves_marketplace_block(mcp_service, mcp_mock_registry_clients, mcp_sample_record, mcp_sample_envelope):
    """The silently-destroyed-key trap: an ordinary ``update()`` on an UNRELATED field
    rebuilds server.json wholesale, so the marketplace key must survive it. A key absent
    from ``to_envelope()`` would be discarded here with no error at all."""
    ctl, _ = mcp_mock_registry_clients
    env = mcp_sample_envelope(marketplace=json.loads(_publication().model_dump_json()))
    ctl.get_registry_record.return_value = mcp_sample_record(status="DRAFT", _envelope=env)

    updated = mcp_service.update(MCP_RECORD_ID, McpServerUpdate(business_unit="Finance"))

    assert updated is not None and updated.business_unit == "Finance"
    envelope = _envelope_from_update(ctl)
    assert envelope["marketplace"]["published"] is True
    assert envelope["marketplace"]["datasheet"]["owner_team"] == "Platform Engineering"
    assert envelope["marketplace"]["declared_by"] == "admin@acme.com"


def test_persist_marketplace_writes_block_through_patch_envelope(
    mcp_service, mcp_mock_registry_clients, mcp_sample_record
):
    """``persist_marketplace`` is the service-only write (mirrors ``persist_identity``):
    read-modify-write through the mcpServer PATCH descriptors wrap — never the agent-side
    ``custom`` branch, which would schema-422 an MCP record."""
    ctl, _ = mcp_mock_registry_clients
    ctl.get_registry_record.return_value = mcp_sample_record(status="APPROVED")

    result = mcp_service.persist_marketplace(MCP_RECORD_ID, _publication())

    assert result.marketplace is not None and result.marketplace.published is True
    kwargs = ctl.update_registry_record.call_args.kwargs
    # name stays a PLAIN string; displayName + description take the one-level wrap.
    assert kwargs["name"] == "internal-claims-mcp"
    assert kwargs["displayName"] == {"optionalValue": "internal-claims-mcp"}
    assert "custom" not in kwargs["descriptors"]["optionalValue"]
    envelope = _envelope_from_update(ctl)
    assert envelope["marketplace"]["published"] is True
    assert envelope["marketplace"]["datasheet"]["support_contact"] == "mcp-platform@acme.com"
    # Untouched governance fields survive the regenerated envelope.
    assert envelope["owner_oid"] == "maria-oid"
    assert envelope["region"] == "DE"


def test_persist_marketplace_preserves_tools_branch(
    mcp_service, mcp_mock_registry_clients, mcp_sample_record
):
    """The sample record carries tools; the write goes through ``persist_identity``, so the
    5-deep tools branch survives (a dropped branch would delete the declared tools)."""
    ctl, _ = mcp_mock_registry_clients
    ctl.get_registry_record.return_value = mcp_sample_record(status="APPROVED")

    mcp_service.persist_marketplace(MCP_RECORD_ID, _publication())

    server_branch = ctl.update_registry_record.call_args.kwargs["descriptors"]["optionalValue"]["mcpServer"]["optionalValue"]
    tools_branch = server_branch["additionalData"]["optionalValue"]["tools"]
    tools_payload = json.loads(tools_branch["optionalValue"]["data"]["optionalValue"])
    assert tools_payload["tools"][0]["name"] == "get_claim"


def test_persist_marketplace_unpublish_keeps_block_with_published_false(
    mcp_service, mcp_mock_registry_clients, mcp_sample_record, mcp_sample_envelope
):
    """Unpublish KEEPS the declared block (history retained) with ``published=False`` —
    it does NOT clear it to None."""
    ctl, _ = mcp_mock_registry_clients
    env = mcp_sample_envelope(marketplace=json.loads(_publication().model_dump_json()))
    ctl.get_registry_record.return_value = mcp_sample_record(status="APPROVED", _envelope=env)

    result = mcp_service.persist_marketplace(MCP_RECORD_ID, _publication(published=False))

    assert result.marketplace is not None and result.marketplace.published is False
    envelope = _envelope_from_update(ctl)
    assert envelope["marketplace"]["published"] is False
    assert envelope["marketplace"]["datasheet"]["owner_team"] == "Platform Engineering"


def test_persist_marketplace_none_clears_the_block(
    mcp_service, mcp_mock_registry_clients, mcp_sample_record, mcp_sample_envelope
):
    """``None`` clears the block outright (the key is written as ``null``, not omitted —
    omitting it would leave the stale block in place on a later read)."""
    ctl, _ = mcp_mock_registry_clients
    env = mcp_sample_envelope(marketplace=json.loads(_publication().model_dump_json()))
    ctl.get_registry_record.return_value = mcp_sample_record(status="APPROVED", _envelope=env)

    result = mcp_service.persist_marketplace(MCP_RECORD_ID, None)

    assert result.marketplace is None
    envelope = _envelope_from_update(ctl)
    assert "marketplace" in envelope
    assert envelope["marketplace"] is None


def test_persist_marketplace_raises_when_mcp_server_missing(mcp_service, mcp_mock_registry_clients):
    """A missing record is an McpServerNotFoundError, not a silent None: the marketplace
    service maps it to a fixed-literal error rather than persisting an APPROVED publish
    request whose registry write never landed."""
    ctl, _ = mcp_mock_registry_clients
    ctl.get_registry_record.side_effect = _not_found()

    with pytest.raises(McpServerNotFoundError):
        mcp_service.persist_marketplace("mcp-missing", _publication())

    ctl.update_registry_record.assert_not_called()


def test_persist_marketplace_bumps_updated_at(mcp_service, mcp_mock_registry_clients, mcp_sample_record):
    ctl, _ = mcp_mock_registry_clients
    ctl.get_registry_record.return_value = mcp_sample_record(status="APPROVED")
    before = mcp_service.get(MCP_RECORD_ID).updated_at

    result = mcp_service.persist_marketplace(MCP_RECORD_ID, _publication())

    assert result.updated_at > before


def test_list_skips_record_with_malformed_marketplace_block(
    mcp_service, mcp_mock_registry_clients, mcp_sample_record, mcp_sample_envelope
):
    """The I2 containment, mirrored on the MCP hydrate seam: a well-formed server.json can
    still hold a structurally invalid ``marketplace`` block, which ``from_record`` parses
    with ``MarketplacePublication.model_validate``. That pydantic ``ValidationError`` must
    reach ``list()`` as ``MalformedMcpRecordError`` — otherwise ONE bad record 500s the whole
    catalog (``/mcp-servers`` and the marketplace product list) instead of being
    skipped-and-warned."""
    ctl, _ = mcp_mock_registry_clients
    ctl.list_registry_records.return_value = {
        "registryRecords": [
            {"recordId": "mcp-bad", "name": "bad", "status": "DRAFT"},
            {"recordId": "mcp-good", "name": "good", "status": "DRAFT"},
        ],
        "nextToken": None,
    }

    def _get(registryId, recordId):
        if recordId == "mcp-bad":
            # server.json parses as JSON; the marketplace block does not validate.
            return mcp_sample_record(
                recordId="mcp-bad",
                _envelope=mcp_sample_envelope(marketplace={"published": True}),
            )
        return mcp_sample_record(recordId="mcp-good")

    ctl.get_registry_record.side_effect = _get

    assert [m.id for m in mcp_service.list()] == ["mcp-good"]


def test_get_raises_malformed_on_invalid_marketplace_block(
    mcp_service, mcp_mock_registry_clients, mcp_sample_record, mcp_sample_envelope
):
    """The same fault on a single-record read surfaces as the malformed-record error (which
    the caller maps), never as a raw pydantic ``ValidationError`` escaping the service."""
    ctl, _ = mcp_mock_registry_clients
    ctl.get_registry_record.return_value = mcp_sample_record(
        _envelope=mcp_sample_envelope(marketplace="not-a-publication")
    )
    with pytest.raises(MalformedMcpRecordError):
        mcp_service.get(MCP_RECORD_ID)


# ---------------------------------------------------------------------------
# E33 Amendment 2 — deprecate ⇒ unlist (contract C12)
# ---------------------------------------------------------------------------

def _method_call_names(ctl) -> list[str]:
    """The ordered method names called on the control client (for ordering pins)."""
    return [call[0] for call in ctl.method_calls]


def test_transition_deprecate_unlists_a_published_marketplace_product(
    mcp_service, mcp_mock_registry_clients, mcp_sample_record, mcp_sample_envelope
):
    """C12: deprecating a PUBLISHED product unlists it — one write per concern, lifecycle
    status FIRST and the unlist SECOND, with the declared datasheet kept as history."""
    ctl, _ = mcp_mock_registry_clients
    env = mcp_sample_envelope(marketplace=json.loads(_publication().model_dump_json()))
    ctl.get_registry_record.return_value = mcp_sample_record(status="DEPRECATED", _envelope=env)

    mcp = mcp_service.transition(MCP_RECORD_ID, "deprecate", reason="superseded by v2")

    ctl.update_registry_record_status.assert_called_with(
        registryId=REGISTRY_ID,
        recordId=MCP_RECORD_ID,
        status="DEPRECATED",
        statusReason="superseded by v2",
    )
    # Ordering pin: the lifecycle write lands BEFORE the unlist envelope write.
    names = _method_call_names(ctl)
    assert names.index("update_registry_record_status") < names.index("update_registry_record")

    envelope = _envelope_from_update(ctl)
    assert envelope["marketplace"]["published"] is False
    # The declared datasheet is HISTORY: the block is kept, never cleared to None.
    assert envelope["marketplace"]["datasheet"]["owner_team"] == "Platform Engineering"
    assert envelope["marketplace"]["datasheet"]["support_contact"] == "mcp-platform@acme.com"
    assert envelope["marketplace"]["datasheet"]["compliance"] == ["GDPR", "BaFin"]
    assert envelope["marketplace"]["declared_by"] == "admin@acme.com"
    assert envelope["marketplace"]["declared_at"] is not None

    # The returned record reflects the unlist (the route hands this straight back).
    assert mcp is not None
    assert mcp.lifecycle_state == LifecycleState.DEPRECATED
    assert mcp.marketplace is not None and mcp.marketplace.published is False
    assert mcp.marketplace.datasheet.owner_team == "Platform Engineering"


def test_transition_deprecate_never_published_writes_no_marketplace_block(
    mcp_service, mcp_mock_registry_clients, mcp_sample_record
):
    """No marketplace block at all → nothing to unlist, so no envelope write happens
    (a spurious write would invent an attestation the publisher never made)."""
    ctl, _ = mcp_mock_registry_clients
    ctl.get_registry_record.return_value = mcp_sample_record(status="DEPRECATED")

    mcp = mcp_service.transition(MCP_RECORD_ID, "deprecate", reason="eol")

    ctl.update_registry_record.assert_not_called()
    assert mcp is not None and mcp.marketplace is None


def test_transition_deprecate_already_unpublished_writes_no_marketplace_block(
    mcp_service, mcp_mock_registry_clients, mcp_sample_record, mcp_sample_envelope
):
    """Already ``published=False`` → idempotent no-op: the unlist is skipped entirely."""
    ctl, _ = mcp_mock_registry_clients
    env = mcp_sample_envelope(
        marketplace=json.loads(_publication(published=False).model_dump_json())
    )
    ctl.get_registry_record.return_value = mcp_sample_record(status="DEPRECATED", _envelope=env)

    mcp = mcp_service.transition(MCP_RECORD_ID, "deprecate", reason="eol")

    ctl.update_registry_record.assert_not_called()
    assert mcp is not None and mcp.marketplace is not None
    assert mcp.marketplace.published is False


@pytest.mark.parametrize("action,status", [("approve", "APPROVED"), ("reject", "REJECTED")])
def test_transition_approve_and_reject_never_touch_the_marketplace_block(
    mcp_service, mcp_mock_registry_clients, mcp_sample_record, mcp_sample_envelope, action, status
):
    """ONLY a DEPRECATED target unlists. This is also what makes a deprecation STICK: a
    later lifecycle re-approve must NOT re-list the product — only a fresh publish
    request can."""
    ctl, _ = mcp_mock_registry_clients
    env = mcp_sample_envelope(
        marketplace=json.loads(_publication(published=False).model_dump_json())
    )
    ctl.get_registry_record.return_value = mcp_sample_record(status=status, _envelope=env)

    mcp = mcp_service.transition(MCP_RECORD_ID, action, reason="ok")

    ctl.update_registry_record.assert_not_called()
    assert mcp is not None and mcp.marketplace is not None
    assert mcp.marketplace.published is False


def test_transition_deprecate_raises_when_the_unlist_write_fails(
    mcp_service, mcp_mock_registry_clients, mcp_sample_record, mcp_sample_envelope
):
    """An unlist failure RAISES (the existing route error mapping surfaces it) rather than
    being swallowed. The lifecycle write already landed, so the record is deprecated but
    still flagged published — which the read-path lifecycle gate refuses to list anyway
    (defense in depth); the retry path is transition-again or an admin unpublish."""
    ctl, _ = mcp_mock_registry_clients
    env = mcp_sample_envelope(marketplace=json.loads(_publication().model_dump_json()))
    ctl.get_registry_record.return_value = mcp_sample_record(status="DEPRECATED", _envelope=env)
    ctl.update_registry_record.side_effect = ClientError(
        {"Error": {"Code": "ThrottlingException", "Message": "slow down"}},
        "UpdateRegistryRecord",
    )

    with pytest.raises(ClientError):
        mcp_service.transition(MCP_RECORD_ID, "deprecate", reason="superseded by v2")

    ctl.update_registry_record_status.assert_called_once()
    ctl.update_registry_record.assert_called_once()


def test_transition_deprecate_returns_none_when_the_record_vanished(
    mcp_service, mcp_mock_registry_clients
):
    """The status write succeeded but the re-read 404s: still ``None`` (the pre-C12
    contract), and no unlist is attempted on a record we could not hydrate."""
    ctl, _ = mcp_mock_registry_clients
    ctl.get_registry_record.side_effect = _not_found()

    assert mcp_service.transition(MCP_RECORD_ID, "deprecate", reason="eol") is None
    ctl.update_registry_record.assert_not_called()
