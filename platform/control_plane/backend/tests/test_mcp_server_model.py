"""Unit tests for the MCP Server models and the envelope / server.json round-trip.

Pure-python tests: no boto3, no FastAPI, no AWS. The lifecycle machinery is reused verbatim from
``models.agent``. Every service-written block (the Entra identity and gateway fields, the Cedar
policy-engine fields, the tenant fields, the marketplace publication, the repo-ownership pair) is
pinned the same way: it rides ``to_envelope`` / ``from_record``, a pre-existing envelope without its
keys hydrates to the defaults so ``ENVELOPE_SCHEMA_VERSION`` stays 1, and it is absent from
``McpServerCreate`` and ``McpServerUpdate``.
"""

import json
import logging
from datetime import datetime
from typing import Any

import pytest

import models.agent as agent_module
from models.mcp_server import (
    ENVELOPE_SCHEMA_VERSION,
    DataClassification,
    IdentityStatus,
    Kind,
    LifecycleState,
    McpServer,
    McpServerCreate,
    McpServerUpdate,
    McpTool,
)


def _make_mcp_server() -> McpServer:
    return McpServer(
        id="mcp-7f3a1b2c9d",
        name="internal-claims-mcp",
        description="Read-only access to motor and property claims records for DE.",
        kind=Kind.STANDARD,
        owner_oid="00000000-0000-0000-0000-000000000000",
        owner_email="maria.bauer@example.com",
        business_unit="Claims",
        region="DE",
        data_classification=DataClassification.CONFIDENTIAL,
        endpoint_url="https://mcp.claims.acme.internal/mcp",
        version="1.0.0",
        available_tools=[
            McpTool(
                name="get_claim",
                description="Fetch a single claim by its claim number.",
                input_schema={
                    "type": "object",
                    "properties": {"claim_number": {"type": "string"}},
                    "required": ["claim_number"],
                },
            )
        ],
        gateway_arn=None,
        lifecycle_state=LifecycleState.APPROVED,
        entra_app_id=None,
        created_at=datetime(2026, 6, 2),
        updated_at=datetime(2026, 6, 2),
        created_by="lars.svensson@example.com",
    )


def _native_record(status: str = "DRAFT") -> dict:
    """A native record with NO ``displayName``, deliberately the older shape, so every test built on
    it also proves the ``displayName`` -> ``name`` fall-back still hydrates records written before
    the migration."""
    return {
        "recordId": "mcp-7f3a1b2c9d",
        "name": "internal-claims-mcp",
        "description": "native description (ignored — server.json is authoritative)",
        "status": status,
        "recordType": "MCP",
        "createdAt": datetime(2026, 6, 2),
        "updatedAt": datetime(2026, 6, 2),
    }


def test_kind_enum_values():
    assert Kind.GATEWAY == "gateway"
    assert Kind.STANDARD == "standard"


def test_to_envelope_includes_governance_fields():
    env = _make_mcp_server().to_envelope()
    assert env["schema_version"] == 1
    assert env["mcp_server_id"] == "mcp-7f3a1b2c9d"
    assert env["kind"] == "standard"
    assert env["owner_oid"] == "00000000-0000-0000-0000-000000000000"
    assert env["owner_email"] == "maria.bauer@example.com"
    assert env["business_unit"] == "Claims"
    assert env["region"] == "DE"
    assert env["data_classification"] == "Confidential"
    assert "entra_app_id" in env
    assert env["entra_app_id"] is None
    assert env["gateway_arn"] is None
    assert env["created_by"] == "lars.svensson@example.com"


def test_to_envelope_excludes_native_and_server_json_fields():
    env = _make_mcp_server().to_envelope()
    for excluded in (
        "name",
        "description",
        "version",
        "endpoint_url",
        "available_tools",
        "lifecycle_state",
    ):
        assert excluded not in env, f"{excluded} must not be in the envelope"


def test_to_envelope_enums_are_json_safe_strings():
    env = _make_mcp_server().to_envelope()
    reparsed = json.loads(json.dumps(env))
    assert reparsed["kind"] == "standard"
    assert reparsed["data_classification"] == "Confidential"
    assert isinstance(reparsed["kind"], str)
    assert isinstance(reparsed["data_classification"], str)


def test_to_server_json_namespaces_name():
    sj = _make_mcp_server().to_server_json()
    assert sj["name"] == "agp/internal-claims-mcp"


def test_to_server_json_includes_meta_governance():
    sj = _make_mcp_server().to_server_json()
    gov = sj["_meta"]["com.agp/governance"]
    assert gov["kind"] == "standard"


def test_to_server_json_remotes_from_endpoint_url():
    sj = _make_mcp_server().to_server_json()
    assert sj["remotes"][0]["url"] == "https://mcp.claims.acme.internal/mcp"
    assert sj["remotes"][0]["type"] == "streamable-http"

    no_endpoint = _make_mcp_server()
    no_endpoint.endpoint_url = None
    sj2 = no_endpoint.to_server_json()
    assert "remotes" not in sj2


def test_tools_as_mcp_maps_input_schema_key():
    mcp = _make_mcp_server()
    mcp.available_tools = [McpTool(name="get_claim", input_schema={"type": "object"})]
    assert mcp.tools_as_mcp() == [{"name": "get_claim", "description": "", "inputSchema": {"type": "object"}}]


def test_from_record_maps_native_fields_and_status():
    server_json = _make_mcp_server().to_server_json()
    mcp = McpServer.from_record(_native_record(status="DRAFT"), server_json, [])
    assert mcp.id == "mcp-7f3a1b2c9d"
    assert mcp.name == "internal-claims-mcp"
    assert mcp.lifecycle_state == LifecycleState.PROPOSED
    assert mcp.created_at == datetime(2026, 6, 2)
    assert mcp.updated_at == datetime(2026, 6, 2)


def test_from_record_prefers_display_name_over_name():
    """Native displayName WINS over the record's name.

    The two values must DIFFER or the test is a tautology. ``name`` is the registry's
    unique dedup key; ``displayName`` is the human-facing label the registry now shows and
    the one our writes set. The fall-back direction (no ``displayName`` at all → use
    ``name``) is what every other test here exercises, since ``_native_record`` omits it.
    """
    server_json = _make_mcp_server().to_server_json()
    record = {**_native_record(), "name": "dedup-key", "displayName": "Human Label"}
    assert McpServer.from_record(record, server_json, []).name == "Human Label"


def test_from_record_falls_back_to_name_without_display_name():
    """A record written before displayName existed must still hydrate, not KeyError."""
    server_json = _make_mcp_server().to_server_json()
    record = _native_record()
    assert "displayName" not in record
    assert McpServer.from_record(record, server_json, []).name == "internal-claims-mcp"


def test_from_record_reads_description_version_endpoint_from_server_json():
    server_json = _make_mcp_server().to_server_json()
    mcp = McpServer.from_record(_native_record(), server_json, [])
    assert mcp.description == "Read-only access to motor and property claims records for DE."
    assert mcp.version == "1.0.0"
    assert mcp.endpoint_url == "https://mcp.claims.acme.internal/mcp"


def test_from_record_populates_governance_from_meta_envelope():
    server_json = _make_mcp_server().to_server_json()
    mcp = McpServer.from_record(_native_record(), server_json, [])
    assert mcp.kind == Kind.STANDARD
    assert mcp.owner_oid == "00000000-0000-0000-0000-000000000000"
    assert mcp.owner_email == "maria.bauer@example.com"
    assert mcp.business_unit == "Claims"
    assert mcp.region == "DE"
    assert mcp.data_classification == DataClassification.CONFIDENTIAL
    assert mcp.created_by == "lars.svensson@example.com"


def test_from_record_maps_tools():
    server_json = _make_mcp_server().to_server_json()
    tools = [{"name": "x", "description": "d", "inputSchema": {"type": "object"}}]
    mcp = McpServer.from_record(_native_record(), server_json, tools)
    assert isinstance(mcp.available_tools[0], McpTool)
    assert mcp.available_tools[0].name == "x"
    assert mcp.available_tools[0].description == "d"
    assert mcp.available_tools[0].input_schema == {"type": "object"}


def test_from_record_defaults_kind_standard_when_missing():
    server_json = {
        "name": "agp/x",
        "description": "",
        "version": "1.0.0",
        "_meta": {"com.agp/governance": {"schema_version": 1}},
    }
    mcp = McpServer.from_record(_native_record(), server_json, [])
    assert mcp.kind == Kind.STANDARD


def test_from_record_empty_tools():
    server_json = _make_mcp_server().to_server_json()
    mcp = McpServer.from_record(_native_record(), server_json, [])
    assert mcp.available_tools == []


def test_from_record_rejects_invalid_kind():
    from pydantic import ValidationError

    server_json = {
        "name": "agp/x",
        "description": "",
        "version": "1.0.0",
        "_meta": {"com.agp/governance": {"schema_version": 1, "kind": "bogus"}},
    }
    with pytest.raises(ValidationError):
        McpServer.from_record(_native_record(), server_json, [])


def test_from_record_rejects_invalid_data_classification():
    from pydantic import ValidationError

    server_json = {
        "name": "agp/x",
        "description": "",
        "version": "1.0.0",
        "_meta": {"com.agp/governance": {"schema_version": 1, "data_classification": "bogus"}},
    }
    with pytest.raises(ValidationError):
        McpServer.from_record(_native_record(), server_json, [])


def test_from_record_tolerates_remote_without_url():
    server_json = _make_mcp_server().to_server_json()
    server_json["remotes"] = [{"type": "streamable-http"}]  # no url key
    mcp = McpServer.from_record(_native_record(), server_json, [])
    assert mcp.endpoint_url is None


def test_round_trip_preserves_governance_and_tools():
    original = _make_mcp_server()
    server_json = original.to_server_json()
    tools = original.tools_as_mcp()
    rebuilt = McpServer.from_record(_native_record(status="APPROVED"), server_json, tools)
    assert rebuilt.kind == original.kind
    assert rebuilt.owner_oid == original.owner_oid
    assert rebuilt.owner_email == original.owner_email
    assert rebuilt.business_unit == original.business_unit
    assert rebuilt.region == original.region
    assert rebuilt.data_classification == original.data_classification
    assert rebuilt.created_by == original.created_by
    assert rebuilt.description == original.description
    assert rebuilt.version == original.version
    assert rebuilt.endpoint_url == original.endpoint_url
    assert len(rebuilt.available_tools) == 1
    assert rebuilt.available_tools[0].name == original.available_tools[0].name
    assert rebuilt.available_tools[0].input_schema == original.available_tools[0].input_schema


def test_mcp_server_create_minimal_only_name():
    c = McpServerCreate(name="x", tenant_id="default")
    assert c.name == "x"
    assert c.kind == Kind.STANDARD
    assert c.version == "1.0.0"
    assert c.available_tools == []


def test_mcp_server_update_all_optional_exclude_none():
    assert McpServerUpdate().model_dump(exclude_none=True) == {}


def test_mcp_server_update_runtime_arn_patchable():
    """runtime_arn is user-suppliable (parallel to gateway_arn) so it must appear on
    McpServerUpdate — confirming a PATCH can set it via model_dump(exclude_none=True)."""
    assert McpServerUpdate(runtime_arn="arn:aws:bedrock-agentcore:eu-central-1:123:runtime/x").model_dump(
        exclude_none=True
    ) == {"runtime_arn": "arn:aws:bedrock-agentcore:eu-central-1:123:runtime/x"}


def test_mcp_server_id_has_no_default():
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        McpServer.model_validate(
            {
                "name": "x",
                "lifecycle_state": LifecycleState.PROPOSED,
                "created_at": datetime(2026, 6, 2),
                "updated_at": datetime(2026, 6, 2),
            }
        )  # missing required id


def test_lifecycle_reused_from_agent_module():
    assert LifecycleState is agent_module.LifecycleState


def _make_mcp_server_with_identity() -> McpServer:
    """An McpServer with all 7 identity/gateway fields + runtime_arn set to distinct sentinel
    values. The identity block mirrors the agent's field for field; ``runtime_arn`` is the one
    user-suppliable member (on Base, parallel to ``gateway_arn``)."""
    mcp = _make_mcp_server()
    mcp.runtime_arn = "arn:aws:bedrock-agentcore:eu-central-1:123:runtime/mcp-rt-9"
    mcp.entra_sp_id = "sp-1111"
    mcp.entra_app_audience = "api://agp-mcp-rec-1"
    mcp.invoker_role_id = "role-invoker-2222"
    mcp.admin_role_id = "role-admin-3333"
    mcp.gateway_id = "gw-abc123"
    mcp.gateway_url = "https://gw-abc123.gateway.bedrock-agentcore.eu-central-1.amazonaws.com/mcp"
    mcp.identity_status = IdentityStatus.PROVISIONED
    return mcp


def test_kind_has_runtime_member():
    assert Kind.RUNTIME == "runtime"


def test_to_envelope_includes_identity_and_gateway_fields():
    """to_envelope carries all 7 new keys with their values (plain pass-through)."""
    env = _make_mcp_server_with_identity().to_envelope()
    assert env["entra_sp_id"] == "sp-1111"
    assert env["entra_app_audience"] == "api://agp-mcp-rec-1"
    assert env["invoker_role_id"] == "role-invoker-2222"
    assert env["admin_role_id"] == "role-admin-3333"
    assert env["gateway_id"] == "gw-abc123"
    assert env["gateway_url"] == "https://gw-abc123.gateway.bedrock-agentcore.eu-central-1.amazonaws.com/mcp"
    assert env["identity_status"] == "provisioned"
    reparsed = json.loads(json.dumps(env))
    assert reparsed["entra_sp_id"] == "sp-1111"
    assert reparsed["identity_status"] == "provisioned"


def test_from_record_reads_identity_and_gateway_fields():
    """from_record hydrates all 7 new fields from an envelope carrying them."""
    server_json = {
        "name": "agp/x",
        "description": "",
        "version": "1.0.0",
        "_meta": {
            "com.agp/governance": {
                "schema_version": 1,
                "entra_sp_id": "sp-1111",
                "entra_app_audience": "api://agp-mcp-rec-1",
                "invoker_role_id": "role-invoker-2222",
                "admin_role_id": "role-admin-3333",
                "gateway_id": "gw-abc123",
                "gateway_url": "https://gw-abc123.example/mcp",
                "identity_status": "provisioned",
            }
        },
    }
    mcp = McpServer.from_record(_native_record(status="APPROVED"), server_json, [])
    assert mcp.entra_sp_id == "sp-1111"
    assert mcp.entra_app_audience == "api://agp-mcp-rec-1"
    assert mcp.invoker_role_id == "role-invoker-2222"
    assert mcp.admin_role_id == "role-admin-3333"
    assert mcp.gateway_id == "gw-abc123"
    assert mcp.gateway_url == "https://gw-abc123.example/mcp"
    assert mcp.identity_status == "provisioned"


def test_from_record_defaults_identity_status_none_when_missing():
    """An envelope without the identity keys hydrates identity_status to 'none' and the other six
    fields to None."""
    server_json = {
        "name": "agp/x",
        "description": "",
        "version": "1.0.0",
        "_meta": {"com.agp/governance": {"schema_version": 1}},
    }
    mcp = McpServer.from_record(_native_record(status="APPROVED"), server_json, [])
    assert mcp.identity_status == "none"
    assert mcp.entra_sp_id is None
    assert mcp.entra_app_audience is None
    assert mcp.invoker_role_id is None
    assert mcp.admin_role_id is None
    assert mcp.gateway_id is None
    assert mcp.gateway_url is None


def test_round_trip_preserves_new_fields():
    """All 7 new fields (plus runtime_arn) survive a to_envelope -> from_record round-trip."""
    original = _make_mcp_server_with_identity()
    server_json = original.to_server_json()
    rebuilt = McpServer.from_record(_native_record(status="APPROVED"), server_json, original.tools_as_mcp())
    assert rebuilt.entra_sp_id == original.entra_sp_id
    assert rebuilt.entra_app_audience == original.entra_app_audience
    assert rebuilt.invoker_role_id == original.invoker_role_id
    assert rebuilt.admin_role_id == original.admin_role_id
    assert rebuilt.gateway_id == original.gateway_id
    assert rebuilt.gateway_url == original.gateway_url
    assert rebuilt.identity_status == original.identity_status
    assert rebuilt.runtime_arn == original.runtime_arn


def test_runtime_arn_on_base_round_trips():
    """runtime_arn set on an McpServer survives to_server_json/to_envelope -> from_record."""
    original = _make_mcp_server()
    original.runtime_arn = "arn:aws:bedrock-agentcore:eu-central-1:123:runtime/mcp-rt-9"
    server_json = original.to_server_json()
    rebuilt = McpServer.from_record(_native_record(status="APPROVED"), server_json, [])
    assert rebuilt.runtime_arn == "arn:aws:bedrock-agentcore:eu-central-1:123:runtime/mcp-rt-9"


def test_create_does_not_require_identity_fields():
    """The identity fields are service-written, NOT user-supplied: McpServerCreate must
    validate with them absent, and (pydantic v2 default extra='ignore') silently drop an
    unexpected one rather than raise -- we must NOT add extra='forbid' (the workspace
    shares one .env and forbids it). runtime_arn IS user-suppliable (on Base)."""
    c = McpServerCreate(name="x", tenant_id="default")
    assert c.name == "x"
    assert c.runtime_arn is None
    extra = McpServerCreate.model_validate({"name": "x", "tenant_id": "default", "entra_sp_id": "y"})
    assert "entra_sp_id" not in extra.model_dump()


def test_envelope_schema_version_still_1():
    """The identity block is additive and backward-compatible: the envelope schema version stays 1."""
    assert ENVELOPE_SCHEMA_VERSION == 1
    assert _make_mcp_server_with_identity().to_envelope()["schema_version"] == 1


_CEDAR_ENGINE_ARN = (
    "arn:aws:bedrock-agentcore:us-east-1:111122223333:policy-engine/agp-cedar-mcp-1-aaaaaaaaaa"
)


def _make_mcp_server_with_cedar_engine() -> McpServer:
    """An McpServer with the three Cedar policy-engine fields set."""
    mcp = _make_mcp_server()
    mcp.cedar_policy_engine_id = "pe-abc"
    mcp.cedar_policy_engine_arn = _CEDAR_ENGINE_ARN
    mcp.cedar_enforcement_mode = "enforce"
    return mcp


def test_to_envelope_includes_cedar_engine_fields():
    """to_envelope carries the three new keys with their values (plain pass-through)."""
    env = _make_mcp_server_with_cedar_engine().to_envelope()
    assert env["cedar_policy_engine_id"] == "pe-abc"
    assert env["cedar_policy_engine_arn"] == _CEDAR_ENGINE_ARN
    assert env["cedar_enforcement_mode"] == "enforce"


def test_from_record_reads_cedar_engine_fields():
    """from_record hydrates the three new fields from an envelope carrying them."""
    server_json = {
        "name": "agp/x",
        "description": "",
        "version": "1.0.0",
        "_meta": {
            "com.agp/governance": {
                "schema_version": 1,
                "cedar_policy_engine_id": "pe-abc",
                "cedar_policy_engine_arn": _CEDAR_ENGINE_ARN,
                "cedar_enforcement_mode": "enforce",
            }
        },
    }
    mcp = McpServer.from_record(_native_record(status="APPROVED"), server_json, [])
    assert mcp.cedar_policy_engine_id == "pe-abc"
    assert mcp.cedar_policy_engine_arn == _CEDAR_ENGINE_ARN
    assert mcp.cedar_enforcement_mode == "enforce"


def test_from_record_defaults_cedar_enforcement_mode_none_when_missing():
    """An envelope without the Cedar keys hydrates the ids to None and cedar_enforcement_mode to
    'none'."""
    server_json = {
        "name": "agp/x",
        "description": "",
        "version": "1.0.0",
        "_meta": {"com.agp/governance": {"schema_version": 1}},
    }
    mcp = McpServer.from_record(_native_record(status="APPROVED"), server_json, [])
    assert mcp.cedar_policy_engine_id is None
    assert mcp.cedar_policy_engine_arn is None
    assert mcp.cedar_enforcement_mode == "none"


def test_round_trip_preserves_cedar_engine_fields():
    """The three new fields survive a to_server_json -> from_record round-trip."""
    original = _make_mcp_server_with_cedar_engine()
    server_json = original.to_server_json()
    rebuilt = McpServer.from_record(_native_record(status="APPROVED"), server_json, original.tools_as_mcp())
    assert rebuilt.cedar_policy_engine_id == original.cedar_policy_engine_id
    assert rebuilt.cedar_policy_engine_arn == original.cedar_policy_engine_arn
    assert rebuilt.cedar_enforcement_mode == original.cedar_enforcement_mode


def test_cedar_fields_not_on_create():
    """The Cedar engine fields are service-written, read-model only: they must NOT be
    declared on McpServerCreate, yet McpServerCreate(name='x') still validates."""
    assert "cedar_policy_engine_id" not in McpServerCreate.model_fields
    assert "cedar_policy_engine_arn" not in McpServerCreate.model_fields
    assert "cedar_enforcement_mode" not in McpServerCreate.model_fields
    assert McpServerCreate(name="x", tenant_id="default").name == "x"


def test_envelope_schema_version_still_1_with_cedar_engine():
    """The Cedar block is additive and backward-compatible: the envelope schema version stays 1."""
    assert _make_mcp_server_with_cedar_engine().to_envelope()["schema_version"] == 1


def _make_mcp_server_with_tenant() -> McpServer:
    mcp = _make_mcp_server()
    mcp.tenant_id = "ten-claims"
    mcp.published = True
    mcp.shared = True
    return mcp


def test_tenant_fields_round_trip_through_envelope():
    """tenant_id/published/shared survive to_server_json -> from_record. The MCP
    to_envelope serializes Base fields EXPLICITLY (they are not carried
    generically), so this guards the three hand-written lines."""
    original = _make_mcp_server_with_tenant()
    env = original.to_envelope()
    assert env["tenant_id"] == "ten-claims"
    assert env["published"] is True
    assert env["shared"] is True

    server_json = original.to_server_json()
    rebuilt = McpServer.from_record(_native_record(status="APPROVED"), server_json, original.tools_as_mcp())
    assert rebuilt.tenant_id == "ten-claims"
    assert rebuilt.published is True
    assert rebuilt.shared is True


def test_from_record_tolerates_an_envelope_without_tenant_fields():
    """An envelope lacking the tenant keys hydrates to None/False/False."""
    server_json = {
        "name": "agp/x",
        "description": "",
        "version": "1.0.0",
        "_meta": {"com.agp/governance": {"schema_version": 1}},
    }
    mcp = McpServer.from_record(_native_record(status="APPROVED"), server_json, [])
    assert mcp.tenant_id is None
    assert mcp.published is False
    assert mcp.shared is False


def test_envelope_schema_version_still_1_with_tenant_fields():
    """The tenant fields are additive and backward-compatible: the envelope schema version stays 1."""
    assert _make_mcp_server_with_tenant().to_envelope()["schema_version"] == 1
    assert ENVELOPE_SCHEMA_VERSION == 1


DECLARED_AT = datetime(2026, 8, 11, 12, 0, 0)


def _publication(published: bool = True):
    """A MarketplacePublication with a fully-populated declared datasheet: publish is the one door
    for both product types, so the MCP envelope carries the same model the agent envelope does."""
    from models.marketplace import Datasheet, MarketplacePublication

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


def _make_mcp_server_with_marketplace(published: bool = True) -> McpServer:
    mcp = _make_mcp_server()
    mcp.marketplace = _publication(published=published)
    return mcp


def test_marketplace_block_round_trips_through_envelope():
    """The declared publication survives to_envelope -> to_server_json -> from_record,
    nested datasheet and attestation fields included. The block is dumped with
    ``mode="json"`` so the envelope stays ``json.dumps``-able (the descriptor is a
    stringified server.json)."""
    original = _make_mcp_server_with_marketplace()

    env = original.to_envelope()
    assert env["marketplace"]["published"] is True
    assert env["marketplace"]["datasheet"]["owner_team"] == "Platform Engineering"
    assert env["marketplace"]["declared_by"] == "admin@acme.com"
    assert isinstance(env["marketplace"]["declared_at"], str)
    json.dumps(env)  # must stay json.dumps-able

    rebuilt = McpServer.from_record(
        _native_record(status="APPROVED"), original.to_server_json(), original.tools_as_mcp()
    )
    assert rebuilt.marketplace is not None
    assert rebuilt.marketplace.published is True
    assert rebuilt.marketplace.datasheet.compliance == ["GDPR", "BaFin"]
    assert rebuilt.marketplace.datasheet.support_contact == "mcp-platform@acme.com"
    assert rebuilt.marketplace.declared_by == "admin@acme.com"
    assert rebuilt.marketplace.declared_at == DECLARED_AT


def test_marketplace_unpublished_block_round_trips():
    """An unpublish KEEPS the block with ``published=False`` (declared history retained),
    so the false value must survive the round-trip rather than reading back as absent."""
    original = _make_mcp_server_with_marketplace(published=False)
    rebuilt = McpServer.from_record(_native_record(status="APPROVED"), original.to_server_json(), [])
    assert rebuilt.marketplace is not None
    assert rebuilt.marketplace.published is False
    assert rebuilt.marketplace.datasheet.owner_team == "Platform Engineering"


def test_from_record_tolerates_an_envelope_without_marketplace():
    """An envelope lacking the key hydrates to None; an explicit stored ``null`` is tolerated too."""
    base = {
        "name": "agp/x",
        "description": "",
        "version": "1.0.0",
        "_meta": {"com.agp/governance": {"schema_version": 1}},
    }
    assert McpServer.from_record(_native_record(), base, []).marketplace is None

    with_null = {
        **base,
        "_meta": {"com.agp/governance": {"schema_version": 1, "marketplace": None}},
    }
    assert McpServer.from_record(_native_record(), with_null, []).marketplace is None


def test_from_record_raises_validation_error_on_malformed_marketplace_block():
    """A structurally invalid block is a pydantic ``ValidationError`` at the parse site (the
    same class of fault as a non-JSON envelope). The service's hydrate seam is what
    translates it into its malformed-record error so ONE bad record can't 500 ``list()`` —
    see test_mcp_server_service."""
    from pydantic import ValidationError

    server_json = {
        "name": "agp/x",
        "description": "",
        "version": "1.0.0",
        "_meta": {"com.agp/governance": {"schema_version": 1, "marketplace": {"published": True}}},
    }
    with pytest.raises(ValidationError):
        McpServer.from_record(_native_record(), server_json, [])


def test_marketplace_is_not_settable_by_a_request_body():
    """SERVICE-WRITTEN (the ``project_id`` convention): the block lives on ``McpServer`` +
    the envelope ONLY, so a body key of that name is dropped by pydantic's default
    ``extra="ignore"`` on both create and update — a caller can never forge an attestation."""
    assert "marketplace" not in McpServerCreate.model_fields
    assert "marketplace" not in McpServerUpdate.model_fields

    created = McpServerCreate.model_validate(
        {"name": "x", "tenant_id": "default", "marketplace": {"published": True}}
    )
    assert not hasattr(created, "marketplace")
    updated = McpServerUpdate.model_validate({"marketplace": {"published": True}})
    assert not hasattr(updated, "marketplace")


def test_envelope_schema_version_still_1_with_marketplace():
    """The marketplace block is additive and backward-compatible: the envelope schema version stays 1."""
    assert _make_mcp_server_with_marketplace().to_envelope()["schema_version"] == 1
    assert ENVELOPE_SCHEMA_VERSION == 1


def test_marketplace_key_always_present_in_envelope():
    """Envelope discipline: the key must be WRITTEN (as ``null`` when there is no block),
    not omitted — an omitted key on a rewrite would leave a stale block in place on the
    next read, and a key missing from ``to_envelope`` is silently destroyed on every write."""
    env = _make_mcp_server().to_envelope()
    assert "marketplace" in env
    assert env["marketplace"] is None


def test_identity_status_reused_from_agent_module():
    """The MCP side must not redefine the enum: a second class would compare unequal
    to the agent-side one and quietly split the contract in two."""
    assert IdentityStatus is agent_module.IdentityStatus
    assert [s.value for s in IdentityStatus] == ["none", "pending", "provisioned", "failed"]
    assert IdentityStatus.PROVISIONED == "provisioned"


def test_mcp_defaults_identity_status_to_enum_none():
    assert _make_mcp_server().identity_status is IdentityStatus.NONE


def test_mcp_to_envelope_stores_the_plain_value_never_the_enum_repr():
    """PINNED: the envelope (which becomes `server.json` `_meta`, then a stored string)
    must hold `"none"`, NEVER `"IdentityStatus.NONE"`."""
    env = _make_mcp_server().to_envelope()
    assert env["identity_status"] == "none"
    assert type(env["identity_status"]) is str
    assert "IdentityStatus" not in json.dumps(env)
    assert "IdentityStatus" not in json.dumps(_make_mcp_server().to_server_json())


def test_mcp_to_envelope_tolerates_a_service_assigned_raw_string(monkeypatch):
    """`services/mcp_identity_service.py:351` assigns `IdentityStatus.FAILED`
    post-construction, but pydantic does not validate assignment, so nothing STOPS a
    future writer from assigning a raw string — the serializer must pass one through
    rather than fail on `.value`."""
    mcp = _make_mcp_server()
    monkeypatch.setattr(mcp, "identity_status", "failed")
    assert mcp.to_envelope()["identity_status"] == "failed"


def test_mcp_from_record_hydrates_a_legacy_envelope_string_to_the_enum():
    server_json = {
        "name": "agp/x",
        "description": "",
        "version": "1.0.0",
        "_meta": {"com.agp/governance": {"schema_version": 1, "identity_status": "provisioned"}},
    }
    mcp = McpServer.from_record(_native_record(status="APPROVED"), server_json, [])
    assert mcp.identity_status is IdentityStatus.PROVISIONED
    assert mcp.identity_status == "provisioned"


def test_mcp_from_record_coerces_unknown_identity_status_to_none_and_warns(caplog):
    """Fails CLOSED on a foreign value instead of raising (the buildspec writes this
    field into the envelope externally), deliberately unlike `lifecycle_for_status`."""
    server_json = {
        "name": "agp/x",
        "description": "",
        "version": "1.0.0",
        "_meta": {"com.agp/governance": {"schema_version": 1, "identity_status": "provisionned"}},
    }
    with caplog.at_level(logging.WARNING, logger="models.agent"):
        mcp = McpServer.from_record(_native_record(status="APPROVED"), server_json, [])
    assert mcp.identity_status is IdentityStatus.NONE
    assert "provisionned" in caplog.text


def test_to_envelope_omits_dropped_debris_keys():
    """The reserved `cedar_policy_set_id` (superseded by `cedar_policy_engine_id`) and the write-only
    `"origin": "Registered"` literal (never hydrated; the frontend reads `origin` off the agent model)
    are gone: neither key is written and the field is off every model, so a future edit cannot
    reintroduce either one silently."""
    env = _make_mcp_server().to_envelope()
    assert "cedar_policy_set_id" not in env
    assert "origin" not in env
    for model in (McpServer, McpServerCreate, McpServerUpdate):
        assert "cedar_policy_set_id" not in model.model_fields


def test_from_record_ignores_legacy_debris_keys():
    """Stored envelopes written before the removal still carry both keys. Hydration reads
    the envelope key by key (never `**`-splats it), so a legacy record still loads
    unchanged and both keys are simply dropped on the next read-modify-write."""
    server_json = {
        "name": "agp/x",
        "description": "",
        "version": "1.0.0",
        "_meta": {
            "com.agp/governance": {
                "schema_version": 1,
                "kind": "standard",
                "owner_email": "maria.bauer@example.com",
                "cedar_policy_set_id": "legacy-policy-set",  # the dropped field
                "origin": "Registered",  # the write-only literal
                "cedar_policy_engine_id": "pe-abc",  # what superseded it
            }
        },
    }
    mcp = McpServer.from_record(_native_record(status="APPROVED"), server_json, [])
    assert mcp.owner_email == "maria.bauer@example.com"
    assert mcp.cedar_policy_engine_id == "pe-abc"
    assert not hasattr(mcp, "cedar_policy_set_id")
    assert not hasattr(mcp, "origin")
    rewritten = mcp.to_envelope()
    assert "cedar_policy_set_id" not in rewritten
    assert "origin" not in rewritten


def test_create_rejects_description_over_100_chars_naming_the_field():
    """server.json caps `description` at 100 chars; the write models validate it on the way in."""
    from pydantic import ValidationError

    with pytest.raises(ValidationError) as exc:
        McpServerCreate(name="x", tenant_id="default", description="d" * 101)
    assert exc.value.errors()[0]["loc"] == ("description",)


def test_create_accepts_description_of_exactly_100_chars():
    created = McpServerCreate(name="x", tenant_id="default", description="d" * 100)
    assert created.description is not None
    assert len(created.description) == 100


def test_update_rejects_description_over_100_chars_naming_the_field():
    from pydantic import ValidationError

    with pytest.raises(ValidationError) as exc:
        McpServerUpdate(description="d" * 101)
    assert exc.value.errors()[0]["loc"] == ("description",)


def test_read_side_still_hydrates_a_longer_stored_description():
    """The cap guards writes only — a record that already holds a longer description
    must still list, not fail hydration."""
    mcp = McpServer(
        id="r1",
        name="x",
        description="d" * 300,
        lifecycle_state=LifecycleState.PROPOSED,
        created_at=datetime(2026, 6, 2),
        updated_at=datetime(2026, 6, 2),
    )
    assert mcp.description is not None
    assert len(mcp.description) == 300


def test_project_and_repo_id_round_trip_through_envelope():
    """The repo-ownership pair is service-written like the marketplace block: stamped by
    ProjectService when a repo materializes the record."""
    mcp = _make_mcp_server()
    mcp.project_id = "p1"
    mcp.repo_id = "r1"
    env = mcp.to_envelope()
    assert env["project_id"] == "p1"
    assert env["repo_id"] == "r1"
    rebuilt = McpServer.from_record(_native_record(), mcp.to_server_json(), [])
    assert (rebuilt.project_id, rebuilt.repo_id) == ("p1", "r1")
    assert rebuilt.is_repo_owned is True


def test_ownership_keys_always_present_in_envelope_and_default_none():
    """Written as explicit nulls (envelope discipline — see the marketplace key above)."""
    env = _make_mcp_server().to_envelope()
    assert env["project_id"] is None
    assert env["repo_id"] is None


def test_from_record_tolerates_an_envelope_without_ownership():
    mcp = McpServer.from_record(_native_record(), _make_mcp_server().to_server_json(), [])
    assert mcp.project_id is None
    assert mcp.repo_id is None
    assert mcp.is_repo_owned is False


def test_is_repo_owned_is_a_read_only_property_not_a_wire_field():
    mcp = _make_mcp_server()
    untyped: Any = mcp
    with pytest.raises(AttributeError):
        untyped.is_repo_owned = True
    assert "is_repo_owned" not in mcp.model_dump()


def test_ownership_is_not_settable_by_a_request_body():
    for field in ("project_id", "repo_id"):
        assert field not in McpServerCreate.model_fields
        assert field not in McpServerUpdate.model_fields
    created = McpServerCreate.model_validate(
        {"name": "x", "tenant_id": "default", "project_id": "p1", "repo_id": "r1"}
    )
    assert not hasattr(created, "project_id")
    assert not hasattr(created, "repo_id")
    updated = McpServerUpdate.model_validate({"project_id": "p1", "repo_id": "r1"})
    assert "project_id" not in updated.model_dump(exclude_none=True)
    assert "repo_id" not in updated.model_dump(exclude_none=True)
