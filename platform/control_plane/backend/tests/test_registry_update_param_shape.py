"""Param-shape tests for the AWS Agent Registry ``UpdateRegistryRecord`` call.

These are the FIRST tests in the suite to run **real botocore param validation**
against the ``agent-registry-control`` service model. Every other service test
injects a ``MagicMock`` ``control_client`` which never runs botocore's
``ParamValidator`` — which is exactly why the live bug below stayed latent until a live run.

THE BUG (root cause): ``UpdateRegistryRecord`` is a PATCH-style API whose mutable
fields are wrapped in an ``{"optionalValue": ...}`` envelope, unlike the FLAT
``CreateRegistryRecord`` shape. Our ``update_registry_record(...)`` calls were
written by mirroring the flat ``create`` shape, so they fail botocore param
validation the first time they run against real AWS:

    ParamValidationError: Invalid type for parameter description,
    value: ..., type: str, valid types: dict

Strategy: capture the EXACT kwargs the registry writer sends to ``update_registry_record``
(via the injected MagicMock ``control_client``), then feed those kwargs to the real
``ParamValidator`` for the ``UpdateRegistryRecord`` input shape. No AWS calls; this
is an offline model-validation check. A matching regression guard proves the
``create`` calls still validate against the (flat) ``CreateRegistryRecord`` shape.

This file is the offline gate for the agent-registry namespace migration. The
``{"optionalValue": ...}`` PATCH envelope SURVIVED the namespace move (verified against the
released botocore model), but its DEPTH did not: every leaf under an ``Updated*Fields``
structure is itself an ``Updated*`` wrapper, so the blobs sit one level deeper than before.
What else changed is the CREATE shape (recordType/name/displayName, descriptors.custom.data
and descriptors.mcpServer.{data,additionalData.tools}) and the List filters.
"""

from __future__ import annotations

from botocore.session import get_session
from botocore.validate import ParamValidator
from conftest import (
    MCP_RECORD_ID,
    RECORD_ID,
    _mcp_sample_record,
    _sample_record,
)

from models.agent import AgentCreate, AgentUpdate, Platform
from models.mcp_server import McpServerCreate, McpServerUpdate, McpTool
from services.registry_writer import RecordContent

_MODEL = get_session().get_service_model("agent-registry-control")


def _assert_valid(op_name: str, params: dict) -> None:
    """Validate ``params`` against the real input shape of ``op_name``.

    Fails with botocore's own human-readable report (the same text that would
    surface as a ``ParamValidationError`` against live AWS) so a regression points
    straight at the offending member.
    """
    report = ParamValidator().validate(params, _MODEL.operation_model(op_name).input_shape)
    assert not report.has_errors(), report.generate_report()


def _update_params(descriptors: dict) -> dict:
    return RecordContent(name="n", description="hello", descriptors=descriptors).update_params()


def test_update_params_wrap_the_description_and_the_display_name_once():
    params = _update_params({"custom": {"data": "X"}})
    assert params["name"] == "n"
    assert params["displayName"] == {"optionalValue": "n"}
    assert params["description"] == {"optionalValue": "hello"}
    assert params["recordVersion"] == "1.0.0"


def test_update_params_wrap_the_custom_descriptor_three_levels():
    assert _update_params({"custom": {"data": "X"}})["descriptors"] == {
        "optionalValue": {"custom": {"optionalValue": {"data": {"optionalValue": "X"}}}}
    }


def test_update_params_wrap_the_mcp_descriptor_three_levels_and_the_tools_five():
    flat = {
        "mcpServer": {
            "data": "S",
            "dataSchemaVersion": "2025-12-11",
            "additionalData": {"tools": {"data": "T", "dataSchemaVersion": "2025-11-25"}},
        }
    }
    assert _update_params(flat)["descriptors"] == {
        "optionalValue": {
            "mcpServer": {
                "optionalValue": {
                    "data": {"optionalValue": "S"},
                    "dataSchemaVersion": {"optionalValue": "2025-12-11"},
                    "additionalData": {
                        "optionalValue": {
                            "tools": {
                                "optionalValue": {
                                    "data": {"optionalValue": "T"},
                                    "dataSchemaVersion": {"optionalValue": "2025-11-25"},
                                }
                            }
                        }
                    },
                }
            }
        }
    }


def test_agent_update_sends_valid_update_params(service, mock_registry_clients):
    ctl, _ = mock_registry_clients
    ctl.get_registry_record.return_value = _sample_record(status="DRAFT")

    service.update(RECORD_ID, AgentUpdate(business_unit="Finance"))

    assert ctl.update_registry_record.called
    kwargs = ctl.update_registry_record.call_args.kwargs
    _assert_valid("UpdateRegistryRecord", kwargs)


def test_agent_persist_identity_sends_valid_update_params(service, mock_registry_clients):
    """The live path: registration's synchronous ``persist_identity`` hook.

    This is the exact call that failed against real AWS with the
    ``ParamValidationError`` on ``description``. Construct an in-hand Agent (no
    GET needed — ``persist_identity`` takes the object directly) and assert the
    captured kwargs validate clean.
    """
    ctl, _ = mock_registry_clients
    agent = service.create(
        AgentCreate(
            name="claims-triage-de",
            purpose="Triage motor claims",
            sponsor_oid="maria-oid",
            sponsor_email="maria.bauer@example.com",
            business_unit="Claims",
            region="DE",
            platform=Platform.AWS_BEDROCK,
            tenant_id="default",
        )
    )
    agent.entra_sp_id = "sp-123"
    agent.entra_app_audience = "api://agp-agent"
    agent.invoker_role_id = "role-invoker"
    agent.admin_role_id = "role-admin"

    ctl.update_registry_record.reset_mock()
    service.persist_identity(agent)

    assert ctl.update_registry_record.called
    kwargs = ctl.update_registry_record.call_args.kwargs
    _assert_valid("UpdateRegistryRecord", kwargs)


def test_mcp_update_sends_valid_update_params_with_tools(mcp_service, mcp_mock_registry_clients):
    ctl, _ = mcp_mock_registry_clients
    ctl.get_registry_record.return_value = _mcp_sample_record(status="DRAFT")

    mcp_service.update(MCP_RECORD_ID, McpServerUpdate(description="New description"))

    assert ctl.update_registry_record.called
    kwargs = ctl.update_registry_record.call_args.kwargs
    server_branch = kwargs["descriptors"]["optionalValue"]["mcpServer"]["optionalValue"]
    assert "tools" in server_branch["additionalData"]["optionalValue"]
    _assert_valid("UpdateRegistryRecord", kwargs)


def test_mcp_update_sends_valid_update_params_without_tools(mcp_service, mcp_mock_registry_clients):
    ctl, _ = mcp_mock_registry_clients
    ctl.get_registry_record.return_value = _mcp_sample_record(status="DRAFT", _tools=[])

    mcp_service.update(MCP_RECORD_ID, McpServerUpdate(description="New description"))

    assert ctl.update_registry_record.called
    kwargs = ctl.update_registry_record.call_args.kwargs
    server_branch = kwargs["descriptors"]["optionalValue"]["mcpServer"]["optionalValue"]
    assert "additionalData" not in server_branch
    _assert_valid("UpdateRegistryRecord", kwargs)


def test_agent_create_params_still_valid(service, mock_registry_clients):
    ctl, _ = mock_registry_clients
    service.create(
        AgentCreate(
            name="fraud-watch-eu",
            purpose="Watch for fraud",
            platform=Platform.AWS_BEDROCK,
            tenant_id="default",
        )
    )
    assert ctl.create_registry_record.called
    kwargs = ctl.create_registry_record.call_args.kwargs
    _assert_valid("CreateRegistryRecord", kwargs)


def test_mcp_create_params_still_valid(mcp_service, mcp_mock_registry_clients):
    ctl, _ = mcp_mock_registry_clients
    mcp_service.create(
        McpServerCreate(
            name="claims-mcp",
            description="Read-only claims",
            available_tools=[McpTool(name="get_claim", description="Fetch", input_schema={"type": "object"})],
            tenant_id="default",
        )
    )
    assert ctl.create_registry_record.called
    kwargs = ctl.create_registry_record.call_args.kwargs
    _assert_valid("CreateRegistryRecord", kwargs)


def test_mcp_create_with_blank_description_still_valid(mcp_service, mcp_mock_registry_clients):
    ctl, _ = mcp_mock_registry_clients
    mcp_service.create(McpServerCreate(name="no-description-mcp", tenant_id="default"))

    kwargs = ctl.create_registry_record.call_args.kwargs
    _assert_valid("CreateRegistryRecord", kwargs)


def test_mcp_update_with_blank_description_still_valid(mcp_service, mcp_mock_registry_clients):
    ctl, _ = mcp_mock_registry_clients
    ctl.get_registry_record.return_value = _mcp_sample_record(
        status="DRAFT", _server_json={"name": "agp/blank-mcp", "description": "", "version": "1.0.0"}
    )

    mcp_service.update(MCP_RECORD_ID, McpServerUpdate(region="DE"))

    kwargs = ctl.update_registry_record.call_args.kwargs
    _assert_valid("UpdateRegistryRecord", kwargs)


def test_agent_update_with_blank_purpose_still_valid(service, mock_registry_clients):
    """A record with a blank native ``description`` hydrates to ``purpose == ""``, so the
    read-modify-write would otherwise patch a zero-length string. Real-model proof:
    ``{"description": {"optionalValue": ""}}`` fails with "Invalid length for parameter
    description.optionalValue, value: 0, valid min length: 1"."""
    ctl, _ = mock_registry_clients
    ctl.get_registry_record.return_value = _sample_record(status="DRAFT", description="")

    service.update(RECORD_ID, AgentUpdate(business_unit="Finance"))

    kwargs = ctl.update_registry_record.call_args.kwargs
    _assert_valid("UpdateRegistryRecord", kwargs)


def test_agent_persist_identity_with_blank_purpose_still_valid(service, mock_registry_clients):
    """The provisioning hook is the more dangerous of the two update paths: unlike
    ``update()`` it has no ``except`` at all, so a blank-purpose agent (``AgentCreate.purpose``
    defaults to ``""`` — e.g. the add_repo template flow) would raise straight out of it."""
    ctl, _ = mock_registry_clients
    agent = service.create(AgentCreate(name="no-purpose-agent", tenant_id="default"))
    assert agent.purpose == ""

    ctl.update_registry_record.reset_mock()
    service.persist_identity(agent)

    kwargs = ctl.update_registry_record.call_args.kwargs
    _assert_valid("UpdateRegistryRecord", kwargs)
