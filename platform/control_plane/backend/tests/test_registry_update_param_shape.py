"""Param-shape tests for the AWS Agent Registry ``UpdateRegistryRecord`` call (E6 fix).

These are the FIRST tests in the suite to run **real botocore param validation**
against the ``agent-registry-control`` service model. Every other service test
injects a ``MagicMock`` ``_ctl`` (research §10) which never runs botocore's
``ParamValidator`` — which is exactly why the live bug below stayed latent until E6.

THE BUG (root cause): ``UpdateRegistryRecord`` is a PATCH-style API whose mutable
fields are wrapped in an ``{"optionalValue": ...}`` envelope, unlike the FLAT
``CreateRegistryRecord`` shape. Our ``update_registry_record(...)`` calls were
written by mirroring the flat ``create`` shape, so they fail botocore param
validation the first time they run against real AWS:

    ParamValidationError: Invalid type for parameter description,
    value: ..., type: str, valid types: dict

Strategy: capture the EXACT kwargs our services pass to ``update_registry_record``
(via the injected MagicMock ``_ctl``), then feed those kwargs to the real
``ParamValidator`` for the ``UpdateRegistryRecord`` input shape. No AWS calls; this
is an offline model-validation check. A matching regression guard proves the
``create`` calls still validate against the (flat) ``CreateRegistryRecord`` shape.

E32: this file is the offline gate for the agent-registry namespace migration. The
``{"optionalValue": ...}`` PATCH envelope SURVIVED the namespace move (verified against the
released botocore model), but its DEPTH did not: every leaf under an ``Updated*Fields``
structure is itself an ``Updated*`` wrapper, so the blobs sit one level deeper than before.
What else changed is the CREATE shape (recordType/name/displayName, descriptors.custom.data
and descriptors.mcpServer.{data,additionalData.tools}) and the List filters.
"""

from __future__ import annotations

import json

import pytest
from botocore.session import get_session
from botocore.validate import ParamValidator

from models.agent import AgentCreate, AgentUpdate, Platform
from models.mcp_server import McpServerCreate, McpServerUpdate, McpTool  # McpTool used by create regression
from services.agent_registry_service import (
    AgentRegistryService,
    _wrap_update_description,
    _wrap_update_descriptors_custom,
)
from services.mcp_server_service import (
    McpServerRegistryService,
    _wrap_update_descriptors,
)

from conftest import (
    MCP_RECORD_ID,
    RECORD_ID,
    REGISTRY_ID,
    _mcp_sample_record,
    _sample_record,
)

# Real service model, loaded once. This is the whole point of this file: validate
# our params against the ACTUAL agent-registry-control shapes, offline.
_MODEL = get_session().get_service_model("agent-registry-control")


def _assert_valid(op_name: str, params: dict) -> None:
    """Validate ``params`` against the real input shape of ``op_name``.

    Fails with botocore's own human-readable report (the same text that would
    surface as a ``ParamValidationError`` against live AWS) so a regression points
    straight at the offending member.
    """
    report = ParamValidator().validate(
        params, _MODEL.operation_model(op_name).input_shape
    )
    assert not report.has_errors(), report.generate_report()


# ---------------------------------------------------------------------------
# Direct helper unit tests — pin the EXACT envelope structure so the
# create-vs-update asymmetry can't be silently re-flattened.
# ---------------------------------------------------------------------------

def test_wrap_update_description_shape():
    assert _wrap_update_description("hello") == {"optionalValue": "hello"}


def test_wrap_update_descriptors_custom_shape():
    # E32: the PATCH-envelope idea survived the namespace move, but the DEPTH did not.
    # The renamed `data` leaf is itself an UpdatedDescriptorData structure on
    # agent-registry-control, so the blob now sits under a THIRD optionalValue. The old
    # two-level shape (`{"data": "X"}`) fails real param validation — see the sibling
    # test_agent_update_sends_valid_update_params, which checks that against the model.
    assert _wrap_update_descriptors_custom("X") == {
        "optionalValue": {
            "custom": {"optionalValue": {"data": {"optionalValue": "X"}}}
        }
    }


def test_wrap_update_descriptors_mcp_shape():
    # The MCP arm is WIDER than the CUSTOM one: `data`, `dataSchemaVersion` AND
    # `additionalData` are each their own Updated* structure, and the tools descriptor
    # nested under additionalData repeats the pattern one level further down — so the
    # server blob sits three optionalValues deep and the tools blob five. Pinned here so
    # the nesting can't be silently flattened; the sibling
    # test_mcp_update_sends_valid_update_params_* check it against the real model.
    flat = {
        "mcpServer": {
            "data": "S",
            "dataSchemaVersion": "2025-12-11",
            "additionalData": {"tools": {"data": "T", "dataSchemaVersion": "2025-11-25"}},
        }
    }
    assert _wrap_update_descriptors(flat) == {
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


# ---------------------------------------------------------------------------
# Agent (CUSTOM record) — update() and persist_identity() must send params that
# validate clean against the real UpdateRegistryRecord shape.
# ---------------------------------------------------------------------------

def test_agent_update_sends_valid_update_params(service, mock_registry_clients):
    ctl, _ = mock_registry_clients
    ctl.get_registry_record.return_value = _sample_record(status="DRAFT")

    service.update(RECORD_ID, AgentUpdate(business_unit="Finance"))

    assert ctl.update_registry_record.called
    kwargs = ctl.update_registry_record.call_args.kwargs
    _assert_valid("UpdateRegistryRecord", kwargs)


def test_agent_persist_identity_sends_valid_update_params(service, mock_registry_clients):
    """The E6-LIVE path: registration's synchronous ``persist_identity`` hook.

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
    # Stamp the E6 identity fields that persist_identity exists to write.
    agent.entra_sp_id = "sp-123"
    agent.entra_app_audience = "api://agp-agent"
    agent.invoker_role_id = "role-invoker"
    agent.admin_role_id = "role-admin"

    ctl.update_registry_record.reset_mock()
    service.persist_identity(agent)

    assert ctl.update_registry_record.called
    kwargs = ctl.update_registry_record.call_args.kwargs
    _assert_valid("UpdateRegistryRecord", kwargs)


# ---------------------------------------------------------------------------
# MCP record — update() must send params that validate clean, with AND without
# a tools branch (the tools branch is conditional on available_tools).
# ---------------------------------------------------------------------------

def test_mcp_update_sends_valid_update_params_with_tools(mcp_service, mcp_mock_registry_clients):
    ctl, _ = mcp_mock_registry_clients
    # GET returns a record that already has a tools branch (fixture default = 2 tools),
    # so the hydrated McpServer keeps those McpTool objects through the read-modify-write
    # and update() regenerates the tools branch. We update an UNRELATED field
    # (description) — passing available_tools through McpServerUpdate is a separate,
    # out-of-scope model quirk and not the path E6/update-shape concerns.
    ctl.get_registry_record.return_value = _mcp_sample_record(status="DRAFT")

    mcp_service.update(MCP_RECORD_ID, McpServerUpdate(description="New description"))

    assert ctl.update_registry_record.called
    kwargs = ctl.update_registry_record.call_args.kwargs
    # Sanity: the tools branch is actually present in this case. E32 moved it under
    # mcpServer.additionalData, and every leaf got its own optionalValue wrapper.
    server_branch = kwargs["descriptors"]["optionalValue"]["mcpServer"]["optionalValue"]
    assert "tools" in server_branch["additionalData"]["optionalValue"]
    _assert_valid("UpdateRegistryRecord", kwargs)


def test_mcp_update_sends_valid_update_params_without_tools(mcp_service, mcp_mock_registry_clients):
    ctl, _ = mcp_mock_registry_clients
    # GET returns a server-only record (no tools) so update() omits the tools branch.
    ctl.get_registry_record.return_value = _mcp_sample_record(status="DRAFT", _tools=[])

    mcp_service.update(MCP_RECORD_ID, McpServerUpdate(description="New description"))

    assert ctl.update_registry_record.called
    kwargs = ctl.update_registry_record.call_args.kwargs
    # Sanity: the whole additionalData branch is genuinely absent in this case.
    server_branch = kwargs["descriptors"]["optionalValue"]["mcpServer"]["optionalValue"]
    assert "additionalData" not in server_branch
    _assert_valid("UpdateRegistryRecord", kwargs)


# ---------------------------------------------------------------------------
# Regression guard — the CREATE calls must STILL validate against the (flat)
# CreateRegistryRecord shape. This proves the fix did NOT accidentally wrap the
# create path (create is correct as-is — only update was wrong).
# ---------------------------------------------------------------------------

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
            available_tools=[
                McpTool(name="get_claim", description="Fetch", input_schema={"type": "object"})
            ],
            tenant_id="default",
        )
    )
    assert ctl.create_registry_record.called
    kwargs = ctl.create_registry_record.call_args.kwargs
    _assert_valid("CreateRegistryRecord", kwargs)


# ---------------------------------------------------------------------------
# Blank-description guard — ``Description`` has min length 1 on BOTH the create and
# update shapes, but ``McpServerCreate.description`` and ``Agent.purpose`` both default
# to ``""``, so a description-less record used to send a zero-length string and fail param
# validation against real AWS (a raw 500, not the 422 a schema rejection maps to).
#
# Only agent ``create`` was ever guarded (``purpose or name``); the MCP paths and BOTH
# agent UPDATE paths were not, and the update ones are the nastier half — a
# ``ParamValidationError`` is not a ``ClientError``, so ``update()``'s handler cannot map
# it and it escapes uncaught. These pin all of them against the real model.
# ---------------------------------------------------------------------------

def test_mcp_create_with_blank_description_still_valid(mcp_service, mcp_mock_registry_clients):
    ctl, _ = mcp_mock_registry_clients
    mcp_service.create(McpServerCreate(name="no-description-mcp", tenant_id="default"))

    kwargs = ctl.create_registry_record.call_args.kwargs
    _assert_valid("CreateRegistryRecord", kwargs)


def test_mcp_update_with_blank_description_still_valid(mcp_service, mcp_mock_registry_clients):
    ctl, _ = mcp_mock_registry_clients
    # A record whose server.json carries an empty description hydrates to description="",
    # so the read-modify-write would otherwise patch a zero-length string.
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
    assert agent.purpose == ""  # the precondition this test exists for

    ctl.update_registry_record.reset_mock()
    service.persist_identity(agent)

    kwargs = ctl.update_registry_record.call_args.kwargs
    _assert_valid("UpdateRegistryRecord", kwargs)
