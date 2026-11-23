"""Tests for ``services.mcp_identity_service``, the MCP identity provisioning orchestrator.

The deltas from the agent-side orchestrator are baked into the assertions: provision never calls
the consent grant (that happens at grant time) and a pre-lockdown tool scan precedes app creation,
so create is not step one; the scan is best-effort (a ``McpScanError`` never fails provisioning and
an empty or failed discovery never wipes seeded tools); all five Entra ids are set after create and
persisted immediately, before the authorizer is configured; the gateway and runtime authorizers
feed both audience forms (URI and GUID).

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

TENANT_ID = "00000000-0000-0000-0000-000000000001"
LOGIN_BASE = "https://login.microsoftonline.com"
REGION = "us-east-1"

GATEWAY_ID = "demo-claims-gw-aBcDeFgHiJ"
GATEWAY_ARN = f"arn:aws:bedrock-agentcore:us-east-1:123456789012:gateway/{GATEWAY_ID}"
GATEWAY_URL = f"https://{GATEWAY_ID}.gateway.bedrock-agentcore.us-east-1.amazonaws.com/mcp"

RUNTIME_RID = "mcp_runtime_test-wdOmsREOEj"
RUNTIME_ARN = f"arn:aws:bedrock-agentcore:us-east-1:123456789012:runtime/{RUNTIME_RID}"


def _make_mcp(
    *,
    mcp_id: str = "mcp-rec-123",
    name: str = "Internal Claims MCP",
    kind: Kind = Kind.GATEWAY,
    gateway_arn: str | None = GATEWAY_ARN,
    runtime_arn: str | None = None,
    entra_sp_id: str | None = None,
    entra_app_audience: str | None = None,
    identity_status: IdentityStatus = IdentityStatus.NONE,
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


def _gateway_control_client(statuses: list[str] | None = None, *, authorizer_type: str = "NONE") -> MagicMock:
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
        control.get_gateway.side_effect = [{**base, "status": s} for s in statuses]
    control.update_gateway.return_value = {"status": "UPDATING"}
    control.list_gateway_targets.return_value = {"items": []}
    return control


def _runtime_control_client(statuses: list[str] | None = None) -> MagicMock:
    """A MagicMock boto3 control client for the RUNTIME-MCP path."""
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


def test_should_provision_gate():
    assert should_provision_mcp(_make_mcp(kind=Kind.GATEWAY, gateway_arn=GATEWAY_ARN)) is True
    assert (
        should_provision_mcp(_make_mcp(kind=Kind.RUNTIME, gateway_arn=None, runtime_arn=RUNTIME_ARN)) is True
    )

    assert should_provision_mcp(_make_mcp(kind=Kind.STANDARD, gateway_arn=None)) is False
    assert should_provision_mcp(_make_mcp(kind=Kind.STANDARD, gateway_arn=GATEWAY_ARN)) is False

    assert should_provision_mcp(_make_mcp(kind=Kind.GATEWAY, gateway_arn=None)) is False
    assert should_provision_mcp(_make_mcp(kind=Kind.GATEWAY, gateway_arn="")) is False
    assert should_provision_mcp(_make_mcp(kind=Kind.RUNTIME, gateway_arn=None, runtime_arn=None)) is False


@pytest.mark.asyncio
async def test_provision_gateway_happy_path(patch_scan):
    mcp = _make_mcp()
    graph = _graph_double(mcp.id)
    registry = _registry_double()
    control = _gateway_control_client(authorizer_type="NONE")

    manager = MagicMock()
    manager.attach_mock(control.get_gateway, "get_gateway")
    manager.attach_mock(patch_scan, "scan_mcp_tools")
    manager.attach_mock(graph.create_mcp_app, "create_mcp_app")
    manager.attach_mock(graph.set_assignment_required, "set_assignment_required")
    manager.attach_mock(registry.persist_identity, "persist_identity")
    manager.attach_mock(control.update_gateway, "update_gateway")

    svc = _make_service(graph=graph, registry=registry, control=control)
    result = await svc.provision(mcp)

    order = [c[0] for c in manager.mock_calls]
    assert order[0] == "get_gateway"
    assert "scan_mcp_tools" in order
    assert order.index("scan_mcp_tools") < order.index("create_mcp_app")
    assert order.index("create_mcp_app") < order.index("set_assignment_required")
    assert order.index("set_assignment_required") < order.index("update_gateway")
    create_idx = order.index("create_mcp_app")
    sar_idx = order.index("set_assignment_required")
    assert "persist_identity" in order[create_idx:sar_idx]

    assert result.identity_status == "provisioned"
    assert result.gateway_url == GATEWAY_URL
    assert result.gateway_id == GATEWAY_ID
    assert result.entra_app_id == f"app-client-guid-{mcp.id}"
    assert result.entra_sp_id == f"sp-obj-id-{mcp.id}"
    assert result.invoker_role_id == f"invoker-{mcp.id}"
    assert result.admin_role_id == f"admin-{mcp.id}"
    assert result.entra_app_audience == f"api://agp-mcp-{mcp.id}"

    graph.create_mcp_app.assert_awaited_once_with(mcp.id, mcp.name)

    graph.set_assignment_required.assert_awaited_once_with(f"sp-obj-id-{mcp.id}", required=False)


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

    assert result.identity_status is IdentityStatus.PROVISIONED

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        result.model_dump()
        jsonable_encoder(result)
    assert [str(w.message) for w in caught if "Pydantic serializer warnings" in str(w.message)] == []


@pytest.mark.asyncio
async def test_provision_never_calls_obo_consent(patch_scan):
    mcp = _make_mcp()
    graph = _graph_double(mcp.id)
    registry = _registry_double()
    control = _gateway_control_client()

    svc = _make_service(graph=graph, registry=registry, control=control)
    await svc.provision(mcp)

    graph.grant_agent_obo_consent.assert_not_awaited()
    graph.grant_agent_obo_consent.assert_not_called()


@pytest.mark.asyncio
async def test_provision_configures_gateway_authorizer_both_audience_forms(patch_scan):
    mcp = _make_mcp()
    graph = _graph_double(mcp.id)
    registry = _registry_double()
    control = _gateway_control_client(authorizer_type="NONE")

    read = control.get_gateway.return_value

    svc = _make_service(graph=graph, registry=registry, control=control)
    await svc.provision(mcp)

    _, kwargs = control.update_gateway.call_args
    jwt = kwargs["authorizerConfiguration"]["customJWTAuthorizer"]

    assert jwt["allowedAudience"] == [mcp.entra_app_audience, mcp.entra_app_id]
    assert "allowedClients" not in jwt
    assert "customClaims" not in jwt
    assert "allowedScopes" not in jwt
    assert jwt["discoveryUrl"] == (f"{LOGIN_BASE}/{TENANT_ID}/v2.0/.well-known/openid-configuration")
    assert kwargs["authorizerType"] == "CUSTOM_JWT"
    assert kwargs["name"] == read["name"]
    assert kwargs["roleArn"] == read["roleArn"]
    assert kwargs["protocolType"] == read["protocolType"]
    assert kwargs["protocolConfiguration"] == read["protocolConfiguration"]
    assert kwargs["gatewayIdentifier"] == GATEWAY_ID
    assert kwargs["gatewayIdentifier"] != GATEWAY_ARN


def _gateway_with_jwt_authorizer(discovery_url: str) -> MagicMock:
    control = _gateway_control_client(authorizer_type="CUSTOM_JWT")
    control.get_gateway.return_value = {
        **control.get_gateway.return_value,
        "authorizerConfiguration": {
            "customJWTAuthorizer": {
                "discoveryUrl": discovery_url,
                "allowedAudience": ["api://stale"],
                "allowedScopes": ["Invoke"],
                "allowedClients": ["client-guid"],
                "customClaims": [{"inboundTokenClaimName": "tid"}],
            }
        },
    }
    return control


@pytest.mark.asyncio
async def test_authorizer_replay_keeps_scopes_and_clients_under_the_same_entra_issuer(patch_scan):
    """A Terraform-built gateway is born with ``allowedScopes=["Invoke"]`` (the gate
    that 403s app-only tokens) under AGP's Entra issuer. The re-config owns only discoveryUrl +
    allowedAudience; the other customJWTAuthorizer fields are carried through, never forced."""
    mcp = _make_mcp()
    entra = f"{LOGIN_BASE}/{TENANT_ID}/v2.0/.well-known/openid-configuration"
    control = _gateway_with_jwt_authorizer(entra)

    svc = _make_service(graph=_graph_double(mcp.id), registry=_registry_double(), control=control)
    await svc.provision(mcp)

    _, kwargs = control.update_gateway.call_args
    jwt = kwargs["authorizerConfiguration"]["customJWTAuthorizer"]
    assert jwt["allowedScopes"] == ["Invoke"]
    assert jwt["allowedClients"] == ["client-guid"]
    assert jwt["customClaims"] == [{"inboundTokenClaimName": "tid"}]
    assert jwt["allowedAudience"] == [mcp.entra_app_audience, mcp.entra_app_id]
    assert jwt["discoveryUrl"] == entra


@pytest.mark.asyncio
async def test_authorizer_from_another_issuer_is_rebuilt_fresh(patch_scan):
    """A gateway previously locked to a different IdP must not keep that IdP's clients/scopes
    under the Entra issuer — the authorizer is built fresh ."""
    mcp = _make_mcp()
    control = _gateway_with_jwt_authorizer("https://other-idp.example/.well-known/openid-configuration")

    svc = _make_service(graph=_graph_double(mcp.id), registry=_registry_double(), control=control)
    await svc.provision(mcp)

    _, kwargs = control.update_gateway.call_args
    assert kwargs["authorizerConfiguration"] == {
        "customJWTAuthorizer": {
            "discoveryUrl": f"{LOGIN_BASE}/{TENANT_ID}/v2.0/.well-known/openid-configuration",
            "allowedAudience": [mcp.entra_app_audience, mcp.entra_app_id],
        }
    }


@pytest.mark.asyncio
async def test_provision_runtime_uses_update_agent_runtime(patch_scan):
    mcp = _make_mcp(kind=Kind.RUNTIME, gateway_arn=None, runtime_arn=RUNTIME_ARN)
    graph = _graph_double(mcp.id)
    registry = _registry_double()
    control = _runtime_control_client()

    svc = _make_service(graph=graph, registry=registry, control=control)
    result = await svc.provision(mcp)

    control.update_agent_runtime.assert_called_once()
    control.update_gateway.assert_not_called()

    _, kwargs = control.update_agent_runtime.call_args
    jwt = kwargs["authorizerConfiguration"]["customJWTAuthorizer"]
    assert jwt["allowedAudience"] == [mcp.entra_app_audience, mcp.entra_app_id]
    assert kwargs["agentRuntimeId"] == RUNTIME_RID
    assert kwargs["agentRuntimeId"] != RUNTIME_ARN
    assert result.identity_status == "provisioned"


@pytest.mark.asyncio
async def test_provision_prelockdown_scan_no_bearer(patch_scan):
    mcp = _make_mcp()
    graph = _graph_double(mcp.id)
    registry = _registry_double()
    control = _gateway_control_client(authorizer_type="NONE")

    svc = _make_service(graph=graph, registry=registry, control=control)
    result = await svc.provision(mcp)

    patch_scan.assert_awaited_once()
    call = patch_scan.await_args
    endpoint = call.args[0] if call.args else call.kwargs.get("endpoint_url")
    assert endpoint == GATEWAY_URL
    bearer = call.args[1] if len(call.args) > 1 else call.kwargs.get("bearer")
    assert bearer is None
    assert [t.name for t in result.available_tools] == ["get_claim"]


@pytest.mark.asyncio
async def test_provision_skips_scan_when_already_custom_jwt(patch_scan):
    seeded = [McpTool(name="seeded_tool", description="seeded", input_schema={})]
    mcp = _make_mcp(available_tools=seeded)
    graph = _graph_double(mcp.id)
    registry = _registry_double()
    control = _gateway_control_client(authorizer_type="CUSTOM_JWT")

    svc = _make_service(graph=graph, registry=registry, control=control)
    result = await svc.provision(mcp)

    patch_scan.assert_not_awaited()
    assert [t.name for t in result.available_tools] == ["seeded_tool"]


@pytest.mark.asyncio
async def test_provision_scan_failure_does_not_fail_provisioning(patch_scan):
    seeded = [McpTool(name="seeded_tool", description="seeded", input_schema={})]
    mcp = _make_mcp(available_tools=seeded)
    graph = _graph_double(mcp.id)
    registry = _registry_double()
    control = _gateway_control_client(authorizer_type="NONE")

    patch_scan.side_effect = McpScanError("MCP server returned HTTP 503")

    svc = _make_service(graph=graph, registry=registry, control=control)
    result = await svc.provision(mcp)

    assert result.identity_status == "provisioned"
    control.update_gateway.assert_called_once()
    assert [t.name for t in result.available_tools] == ["seeded_tool"]


@pytest.mark.asyncio
async def test_provision_empty_scan_does_not_wipe_seeded_tools(patch_scan):
    seeded = [McpTool(name="seeded_tool", description="seeded", input_schema={})]
    mcp = _make_mcp(available_tools=seeded)
    graph = _graph_double(mcp.id)
    registry = _registry_double()
    control = _gateway_control_client(authorizer_type="NONE")

    patch_scan.return_value = []

    svc = _make_service(graph=graph, registry=registry, control=control)
    result = await svc.provision(mcp)

    assert result.identity_status == "provisioned"
    assert [t.name for t in result.available_tools] == ["seeded_tool"]


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


@pytest.mark.asyncio
async def test_provision_persists_ids_immediately_after_create(patch_scan):
    mcp = _make_mcp()
    graph = _graph_double(mcp.id)
    registry = _registry_double()
    control = _gateway_control_client()

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
    assert snapshot["persist_count_at_step"] >= 1


@pytest.mark.asyncio
async def test_provision_idempotent_skips_create_when_sp_exists(patch_scan):
    mcp = _make_mcp(
        entra_sp_id="sp-existing",
        entra_app_audience="api://agp-mcp-mcp-rec-123",
        identity_status=IdentityStatus.FAILED,
    )
    mcp.entra_app_id = "app-existing"
    mcp.invoker_role_id = "invoker-existing"
    mcp.admin_role_id = "admin-existing"
    graph = _graph_double(mcp.id)
    registry = _registry_double()
    control = _gateway_control_client()

    svc = _make_service(graph=graph, registry=registry, control=control)
    result = await svc.provision(mcp)

    graph.create_mcp_app.assert_not_awaited()
    control.update_gateway.assert_called_once()
    graph.set_assignment_required.assert_awaited_once_with("sp-existing", required=False)
    assert result.identity_status == "provisioned"


@pytest.mark.asyncio
async def test_provision_failure_sets_failed_and_raises(patch_scan):
    mcp = _make_mcp()
    graph = _graph_double(mcp.id)
    registry = _registry_double()
    control = _gateway_control_client()

    control.update_gateway.side_effect = RuntimeError("ValidationException")

    svc = _make_service(graph=graph, registry=registry, control=control)
    with pytest.raises(McpProvisioningError):
        await svc.provision(mcp)

    assert mcp.identity_status == "failed"
    persisted_statuses = [c.args[0].identity_status for c in registry.persist_identity.call_args_list]
    assert "failed" in persisted_statuses


@pytest.mark.asyncio
async def test_provision_runtime_update_failed_status_raises(patch_scan):
    mcp = _make_mcp(kind=Kind.RUNTIME, gateway_arn=None, runtime_arn=RUNTIME_ARN)
    graph = _graph_double(mcp.id)
    registry = _registry_double()
    control = _runtime_control_client(statuses=["READY", "UPDATE_FAILED"])

    svc = _make_service(graph=graph, registry=registry, control=control)
    with pytest.raises(McpProvisioningError):
        await svc.provision(mcp)

    assert mcp.identity_status == "failed"


@pytest.mark.asyncio
async def test_provision_failure_logs_traceback_for_unexpected_error(patch_scan, caplog):
    mcp = _make_mcp()
    graph = _graph_double(mcp.id)
    registry = _registry_double()
    control = _gateway_control_client()

    control.get_gateway.side_effect = RuntimeError("boom — unexpected control-plane error")

    svc = _make_service(graph=graph, registry=registry, control=control)
    with caplog.at_level(logging.ERROR, logger="services.mcp_identity_service"):
        with pytest.raises(McpProvisioningError):
            await svc.provision(mcp)

    failure_records = [
        r
        for r in caplog.records
        if r.name == "services.mcp_identity_service"
        and r.levelno == logging.ERROR
        and r.exc_info is not None
        and mcp.id in r.getMessage()
    ]
    assert failure_records, "expected an ERROR record with a traceback for the unexpected failure"

    assert mcp.identity_status == "failed"
    persisted_statuses = [c.args[0].identity_status for c in registry.persist_identity.call_args_list]
    assert "failed" in persisted_statuses


@pytest.mark.asyncio
async def test_configure_gateway_dispatched_off_loop(patch_scan, monkeypatch):
    mcp = _make_mcp()
    graph = _graph_double(mcp.id)
    registry = _registry_double()
    control = _gateway_control_client()

    import services.mcp_identity_service as mod

    real_run_sync = mod.anyio.to_thread.run_sync
    recorder = AsyncMock(side_effect=real_run_sync)
    monkeypatch.setattr(mod.anyio.to_thread, "run_sync", recorder)

    svc = _make_service(graph=graph, registry=registry, control=control)
    await svc.provision(mcp)

    authorizer_calls = [
        c for c in recorder.await_args_list if c.args and c.args[0] == svc._configure_gateway_authorizer
    ]
    assert len(authorizer_calls) == 1
    awaited_args = authorizer_calls[0].args
    assert awaited_args[1] == GATEWAY_ID
    assert awaited_args[2] is mcp
    native_calls = [
        c for c in recorder.await_args_list if c.args and c.args[0] == svc._read_gateway_tools_native
    ]
    assert len(native_calls) == 1


@pytest.mark.asyncio
async def test_provision_polls_gateway_to_ready(patch_scan):
    mcp = _make_mcp()
    graph = _graph_double(mcp.id)
    registry = _registry_double()
    control = _gateway_control_client(statuses=["READY", "UPDATING", "UPDATING", "READY"])

    svc = _make_service(graph=graph, registry=registry, control=control)
    result = await svc.provision(mcp)

    assert result.identity_status == "provisioned"
    assert control.get_gateway.call_count >= 2


@pytest.mark.asyncio
async def test_provision_runtime_replays_optional_fields(patch_scan):
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
    assert kwargs["environmentVariables"] == env_vars
    assert kwargs["protocolConfiguration"] == proto_cfg
    assert "agentRuntimeArtifact" in kwargs
    assert "roleArn" in kwargs
    assert "networkConfiguration" in kwargs
    assert "authorizerConfiguration" in kwargs


@pytest.mark.asyncio
async def test_provision_empty_audience_raises_before_update_gateway(patch_scan):
    mcp = _make_mcp(
        entra_sp_id="sp-existing",
        entra_app_audience=None,
        identity_status=IdentityStatus.FAILED,
    )
    mcp.entra_app_id = None
    graph = _graph_double(mcp.id)
    registry = _registry_double()
    control = _gateway_control_client()

    svc = _make_service(graph=graph, registry=registry, control=control)
    with pytest.raises(McpProvisioningError):
        await svc.provision(mcp)

    control.update_gateway.assert_not_called()
    assert mcp.identity_status == "failed"


def _lambda_target_config(*, tools: list[dict]) -> dict:
    """A GetGatewayTarget targetConfiguration for a LAMBDA target with an inline toolSchema."""
    return {"targetConfiguration": {"mcp": {"lambda": {"toolSchema": {"inlinePayload": tools}}}}}


@pytest.mark.asyncio
async def test_provision_reads_native_lambda_tools_on_custom_jwt_gateway(patch_scan):
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

    assert sorted(t.name for t in result.available_tools) == ["claims___add", "claims___echo"]
    patch_scan.assert_not_awaited()
    control.list_gateway_targets.assert_called()
    control.get_gateway_target.assert_called_once_with(gatewayIdentifier=GATEWAY_ID, targetId="tgt-1")
    assert result.identity_status == "provisioned"


@pytest.mark.asyncio
async def test_native_tool_read_paginates_targets(patch_scan):
    mcp = _make_mcp()
    graph = _graph_double(mcp.id)
    registry = _registry_double()
    control = _gateway_control_client(authorizer_type="CUSTOM_JWT")

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
    seeded = [McpTool(name="seeded_tool", description="seeded", input_schema={})]
    mcp = _make_mcp(available_tools=seeded)
    graph = _graph_double(mcp.id)
    registry = _registry_double()
    control = _gateway_control_client(authorizer_type="CUSTOM_JWT")

    control.list_gateway_targets.side_effect = RuntimeError("AccessDeniedException")

    svc = _make_service(graph=graph, registry=registry, control=control)
    result = await svc.provision(mcp)

    assert result.identity_status == "provisioned"
    control.update_gateway.assert_called_once()
    assert [t.name for t in result.available_tools] == ["seeded_tool"]


@pytest.mark.asyncio
async def test_provision_falls_back_to_wire_scan_when_open_and_native_empty(patch_scan):
    mcp = _make_mcp()
    graph = _graph_double(mcp.id)
    registry = _registry_double()
    control = _gateway_control_client(authorizer_type="NONE")

    patch_scan.return_value = [
        McpTool(name="wire_tool", description="from wire", input_schema={"type": "object"})
    ]

    svc = _make_service(graph=graph, registry=registry, control=control)
    result = await svc.provision(mcp)

    patch_scan.assert_awaited_once()
    call = patch_scan.await_args
    bearer = call.args[1] if len(call.args) > 1 else call.kwargs.get("bearer")
    assert bearer is None
    assert [t.name for t in result.available_tools] == ["wire_tool"]
    assert result.identity_status == "provisioned"


@pytest.mark.asyncio
async def test_native_tool_read_per_target_error_is_isolated(patch_scan):
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
    control.get_gateway_target.side_effect = [
        RuntimeError("boom"),
        {
            "name": "good",
            **_lambda_target_config(tools=[{"name": "ok_tool", "inputSchema": {}}]),
        },
    ]

    svc = _make_service(graph=graph, registry=registry, control=control)
    result = await svc.provision(mcp)

    assert [t.name for t in result.available_tools] == ["good___ok_tool"]
    assert result.identity_status == "provisioned"


@pytest.mark.asyncio
async def test_native_tool_read_pagination_is_bounded(patch_scan):
    from services.mcp_identity_service import _MAX_TARGET_PAGES

    mcp = _make_mcp()
    graph = _graph_double(mcp.id)
    registry = _registry_double()
    control = _gateway_control_client(authorizer_type="CUSTOM_JWT")

    control.list_gateway_targets.return_value = {"items": [], "nextToken": "forever"}

    svc = _make_service(graph=graph, registry=registry, control=control)
    result = await svc.provision(mcp)

    assert control.list_gateway_targets.call_count == _MAX_TARGET_PAGES
    assert result.identity_status == "provisioned"


@pytest.mark.asyncio
async def test_refresh_tools_reads_native_and_persists():
    mcp = _make_mcp(
        entra_sp_id="sp-existing",
        entra_app_audience="api://agp-mcp-mcp-rec-123",
        identity_status=IdentityStatus.PROVISIONED,
        available_tools=[McpTool(name="old___stale", description="stale", input_schema={})],
    )
    mcp.gateway_id = GATEWAY_ID
    graph = _graph_double(mcp.id)
    registry = _registry_double()
    control = _gateway_control_client(authorizer_type="CUSTOM_JWT")

    control.list_gateway_targets.return_value = {"items": [{"targetId": "tgt-1", "name": "claims"}]}
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

    assert sorted(t.name for t in result.available_tools) == ["claims___add", "claims___echo"]
    registry.persist_identity.assert_called_once()
    control.update_gateway.assert_not_called()
    graph.create_mcp_app.assert_not_awaited()
    graph.set_assignment_required.assert_not_awaited()
    assert result.identity_status == "provisioned"
    assert result.entra_sp_id == "sp-existing"


@pytest.mark.asyncio
async def test_refresh_tools_derives_gateway_id_from_arn_when_absent():
    mcp = _make_mcp(identity_status=IdentityStatus.PROVISIONED)
    assert mcp.gateway_id is None
    graph = _graph_double(mcp.id)
    registry = _registry_double()
    control = _gateway_control_client(authorizer_type="CUSTOM_JWT")

    control.list_gateway_targets.return_value = {"items": [{"targetId": "tgt-1", "name": "claims"}]}
    control.get_gateway_target.return_value = {
        "name": "claims",
        **_lambda_target_config(tools=[{"name": "echo", "inputSchema": {}}]),
    }

    svc = _make_service(graph=graph, registry=registry, control=control)
    result = await svc.refresh_tools(mcp)

    control.list_gateway_targets.assert_called()
    assert control.list_gateway_targets.call_args.kwargs["gatewayIdentifier"] == GATEWAY_ID
    assert [t.name for t in result.available_tools] == ["claims___echo"]


@pytest.mark.asyncio
async def test_refresh_tools_empty_keeps_existing():
    seeded = [McpTool(name="seeded___tool", description="from before", input_schema={})]
    mcp = _make_mcp(identity_status=IdentityStatus.PROVISIONED, available_tools=seeded)
    mcp.gateway_id = GATEWAY_ID
    graph = _graph_double(mcp.id)
    registry = _registry_double()
    control = _gateway_control_client(authorizer_type="CUSTOM_JWT")

    svc = _make_service(graph=graph, registry=registry, control=control)
    result = await svc.refresh_tools(mcp)

    assert [t.name for t in result.available_tools] == ["seeded___tool"]
    registry.persist_identity.assert_not_called()
    control.update_gateway.assert_not_called()


@pytest.mark.asyncio
async def test_refresh_tools_error_is_best_effort():
    seeded = [McpTool(name="seeded___tool", description="from before", input_schema={})]
    mcp = _make_mcp(identity_status=IdentityStatus.PROVISIONED, available_tools=seeded)
    mcp.gateway_id = GATEWAY_ID
    graph = _graph_double(mcp.id)
    registry = _registry_double()
    control = _gateway_control_client(authorizer_type="CUSTOM_JWT")

    control.list_gateway_targets.side_effect = RuntimeError("AccessDeniedException")

    svc = _make_service(graph=graph, registry=registry, control=control)
    result = await svc.refresh_tools(mcp)

    assert [t.name for t in result.available_tools] == ["seeded___tool"]
    registry.persist_identity.assert_not_called()
    control.update_gateway.assert_not_called()


@pytest.mark.asyncio
async def test_refresh_tools_no_gateway_handle_returns_unchanged():
    seeded = [McpTool(name="seeded___tool", description="from before", input_schema={})]
    mcp = _make_mcp(gateway_arn=None, identity_status=IdentityStatus.PROVISIONED, available_tools=seeded)
    assert mcp.gateway_id is None and mcp.gateway_arn is None
    graph = _graph_double(mcp.id)
    registry = _registry_double()
    control = _gateway_control_client(authorizer_type="CUSTOM_JWT")

    svc = _make_service(graph=graph, registry=registry, control=control)
    result = await svc.refresh_tools(mcp)

    assert [t.name for t in result.available_tools] == ["seeded___tool"]
    registry.persist_identity.assert_not_called()
    control.list_gateway_targets.assert_not_called()


@pytest.mark.asyncio
async def test_refresh_tools_identical_read_does_not_write():
    current = [McpTool(name="claims___echo", description="echo back", input_schema={})]
    mcp = _make_mcp(identity_status=IdentityStatus.PROVISIONED, available_tools=current)
    mcp.gateway_id = GATEWAY_ID
    registry = _registry_double()
    control = _gateway_control_client(authorizer_type="CUSTOM_JWT")
    control.list_gateway_targets.return_value = {"items": [{"targetId": "tgt-1", "name": "claims"}]}
    control.get_gateway_target.return_value = {
        "name": "claims",
        **_lambda_target_config(tools=[{"name": "echo", "description": "echo back", "inputSchema": {}}]),
    }

    svc = _make_service(graph=_graph_double(mcp.id), registry=registry, control=control)
    result = await svc.refresh_tools(mcp)

    assert [t.name for t in result.available_tools] == ["claims___echo"]
    registry.persist_identity.assert_not_called()


def test_mint_app_creates_app_and_persists_once_without_touching_a_gateway():
    mcp = _make_mcp(gateway_arn=None)
    graph = _graph_double(mcp.id)
    registry = _registry_double()
    control = _gateway_control_client()

    svc = _make_service(graph=graph, registry=registry, control=control)
    result = svc.mint_app(mcp)

    graph.create_mcp_app.assert_awaited_once_with(mcp.id, mcp.name)
    graph.set_assignment_required.assert_awaited_once_with(f"sp-obj-id-{mcp.id}", required=False)
    assert result.entra_app_id == f"app-client-guid-{mcp.id}"
    assert result.entra_sp_id == f"sp-obj-id-{mcp.id}"
    assert result.entra_app_audience == f"api://agp-mcp-{mcp.id}"
    assert result.invoker_role_id == f"invoker-{mcp.id}"
    assert result.admin_role_id == f"admin-{mcp.id}"
    assert result.identity_status == IdentityStatus.PENDING
    registry.persist_identity.assert_called_once()
    assert control.method_calls == []


def test_mint_app_is_idempotent_when_the_sp_already_exists():
    mcp = _make_mcp(gateway_arn=None, entra_sp_id="sp-existing")
    graph = _graph_double(mcp.id)
    registry = _registry_double()
    control = _gateway_control_client()

    svc = _make_service(graph=graph, registry=registry, control=control)
    result = svc.mint_app(mcp)

    assert result is mcp
    graph.create_mcp_app.assert_not_awaited()
    registry.persist_identity.assert_not_called()
    assert control.method_calls == []


def test_mint_app_without_sp_id_raises():
    mcp = _make_mcp(gateway_arn=None)
    graph = _graph_double(mcp.id)
    graph.create_mcp_app.return_value = {**graph.create_mcp_app.return_value, "sp_id": None}
    registry = _registry_double()

    svc = _make_service(graph=graph, registry=registry, control=_gateway_control_client())
    with pytest.raises(McpProvisioningError):
        svc.mint_app(mcp)
    graph.set_assignment_required.assert_not_awaited()


def _adopt_control() -> MagicMock:
    control = _gateway_control_client(authorizer_type="CUSTOM_JWT")
    control.list_gateway_targets.return_value = {"items": [{"targetId": "tgt-1", "name": "api"}]}
    control.get_gateway_target.return_value = {
        "name": "api",
        **_lambda_target_config(tools=[{"name": "getClaim", "inputSchema": {}}]),
    }
    return control


def _minted_mcp() -> McpServer:
    mcp = _make_mcp(
        gateway_arn=None,
        entra_sp_id="sp-existing",
        entra_app_audience="api://agp-mcp-mcp-rec-123",
        identity_status=IdentityStatus.PENDING,
    )
    return mcp


def test_adopt_built_gateway_writes_once_then_noops_on_identical_input():
    mcp = _minted_mcp()
    registry = _registry_double()
    control = _adopt_control()
    svc = _make_service(graph=_graph_double(mcp.id), registry=registry, control=control)

    result = svc.adopt_built_gateway(
        mcp, gateway_arn=GATEWAY_ARN, gateway_id=GATEWAY_ID, gateway_url=GATEWAY_URL
    )

    control.get_gateway.assert_called_once_with(gatewayIdentifier=GATEWAY_ID)
    assert result.gateway_arn == GATEWAY_ARN
    assert result.gateway_id == GATEWAY_ID
    assert result.gateway_url == GATEWAY_URL
    assert [t.name for t in result.available_tools] == ["api___getClaim"]
    assert result.identity_status == IdentityStatus.PROVISIONED
    registry.persist_identity.assert_called_once()

    svc.adopt_built_gateway(result, gateway_arn=GATEWAY_ARN, gateway_id=GATEWAY_ID, gateway_url=GATEWAY_URL)
    registry.persist_identity.assert_called_once()


def test_adopt_built_gateway_never_updates_the_gateway():
    mcp = _minted_mcp()
    control = _adopt_control()
    svc = _make_service(graph=_graph_double(mcp.id), registry=_registry_double(), control=control)

    svc.adopt_built_gateway(mcp, gateway_arn=GATEWAY_ARN, gateway_id=GATEWAY_ID, gateway_url=GATEWAY_URL)

    control.update_gateway.assert_not_called()


def test_adopt_built_gateway_raises_when_the_gateway_is_missing():
    mcp = _minted_mcp()
    registry = _registry_double()
    control = _adopt_control()
    control.get_gateway.side_effect = RuntimeError("ResourceNotFoundException")
    svc = _make_service(graph=_graph_double(mcp.id), registry=registry, control=control)

    with pytest.raises(McpProvisioningError):
        svc.adopt_built_gateway(mcp, gateway_arn=GATEWAY_ARN, gateway_id=GATEWAY_ID, gateway_url=GATEWAY_URL)
    registry.persist_identity.assert_not_called()


def test_adopt_built_gateway_keeps_existing_tools_when_discovery_is_empty():
    seeded = [McpTool(name="api___getClaim", description="", input_schema={})]
    mcp = _minted_mcp()
    mcp.available_tools = seeded
    registry = _registry_double()
    control = _adopt_control()
    control.list_gateway_targets.side_effect = RuntimeError("AccessDeniedException")
    svc = _make_service(graph=_graph_double(mcp.id), registry=registry, control=control)

    result = svc.adopt_built_gateway(
        mcp, gateway_arn=GATEWAY_ARN, gateway_id=GATEWAY_ID, gateway_url=GATEWAY_URL
    )

    assert [t.name for t in result.available_tools] == ["api___getClaim"]
    assert result.identity_status == IdentityStatus.PROVISIONED
    registry.persist_identity.assert_called_once()


def test_registry_create_stamps_project_and_repo_ownership(mcp_service, mcp_mock_registry_clients):
    import json

    from models.mcp_server import McpServerCreate

    ctl, _ = mcp_mock_registry_clients
    req = McpServerCreate(name="claims-api", kind=Kind.GATEWAY, tenant_id="default")

    mcp = mcp_service.create(req, project_id="proj-1", repo_id="repo-1")

    gov = json.loads(ctl.create_registry_record.call_args.kwargs["descriptors"]["mcpServer"]["data"])[
        "_meta"
    ]["com.agp/governance"]
    assert (gov["project_id"], gov["repo_id"]) == ("proj-1", "repo-1")
    assert mcp.is_repo_owned
    assert not mcp_service.create(
        McpServerCreate(name="byo-gw", kind=Kind.GATEWAY, tenant_id="default")
    ).is_repo_owned


def test_adopt_built_gateway_reordered_discovery_is_no_change():
    a = McpTool(name="api___a", description="", input_schema={})
    b = McpTool(name="api___b", description="", input_schema={})
    mcp = _minted_mcp()
    mcp.gateway_arn, mcp.gateway_id, mcp.gateway_url = GATEWAY_ARN, GATEWAY_ID, GATEWAY_URL
    mcp.identity_status = IdentityStatus.PROVISIONED
    mcp.available_tools = [b, a]
    registry = _registry_double()
    control = _adopt_control()
    control.get_gateway_target.return_value = {
        "name": "api",
        **_lambda_target_config(tools=[{"name": "a"}, {"name": "b"}]),
    }
    svc = _make_service(graph=_graph_double(mcp.id), registry=registry, control=control)

    svc.adopt_built_gateway(mcp, gateway_arn=GATEWAY_ARN, gateway_id=GATEWAY_ID, gateway_url=GATEWAY_URL)

    registry.persist_identity.assert_not_called()


def _alias_bomb(levels: int) -> str:
    s = "openapi: '3'\npaths: {}\na0: &a0 [lol,lol,lol,lol,lol,lol,lol,lol,lol,lol]\n"
    for i in range(1, levels):
        s += f"a{i}: &a{i} [" + ",".join([f"*a{i - 1}"] * 10) + "]\n"
    return s


def test_parse_openapi_refuses_a_yaml_alias_bomb():
    import time

    from services.mcp_identity_service import _parse_openapi

    started = time.monotonic()
    assert _parse_openapi(_alias_bomb(9)) is None
    assert time.monotonic() - started < 1


def test_parse_openapi_refuses_any_alias_but_keeps_plain_yaml():
    from services.mcp_identity_service import _parse_openapi

    assert _parse_openapi("openapi: '3'\nx: &a 1\ny: *a\n") is None
    assert _parse_openapi("openapi: '3'\npaths: {}\ninfo: {version: 2026-10-05}\n") == {
        "openapi": "3",
        "paths": {},
        "info": {"version": "2026-10-05"},
    }


def test_parse_openapi_keeps_base60_scalars_as_strings_and_is_fast():
    import time

    from services.mcp_identity_service import _parse_openapi

    value = "1" + ":5" * 99_000
    quoted = f"openapi: '3'\npaths: {{}}\nx: '{value}'\ny: '{value}.5'\n"
    unquoted = f"openapi: '3'\npaths: {{}}\nx: {value}\ny: {value}.5\n"
    started = time.monotonic()
    baseline = _parse_openapi(quoted)
    quoted_seconds = time.monotonic() - started
    started = time.monotonic()
    spec = _parse_openapi(unquoted)
    unquoted_seconds = time.monotonic() - started
    assert unquoted_seconds < 5 * quoted_seconds, (quoted_seconds, unquoted_seconds)
    assert spec == baseline
    assert spec is not None
    assert spec["x"] == value and spec["y"] == value + ".5"
    assert _parse_openapi("openapi: '3'\na: 1:30\nb: 12\nc: 1.5\n") == {
        "openapi": "3",
        "a": "1:30",
        "b": 12,
        "c": 1.5,
    }


@pytest.mark.asyncio
async def test_native_tool_read_isolates_a_spec_that_crashes_the_parser(patch_scan):
    mcp = _make_mcp()
    control = _gateway_control_client(authorizer_type="CUSTOM_JWT")
    control.list_gateway_targets.return_value = {
        "items": [{"targetId": "tgt-bad", "name": "bad"}, {"targetId": "tgt-good", "name": "good"}]
    }
    deep = '{"openapi":"3","paths":' + "[" * 200_000 + "]" * 200_000 + "}"
    control.get_gateway_target.side_effect = [
        {"name": "bad", "targetConfiguration": {"mcp": {"openApiSchema": {"inlinePayload": deep}}}},
        {"name": "good", **_lambda_target_config(tools=[{"name": "ok_tool", "inputSchema": {}}])},
    ]
    svc = _make_service(graph=_graph_double(mcp.id), registry=_registry_double(), control=control)
    result = await svc.provision(mcp)
    assert [t.name for t in result.available_tools] == ["good___ok_tool"]
