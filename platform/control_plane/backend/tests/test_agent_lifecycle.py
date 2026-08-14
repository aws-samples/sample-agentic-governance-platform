"""Unit tests for the Agent models + lifecycle/status mapping (Epic 4, Task 1).

Spec: the Epic 4 agent-registry plan (Task 1) and the AWS Agent Registry research notes
      (§4 envelope, §5 status mapping) — design artifacts kept outside this repository.

These are pure-python tests: no boto3, no FastAPI, no AWS.
"""

import json
import logging
from datetime import datetime
from enum import Enum

import pytest

from models.agent import (
    STATUS_TO_LIFECYCLE,
    TRANSITION_TO_STATUS,
    Agent,
    AgentCreate,
    AgentUpdate,
    AuthType,
    DataClassification,
    IdentityStatus,
    LifecycleState,
    Origin,
    Platform,
    UnknownRegistryStatusError,
    coerce_identity_status,
    lifecycle_for_status,
)


# The full native registry status enum (research §5). The mapping must be total over this set.
NATIVE_STATUSES = [
    "DRAFT",
    "PENDING_APPROVAL",
    "APPROVED",
    "REJECTED",
    "DEPRECATED",
    "CREATING",
    "UPDATING",
    "CREATE_FAILED",
    "UPDATE_FAILED",
]


# ---------------------------------------------------------------------------
# Mapping
# ---------------------------------------------------------------------------

def test_status_to_lifecycle_is_total_over_native_statuses():
    """Every native registry status maps to a LifecycleState (mapping is total)."""
    for status in NATIVE_STATUSES:
        assert status in STATUS_TO_LIFECYCLE, f"missing mapping for native status {status}"
        assert isinstance(STATUS_TO_LIFECYCLE[status], LifecycleState)


def test_status_to_lifecycle_specific_mappings():
    """The terminal/approval statuses map per research §5."""
    assert STATUS_TO_LIFECYCLE["DRAFT"] == LifecycleState.PROPOSED
    assert STATUS_TO_LIFECYCLE["PENDING_APPROVAL"] == LifecycleState.PENDING_APPROVAL
    assert STATUS_TO_LIFECYCLE["APPROVED"] == LifecycleState.APPROVED
    assert STATUS_TO_LIFECYCLE["REJECTED"] == LifecycleState.REJECTED
    assert STATUS_TO_LIFECYCLE["DEPRECATED"] == LifecycleState.DEPRECATED
    # transient sync states
    assert STATUS_TO_LIFECYCLE["CREATING"] == LifecycleState.PROPOSED
    assert STATUS_TO_LIFECYCLE["UPDATING"] == LifecycleState.APPROVED
    assert STATUS_TO_LIFECYCLE["CREATE_FAILED"] == LifecycleState.PROPOSED
    assert STATUS_TO_LIFECYCLE["UPDATE_FAILED"] == LifecycleState.APPROVED


def test_transition_to_status_mapping():
    assert TRANSITION_TO_STATUS == {
        "approve": "APPROVED",
        "reject": "REJECTED",
        "deprecate": "DEPRECATED",
    }


# ---------------------------------------------------------------------------
# to_envelope
# ---------------------------------------------------------------------------

def _make_agent() -> Agent:
    return Agent(
        id="rec-1",
        name="claims-triage-de",
        purpose="Triage inbound motor claims for the DE market",
        sponsor_oid="00000000-0000-0000-0000-000000000000",
        sponsor_email="maria.bauer@example.com",
        business_unit="Claims",
        region="DE",
        data_classification=DataClassification.CONFIDENTIAL,
        platform=Platform.AWS_BEDROCK,
        framework="langgraph",
        mcp_server_ids=["mcp-7f3a"],
        origin=Origin.REGISTERED,
        endpoint_url="https://agents.example.com/claims-triage-de",
        auth_type=AuthType.ENTRA,
        agent_arn="arn:aws:bedrock-agentcore:eu-central-1:123456789012:runtime/claims-triage-de",
        lifecycle_state=LifecycleState.APPROVED,
        entra_app_id=None,
        entra_api_app_id=None,
        created_at=datetime(2026, 6, 1),
        updated_at=datetime(2026, 6, 1),
        created_by="lars.svensson@example.com",
    )


def test_to_envelope_excludes_native_fields():
    """Envelope must EXCLUDE the native-record fields and the derived lifecycle_state."""
    env = _make_agent().to_envelope()
    for excluded in ("name", "purpose", "created_at", "updated_at", "lifecycle_state"):
        assert excluded not in env, f"{excluded} must not be in the envelope"


def test_to_envelope_includes_governance_fields():
    env = _make_agent().to_envelope()
    assert env["schema_version"] == 1
    assert env["sponsor_oid"] == "00000000-0000-0000-0000-000000000000"
    assert env["business_unit"] == "Claims"
    assert env["mcp_server_ids"] == ["mcp-7f3a"]
    assert env["entra_app_id"] is None
    # enums serialized as their .value (JSON-safe)
    assert env["platform"] == "aws_bedrock"
    assert env["origin"] == "Registered"
    assert env["data_classification"] == "Confidential"


def test_to_envelope_enums_are_json_safe_strings():
    env = _make_agent().to_envelope()
    # round-trips through json without error -> enums are plain strings
    reparsed = json.loads(json.dumps(env))
    assert reparsed["platform"] == "aws_bedrock"
    assert reparsed["origin"] == "Registered"


# ---------------------------------------------------------------------------
# Invocation fields (Epic 4b, Task 1): endpoint_url / auth_type / agent_arn
# ---------------------------------------------------------------------------

def test_to_envelope_includes_invocation_fields():
    """The envelope carries the new invocation fields, auth_type as its .value."""
    env = _make_agent().to_envelope()
    assert env["endpoint_url"] == "https://agents.example.com/claims-triage-de"
    assert env["auth_type"] == "entra"  # .value string, JSON-safe
    assert (
        env["agent_arn"]
        == "arn:aws:bedrock-agentcore:eu-central-1:123456789012:runtime/claims-triage-de"
    )
    # still JSON-safe end-to-end
    reparsed = json.loads(json.dumps(env))
    assert reparsed["auth_type"] == "entra"


def test_auth_type_enum_values():
    """AuthType mirrors the str-Enum style; values are the wire strings."""
    assert AuthType.NONE.value == "none"
    assert AuthType.ENTRA.value == "entra"
    assert AuthType.API_KEY.value == "api_key"


def test_from_record_invocation_fields_default_when_missing():
    """Old envelopes predate the invocation keys -> backward-compatible defaults."""
    # _make_agent envelope minus the invocation keys (simulating a pre-4b record)
    envelope = _make_agent().to_envelope()
    for key in ("endpoint_url", "auth_type", "agent_arn"):
        envelope.pop(key, None)
    agent = Agent.from_record(_fake_record(status="APPROVED"), envelope)
    assert agent.endpoint_url is None
    assert agent.auth_type == AuthType.NONE
    assert agent.agent_arn is None


def test_round_trip_preserves_invocation_fields():
    """endpoint_url + auth_type=entra + agent_arn survive a to_envelope/from_record round-trip."""
    original = _make_agent()
    rebuilt = Agent.from_record(_fake_record(status="APPROVED"), original.to_envelope())
    assert rebuilt.endpoint_url == original.endpoint_url
    assert rebuilt.auth_type == AuthType.ENTRA
    assert rebuilt.agent_arn == original.agent_arn


def test_agent_create_defaults_auth_type_none():
    """AgentCreate inherits the invocation fields; auth_type defaults to NONE."""
    c = AgentCreate(name="fraud-watch-eu", tenant_id="default")
    assert c.endpoint_url is None
    assert c.auth_type == AuthType.NONE
    assert c.agent_arn is None


def test_agent_update_accepts_invocation_fields_exclude_none():
    """AgentUpdate accepts the 3 invocation fields; exclude_none keeps only the set ones."""
    u = AgentUpdate(endpoint_url="https://x/y", auth_type=AuthType.API_KEY)
    dumped = u.model_dump(exclude_none=True)
    assert dumped == {"endpoint_url": "https://x/y", "auth_type": AuthType.API_KEY}
    assert "agent_arn" not in dumped


# ---------------------------------------------------------------------------
# from_record
# ---------------------------------------------------------------------------

def _fake_record(status: str = "APPROVED") -> dict:
    return {
        "recordId": "rec-1",
        "name": "claims-triage-de",
        "description": "Triage inbound motor claims for the DE market",
        "status": status,
        "recordType": "CUSTOM",
        "createdAt": datetime(2026, 6, 1),
        "updatedAt": datetime(2026, 6, 1),
    }


def test_from_record_maps_native_fields_and_status():
    envelope = _make_agent().to_envelope()
    agent = Agent.from_record(_fake_record(status="APPROVED"), envelope)
    assert agent.id == "rec-1"
    assert agent.name == "claims-triage-de"
    assert agent.purpose == "Triage inbound motor claims for the DE market"
    assert agent.lifecycle_state == LifecycleState.APPROVED
    assert agent.created_at == datetime(2026, 6, 1)
    assert agent.updated_at == datetime(2026, 6, 1)


def test_from_record_populates_governance_from_envelope():
    envelope = _make_agent().to_envelope()
    agent = Agent.from_record(_fake_record(), envelope)
    assert agent.business_unit == "Claims"
    assert agent.platform == Platform.AWS_BEDROCK
    assert agent.sponsor_oid == "00000000-0000-0000-0000-000000000000"
    assert agent.origin == Origin.REGISTERED
    assert agent.mcp_server_ids == ["mcp-7f3a"]


def test_from_record_coerces_string_enums():
    """Envelope enum values arrive as plain strings (from JSON) and must coerce."""
    envelope = {
        "schema_version": 1,
        "platform": "azure",
        "origin": "Deployed",
        "data_classification": "Internal",
        "mcp_server_ids": [],
    }
    agent = Agent.from_record(_fake_record(status="DRAFT"), envelope)
    assert agent.platform == Platform.AZURE
    assert agent.origin == Origin.DEPLOYED
    assert agent.data_classification == DataClassification.INTERNAL
    assert agent.lifecycle_state == LifecycleState.PROPOSED


def test_from_record_missing_description_defaults_empty_purpose():
    record = _fake_record()
    del record["description"]
    agent = Agent.from_record(record, {"schema_version": 1, "mcp_server_ids": []})
    assert agent.purpose == ""


def test_round_trip_preserves_governance_fields():
    original = _make_agent()
    rebuilt = Agent.from_record(_fake_record(status="APPROVED"), original.to_envelope())
    assert rebuilt.business_unit == original.business_unit
    assert rebuilt.platform == original.platform
    assert rebuilt.sponsor_oid == original.sponsor_oid
    assert rebuilt.sponsor_email == original.sponsor_email
    assert rebuilt.origin == original.origin
    assert rebuilt.mcp_server_ids == original.mcp_server_ids
    assert rebuilt.region == original.region
    assert rebuilt.data_classification == original.data_classification
    assert rebuilt.framework == original.framework


# ---------------------------------------------------------------------------
# Model split / defaults
# ---------------------------------------------------------------------------

def test_agent_create_minimal_only_name():
    """AgentCreate requires only name + tenant_id (E24); sponsor optional -> 'Ownerless'."""
    c = AgentCreate(name="fraud-watch-eu", tenant_id="default")
    assert c.name == "fraud-watch-eu"
    assert c.purpose == ""
    assert c.origin == Origin.REGISTERED
    assert c.mcp_server_ids == []
    assert c.sponsor_oid is None


def test_agent_update_all_optional_exclude_none():
    """AgentUpdate is standalone with every field optional -> exclude_none yields a sparse dict."""
    u = AgentUpdate(business_unit="Underwriting")
    dumped = u.model_dump(exclude_none=True)
    assert dumped == {"business_unit": "Underwriting"}


def test_agent_id_has_no_default():
    """Agent.id is assigned by the registry (recordId) -> no default_factory."""
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        Agent(
            name="x",
            lifecycle_state=LifecycleState.PROPOSED,
            created_at=datetime(2026, 6, 1),
            updated_at=datetime(2026, 6, 1),
        )  # missing required id


def test_lifecycle_for_status_maps_known_status():
    assert lifecycle_for_status("APPROVED") == LifecycleState.APPROVED
    assert lifecycle_for_status("DRAFT") == LifecycleState.PROPOSED


def test_lifecycle_for_status_raises_on_unmapped_status():
    """A future/unknown registry status fails loudly, not with an opaque KeyError."""
    with pytest.raises(UnknownRegistryStatusError):
        lifecycle_for_status("SOME_NEW_PREVIEW_STATUS")


def test_from_record_raises_on_unmapped_status():
    with pytest.raises(UnknownRegistryStatusError):
        Agent.from_record(
            {
                "recordId": "rec-x",
                "name": "x",
                "description": "",
                "status": "BOGUS",
                "createdAt": datetime(2026, 6, 1),
                "updatedAt": datetime(2026, 6, 1),
            },
            {},
        )


# ---------------------------------------------------------------------------
# Entra identity fields (Epic 6, Task T-MODEL): entra_sp_id / entra_app_audience /
# invoker_role_id / admin_role_id / identity_status. Service-written (like
# entra_app_id) -> on the Agent read model + envelope only, NOT Create/Update.
# Research §4. Additive + backward-compatible: ENVELOPE_SCHEMA_VERSION stays 1.
# ---------------------------------------------------------------------------

def _make_agent_with_identity() -> Agent:
    """An Agent with all 5 E6 identity fields set to distinct sentinel values."""
    agent = _make_agent()
    agent.entra_sp_id = "sp-1111"
    agent.entra_app_audience = "api://agp-agent-rec-1"
    agent.invoker_role_id = "role-invoker-2222"
    agent.admin_role_id = "role-admin-3333"
    agent.identity_status = "provisioned"
    return agent


def test_to_envelope_includes_identity_fields():
    """to_envelope carries all 5 identity keys with their values (plain pass-through)."""
    env = _make_agent_with_identity().to_envelope()
    assert env["entra_sp_id"] == "sp-1111"
    assert env["entra_app_audience"] == "api://agp-agent-rec-1"
    assert env["invoker_role_id"] == "role-invoker-2222"
    assert env["admin_role_id"] == "role-admin-3333"
    assert env["identity_status"] == "provisioned"
    # still JSON-safe end-to-end (plain strings)
    reparsed = json.loads(json.dumps(env))
    assert reparsed["entra_sp_id"] == "sp-1111"
    assert reparsed["identity_status"] == "provisioned"


def test_from_record_reads_identity_fields():
    """from_record hydrates all 5 identity fields from an envelope carrying them."""
    envelope = {
        "schema_version": 1,
        "mcp_server_ids": [],
        "entra_sp_id": "sp-1111",
        "entra_app_audience": "api://agp-agent-rec-1",
        "invoker_role_id": "role-invoker-2222",
        "admin_role_id": "role-admin-3333",
        "identity_status": "provisioned",
    }
    agent = Agent.from_record(_fake_record(status="APPROVED"), envelope)
    assert agent.entra_sp_id == "sp-1111"
    assert agent.entra_app_audience == "api://agp-agent-rec-1"
    assert agent.invoker_role_id == "role-invoker-2222"
    assert agent.admin_role_id == "role-admin-3333"
    assert agent.identity_status == "provisioned"


def test_from_record_defaults_identity_status_none_when_missing():
    """A pre-E6 envelope lacks the identity keys -> identity_status defaults to 'none',
    the other 4 fields default to None (backward-compatible hydration)."""
    envelope = {"schema_version": 1, "mcp_server_ids": []}  # pre-E6: no identity keys
    agent = Agent.from_record(_fake_record(status="APPROVED"), envelope)
    assert agent.identity_status == "none"
    assert agent.entra_sp_id is None
    assert agent.entra_app_audience is None
    assert agent.invoker_role_id is None
    assert agent.admin_role_id is None


def test_from_record_reads_explicit_identity_status():
    """A real non-default identity_status ("failed") survives the read, AND an empty-string
    value collapses to "none" — pins both arms of the `or "none"` fallback deliberately."""
    # a real non-default value is read through verbatim
    survived = Agent.from_record(
        _fake_record(status="APPROVED"),
        {"schema_version": 1, "mcp_server_ids": [], "identity_status": "failed"},
    )
    assert survived.identity_status == "failed"
    # an explicit falsy value (empty string) collapses to the "none" default
    collapsed = Agent.from_record(
        _fake_record(status="APPROVED"),
        {"schema_version": 1, "mcp_server_ids": [], "identity_status": ""},
    )
    assert collapsed.identity_status == "none"


def test_round_trip_preserves_identity_fields():
    """All 5 identity fields survive a to_envelope/from_record round-trip."""
    original = _make_agent_with_identity()
    rebuilt = Agent.from_record(_fake_record(status="APPROVED"), original.to_envelope())
    assert rebuilt.entra_sp_id == original.entra_sp_id
    assert rebuilt.entra_app_audience == original.entra_app_audience
    assert rebuilt.invoker_role_id == original.invoker_role_id
    assert rebuilt.admin_role_id == original.admin_role_id
    assert rebuilt.identity_status == original.identity_status


def test_agent_create_does_not_accept_identity_fields():
    """The identity fields are service-written, NOT user-supplied: AgentCreate must not
    carry them. The models declare no model_config, so pydantic v2's default
    extra='ignore' applies -> an unexpected field is silently dropped (NOT raised, and
    we must NOT add extra='forbid' -- the workspace shares one .env and forbids it)."""
    assert "entra_sp_id" not in AgentCreate(name="x", tenant_id="default", entra_sp_id="y").model_dump()


def test_envelope_schema_version_still_1():
    """T-MODEL is additive + backward-compatible -> the envelope schema version stays 1."""
    from models.agent import ENVELOPE_SCHEMA_VERSION

    assert ENVELOPE_SCHEMA_VERSION == 1
    assert _make_agent_with_identity().to_envelope()["schema_version"] == 1


# ---------------------------------------------------------------------------
# IdentityStatus is a pinned str-Enum (Epic 36/T20, verification item 16)
#
# The field used to be a free `str`. It is now `IdentityStatus`, a `str, Enum`, so
# every existing `== "provisioned"` gate keeps working untouched. Reads stay
# TOLERANT (coerce-unknown-to-NONE + warn) because the CodeBuild buildspec patches
# `identity_status="provisioned"` straight into the stored envelope with `jq`
# (infrastructure/modules/codebuild/buildspec.yml:525) — an external writer.
# ---------------------------------------------------------------------------

def test_identity_status_enum_values():
    """The CLOSED four-value union, mirrored verbatim by the frontend's
    `identity_status` union (`frontend/src/api/client.ts:151`, `:527`)."""
    assert IdentityStatus.NONE.value == "none"
    assert IdentityStatus.PENDING.value == "pending"
    assert IdentityStatus.PROVISIONED.value == "provisioned"
    assert IdentityStatus.FAILED.value == "failed"
    assert [s.value for s in IdentityStatus] == ["none", "pending", "provisioned", "failed"]


def test_identity_status_compares_equal_to_its_wire_string():
    """THE reason this migration is cheap: ~10 bare `== "provisioned"` gates
    (`api/routes/grants.py`, `services/marketplace_service.py`,
    `services/governance_graph_service.py`, …) keep working with no edit."""
    assert IdentityStatus.PROVISIONED == "provisioned"
    assert IdentityStatus.NONE == "none"
    assert IdentityStatus.FAILED == "failed"
    # and the gates' negative arm still discriminates
    assert IdentityStatus.PENDING != "provisioned"


def test_agent_defaults_identity_status_to_enum_none():
    agent = _make_agent()
    assert agent.identity_status is IdentityStatus.NONE


def test_to_envelope_stores_the_plain_value_never_the_enum_repr():
    """PINNED: the envelope must store `"none"`, NEVER `"IdentityStatus.NONE"`.

    `to_envelope` output is `json.dumps`-ed into DynamoDB, so a stored enum *repr*
    would be a data-corruption bug. `type() is str` is the assertion with teeth:
    a str-Enum member compares equal to `"none"` and even `json.dumps` to `"none"`,
    so an `==` check alone would NOT catch a missing `.value`."""
    env = _make_agent().to_envelope()
    assert env["identity_status"] == "none"
    assert type(env["identity_status"]) is str
    assert not isinstance(env["identity_status"], Enum)
    assert "IdentityStatus" not in json.dumps(env)


def test_to_envelope_tolerates_a_service_assigned_raw_string():
    """The 10 provisioning writers assign a MEMBER
    (`services/agent_identity_service.py:282` -> `IdentityStatus.FAILED`), but pydantic
    does NOT validate assignment, so nothing STOPS a future writer from assigning a raw
    string — the serializer must pass one through instead of blowing up on `.value`."""
    agent = _make_agent()
    agent.identity_status = "failed"
    env = agent.to_envelope()
    assert env["identity_status"] == "failed"
    assert type(env["identity_status"]) is str


def test_from_record_hydrates_a_legacy_envelope_string_to_the_enum():
    """A record written before the enum landed (or by the buildspec's `jq`) carries a
    plain `"provisioned"`; it must hydrate as the enum member, not a bare string."""
    agent = Agent.from_record(
        _fake_record(status="APPROVED"),
        {"schema_version": 1, "mcp_server_ids": [], "identity_status": "provisioned"},
    )
    assert agent.identity_status is IdentityStatus.PROVISIONED
    assert agent.identity_status == "provisioned"


def test_from_record_coerces_unknown_identity_status_to_none_and_warns(caplog):
    """DELIBERATELY differs from the lifecycle-state raise idiom next door
    (`lifecycle_for_status` -> `UnknownRegistryStatusError`): `identity_status` has an
    EXTERNAL writer, so one foreign value must not 500 a list route. It FAILS CLOSED —
    `none` loses every `== "provisioned"` gate — and says so in the log."""
    with caplog.at_level(logging.WARNING, logger="models.agent"):
        agent = Agent.from_record(
            _fake_record(status="APPROVED"),
            {"schema_version": 1, "mcp_server_ids": [], "identity_status": "PROVISIONED"},
        )
    assert agent.identity_status is IdentityStatus.NONE
    assert "PROVISIONED" in caplog.text
    assert any(r.levelno == logging.WARNING for r in caplog.records)


def test_coerce_identity_status_is_total():
    """The helper is total over anything a stored envelope can hold."""
    assert coerce_identity_status("pending") is IdentityStatus.PENDING
    assert coerce_identity_status(IdentityStatus.FAILED) is IdentityStatus.FAILED
    assert coerce_identity_status(None) is IdentityStatus.NONE      # key absent
    assert coerce_identity_status("") is IdentityStatus.NONE        # stored empty string
    assert coerce_identity_status("bogus") is IdentityStatus.NONE   # foreign writer
    assert coerce_identity_status(17) is IdentityStatus.NONE        # wrong type entirely


def test_identity_status_round_trip_stays_a_plain_string_in_the_envelope():
    """to_envelope -> json -> from_record -> to_envelope is a fixed point on the wire
    form: the enum never leaks its repr into storage on a re-serialize."""
    original = _make_agent_with_identity()
    stored = json.loads(json.dumps(original.to_envelope()))
    rebuilt = Agent.from_record(_fake_record(status="APPROVED"), stored)
    assert rebuilt.identity_status is IdentityStatus.PROVISIONED
    assert rebuilt.to_envelope()["identity_status"] == "provisioned"
    assert type(rebuilt.to_envelope()["identity_status"]) is str


# ---------------------------------------------------------------------------
# Databricks runtime fields (Epic 29/T5, contract C-4): runtime_handle /
# runtime_kind / binding_mode / databricks_sp_id / databricks_sp_secret_arn /
# oauth2_app_client_id, plus the `is_databricks_governed` gate.
#
# The SEVENTH additive round on this envelope — same rules as the six before it:
# every new key is written unconditionally by `to_envelope` and read with
# `envelope.get(...)`, so a pre-E29 envelope hydrates them all as None and
# ENVELOPE_SCHEMA_VERSION stays 1.
# ---------------------------------------------------------------------------

# a workspace URL of the shape the plan's Global Constraints pin for test fakes
_DB_HANDLE = "https://dbc-test.cloud.databricks.com/apps/claims-triage"


def _make_databricks_agent(**overrides) -> Agent:
    """A federation-mode Databricks-governed Agent: handle + ENTRA + DATABRICKS."""
    agent = _make_agent()
    agent.platform = Platform.DATABRICKS
    agent.auth_type = AuthType.ENTRA
    agent.runtime_handle = _DB_HANDLE
    agent.runtime_kind = "app"
    agent.binding_mode = "federation"
    agent.oauth2_app_client_id = "db-oauth-client-1111"
    # sp_secret-mode fields — set too so the round-trip covers all six keys at once
    agent.databricks_sp_id = "db-sp-app-2222"
    agent.databricks_sp_secret_arn = "arn:aws:secretsmanager:eu-central-1:secret:agp/agent/rec-1-abcdef"
    for key, value in overrides.items():
        setattr(agent, key, value)
    return agent


def test_is_databricks_governed_true_for_handle_entra_databricks():
    """The gate is True only for the full conjunction (handle + ENTRA + DATABRICKS)."""
    assert _make_databricks_agent().is_databricks_governed is True


def test_is_databricks_governed_falsified_by_missing_runtime_handle():
    """Leg 1: no runtime_handle -> not governed (a Databricks record with no runtime
    is inert metadata, exactly like an AgentCore record with no ARN)."""
    assert _make_databricks_agent(runtime_handle=None).is_databricks_governed is False
    assert _make_databricks_agent(runtime_handle="").is_databricks_governed is False


def test_is_databricks_governed_falsified_by_non_entra_auth():
    """Leg 2: auth_type must be ENTRA — the gate is an IDENTITY gate, so an api_key
    or unauthenticated agent is never Entra-provisioned."""
    assert _make_databricks_agent(auth_type=AuthType.API_KEY).is_databricks_governed is False
    assert _make_databricks_agent(auth_type=AuthType.NONE).is_databricks_governed is False


def test_is_databricks_governed_falsified_by_non_databricks_platform():
    """Leg 3: platform must be DATABRICKS. AWS_BEDROCK is is_agentcore's territory;
    an unset platform is not a licence to guess."""
    assert _make_databricks_agent(platform=Platform.AWS_BEDROCK).is_databricks_governed is False
    assert _make_databricks_agent(platform=Platform.AZURE).is_databricks_governed is False
    assert _make_databricks_agent(platform=None).is_databricks_governed is False


def test_is_databricks_governed_and_is_agentcore_are_mutually_exclusive():
    """The two gates can never both be True: they demand different `platform` values.
    This is what makes the route's if/elif dispatch a real dispatch and not a race."""
    db = _make_databricks_agent()
    assert db.is_databricks_governed is True and db.is_agentcore is False
    ac = _make_agent()  # arn + ENTRA + AWS_BEDROCK
    assert ac.is_agentcore is True and ac.is_databricks_governed is False


def test_is_databricks_governed_agent_module_function_delegates():
    """The module-level function is the same thin delegate `resolve_runtime_arns` /
    `is_agentcore_agent` are — one implementation, two ergonomic call shapes."""
    from models.agent import is_databricks_governed_agent

    assert is_databricks_governed_agent(_make_databricks_agent()) is True
    assert is_databricks_governed_agent(_make_agent()) is False


def test_to_envelope_includes_databricks_fields():
    """All six C-4 keys ride the envelope with their values, JSON-safe (plain strings)."""
    env = _make_databricks_agent().to_envelope()
    assert env["runtime_handle"] == _DB_HANDLE
    assert env["runtime_kind"] == "app"
    assert env["binding_mode"] == "federation"
    assert env["databricks_sp_id"] == "db-sp-app-2222"
    assert env["databricks_sp_secret_arn"].startswith("arn:aws:secretsmanager:")
    assert env["oauth2_app_client_id"] == "db-oauth-client-1111"
    reparsed = json.loads(json.dumps(env))
    assert reparsed["runtime_handle"] == _DB_HANDLE
    assert reparsed["binding_mode"] == "federation"


def test_round_trip_preserves_databricks_fields():
    """All six fields survive to_envelope -> from_record, and the rebuilt agent still
    passes the gate (the round-trip must not silently drop the handle)."""
    original = _make_databricks_agent()
    rebuilt = Agent.from_record(_fake_record(status="APPROVED"), original.to_envelope())
    assert rebuilt.runtime_handle == original.runtime_handle
    assert rebuilt.runtime_kind == original.runtime_kind
    assert rebuilt.binding_mode == original.binding_mode
    assert rebuilt.databricks_sp_id == original.databricks_sp_id
    assert rebuilt.databricks_sp_secret_arn == original.databricks_sp_secret_arn
    assert rebuilt.oauth2_app_client_id == original.oauth2_app_client_id
    assert rebuilt.is_databricks_governed is True


def test_from_record_tolerates_pre_e29_envelope_without_databricks_fields():
    """THE ADDITIVE FENCE: a pre-E29 envelope (no C-4 keys at all) hydrates all six as
    None and is NOT an error — every stored record in the registry today is that shape.
    The agent is then simply not Databricks-governed."""
    envelope = {"schema_version": 1, "mcp_server_ids": []}  # pre-E29: no C-4 keys
    agent = Agent.from_record(_fake_record(status="APPROVED"), envelope)
    assert agent.runtime_handle is None
    assert agent.runtime_kind is None
    assert agent.binding_mode is None
    assert agent.databricks_sp_id is None
    assert agent.databricks_sp_secret_arn is None
    assert agent.oauth2_app_client_id is None
    assert agent.is_databricks_governed is False


def test_databricks_fields_do_not_bump_envelope_schema_version():
    """C-4 is additive -> the envelope version stays 1 (Global Constraints)."""
    from models.agent import ENVELOPE_SCHEMA_VERSION

    assert ENVELOPE_SCHEMA_VERSION == 1
    assert _make_databricks_agent().to_envelope()["schema_version"] == 1


def test_agent_create_accepts_databricks_fields():
    """C-4 puts the six on AgentBase, so AgentCreate carries them: registration is where
    the handle/kind/binding_mode arrive (discovery-picked), unlike the service-written
    Entra ids. They default to None on a minimal create."""
    minimal = AgentCreate(name="fraud-watch-eu", tenant_id="default")
    assert minimal.runtime_handle is None
    assert minimal.runtime_kind is None
    assert minimal.binding_mode is None
    supplied = AgentCreate(
        name="fraud-watch-eu",
        tenant_id="default",
        runtime_handle=_DB_HANDLE,
        runtime_kind="serving_endpoint",
        binding_mode="sp_secret",
    )
    assert supplied.runtime_handle == _DB_HANDLE
    assert supplied.runtime_kind == "serving_endpoint"
    assert supplied.binding_mode == "sp_secret"
