"""Model tests for the agent models: the write-time ``agent_arn`` guard, the service-written field
seam, the envelope discipline and the mirror the invoke-time guard keeps of this module's region regex.

Pure models: no I/O, no HTTP harness needed.
"""

from __future__ import annotations

import logging
from typing import Any, ClassVar

import pytest
from pydantic import ValidationError

from models.agent import (
    AWS_ARN_REGION_RE,
    Agent,
    AgentCreate,
    AgentUpdate,
    RuntimeStatus,
    is_harness_arn,
)

GOOD_ARN = "arn:aws:bedrock-agentcore:us-east-1:111111111111:runtime/my-agent"
BAD_REGION_ARN = "arn:aws:bedrock-agentcore:not-a-region:111111111111:runtime/my-agent"
SHORT_ARN = "arn:aws:bedrock-agentcore"
HOST_INJECTION_ARN = "arn:aws:bedrock-agentcore:evil.example.com:111111111111:runtime/x"

RECORD = {
    "recordId": "a1",
    "name": "claims",
    "description": "",
    "status": "DRAFT",
    "createdAt": "2026-10-13T00:00:00Z",
    "updatedAt": "2026-10-13T00:00:00Z",
}

SERVICE_WRITTEN_FIELDS = (
    "entra_app_id",
    "entra_api_app_id",
    "entra_sp_id",
    "entra_app_audience",
    "invoker_role_id",
    "admin_role_id",
    "identity_status",
    "oauth2_credential_provider_name",
    "project_id",
    "langfuse_project_id",
    "langfuse_key_secret_name",
    "marketplace",
    "bound_stage",
)

ENVELOPE_KEYS = frozenset(
    {
        "schema_version",
        "agent_id",
        "sponsor_oid",
        "sponsor_email",
        "business_unit",
        "region",
        "data_classification",
        "platform",
        "framework",
        "model_id",
        "mcp_server_ids",
        "declared_mcp_server_ids",
        "entra_app_id",
        "entra_api_app_id",
        "origin",
        "created_by",
        "endpoint_url",
        "auth_type",
        "agent_arn",
        "agent_arns",
        "entra_sp_id",
        "entra_app_audience",
        "invoker_role_id",
        "admin_role_id",
        "identity_status",
        "oauth2_credential_provider_name",
        "tenant_id",
        "published",
        "project_id",
        "langfuse_project_id",
        "langfuse_key_secret_name",
        "runtime_handle",
        "runtime_kind",
        "binding_mode",
        "databricks_sp_id",
        "databricks_sp_secret_arn",
        "oauth2_app_client_id",
        "marketplace",
        "bound_stage",
        "trace_delivery",
    }
)


def _create_body(**overrides):
    body: dict[str, Any] = dict(name="N", tenant_id="ten-1")
    body.update(overrides)
    return body


class TestAgentCreateArnValidation:
    def test_accepts_a_well_formed_arn(self):
        agent = AgentCreate(**_create_body(agent_arn=GOOD_ARN))
        assert agent.agent_arn == GOOD_ARN

    def test_accepts_no_arn_at_all(self):
        agent = AgentCreate(**_create_body())
        assert agent.agent_arn is None

    @pytest.mark.parametrize("arn", [BAD_REGION_ARN, SHORT_ARN, HOST_INJECTION_ARN])
    def test_refuses_a_malformed_or_bad_region_arn(self, arn):
        """A 422 at the API boundary; here, a ValidationError at the model boundary."""
        with pytest.raises(ValidationError):
            AgentCreate(**_create_body(agent_arn=arn))


class TestAgentUpdateArnValidation:
    def test_accepts_a_well_formed_arn(self):
        upd = AgentUpdate(agent_arn=GOOD_ARN)
        assert upd.agent_arn == GOOD_ARN

    def test_absent_means_leave_unchanged_and_is_not_validated(self):
        upd = AgentUpdate()
        assert upd.agent_arn is None

    @pytest.mark.parametrize("arn", [BAD_REGION_ARN, SHORT_ARN, HOST_INJECTION_ARN])
    def test_refuses_a_malformed_or_bad_region_arn(self, arn):
        with pytest.raises(ValidationError):
            AgentUpdate(agent_arn=arn)


class TestAwsArnRegionRe:
    """The shape itself, pinned by execution (same discipline as `TenantModel`'s regexes)."""

    @pytest.mark.parametrize(
        "region",
        [
            "us-east-1",
            "eu-central-1",
            "ap-southeast-4",
            "il-central-1",
            "us-gov-west-1",
            "us-iso-east-1",
            "us-isob-east-1",
        ],
    )
    def test_admits_every_real_partition_shape(self, region):
        assert AWS_ARN_REGION_RE.fullmatch(region)

    @pytest.mark.parametrize(
        "region",
        ["evil.example.com", "us-east-1/x", "US-EAST-1", "", "us-east-1\ninjected", "us-east-1@evil.com"],
    )
    def test_refuses_everything_an_exploit_needs(self, region):
        assert not AWS_ARN_REGION_RE.fullmatch(region)

    def test_the_invoke_time_guard_is_the_same_regex_object(self):
        """`platform_adapters.aws` checks the region at invoke time with this very pattern, so the
        write-time and invoke-time guards cannot drift into checking two different shapes."""
        from services.platform_adapters import aws

        assert aws._AWS_REGION_RE is AWS_ARN_REGION_RE


class TestIsHarnessArn:
    """The ARN's resource type is the harness/runtime discriminator."""

    def test_harness_arn_is_a_harness(self):
        assert is_harness_arn("arn:aws:bedrock-agentcore:us-east-1:123456789012:harness/hrn-abc")

    @pytest.mark.parametrize(
        "arn",
        [
            "arn:aws:bedrock-agentcore:us-east-1:123456789012:runtime/agent-abc",
            None,
            "",
            "harness/hrn-abc",
            "arn:aws:bedrock-agentcore:us-east-1:123456789012",
            "arn:aws:bedrock-agentcore:us-east-1:123456789012:runtime/harness/x",
        ],
    )
    def test_anything_else_is_not(self, arn):
        assert not is_harness_arn(arn)


class TestDeclaredMcpServerIds:
    """``declared_mcp_server_ids``: the MCP servers a developer WIRED the agent to, distinct from
    ``mcp_server_ids`` (what Governance GRANTED). An additive envelope key."""

    _RECORD: ClassVar[dict] = RECORD

    def test_round_trips_through_the_envelope(self):
        agent = Agent.from_record(
            self._RECORD, {"schema_version": 1, "declared_mcp_server_ids": ["m1", "m2"]}
        )
        assert agent.declared_mcp_server_ids == ["m1", "m2"]
        assert agent.to_envelope()["declared_mcp_server_ids"] == ["m1", "m2"]
        assert Agent.from_record(self._RECORD, agent.to_envelope()).declared_mcp_server_ids == [
            "m1",
            "m2",
        ]

    def test_an_old_record_without_the_key_hydrates_empty(self):
        agent = Agent.from_record(self._RECORD, {"schema_version": 1, "mcp_server_ids": ["g1"]})
        assert agent.declared_mcp_server_ids == []
        assert agent.mcp_server_ids == ["g1"]

    def test_create_and_update_accept_it(self):
        assert AgentCreate(**_create_body(declared_mcp_server_ids=["m1"])).declared_mcp_server_ids == ["m1"]
        assert AgentCreate(**_create_body()).declared_mcp_server_ids == []
        assert AgentUpdate(declared_mcp_server_ids=["m1"]).declared_mcp_server_ids == ["m1"]
        assert "declared_mcp_server_ids" not in AgentUpdate().model_dump(exclude_none=True)


class TestServiceWrittenSeam:
    """A service-written field lives on ``Agent`` and the envelope only: a request body cannot forge
    an attestation (``marketplace``), re-parent a record (``project_id``) or re-point its runtimes
    (``agent_arns``); pydantic's default ``extra="ignore"`` drops such a key."""

    @pytest.mark.parametrize("field", SERVICE_WRITTEN_FIELDS)
    def test_a_service_written_field_is_absent_from_create_and_update(self, field):
        assert field in Agent.model_fields
        assert field not in AgentCreate.model_fields
        assert field not in AgentUpdate.model_fields
        assert not hasattr(AgentCreate(**_create_body(**{field: "forged"})), field)
        assert field not in AgentUpdate.model_validate({field: "forged"}).model_dump(exclude_none=True)

    def test_update_has_no_agent_arns_while_create_does(self):
        assert "agent_arns" in AgentCreate.model_fields
        assert "agent_arns" not in AgentUpdate.model_fields
        assert "agent_arns" not in AgentUpdate.model_validate({"agent_arns": {"dev": GOOD_ARN}}).model_dump(
            exclude_none=True
        )

    def test_model_id_is_a_plain_field_despite_pydantics_protected_prefix(self):
        """pydantic reserves the ``model_`` prefix; the family opts out so ``model_id`` (the Bedrock
        model) is an ordinary field on every model and in the envelope."""
        assert "protected_namespaces" in AgentCreate.model_config
        assert AgentCreate.model_config["protected_namespaces"] == ()
        agent = AgentCreate(**_create_body(model_id="us.anthropic.claude-sonnet-4-6"))
        assert agent.model_dump()["model_id"] == "us.anthropic.claude-sonnet-4-6"
        assert "model_id" in Agent.model_fields


class TestEnvelopeDiscipline:
    """Every key is written on every save, ``None`` or an empty container when unset, and a key missing
    from a stored envelope hydrates to the field's default; this is what keeps the schema version at 1
    through every additive round and keeps a buildspec-patched ``agent_arns`` from being dropped."""

    def test_to_envelope_writes_every_key_even_when_unset(self):
        agent = Agent.from_record(RECORD, {"schema_version": 1})

        env = agent.to_envelope()

        assert set(env) == ENVELOPE_KEYS
        assert env["agent_arns"] == {}
        assert env["mcp_server_ids"] == []
        assert env["marketplace"] is None
        assert env["identity_status"] == "none"

    def test_from_record_hydrates_a_bare_envelope_with_defaults(self):
        agent = Agent.from_record(RECORD, {"schema_version": 1})

        assert agent.id == "a1" and agent.name == "claims"
        assert agent.lifecycle_state == "proposed"
        assert agent.auth_type == "none" and agent.origin == "Registered"
        assert agent.agent_arns == {} and agent.agent_arn is None
        assert agent.identity_status == "none"
        assert agent.published is False and agent.tenant_id is None
        assert agent.trace_delivery is None and agent.marketplace is None
        assert Agent.from_record(RECORD, agent.to_envelope()) == agent

    def test_a_stored_null_agent_arns_hydrates_to_an_empty_map(self):
        """`jq` can leave an explicit null behind; `or {}` absorbs it."""
        agent = Agent.from_record(RECORD, {"schema_version": 1, "agent_arns": None, "agent_arn": GOOD_ARN})
        assert agent.agent_arns == {}
        assert agent.runtime_arns() == {"unknown": GOOD_ARN}


def test_runtime_status_refuses_a_value_outside_its_set():
    base: dict[str, Any] = dict(agent_id="a1", stage="dev", checked_at="2026-10-29T00:00:00Z")
    assert RuntimeStatus(status="not_deployed", **base).status == "not_deployed"
    with pytest.raises(ValidationError):
        RuntimeStatus.model_validate({"status": "bogus", **base})


@pytest.mark.parametrize("mode", [None, "", "federation", "invoke_unavailable", "sp_secret"])
def test_a_stored_agent_binding_mode_inside_the_set_hydrates_verbatim_without_a_warning(mode, caplog):
    with caplog.at_level(logging.WARNING, logger="models.agent"):
        agent = Agent.from_record(RECORD, {"schema_version": 1, "binding_mode": mode})
    assert agent.binding_mode == mode
    assert caplog.records == []


def test_a_stored_agent_binding_mode_outside_the_set_folds_to_none_and_warns(caplog):
    with caplog.at_level(logging.WARNING, logger="models.agent"):
        agent = Agent.from_record(RECORD, {"schema_version": 1, "binding_mode": "legacy"})
    assert agent.binding_mode is None
    assert [r.levelname for r in caplog.records] == ["WARNING"]
    message = caplog.records[0].getMessage()
    assert "binding_mode" in message
    assert "'legacy'" in message
    assert RECORD["recordId"] in message
    assert agent.to_envelope()["binding_mode"] is None


def test_the_agent_auth_type_reaches_the_document_under_its_own_name():
    """Two enums named alike in one app made Pydantic qualify both by module."""
    from main import app

    schemas = app.openapi()["components"]["schemas"]
    assert "models__agent__AuthType" not in schemas and "AuthType" not in schemas
    assert schemas["AgentAuthType"]["enum"] == ["none", "entra", "api_key"]
