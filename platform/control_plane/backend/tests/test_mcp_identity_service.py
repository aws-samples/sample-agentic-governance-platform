"""Tests for ``services.mcp_identity_service`` — the E7 MCP provisioning orchestrator.

A CLONE of ``test_agent_identity_service`` in idiom, with the E7 deltas baked into
the assertions (the CRITIC callouts that distinguish this from the agent clone):
  - CRITIC-C2: provision NEVER calls obo-consent (the agent→MCP delegated consent
    moves to GRANT time, not provision time) + a NEW pre-lockdown tool-scan precedes
    app creation, so create is not step 1.
  - CRITIC-I1: the pre-lockdown scan is best-effort — a ``McpScanError`` does NOT fail
    provisioning, and a transient empty/error scan never wipes E5-seeded tools.
  - CRITIC-I3: ``persist_identity`` writes the ``mcp.server`` descriptor, NOT a CUSTOM
    one (covered in test_mcp_server_service.py).
  - CRITIC-I4: all five Entra ids are set after create.
  - CRITIC-M3: the gateway/runtime authorizer feeds BOTH audience forms (URI + GUID).
  - E6 CRITIQUE-FIX-A: the ids persist IMMEDIATELY after create, before authorizer config.

ALL external collaborators are mocked — there are NO live AWS / Graph / MCP calls:
  - ``GraphService`` → an ``AsyncMock`` double (``create_mcp_app`` returns the 6-key
    dict; ``set_assignment_required`` an async no-op; ``grant_agent_obo_consent`` an
    async no-op that MUST NOT be called during provision).
  - the boto3 ``bedrock-agentcore-control`` client → a ``MagicMock`` whose
    ``get_gateway`` / ``get_agent_runtime`` return a replayable dict + a sequenceable
    ``status``; ``update_gateway`` / ``update_agent_runtime`` record the call.
  - ``McpServerRegistryService`` → a ``MagicMock`` whose ``persist_identity`` records
    each call (and mutate-returns the in-hand mcp, mirroring the real method).
  - ``scan_mcp_tools`` → patched on the module under test (AsyncMock).

The repo is NOT in pytest-asyncio ``auto`` mode, so every async test is decorated
``@pytest.mark.asyncio`` explicitly. The poll-to-READY ``time.sleep`` is patched to a
no-op so no test blocks on a real sleep.
"""

from __future__ import annotations

import logging
import warnings
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.encoders import jsonable_encoder

from models.mcp_server import IdentityStatus, Kind, LifecycleState, McpServer, McpTool
from services.mcp_identity_service import (
    McpIdentityService,
    McpProvisioningError,
    should_provision_mcp,
)
from services.mcp_tool_scanner import McpScanError

# ---------------------------------------------------------------------------
# Fixtures / builders
# ---------------------------------------------------------------------------
TENANT_ID = "00000000-0000-0000-0000-000000000001"
LOGIN_BASE = "https://login.microsoftonline.com"
REGION = "us-east-1"

# A realistic AgentCore gateway ARN; the gatewayId is the last "/"-segment.
GATEWAY_ID = "demo-claims-gw-aBcDeFgHiJ"
GATEWAY_ARN = (
    f"arn:aws:bedrock-agentcore:us-east-1:123456789012:gateway/{GATEWAY_ID}"
)
GATEWAY_URL = f"https://{GATEWAY_ID}.gateway.bedrock-agentcore.us-east-1.amazonaws.com/mcp"

# A realistic AgentCore runtime ARN (the runtime-MCP path); RID is the last segment.
RUNTIME_RID = "mcp_runtime_test-wdOmsREOEj"
RUNTIME_ARN = (
    f"arn:aws:bedrock-agentcore:us-east-1:123456789012:runtime/{RUNTIME_RID}"
)


def _make_mcp(
    *,
    mcp_id: str = "mcp-rec-123",
    name: str = "Internal Claims MCP",
    kind: Kind = Kind.GATEWAY,
    gateway_arn: str | None = GATEWAY_ARN,
    runtime_arn: str | None = None,
    entra_sp_id: str | None = None,
    entra_app_audience: str | None = None,
    identity_status: str = "none",
    available_tools: list[McpTool] | None = None,
) -> McpServer:
    now = datetime.now(timezone.utc)
    return McpServer(
        id=mcp_id,
        name=name,
        kind=kind,
        gateway_arn=gateway_arn,
        runtime_arn=runtime_arn,
        lifecycle_state=LifecycleState.APPROVED,
        entra_sp_id=entra_sp_id,
        entra_app_audience=entra_app_audience,
        identity_status=identity_status,
        available_tools=available_tools if available_tools is not None else [],
        created_at=now,
        updated_at=now,
    )


def _graph_double(mcp_id: str = "mcp-rec-123") -> AsyncMock:
    """An AsyncMock GraphService double.

    ``create_mcp_app`` returns the SAME 6-key dict as ``create_agent_app`` (app_id /
    sp_id / app_uri / invoke_scope_id / invoker_role_id / admin_role_id), with the
    per-MCP ``app_uri`` derived from the id so two MCPs get distinct audiences.
    ``set_assignment_required`` + ``grant_agent_obo_consent`` are async no-ops.
    """
    graph = AsyncMock(name="GraphService")
    graph.create_mcp_app.return_value = {
        "app_id": f"app-client-guid-{mcp_id}",
        "sp_id": f"sp-obj-id-{mcp_id}",
        "app_uri": f"api://agp-mcp-{mcp_id}",
        "invoke_scope_id": f"scope-{mcp_id}",
        "invoker_role_id": f"invoker-{mcp_id}",
        "admin_role_id": f"admin-{mcp_id}",
    }
    graph.set_assignment_required.return_value = None
    graph.grant_agent_obo_consent.return_value = None
    return graph


def _gateway_control_client(
    statuses: list[str] | None = None, *, authorizer_type: str = "NONE"
) -> MagicMock:
    """A MagicMock boto3 control client for the GATEWAY path.

    ``get_gateway`` returns a dict carrying the replay fields (name/roleArn/
    protocolType/protocolConfiguration), gatewayUrl, the current authorizerType, and a
    status. When ``statuses`` is given, the status is sequenced via ``side_effect``
    (each ``get_gateway`` call pops the next status); otherwise every read is READY.
    ``update_gateway`` records the call.
    """
    control = MagicMock(name="bedrock-agentcore-control")
    base = {
        "gatewayId": GATEWAY_ID,
        "name": "demo-claims-gw",
        "roleArn": "arn:aws:iam::123456789012:role/gateway-service-role",
        "protocolType": "MCP",
        "protocolConfiguration": {"mcp": {"searchType": "SEMANTIC"}},
        "gatewayUrl": GATEWAY_URL,
        "authorizerType": authorizer_type,
    }

    if statuses is None:
        control.get_gateway.return_value = {**base, "status": "READY"}
    else:
        control.get_gateway.side_effect = [
            {**base, "status": s} for s in statuses
        ]
    control.update_gateway.return_value = {"status": "UPDATING"}
    # Native tool-read (T-NATIVE-TOOLSCAN): default to NO targets so the existing
    # open-gateway tests still exercise the wire-scan fallback. Tests that exercise the
    # native path override list_gateway_targets / get_gateway_target explicitly.
    control.list_gateway_targets.return_value = {"items": []}
    return control


def _runtime_control_client(statuses: list[str] | None = None) -> MagicMock:
    """A MagicMock boto3 control client for the RUNTIME-MCP path (E6 verbatim)."""
    control = MagicMock(name="bedrock-agentcore-control")
    artifact = {"containerConfiguration": {"containerUri": "123.dkr.ecr/mcp:latest"}}
    role_arn = "arn:aws:iam::123456789012:role/mcp-runtime-role"
    network = {"networkMode": "PUBLIC"}

    if statuses is None:
        control.get_agent_runtime.return_value = {
            "agentRuntimeArtifact": artifact,
            "roleArn": role_arn,
            "networkConfiguration": network,
            "status": "READY",
            "agentRuntimeVersion": "1",
        }
    else:
        control.get_agent_runtime.side_effect = [
            {
                "agentRuntimeArtifact": artifact,
                "roleArn": role_arn,
                "networkConfiguration": network,
                "status": s,
                "agentRuntimeVersion": "1",
            }
            for s in statuses
        ]
    control.update_agent_runtime.return_value = {"status": "UPDATING"}
    return control


def _registry_double() -> MagicMock:
    """A MagicMock McpServerRegistryService whose ``persist_identity`` returns its arg."""
    registry = MagicMock(name="McpServerRegistryService")
    registry.persist_identity.side_effect = lambda mcp: mcp
    return registry


def _make_service(
    *,
    graph: AsyncMock,
    registry: MagicMock,
    control: MagicMock,
) -> McpIdentityService:
    return McpIdentityService(
        graph=graph,
        registry=registry,
        control_client=control,
        region=REGION,
        tenant_id=TENANT_ID,
        login_base=LOGIN_BASE,
    )


@pytest.fixture(autouse=True)
def _instant_sleep(monkeypatch):
    """Patch the poll loop's sleep so no test blocks on a real sleep."""
    import services.mcp_identity_service as mod

    monkeypatch.setattr(mod.time, "sleep", lambda *_a, **_k: None)


@pytest.fixture
def patch_scan(monkeypatch):
    """Patch ``scan_mcp_tools`` on the module under test with an AsyncMock.

    Returns the AsyncMock so tests can set its return_value / side_effect and assert
    on the call args (esp. ``bearer=None``).
    """
    import services.mcp_identity_service as mod

    scanner = AsyncMock(name="scan_mcp_tools")
    scanner.return_value = [
        McpTool(name="get_claim", description="Fetch a claim", input_schema={"type": "object"})
    ]
    monkeypatch.setattr(mod, "scan_mcp_tools", scanner)
    return scanner


# ===========================================================================
# Gate
# ===========================================================================
def test_should_provision_gate():
    # Gateway + arn → True.
    assert should_provision_mcp(_make_mcp(kind=Kind.GATEWAY, gateway_arn=GATEWAY_ARN)) is True
    # Runtime + runtime_arn → True.
    assert (
        should_provision_mcp(
            _make_mcp(kind=Kind.RUNTIME, gateway_arn=None, runtime_arn=RUNTIME_ARN)
        )
        is True
    )

    # Standard → False (metadata-only, no identity).
    assert should_provision_mcp(_make_mcp(kind=Kind.STANDARD, gateway_arn=None)) is False
    assert should_provision_mcp(_make_mcp(kind=Kind.STANDARD, gateway_arn=GATEWAY_ARN)) is False

    # Gateway WITHOUT a handle → False (no arn to configure).
    assert should_provision_mcp(_make_mcp(kind=Kind.GATEWAY, gateway_arn=None)) is False
    assert should_provision_mcp(_make_mcp(kind=Kind.GATEWAY, gateway_arn="")) is False
    # Runtime WITHOUT a handle → False.
    assert (
        should_provision_mcp(_make_mcp(kind=Kind.RUNTIME, gateway_arn=None, runtime_arn=None))
        is False
    )


# ===========================================================================
# Happy path — order + ids + status (gateway)
# ===========================================================================
@pytest.mark.asyncio
async def test_provision_gateway_happy_path(patch_scan):
    mcp = _make_mcp()
    graph = _graph_double(mcp.id)
    registry = _registry_double()
    control = _gateway_control_client(authorizer_type="NONE")

    # A shared manager records the relative order of the collaborator calls.
    manager = MagicMock()
    manager.attach_mock(control.get_gateway, "get_gateway")
    manager.attach_mock(patch_scan, "scan_mcp_tools")
    manager.attach_mock(graph.create_mcp_app, "create_mcp_app")
    manager.attach_mock(graph.set_assignment_required, "set_assignment_required")
    manager.attach_mock(registry.persist_identity, "persist_identity")
    manager.attach_mock(control.update_gateway, "update_gateway")

    svc = _make_service(graph=graph, registry=registry, control=control)
    result = await svc.provision(mcp)

    # The canonical E7 order (CRITIC-C2): GetGateway → persist(pending) →
    # scan(bearer=None) → create_mcp_app → persist(pending) → set_assignment_required
    # → update_gateway(CUSTOM_JWT) [→ poll READY via get_gateway] → persist(provisioned).
    order = [c[0] for c in manager.mock_calls]
    # Filter out the poll-to-READY get_gateway reads after update so the spine is clear:
    # the load-bearing relative order is GetGateway-first, scan before create_mcp_app,
    # create before update_gateway, with persist(pending) landing after create.
    assert order[0] == "get_gateway"
    assert "scan_mcp_tools" in order
    assert order.index("scan_mcp_tools") < order.index("create_mcp_app")
    assert order.index("create_mcp_app") < order.index("set_assignment_required")
    assert order.index("set_assignment_required") < order.index("update_gateway")
    # A persist landed between create and set_assignment_required (the immediate persist).
    create_idx = order.index("create_mcp_app")
    sar_idx = order.index("set_assignment_required")
    assert "persist_identity" in order[create_idx:sar_idx]

    # Final status + gateway_url captured verbatim + all five ids set.
    assert result.identity_status == "provisioned"
    assert result.gateway_url == GATEWAY_URL
    assert result.gateway_id == GATEWAY_ID
    assert result.entra_app_id == f"app-client-guid-{mcp.id}"
    assert result.entra_sp_id == f"sp-obj-id-{mcp.id}"
    assert result.invoker_role_id == f"invoker-{mcp.id}"
    assert result.admin_role_id == f"admin-{mcp.id}"
    assert result.entra_app_audience == f"api://agp-mcp-{mcp.id}"

    # create_mcp_app was called with (mcp.id, mcp.name).
    graph.create_mcp_app.assert_awaited_once_with(mcp.id, mcp.name)

    # E7 (research §2.4/§2.5): the MCP SP is set assignment-required=FALSE — the
    # delegated/OBO user is gated by the agent→MCP consent grant, never by an app-role
    # assignment (which Entra would enforce against the USER in an OBO flow → AADSTS50105).
    # SPECIFIC to False: this fails if the call reverts to the hardcoded True default.
    graph.set_assignment_required.assert_awaited_once_with(
        f"sp-obj-id-{mcp.id}", required=False
    )


# ===========================================================================
# The writers assign IdentityStatus MEMBERS (Epic 36/T20) — no serializer warnings
# ===========================================================================
@pytest.mark.asyncio
async def test_provisioned_mcp_serializes_without_pydantic_serializer_warnings(patch_scan):
    """PINNED (agent-side twin: test_agent_identity_service.py): the MCP provisioning
    writers assign MEMBERS, never bare strings.

    `identity_status` is enum-ANNOTATED and pydantic does NOT validate assignment, so a
    bare-string writer leaves a plain `str` in an enum-typed field — output stays correct
    but pydantic's serializer warns ``Expected `enum` but got `str``` on every
    `model_dump()` / FastAPI `jsonable_encoder()`, i.e. on the provisioning response
    paths. This drives the REAL `provision()` (`mcp_identity_service.py:202` pending →
    `:345` provisioned) and pins ZERO such warnings.
    """
    mcp = _make_mcp()
    svc = _make_service(
        graph=_graph_double(mcp.id),
        registry=_registry_double(),
        control=_gateway_control_client(authorizer_type="NONE"),
    )
    result = await svc.provision(mcp)

    # the MEMBER — an `==` check alone would pass on a bare string too (str-Enum).
    assert result.identity_status is IdentityStatus.PROVISIONED

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        result.model_dump()
        jsonable_encoder(result)  # the FastAPI response path
    assert [
        str(w.message) for w in caught if "Pydantic serializer warnings" in str(w.message)
    ] == []


# ===========================================================================
# CRITIC-C2 — provision NEVER calls obo-consent (that's grant-time)
# ===========================================================================
@pytest.mark.asyncio
async def test_provision_never_calls_obo_consent(patch_scan):
    mcp = _make_mcp()
    graph = _graph_double(mcp.id)
    registry = _registry_double()
    control = _gateway_control_client()

    svc = _make_service(graph=graph, registry=registry, control=control)
    await svc.provision(mcp)

    # The agent→MCP delegated consent fires at GRANT time, never during provision.
    graph.grant_agent_obo_consent.assert_not_awaited()
    graph.grant_agent_obo_consent.assert_not_called()


# ===========================================================================
# CRITIC-M3 — both audience forms, no allowedClients/customClaims, replay name/role/protocol
# ===========================================================================
@pytest.mark.asyncio
async def test_provision_configures_gateway_authorizer_both_audience_forms(patch_scan):
    mcp = _make_mcp()
    graph = _graph_double(mcp.id)
    registry = _registry_double()
    control = _gateway_control_client(authorizer_type="NONE")

    # Capture the GetGateway read so we can compare the replayed fields.
    read = control.get_gateway.return_value

    svc = _make_service(graph=graph, registry=registry, control=control)
    await svc.provision(mcp)

    _, kwargs = control.update_gateway.call_args
    jwt = kwargs["authorizerConfiguration"]["customJWTAuthorizer"]

    # Both forms, order = (URI, GUID).
    assert jwt["allowedAudience"] == [mcp.entra_app_audience, mcp.entra_app_id]
    # Audience ONLY — no allowedClients / customClaims / allowedScopes.
    assert "allowedClients" not in jwt
    assert "customClaims" not in jwt
    assert "allowedScopes" not in jwt
    # discoveryUrl is the Entra v2 well-known, per-tenant.
    assert jwt["discoveryUrl"] == (
        f"{LOGIN_BASE}/{TENANT_ID}/v2.0/.well-known/openid-configuration"
    )
    # authorizerType flipped to CUSTOM_JWT.
    assert kwargs["authorizerType"] == "CUSTOM_JWT"
    # The required fields are replayed from the GET (full-replace PUT).
    assert kwargs["name"] == read["name"]
    assert kwargs["roleArn"] == read["roleArn"]
    assert kwargs["protocolType"] == read["protocolType"]
    # The optional protocolConfiguration (carries searchType=SEMANTIC) is replayed.
    assert kwargs["protocolConfiguration"] == read["protocolConfiguration"]
    # gatewayIdentifier is the short id, NOT the ARN.
    assert kwargs["gatewayIdentifier"] == GATEWAY_ID
    assert kwargs["gatewayIdentifier"] != GATEWAY_ARN


# ===========================================================================
# Runtime-MCP path → UpdateAgentRuntime, NOT update_gateway
# ===========================================================================
@pytest.mark.asyncio
async def test_provision_runtime_uses_update_agent_runtime(patch_scan):
    mcp = _make_mcp(kind=Kind.RUNTIME, gateway_arn=None, runtime_arn=RUNTIME_ARN)
    graph = _graph_double(mcp.id)
    registry = _registry_double()
    control = _runtime_control_client()

    svc = _make_service(graph=graph, registry=registry, control=control)
    result = await svc.provision(mcp)

    # The runtime path: UpdateAgentRuntime, NOT update_gateway.
    control.update_agent_runtime.assert_called_once()
    control.update_gateway.assert_not_called()

    # allowedAudience still carries both forms (CRITIC-M3).
    _, kwargs = control.update_agent_runtime.call_args
    jwt = kwargs["authorizerConfiguration"]["customJWTAuthorizer"]
    assert jwt["allowedAudience"] == [mcp.entra_app_audience, mcp.entra_app_id]
    # agentRuntimeId is the RID, not the ARN.
    assert kwargs["agentRuntimeId"] == RUNTIME_RID
    assert kwargs["agentRuntimeId"] != RUNTIME_ARN
    assert result.identity_status == "provisioned"


# ===========================================================================
# CRITIC-I1 / pre-lockdown scan — scan called with bearer=None when authorizer is NONE
# ===========================================================================
@pytest.mark.asyncio
async def test_provision_prelockdown_scan_no_bearer(patch_scan):
    mcp = _make_mcp()
    graph = _graph_double(mcp.id)
    registry = _registry_double()
    control = _gateway_control_client(authorizer_type="NONE")

    svc = _make_service(graph=graph, registry=registry, control=control)
    result = await svc.provision(mcp)

    # The scan ran against the verbatim gatewayUrl with bearer=None (no token —
    # the gateway is not yet locked down).
    patch_scan.assert_awaited_once()
    call = patch_scan.await_args
    # endpoint_url is the gateway_url (positional or kw).
    endpoint = call.args[0] if call.args else call.kwargs.get("endpoint_url")
    assert endpoint == GATEWAY_URL
    # bearer is None (positional second arg or kw).
    bearer = call.args[1] if len(call.args) > 1 else call.kwargs.get("bearer")
    assert bearer is None
    # The scanned tools landed on the record.
    assert [t.name for t in result.available_tools] == ["get_claim"]


# ===========================================================================
# Skip the scan when the gateway is already CUSTOM_JWT (re-provision)
# ===========================================================================
@pytest.mark.asyncio
async def test_provision_skips_scan_when_already_custom_jwt(patch_scan):
    seeded = [McpTool(name="seeded_tool", description="from E5", input_schema={})]
    mcp = _make_mcp(available_tools=seeded)
    graph = _graph_double(mcp.id)
    registry = _registry_double()
    # The gateway is ALREADY locked down (re-provision case).
    control = _gateway_control_client(authorizer_type="CUSTOM_JWT")

    svc = _make_service(graph=graph, registry=registry, control=control)
    result = await svc.provision(mcp)

    # No scan when the gateway is already CUSTOM_JWT (scanning would need a token).
    patch_scan.assert_not_awaited()
    # Existing/seeded tools are kept.
    assert [t.name for t in result.available_tools] == ["seeded_tool"]


# ===========================================================================
# CRITIC-I1 — a scan failure does NOT fail provisioning, seeded tools NOT wiped
# ===========================================================================
@pytest.mark.asyncio
async def test_provision_scan_failure_does_not_fail_provisioning(patch_scan):
    seeded = [McpTool(name="seeded_tool", description="from E5", input_schema={})]
    mcp = _make_mcp(available_tools=seeded)
    graph = _graph_double(mcp.id)
    registry = _registry_double()
    control = _gateway_control_client(authorizer_type="NONE")

    # The pre-lockdown scan blows up (a flaky endpoint).
    patch_scan.side_effect = McpScanError("MCP server returned HTTP 503")

    svc = _make_service(graph=graph, registry=registry, control=control)
    result = await svc.provision(mcp)

    # Provisioning CONTINUED past the swallowed scan error and reached 'provisioned'.
    assert result.identity_status == "provisioned"
    # The authorizer was still configured.
    control.update_gateway.assert_called_once()
    # The E5-seeded tools were NOT wiped by the failed scan.
    assert [t.name for t in result.available_tools] == ["seeded_tool"]


@pytest.mark.asyncio
async def test_provision_empty_scan_does_not_wipe_seeded_tools(patch_scan):
    # CRITIC-I1: OVERWRITE available_tools ONLY on a successful NON-EMPTY scan.
    seeded = [McpTool(name="seeded_tool", description="from E5", input_schema={})]
    mcp = _make_mcp(available_tools=seeded)
    graph = _graph_double(mcp.id)
    registry = _registry_double()
    control = _gateway_control_client(authorizer_type="NONE")

    # The scan succeeds but returns NO tools — must not wipe the seed.
    patch_scan.return_value = []

    svc = _make_service(graph=graph, registry=registry, control=control)
    result = await svc.provision(mcp)

    assert result.identity_status == "provisioned"
    assert [t.name for t in result.available_tools] == ["seeded_tool"]


# ===========================================================================
# CRITIC-I4 — all five Entra ids set after create
# ===========================================================================
@pytest.mark.asyncio
async def test_provision_sets_all_five_entra_ids(patch_scan):
    mcp = _make_mcp()
    graph = _graph_double(mcp.id)
    registry = _registry_double()
    control = _gateway_control_client()

    svc = _make_service(graph=graph, registry=registry, control=control)
    result = await svc.provision(mcp)

    assert result.entra_app_id == f"app-client-guid-{mcp.id}"
    assert result.entra_sp_id == f"sp-obj-id-{mcp.id}"
    assert result.entra_app_audience == f"api://agp-mcp-{mcp.id}"
    assert result.invoker_role_id == f"invoker-{mcp.id}"
    assert result.admin_role_id == f"admin-{mcp.id}"


# ===========================================================================
# E6 CRITIQUE-FIX-A — persist ids IMMEDIATELY after create, before authorizer config
# ===========================================================================
@pytest.mark.asyncio
async def test_provision_persists_ids_immediately_after_create(patch_scan):
    mcp = _make_mcp()
    graph = _graph_double(mcp.id)
    registry = _registry_double()
    control = _gateway_control_client()

    # Record the mcp's identity_status + ids AT THE MOMENT set_assignment_required is
    # invoked. If the immediate persist happened first, the snapshot must show the ids
    # set + status 'pending' + a persist already counted.
    snapshot: dict = {}

    async def _capture_on_set_assignment(sp_id, required=True):
        snapshot["identity_status"] = mcp.identity_status
        snapshot["entra_sp_id"] = mcp.entra_sp_id
        snapshot["entra_app_id"] = mcp.entra_app_id
        snapshot["invoker_role_id"] = mcp.invoker_role_id
        snapshot["admin_role_id"] = mcp.admin_role_id
        snapshot["entra_app_audience"] = mcp.entra_app_audience
        snapshot["persist_count_at_step"] = registry.persist_identity.call_count
        return None

    graph.set_assignment_required.side_effect = _capture_on_set_assignment

    svc = _make_service(graph=graph, registry=registry, control=control)
    await svc.provision(mcp)

    assert snapshot["identity_status"] == "pending"
    assert snapshot["entra_sp_id"] == f"sp-obj-id-{mcp.id}"
    assert snapshot["entra_app_id"] == f"app-client-guid-{mcp.id}"
    assert snapshot["invoker_role_id"] == f"invoker-{mcp.id}"
    assert snapshot["admin_role_id"] == f"admin-{mcp.id}"
    assert snapshot["entra_app_audience"] == f"api://agp-mcp-{mcp.id}"
    # At least one persist (the immediate one after create) had landed by step (4).
    assert snapshot["persist_count_at_step"] >= 1


# ===========================================================================
# Idempotency — skip create when entra_sp_id already set (re-provision)
# ===========================================================================
@pytest.mark.asyncio
async def test_provision_idempotent_skips_create_when_sp_exists(patch_scan):
    # The MCP already carries its identity ids (a re-provision).
    mcp = _make_mcp(
        entra_sp_id="sp-existing",
        entra_app_audience="api://agp-mcp-mcp-rec-123",
        identity_status="failed",
    )
    mcp.entra_app_id = "app-existing"
    mcp.invoker_role_id = "invoker-existing"
    mcp.admin_role_id = "admin-existing"
    graph = _graph_double(mcp.id)
    registry = _registry_double()
    control = _gateway_control_client()

    svc = _make_service(graph=graph, registry=registry, control=control)
    result = await svc.provision(mcp)

    # create_mcp_app NOT called (sp already known).
    graph.create_mcp_app.assert_not_awaited()
    # Authorizer still re-configured (the point of re-provision).
    control.update_gateway.assert_called_once()
    # set_assignment_required still runs (idempotent), on the existing sp, and EXPLICITLY
    # with required=False so a re-provision of an SP currently set TRUE (e.g. one a human
    # flipped, or any prior-true record) CONVERGES to False — idempotent + self-healing
    # (research §2.4/§2.5: the agent→MCP consent grant is the OBO gate, not assignment).
    graph.set_assignment_required.assert_awaited_once_with("sp-existing", required=False)
    assert result.identity_status == "provisioned"


# ===========================================================================
# Failure → identity_status='failed' + McpProvisioningError
# ===========================================================================
@pytest.mark.asyncio
async def test_provision_failure_sets_failed_and_raises(patch_scan):
    mcp = _make_mcp()
    graph = _graph_double(mcp.id)
    registry = _registry_double()
    control = _gateway_control_client()

    # The authorizer config step raises.
    control.update_gateway.side_effect = RuntimeError("ValidationException")

    svc = _make_service(graph=graph, registry=registry, control=control)
    with pytest.raises(McpProvisioningError):
        await svc.provision(mcp)

    assert mcp.identity_status == "failed"
    # The failed status was persisted.
    persisted_statuses = [
        c.args[0].identity_status for c in registry.persist_identity.call_args_list
    ]
    assert "failed" in persisted_statuses


@pytest.mark.asyncio
async def test_provision_runtime_update_failed_status_raises(patch_scan):
    mcp = _make_mcp(kind=Kind.RUNTIME, gateway_arn=None, runtime_arn=RUNTIME_ARN)
    graph = _graph_double(mcp.id)
    registry = _registry_double()
    # Initial read READY, then the poll sees UPDATE_FAILED → must raise.
    control = _runtime_control_client(statuses=["READY", "UPDATE_FAILED"])

    svc = _make_service(graph=graph, registry=registry, control=control)
    with pytest.raises(McpProvisioningError):
        await svc.provision(mcp)

    assert mcp.identity_status == "failed"


# ===========================================================================
# T-OBSERVABILITY — a NON-Graph (unexpected) provisioning failure logs the full
# traceback (logger.exception → ERROR record with exc_info), so the next silent
# 502 is diagnosable from logs. The 'failed' persist + McpProvisioningError
# contract is unchanged.
# ===========================================================================
@pytest.mark.asyncio
async def test_provision_failure_logs_traceback_for_unexpected_error(patch_scan, caplog):
    mcp = _make_mcp()
    graph = _graph_double(mcp.id)
    registry = _registry_double()
    control = _gateway_control_client()

    # The very first control call (the step-1 GetGateway) raises a NON-Graph error.
    control.get_gateway.side_effect = RuntimeError("boom — unexpected control-plane error")

    svc = _make_service(graph=graph, registry=registry, control=control)
    with caplog.at_level(logging.ERROR, logger="services.mcp_identity_service"):
        with pytest.raises(McpProvisioningError):
            await svc.provision(mcp)

    # logger.exception fired: an ERROR-level record carrying a traceback (exc_info).
    failure_records = [
        r
        for r in caplog.records
        if r.name == "services.mcp_identity_service"
        and r.levelno == logging.ERROR
        and r.exc_info is not None
        and mcp.id in r.getMessage()
    ]
    assert failure_records, "expected an ERROR record with a traceback for the unexpected failure"

    # Existing behavior unchanged: 'failed' persisted + McpProvisioningError raised.
    assert mcp.identity_status == "failed"
    persisted_statuses = [
        c.args[0].identity_status for c in registry.persist_identity.call_args_list
    ]
    assert "failed" in persisted_statuses


# ===========================================================================
# CRITIQUE-FIX-B — the blocking authorizer config runs OFF the event loop
# ===========================================================================
@pytest.mark.asyncio
async def test_configure_gateway_dispatched_off_loop(patch_scan, monkeypatch):
    mcp = _make_mcp()
    graph = _graph_double(mcp.id)
    registry = _registry_double()
    control = _gateway_control_client()

    import services.mcp_identity_service as mod

    # Patch anyio.to_thread.run_sync with an AsyncMock that actually runs the target
    # (so the rest of provision still works) but records that it was awaited with the
    # blocking method + its args.
    real_run_sync = mod.anyio.to_thread.run_sync
    recorder = AsyncMock(side_effect=real_run_sync)
    monkeypatch.setattr(mod.anyio.to_thread, "run_sync", recorder)

    svc = _make_service(graph=graph, registry=registry, control=control)
    await svc.provision(mcp)

    # run_sync was awaited with the bound _configure_gateway_authorizer + (gateway_id, mcp).
    # NOTE: run_sync is now awaited MORE than once — the native tool read
    # (_read_gateway_tools_native) is ALSO dispatched off-loop (T-NATIVE-TOOLSCAN) — so we
    # assert the authorizer-config dispatch is PRESENT among the off-loop calls, not that
    # it is the only one.
    authorizer_calls = [
        c
        for c in recorder.await_args_list
        if c.args and c.args[0] == svc._configure_gateway_authorizer
    ]
    assert len(authorizer_calls) == 1
    awaited_args = authorizer_calls[0].args
    assert awaited_args[1] == GATEWAY_ID
    assert awaited_args[2] is mcp
    # The native tool read was ALSO dispatched off the loop (never blocks the uvicorn loop).
    native_calls = [
        c
        for c in recorder.await_args_list
        if c.args and c.args[0] == svc._read_gateway_tools_native
    ]
    assert len(native_calls) == 1


# ===========================================================================
# Poll to READY — the gateway poll waits before flipping to 'provisioned'
# ===========================================================================
@pytest.mark.asyncio
async def test_provision_polls_gateway_to_ready(patch_scan):
    mcp = _make_mcp()
    graph = _graph_double(mcp.id)
    registry = _registry_double()
    # First get_gateway read (for replay) + poll reads: UPDATING, UPDATING, READY.
    control = _gateway_control_client(statuses=["READY", "UPDATING", "UPDATING", "READY"])

    svc = _make_service(graph=graph, registry=registry, control=control)
    result = await svc.provision(mcp)

    assert result.identity_status == "provisioned"
    # get_gateway called more than once (initial replay read + ≥1 poll until READY).
    assert control.get_gateway.call_count >= 2


# ===========================================================================
# Fix #1 — runtime optional fields are replayed into UpdateAgentRuntime
# ===========================================================================
@pytest.mark.asyncio
async def test_provision_runtime_replays_optional_fields(patch_scan):
    # Seed environmentVariables + protocolConfiguration on the get_agent_runtime mock.
    # Both are optional fields UpdateAgentRuntime accepts; without the fix they would be
    # silently dropped — the runtime comes back READY but mis-configured.
    mcp = _make_mcp(kind=Kind.RUNTIME, gateway_arn=None, runtime_arn=RUNTIME_ARN)
    graph = _graph_double(mcp.id)
    registry = _registry_double()
    control = _runtime_control_client()

    env_vars = {"LOG_LEVEL": "DEBUG", "MCP_MODE": "streamable-http"}
    proto_cfg = {"serverProtocol": "MCP"}
    control.get_agent_runtime.return_value = {
        **control.get_agent_runtime.return_value,
        "environmentVariables": env_vars,
        "protocolConfiguration": proto_cfg,
    }

    svc = _make_service(graph=graph, registry=registry, control=control)
    await svc.provision(mcp)

    _, kwargs = control.update_agent_runtime.call_args
    # The optional fields from the GET are forwarded into the Update kwargs.
    assert kwargs["environmentVariables"] == env_vars
    assert kwargs["protocolConfiguration"] == proto_cfg
    # Required fields still present.
    assert "agentRuntimeArtifact" in kwargs
    assert "roleArn" in kwargs
    assert "networkConfiguration" in kwargs
    assert "authorizerConfiguration" in kwargs


# ===========================================================================
# Fix #2 — empty allowedAudience raises before the Update (fail-loud guard)
# ===========================================================================
@pytest.mark.asyncio
async def test_provision_empty_audience_raises_before_update_gateway(patch_scan):
    # Both entra_app_audience and entra_app_id are None → _allowed_audience returns [].
    # The guard must raise McpProvisioningError and must NOT call update_gateway.
    mcp = _make_mcp(
        entra_sp_id="sp-existing",        # skip create so we reach the authorizer step
        entra_app_audience=None,
        identity_status="failed",
    )
    mcp.entra_app_id = None               # both forms absent
    graph = _graph_double(mcp.id)
    registry = _registry_double()
    control = _gateway_control_client()

    svc = _make_service(graph=graph, registry=registry, control=control)
    with pytest.raises(McpProvisioningError):
        await svc.provision(mcp)

    # The guard fired before the Update — no authorizer was configured.
    control.update_gateway.assert_not_called()
    # Failure path: identity_status persisted as 'failed'.
    assert mcp.identity_status == "failed"


# ===========================================================================
# T-NATIVE-TOOLSCAN — native control-plane tool read (no token; works on a
# born-CUSTOM_JWT gateway) via ListGatewayTargets → GetGatewayTarget.
# ===========================================================================
def _lambda_target_config(*, tools: list[dict]) -> dict:
    """A GetGatewayTarget targetConfiguration for a LAMBDA target with an inline toolSchema."""
    return {
        "targetConfiguration": {
            "mcp": {"lambda": {"toolSchema": {"inlinePayload": tools}}}
        }
    }


@pytest.mark.asyncio
async def test_provision_reads_native_lambda_tools_on_custom_jwt_gateway(patch_scan):
    # The gateway is born CUSTOM_JWT (immutable post-create), so the wire-scan is never
    # eligible. The native control-plane read must populate tools regardless.
    mcp = _make_mcp()
    graph = _graph_double(mcp.id)
    registry = _registry_double()
    control = _gateway_control_client(authorizer_type="CUSTOM_JWT")

    control.list_gateway_targets.return_value = {
        "items": [{"targetId": "tgt-1", "name": "claims", "status": "READY"}]
    }
    control.get_gateway_target.return_value = {
        "name": "claims",
        **_lambda_target_config(
            tools=[
                {"name": "echo", "description": "echo back", "inputSchema": {"type": "object"}},
                {"name": "add", "description": "add two numbers", "inputSchema": {"type": "object"}},
            ]
        ),
    }

    svc = _make_service(graph=graph, registry=registry, control=control)
    result = await svc.provision(mcp)

    # Native read used the prefixed <targetName>___<toolName> names, verbatim.
    assert sorted(t.name for t in result.available_tools) == ["claims___add", "claims___echo"]
    # The wire-scan was NOT used (native path served a locked CUSTOM_JWT gateway).
    patch_scan.assert_not_awaited()
    # Native read targeted the gateway by id.
    control.list_gateway_targets.assert_called()
    control.get_gateway_target.assert_called_once_with(
        gatewayIdentifier=GATEWAY_ID, targetId="tgt-1"
    )
    # Provisioning still completed.
    assert result.identity_status == "provisioned"


@pytest.mark.asyncio
async def test_native_tool_read_paginates_targets(patch_scan):
    mcp = _make_mcp()
    graph = _graph_double(mcp.id)
    registry = _registry_double()
    control = _gateway_control_client(authorizer_type="CUSTOM_JWT")

    # Two pages of targets (nextToken on the first, none on the second).
    control.list_gateway_targets.side_effect = [
        {"items": [{"targetId": "tgt-1", "name": "alpha"}], "nextToken": "PAGE2"},
        {"items": [{"targetId": "tgt-2", "name": "beta"}]},
    ]
    control.get_gateway_target.side_effect = [
        {"name": "alpha", **_lambda_target_config(tools=[{"name": "a_tool", "inputSchema": {}}])},
        {"name": "beta", **_lambda_target_config(tools=[{"name": "b_tool", "inputSchema": {}}])},
    ]

    svc = _make_service(graph=graph, registry=registry, control=control)
    result = await svc.provision(mcp)

    assert sorted(t.name for t in result.available_tools) == ["alpha___a_tool", "beta___b_tool"]
    # The second page was requested with the nextToken.
    second_call = control.list_gateway_targets.call_args_list[1]
    assert second_call.kwargs.get("nextToken") == "PAGE2"
    assert result.identity_status == "provisioned"


@pytest.mark.asyncio
async def test_native_tool_read_skips_non_lambda_targets(patch_scan):
    mcp = _make_mcp()
    graph = _graph_double(mcp.id)
    registry = _registry_double()
    control = _gateway_control_client(authorizer_type="CUSTOM_JWT")

    control.list_gateway_targets.return_value = {
        "items": [
            {"targetId": "tgt-mcp", "name": "remote"},
            {"targetId": "tgt-lambda", "name": "local"},
        ]
    }
    # First target is an mcpServer (no inline lambda schema) → skipped, no crash.
    # Second target is a lambda → its tools still come through.
    control.get_gateway_target.side_effect = [
        {"name": "remote", "targetConfiguration": {"mcp": {"mcpServer": {"endpoint": "https://x"}}}},
        {"name": "local", **_lambda_target_config(tools=[{"name": "do_it", "inputSchema": {}}])},
    ]

    svc = _make_service(graph=graph, registry=registry, control=control)
    result = await svc.provision(mcp)

    assert [t.name for t in result.available_tools] == ["local___do_it"]
    assert result.identity_status == "provisioned"


@pytest.mark.asyncio
async def test_native_tool_read_best_effort_does_not_fail_provisioning(patch_scan):
    # ListGatewayTargets raises — provisioning must continue (best-effort) and must NOT
    # wipe pre-seeded tools.
    seeded = [McpTool(name="seeded_tool", description="from E5", input_schema={})]
    mcp = _make_mcp(available_tools=seeded)
    graph = _graph_double(mcp.id)
    registry = _registry_double()
    control = _gateway_control_client(authorizer_type="CUSTOM_JWT")

    control.list_gateway_targets.side_effect = RuntimeError("AccessDeniedException")

    svc = _make_service(graph=graph, registry=registry, control=control)
    result = await svc.provision(mcp)

    assert result.identity_status == "provisioned"
    control.update_gateway.assert_called_once()
    # Seeded tools NOT wiped by the failed native read.
    assert [t.name for t in result.available_tools] == ["seeded_tool"]


@pytest.mark.asyncio
async def test_provision_falls_back_to_wire_scan_when_open_and_native_empty(patch_scan):
    # An OPEN (NONE) gateway whose native read finds NO targets must fall back to the
    # token-less wire-scan (which still works while the gateway is open).
    mcp = _make_mcp()
    graph = _graph_double(mcp.id)
    registry = _registry_double()
    control = _gateway_control_client(authorizer_type="NONE")

    # Native read returns no targets (default helper already returns {"items": []}).
    patch_scan.return_value = [
        McpTool(name="wire_tool", description="from wire", input_schema={"type": "object"})
    ]

    svc = _make_service(graph=graph, registry=registry, control=control)
    result = await svc.provision(mcp)

    # The wire-scan fallback ran with bearer=None and its tool landed.
    patch_scan.assert_awaited_once()
    call = patch_scan.await_args
    bearer = call.args[1] if len(call.args) > 1 else call.kwargs.get("bearer")
    assert bearer is None
    assert [t.name for t in result.available_tools] == ["wire_tool"]
    assert result.identity_status == "provisioned"


@pytest.mark.asyncio
async def test_native_tool_read_per_target_error_is_isolated(patch_scan):
    # A get_gateway_target error on the FIRST target must be isolated: the second
    # target's tools still land. Proves the per-target try/except guarantee.
    mcp = _make_mcp()
    graph = _graph_double(mcp.id)
    registry = _registry_double()
    control = _gateway_control_client(authorizer_type="CUSTOM_JWT")

    control.list_gateway_targets.return_value = {
        "items": [
            {"targetId": "tgt-bad", "name": "bad"},
            {"targetId": "tgt-good", "name": "good"},
        ]
    }
    # First call raises; second call returns a valid lambda target.
    control.get_gateway_target.side_effect = [
        RuntimeError("boom"),
        {
            "name": "good",
            **_lambda_target_config(tools=[{"name": "ok_tool", "inputSchema": {}}]),
        },
    ]

    svc = _make_service(graph=graph, registry=registry, control=control)
    result = await svc.provision(mcp)

    # The bad target was skipped; the good target's tool still populated.
    assert [t.name for t in result.available_tools] == ["good___ok_tool"]
    assert result.identity_status == "provisioned"


@pytest.mark.asyncio
async def test_native_tool_read_pagination_is_bounded(patch_scan):
    # list_gateway_targets ALWAYS returns a nextToken (never self-terminates).
    # The loop must stop at _MAX_TARGET_PAGES and provisioning must still complete
    # (no infinite loop). Locks in the bound against future edits.
    from services.mcp_identity_service import _MAX_TARGET_PAGES

    mcp = _make_mcp()
    graph = _graph_double(mcp.id)
    registry = _registry_double()
    control = _gateway_control_client(authorizer_type="CUSTOM_JWT")

    # Every page returns a nextToken — the loop must self-stop at _MAX_TARGET_PAGES.
    control.list_gateway_targets.return_value = {"items": [], "nextToken": "forever"}

    svc = _make_service(graph=graph, registry=registry, control=control)
    result = await svc.provision(mcp)

    assert control.list_gateway_targets.call_count == _MAX_TARGET_PAGES
    assert result.identity_status == "provisioned"


# ===========================================================================
# T-REFRESH-TOOLS-BE — refresh_tools: synchronous, tools-ONLY native re-read.
# Re-reads a gateway's tools natively + persists, WITHOUT touching the
# authorizer / identity. Best-effort + overwrite-only-on-non-empty (CRITIC-I1).
# ===========================================================================
@pytest.mark.asyncio
async def test_refresh_tools_reads_native_and_persists():
    # A locked CUSTOM_JWT gateway whose native read returns 2 tools — available_tools
    # is overwritten to those 2 + persist_identity is called. The authorizer / identity
    # are left untouched (update_gateway NOT called; create_mcp_app NOT called).
    mcp = _make_mcp(
        entra_sp_id="sp-existing",
        entra_app_audience="api://agp-mcp-mcp-rec-123",
        identity_status="provisioned",
        available_tools=[McpTool(name="old___stale", description="stale", input_schema={})],
    )
    mcp.gateway_id = GATEWAY_ID
    graph = _graph_double(mcp.id)
    registry = _registry_double()
    control = _gateway_control_client(authorizer_type="CUSTOM_JWT")

    control.list_gateway_targets.return_value = {
        "items": [{"targetId": "tgt-1", "name": "claims"}]
    }
    control.get_gateway_target.return_value = {
        "name": "claims",
        **_lambda_target_config(
            tools=[
                {"name": "echo", "description": "echo back", "inputSchema": {"type": "object"}},
                {"name": "add", "description": "add two numbers", "inputSchema": {"type": "object"}},
            ]
        ),
    }

    svc = _make_service(graph=graph, registry=registry, control=control)
    result = await svc.refresh_tools(mcp)

    # The fresh native tools replaced the stale ones.
    assert sorted(t.name for t in result.available_tools) == ["claims___add", "claims___echo"]
    # Persisted exactly once (only when we got tools).
    registry.persist_identity.assert_called_once()
    # Tools-only: NO authorizer reconfigure / identity create / status change.
    control.update_gateway.assert_not_called()
    graph.create_mcp_app.assert_not_awaited()
    graph.set_assignment_required.assert_not_awaited()
    assert result.identity_status == "provisioned"
    assert result.entra_sp_id == "sp-existing"


@pytest.mark.asyncio
async def test_refresh_tools_derives_gateway_id_from_arn_when_absent():
    # No gateway_id on the record, but a gateway_arn is present → it must be derived.
    mcp = _make_mcp(identity_status="provisioned")  # gateway_arn set, gateway_id None
    assert mcp.gateway_id is None
    graph = _graph_double(mcp.id)
    registry = _registry_double()
    control = _gateway_control_client(authorizer_type="CUSTOM_JWT")

    control.list_gateway_targets.return_value = {
        "items": [{"targetId": "tgt-1", "name": "claims"}]
    }
    control.get_gateway_target.return_value = {
        "name": "claims",
        **_lambda_target_config(tools=[{"name": "echo", "inputSchema": {}}]),
    }

    svc = _make_service(graph=graph, registry=registry, control=control)
    result = await svc.refresh_tools(mcp)

    # The native read was targeted by the gatewayId derived from the ARN.
    control.list_gateway_targets.assert_called()
    assert control.list_gateway_targets.call_args.kwargs["gatewayIdentifier"] == GATEWAY_ID
    assert [t.name for t in result.available_tools] == ["claims___echo"]


@pytest.mark.asyncio
async def test_refresh_tools_empty_keeps_existing():
    # CRITIC-I1: an empty native read must NOT wipe existing tools + must NOT persist.
    seeded = [McpTool(name="seeded___tool", description="from before", input_schema={})]
    mcp = _make_mcp(identity_status="provisioned", available_tools=seeded)
    mcp.gateway_id = GATEWAY_ID
    graph = _graph_double(mcp.id)
    registry = _registry_double()
    control = _gateway_control_client(authorizer_type="CUSTOM_JWT")

    # Native read finds nothing (default helper returns {"items": []}).
    svc = _make_service(graph=graph, registry=registry, control=control)
    result = await svc.refresh_tools(mcp)

    # Existing tools kept, nothing persisted (no needless write / UPDATING-race).
    assert [t.name for t in result.available_tools] == ["seeded___tool"]
    registry.persist_identity.assert_not_called()
    control.update_gateway.assert_not_called()


@pytest.mark.asyncio
async def test_refresh_tools_error_is_best_effort():
    # CRITIC-I1: a native-read error is swallowed — returns the mcp unchanged, no raise,
    # no wipe, no persist. (A flaky read must never 500 the refresh button.)
    seeded = [McpTool(name="seeded___tool", description="from before", input_schema={})]
    mcp = _make_mcp(identity_status="provisioned", available_tools=seeded)
    mcp.gateway_id = GATEWAY_ID
    graph = _graph_double(mcp.id)
    registry = _registry_double()
    control = _gateway_control_client(authorizer_type="CUSTOM_JWT")

    control.list_gateway_targets.side_effect = RuntimeError("AccessDeniedException")

    svc = _make_service(graph=graph, registry=registry, control=control)
    result = await svc.refresh_tools(mcp)  # must NOT raise

    assert [t.name for t in result.available_tools] == ["seeded___tool"]
    registry.persist_identity.assert_not_called()
    control.update_gateway.assert_not_called()


@pytest.mark.asyncio
async def test_refresh_tools_no_gateway_handle_returns_unchanged():
    # Defensive: no gateway_id AND no gateway_arn → return unchanged, no read, no persist.
    seeded = [McpTool(name="seeded___tool", description="from before", input_schema={})]
    mcp = _make_mcp(gateway_arn=None, identity_status="provisioned", available_tools=seeded)
    assert mcp.gateway_id is None and mcp.gateway_arn is None
    graph = _graph_double(mcp.id)
    registry = _registry_double()
    control = _gateway_control_client(authorizer_type="CUSTOM_JWT")

    svc = _make_service(graph=graph, registry=registry, control=control)
    result = await svc.refresh_tools(mcp)

    assert [t.name for t in result.available_tools] == ["seeded___tool"]
    registry.persist_identity.assert_not_called()
    control.list_gateway_targets.assert_not_called()
