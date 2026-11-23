"""Tests for ``services.agent_identity_service``, the provisioning orchestrator.

ALL external collaborators are mocked — there are NO live AWS / Graph calls:
  - ``GraphService`` → an ``AsyncMock``-based double (``create_agent_app`` returns the
    6-key dict; ``set_assignment_required`` / ``grant_backend_obo_consent`` are async
    no-ops).
  - the boto3 ``bedrock-agentcore-control`` client → a ``MagicMock`` whose
    ``get_agent_runtime`` returns a dict carrying ``agentRuntimeArtifact`` / ``roleArn`` /
    ``networkConfiguration`` + a sequenceable ``status``; ``update_agent_runtime`` records
    the call.
  - ``AgentRegistryService`` → a ``MagicMock`` whose ``persist_identity`` records each
    call (and is wired to mutate-and-return the in-hand agent, mirroring the real method).

The repo is NOT in pytest-asyncio ``auto`` mode (no pytest.ini / pyproject config), so
every async test is decorated with ``@pytest.mark.asyncio`` explicitly.

The poll-to-READY loop's sleep is patched to be instant (``time.sleep``) so no test
blocks on a real sleep.

Pinned here: the gate, ``provision``'s step order and its immediate persist of the minted ids,
``_configure_runtime_authorizer`` (GET, replay the required fields, UpdateAgentRuntime, poll to
READY; audience only, in both audience forms) and the ``agentRuntimeId``-not-ARN rule.
"""

from __future__ import annotations

import warnings
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from botocore.exceptions import ClientError
from fastapi.encoders import jsonable_encoder

from models.agent import Agent, AgentAuthType, IdentityStatus, LifecycleState, Platform
from services.agent_identity_service import (
    AgentIdentityService,
    ProvisioningError,
    is_agentcore_agent,
)

TENANT_ID = "00000000-0000-0000-0000-000000000001"
LOGIN_BASE = "https://login.microsoftonline.com"
REGION = "us-east-1"

RUNTIME_RID = "agent_governance_test_agent-wdOmsREOEj"
RUNTIME_ARN = f"arn:aws:bedrock-agentcore:us-east-1:123456789012:runtime/{RUNTIME_RID}"


def _make_agent(
    *,
    agent_id: str = "rec-abc123",
    name: str = "Claims Triage DE",
    agent_arn: str | None = RUNTIME_ARN,
    agent_arns: dict[str, str] | None = None,
    auth_type: AgentAuthType = AgentAuthType.ENTRA,
    platform: Platform | None = Platform.AWS_BEDROCK,
    entra_sp_id: str | None = None,
    entra_app_audience: str | None = None,
    identity_status: IdentityStatus = IdentityStatus.NONE,
) -> Agent:
    now = datetime.now(timezone.utc)
    return Agent(
        id=agent_id,
        name=name,
        purpose="Triage inbound motor claims",
        lifecycle_state=LifecycleState.APPROVED,
        platform=platform,
        auth_type=auth_type,
        agent_arn=agent_arn,
        agent_arns=agent_arns or {},
        entra_sp_id=entra_sp_id,
        entra_app_audience=entra_app_audience,
        identity_status=identity_status,
        created_at=now,
        updated_at=now,
    )


def _graph_double(agent_id: str = "rec-abc123") -> AsyncMock:
    """An AsyncMock GraphService double.

    ``create_agent_app`` returns the 6-key dict (app_id / sp_id / app_uri /
    invoke_scope_id / invoker_role_id / admin_role_id), with the per-agent ``app_uri``
    derived from the agent id so two agents get distinct audiences. The other methods
    are async no-ops.
    """
    graph = AsyncMock(name="GraphService")
    graph.create_agent_app.return_value = {
        "app_id": f"app-client-guid-{agent_id}",
        "sp_id": f"sp-obj-id-{agent_id}",
        "app_uri": f"api://agp-agent-{agent_id}",
        "invoke_scope_id": f"scope-{agent_id}",
        "invoker_role_id": f"invoker-{agent_id}",
        "admin_role_id": f"admin-{agent_id}",
    }
    graph.set_assignment_required.return_value = None
    graph.grant_backend_obo_consent.return_value = None
    return graph


def _control_client(statuses: list[str] | None = None) -> MagicMock:
    """A MagicMock boto3 control client.

    ``get_agent_runtime`` returns a dict carrying the three replay fields + a status.
    When ``statuses`` is given, the status is sequenced via ``side_effect`` (each
    ``get_agent_runtime`` call pops the next status); otherwise every read is READY.
    ``update_agent_runtime`` records the call.
    """
    control = MagicMock(name="bedrock-agentcore-control")
    artifact = {"containerConfiguration": {"containerUri": "123.dkr.ecr/agent:latest"}}
    role_arn = "arn:aws:iam::123456789012:role/agent-runtime-role"
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
    """A MagicMock AgentRegistryService whose ``persist_identity`` returns its arg.

    Mirrors the real method's return contract (it returns the in-hand agent) so the
    service code can chain on it if needed; records every call for order assertions.
    """
    registry = MagicMock(name="AgentRegistryService")
    registry.persist_identity.side_effect = lambda agent: agent
    return registry


def _make_service(
    *,
    graph: AsyncMock,
    registry: MagicMock,
    control: MagicMock,
) -> AgentIdentityService:
    return AgentIdentityService(
        graph=graph,
        registry=registry,
        control_client=control,
        region=REGION,
        tenant_id=TENANT_ID,
        login_base=LOGIN_BASE,
    )


@pytest.fixture(autouse=True)
def _instant_sleep(monkeypatch):
    """Patch the poll loop's sleep so no test blocks on a real sleep.

    The service polls via ``time.sleep`` (called inside the sync, off-loop
    ``_configure_runtime_authorizer``). Patch it module-wide to a no-op.
    """
    import services.agent_identity_service as mod

    monkeypatch.setattr(mod.time, "sleep", lambda *_a, **_k: None)


def test_is_agentcore_agent_gate():
    assert is_agentcore_agent(_make_agent()) is True

    assert is_agentcore_agent(_make_agent(agent_arn=None)) is False
    assert is_agentcore_agent(_make_agent(agent_arn="")) is False

    assert is_agentcore_agent(_make_agent(auth_type=AgentAuthType.API_KEY)) is False
    assert is_agentcore_agent(_make_agent(auth_type=AgentAuthType.NONE)) is False

    assert is_agentcore_agent(_make_agent(platform=Platform.AZURE)) is False
    assert is_agentcore_agent(_make_agent(platform=Platform.OTHER)) is False
    assert is_agentcore_agent(_make_agent(platform=None)) is False


@pytest.mark.asyncio
async def test_provision_happy_path_calls_graph_then_authorizer_then_persists():
    agent = _make_agent()
    graph = _graph_double(agent.id)
    registry = _registry_double()
    control = _control_client()

    manager = MagicMock()
    manager.attach_mock(graph.create_agent_app, "create_agent_app")
    manager.attach_mock(graph.set_assignment_required, "set_assignment_required")
    manager.attach_mock(graph.grant_backend_obo_consent, "grant_backend_obo_consent")
    manager.attach_mock(registry.persist_identity, "persist_identity")
    manager.attach_mock(control.update_agent_runtime, "update_agent_runtime")

    svc = _make_service(graph=graph, registry=registry, control=control)
    result = await svc.provision(agent)

    order = [c[0] for c in manager.mock_calls]
    assert order == [
        "create_agent_app",
        "persist_identity",
        "set_assignment_required",
        "grant_backend_obo_consent",
        "update_agent_runtime",
        "persist_identity",
    ]

    assert result.identity_status == "provisioned"
    assert result.entra_app_id == f"app-client-guid-{agent.id}"
    assert result.entra_sp_id == f"sp-obj-id-{agent.id}"
    assert result.invoker_role_id == f"invoker-{agent.id}"
    assert result.admin_role_id == f"admin-{agent.id}"
    assert result.entra_app_audience == f"api://agp-agent-{agent.id}"

    graph.create_agent_app.assert_awaited_once_with(agent.id, agent.name)


@pytest.mark.asyncio
async def test_provisioned_agent_serializes_without_pydantic_serializer_warnings():
    """PINNED: the provisioning writers assign MEMBERS, never bare strings.

    `identity_status` is enum-ANNOTATED on the model and pydantic does NOT validate
    assignment, so a bare-string writer leaves a plain `str` in an enum-typed field.
    The output stays correct, but pydantic's own serializer then warns
    ``Expected `enum` but got `str``` on EVERY serialization of the touched model —
    once per `model_dump()`, once per FastAPI `jsonable_encoder()` — i.e. on the very
    provisioning response paths. This drives the REAL `provision()` (which writes
    `pending` at `agent_identity_service.py:343` then `provisioned` at `:402`) and
    pins ZERO such warnings.
    """
    agent = _make_agent()
    svc = _make_service(
        graph=_graph_double(agent.id),
        registry=_registry_double(),
        control=_control_client(),
    )
    result = await svc.provision(agent)

    assert result.identity_status is IdentityStatus.PROVISIONED

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        result.model_dump()
        jsonable_encoder(result)
    assert [str(w.message) for w in caught if "Pydantic serializer warnings" in str(w.message)] == []


@pytest.mark.asyncio
async def test_provision_configures_authorizer_audience_only():
    agent = _make_agent()
    graph = _graph_double(agent.id)
    registry = _registry_double()
    control = _control_client()

    svc = _make_service(graph=graph, registry=registry, control=control)
    await svc.provision(agent)

    _, kwargs = control.update_agent_runtime.call_args
    authorizer = kwargs["authorizerConfiguration"]
    jwt = authorizer["customJWTAuthorizer"]

    assert jwt["allowedAudience"] == [agent.entra_app_audience, agent.entra_app_id]
    assert "allowedClients" not in jwt
    assert "customClaims" not in jwt
    assert "allowedScopes" not in jwt
    assert jwt["discoveryUrl"] == (f"{LOGIN_BASE}/{TENANT_ID}/v2.0/.well-known/openid-configuration")


@pytest.mark.asyncio
async def test_provision_replays_artifact_role_network():
    agent = _make_agent()
    graph = _graph_double(agent.id)
    registry = _registry_double()
    control = _control_client()

    read = control.get_agent_runtime.return_value

    svc = _make_service(graph=graph, registry=registry, control=control)
    await svc.provision(agent)

    _, kwargs = control.update_agent_runtime.call_args
    assert kwargs["agentRuntimeArtifact"] == read["agentRuntimeArtifact"]
    assert kwargs["roleArn"] == read["roleArn"]
    assert kwargs["networkConfiguration"] == read["networkConfiguration"]


@pytest.mark.asyncio
async def test_provision_polls_runtime_to_ready():
    agent = _make_agent()
    graph = _graph_double(agent.id)
    registry = _registry_double()
    control = _control_client(statuses=["UPDATING", "UPDATING", "UPDATING", "READY"])

    svc = _make_service(graph=graph, registry=registry, control=control)
    result = await svc.provision(agent)

    assert result.identity_status == "provisioned"
    assert control.get_agent_runtime.call_count >= 2


@pytest.mark.asyncio
async def test_provision_idempotent_skips_app_create_when_sp_exists():
    agent = _make_agent(
        entra_sp_id="sp-existing",
        entra_app_audience="api://agp-agent-rec-abc123",
        identity_status=IdentityStatus.FAILED,
    )
    agent.invoker_role_id = "invoker-existing"
    agent.admin_role_id = "admin-existing"
    graph = _graph_double(agent.id)
    registry = _registry_double()
    control = _control_client()

    svc = _make_service(graph=graph, registry=registry, control=control)
    result = await svc.provision(agent)

    graph.create_agent_app.assert_not_awaited()
    control.update_agent_runtime.assert_called_once()
    graph.set_assignment_required.assert_awaited_once_with("sp-existing")
    graph.grant_backend_obo_consent.assert_awaited_once_with("sp-existing")
    assert result.identity_status == "provisioned"


@pytest.mark.asyncio
async def test_provision_persists_ids_immediately_after_app_create():
    agent = _make_agent()
    graph = _graph_double(agent.id)
    registry = _registry_double()
    control = _control_client()

    snapshot: dict = {}

    async def _capture_on_step2(sp_id):
        snapshot["identity_status"] = agent.identity_status
        snapshot["entra_sp_id"] = agent.entra_sp_id
        snapshot["entra_app_id"] = agent.entra_app_id
        snapshot["invoker_role_id"] = agent.invoker_role_id
        snapshot["admin_role_id"] = agent.admin_role_id
        snapshot["entra_app_audience"] = agent.entra_app_audience
        snapshot["persist_count_at_step2"] = registry.persist_identity.call_count
        return None

    graph.set_assignment_required.side_effect = _capture_on_step2

    svc = _make_service(graph=graph, registry=registry, control=control)
    await svc.provision(agent)

    assert snapshot["identity_status"] == "pending"
    assert snapshot["entra_sp_id"] == f"sp-obj-id-{agent.id}"
    assert snapshot["entra_app_id"] == f"app-client-guid-{agent.id}"
    assert snapshot["invoker_role_id"] == f"invoker-{agent.id}"
    assert snapshot["admin_role_id"] == f"admin-{agent.id}"
    assert snapshot["entra_app_audience"] == f"api://agp-agent-{agent.id}"
    assert snapshot["persist_count_at_step2"] == 1


@pytest.mark.asyncio
async def test_provision_midsequence_failure_persists_ids_then_reprovision_skips_create():
    agent = _make_agent()
    graph = _graph_double(agent.id)
    registry = _registry_double()
    control = _control_client()

    graph.set_assignment_required.side_effect = RuntimeError("Graph 500 on PATCH")

    svc = _make_service(graph=graph, registry=registry, control=control)
    with pytest.raises(ProvisioningError):
        await svc.provision(agent)

    assert agent.entra_sp_id == f"sp-obj-id-{agent.id}"
    assert agent.entra_app_id == f"app-client-guid-{agent.id}"
    assert agent.entra_app_audience == f"api://agp-agent-{agent.id}"
    assert agent.identity_status == "failed"
    assert graph.create_agent_app.await_count == 1

    graph.set_assignment_required.side_effect = None
    graph.set_assignment_required.return_value = None

    result = await svc.provision(agent)

    assert graph.create_agent_app.await_count == 1
    graph.set_assignment_required.assert_awaited_with(f"sp-obj-id-{agent.id}")
    graph.grant_backend_obo_consent.assert_awaited_with(f"sp-obj-id-{agent.id}")
    control.update_agent_runtime.assert_called_once()
    assert result.identity_status == "provisioned"


@pytest.mark.asyncio
async def test_provision_dispatches_authorizer_off_loop(monkeypatch):
    agent = _make_agent()
    graph = _graph_double(agent.id)
    registry = _registry_double()
    control = _control_client()

    import services.agent_identity_service as mod

    real_run_sync = mod.anyio.to_thread.run_sync
    recorder = AsyncMock(side_effect=real_run_sync)
    monkeypatch.setattr(mod.anyio.to_thread, "run_sync", recorder)

    svc = _make_service(graph=graph, registry=registry, control=control)
    await svc.provision(agent)

    fanouts = [c.args for c in recorder.await_args_list if c.args[0] == svc._configure_runtime_authorizers]
    assert len(fanouts) == 1
    assert fanouts[0][1] is agent


@pytest.mark.asyncio
async def test_two_agents_get_distinct_audiences():
    agent_a = _make_agent(agent_id="rec-aaa", name="Agent A")
    agent_b = _make_agent(agent_id="rec-bbb", name="Agent B")

    graph_a = _graph_double(agent_a.id)
    graph_b = _graph_double(agent_b.id)
    registry = _registry_double()
    control_a = _control_client()
    control_b = _control_client()

    svc_a = _make_service(graph=graph_a, registry=registry, control=control_a)
    svc_b = _make_service(graph=graph_b, registry=registry, control=control_b)

    await svc_a.provision(agent_a)
    await svc_b.provision(agent_b)

    _, kwargs_a = control_a.update_agent_runtime.call_args
    _, kwargs_b = control_b.update_agent_runtime.call_args
    aud_a = kwargs_a["authorizerConfiguration"]["customJWTAuthorizer"]["allowedAudience"]
    aud_b = kwargs_b["authorizerConfiguration"]["customJWTAuthorizer"]["allowedAudience"]

    assert aud_a == [agent_a.entra_app_audience, agent_a.entra_app_id]
    assert aud_b == [agent_b.entra_app_audience, agent_b.entra_app_id]
    assert aud_a != aud_b


@pytest.mark.asyncio
async def test_provision_failure_sets_identity_status_failed_and_raises():
    agent = _make_agent()
    graph = _graph_double(agent.id)
    registry = _registry_double()
    control = _control_client()

    control.get_agent_runtime.side_effect = RuntimeError("AccessDeniedException")

    svc = _make_service(graph=graph, registry=registry, control=control)
    with pytest.raises(ProvisioningError):
        await svc.provision(agent)

    assert agent.identity_status == "failed"
    assert agent.identity_status is IdentityStatus.FAILED
    persisted_statuses = [c.args[0].identity_status for c in registry.persist_identity.call_args_list]
    assert "failed" in persisted_statuses


@pytest.mark.asyncio
async def test_provision_raises_when_create_returns_no_sp_id():
    agent = _make_agent()
    graph = _graph_double(agent.id)
    graph.create_agent_app.return_value = {
        "app_id": f"app-client-guid-{agent.id}",
        "sp_id": None,
        "app_uri": f"api://agp-agent-{agent.id}",
        "invoke_scope_id": f"scope-{agent.id}",
        "invoker_role_id": f"invoker-{agent.id}",
        "admin_role_id": f"admin-{agent.id}",
    }
    registry = _registry_double()
    control = _control_client()

    svc = _make_service(graph=graph, registry=registry, control=control)
    with pytest.raises(ProvisioningError):
        await svc.provision(agent)

    assert agent.identity_status == "failed"
    assert agent.entra_app_id == f"app-client-guid-{agent.id}"
    assert agent.entra_app_audience == f"api://agp-agent-{agent.id}"
    graph.set_assignment_required.assert_not_awaited()
    graph.grant_backend_obo_consent.assert_not_awaited()
    control.update_agent_runtime.assert_not_called()


@pytest.mark.asyncio
async def test_provision_graph_failure_sets_identity_status_failed_and_raises():
    agent = _make_agent()
    graph = _graph_double(agent.id)
    graph.create_agent_app.side_effect = RuntimeError("Graph 403")
    registry = _registry_double()
    control = _control_client()

    svc = _make_service(graph=graph, registry=registry, control=control)
    with pytest.raises(ProvisioningError):
        await svc.provision(agent)

    assert agent.identity_status == "failed"


@pytest.mark.asyncio
async def test_configure_runtime_authorizer_parses_rid_from_arn():
    agent = _make_agent()
    graph = _graph_double(agent.id)
    registry = _registry_double()
    control = _control_client()

    svc = _make_service(graph=graph, registry=registry, control=control)
    await svc.provision(agent)

    get_kwargs = control.get_agent_runtime.call_args.kwargs
    update_kwargs = control.update_agent_runtime.call_args.kwargs
    assert get_kwargs["agentRuntimeId"] == RUNTIME_RID
    assert update_kwargs["agentRuntimeId"] == RUNTIME_RID
    assert get_kwargs["agentRuntimeId"] != RUNTIME_ARN
    assert update_kwargs["agentRuntimeId"] != RUNTIME_ARN


@pytest.mark.asyncio
async def test_provision_runtime_update_failed_status_raises():
    agent = _make_agent()
    graph = _graph_double(agent.id)
    registry = _registry_double()
    control = _control_client(statuses=["READY", "UPDATE_FAILED"])

    svc = _make_service(graph=graph, registry=registry, control=control)
    with pytest.raises(ProvisioningError):
        await svc.provision(agent)

    assert agent.identity_status == "failed"


@pytest.mark.asyncio
async def test_provision_identity_skips_runtime_calls_when_no_arn():
    agent = _make_agent(agent_arn=None)
    graph = _graph_double(agent.id)
    registry = _registry_double()
    control = _control_client()

    svc = _make_service(graph=graph, registry=registry, control=control)
    out = await svc.provision_identity(agent)

    assert out.identity_status in ("provisioned", "pending")
    control.update_agent_runtime.assert_not_called()
    control.get_agent_runtime.assert_not_called()


@pytest.mark.asyncio
async def test_provision_runtime_requires_arn():
    graph = _graph_double()
    registry = _registry_double()
    control = _control_client()
    svc = _make_service(graph=graph, registry=registry, control=control)

    with pytest.raises(ValueError, match="agent_arn"):
        await svc.provision_runtime(_make_agent(agent_arn=None))


@pytest.mark.asyncio
async def test_provision_still_does_both_when_arn_present():
    agent = _make_agent(agent_arn=RUNTIME_ARN)
    graph = _graph_double(agent.id)
    registry = _registry_double()
    control = _control_client()

    svc = _make_service(graph=graph, registry=registry, control=control)
    await svc.provision(agent)

    control.update_agent_runtime.assert_called()


def _control_client_with_authorizer_and_env(
    *,
    existing_env: dict | None = None,
    existing_authorizer: dict | None = None,
    statuses: list[str] | None = None,
) -> MagicMock:
    """A control client whose GetAgentRuntime ALSO returns an inbound authorizer +
    a pre-existing environmentVariables map (the live shape set_runtime_environment
    must preserve/merge). Defaults to a realistic customJWTAuthorizer + one
    pre-existing env key. ``statuses`` sequences the poll reads (else every read READY).
    """
    control = _control_client(statuses=statuses)
    artifact = {"containerConfiguration": {"containerUri": "123.dkr.ecr/agent:latest"}}
    role_arn = "arn:aws:iam::123456789012:role/agent-runtime-role"
    network = {"networkMode": "PUBLIC"}
    authorizer = existing_authorizer or {
        "customJWTAuthorizer": {
            "discoveryUrl": (f"{LOGIN_BASE}/{TENANT_ID}/v2.0/.well-known/openid-configuration"),
            "allowedAudience": ["api://agp-agent-rec-abc123", "app-client-guid"],
        }
    }
    base_read = {
        "agentRuntimeArtifact": artifact,
        "roleArn": role_arn,
        "networkConfiguration": network,
        "authorizerConfiguration": authorizer,
        "authorizerType": "CUSTOM_JWT",
        "agentRuntimeVersion": "1",
    }
    if existing_env is not None:
        base_read["environmentVariables"] = existing_env

    if statuses is None:
        control.get_agent_runtime.return_value = {**base_read, "status": "READY"}
    else:
        control.get_agent_runtime.side_effect = [{**base_read, "status": s} for s in statuses]
    return control


def test_set_runtime_environment_merges_env_and_preserves_authorizer():
    existing_authorizer = {
        "customJWTAuthorizer": {
            "discoveryUrl": (f"{LOGIN_BASE}/{TENANT_ID}/v2.0/.well-known/openid-configuration"),
            "allowedAudience": ["api://agp-agent-rec-abc123", "app-client-guid"],
        }
    }
    existing_env = {"LOG_LEVEL": "INFO", "MODEL_ID": "some-existing-model"}
    control = _control_client_with_authorizer_and_env(
        existing_env=existing_env, existing_authorizer=existing_authorizer
    )
    read = control.get_agent_runtime.return_value

    svc = AgentIdentityService(
        graph=AsyncMock(),
        registry=_registry_double(),
        control_client=control,
        region=REGION,
        tenant_id=TENANT_ID,
        login_base=LOGIN_BASE,
    )

    injected = {
        "CREDENTIAL_PROVIDER_NAME": "agp-agent-obo-rec-abc123",
        "MCP_GATEWAY_URL": "https://gw-abc.gateway.bedrock-agentcore.us-east-1.amazonaws.com/mcp",
        "MCP_AUDIENCE": "api://agp-mcp-mcp-123",
        "AWS_REGION": REGION,
    }
    svc.set_runtime_environment(RUNTIME_ARN, injected)

    _, kwargs = control.update_agent_runtime.call_args
    sent_env = kwargs["environmentVariables"]
    for k, v in injected.items():
        assert sent_env[k] == v
    assert sent_env["LOG_LEVEL"] == "INFO"
    assert sent_env["MODEL_ID"] == "some-existing-model"
    assert kwargs["authorizerConfiguration"] == existing_authorizer
    assert kwargs["agentRuntimeArtifact"] == read["agentRuntimeArtifact"]
    assert kwargs["roleArn"] == read["roleArn"]
    assert kwargs["networkConfiguration"] == read["networkConfiguration"]
    assert kwargs["agentRuntimeId"] == RUNTIME_RID
    assert control.get_agent_runtime.call_args.kwargs["agentRuntimeId"] == RUNTIME_RID


def test_set_runtime_environment_merges_when_runtime_has_no_existing_env():
    control = _control_client_with_authorizer_and_env(existing_env=None)

    svc = AgentIdentityService(
        graph=AsyncMock(),
        registry=_registry_double(),
        control_client=control,
        region=REGION,
        tenant_id=TENANT_ID,
        login_base=LOGIN_BASE,
    )

    injected = {"CREDENTIAL_PROVIDER_NAME": "agp-agent-obo-rec-abc123", "AWS_REGION": REGION}
    svc.set_runtime_environment(RUNTIME_ARN, injected)

    _, kwargs = control.update_agent_runtime.call_args
    assert kwargs["environmentVariables"] == injected
    assert "authorizerConfiguration" in kwargs


def test_set_runtime_environment_polls_to_ready():
    control = _control_client_with_authorizer_and_env(
        existing_env={"LOG_LEVEL": "INFO"},
        statuses=["UPDATING", "UPDATING", "READY"],
    )

    svc = AgentIdentityService(
        graph=AsyncMock(),
        registry=_registry_double(),
        control_client=control,
        region=REGION,
        tenant_id=TENANT_ID,
        login_base=LOGIN_BASE,
    )

    svc.set_runtime_environment(RUNTIME_ARN, {"AWS_REGION": REGION})

    assert control.get_agent_runtime.call_count >= 2
    control.update_agent_runtime.assert_called_once()


def test_set_runtime_environment_returns_the_execution_role_arn():
    """the method RETURNS the runtime's exec ``roleArn``, read from the GET it already
    performs. The Langfuse wiring has to grant that exact principal ``GetSecretValue`` on the
    per-agent secret, and returning the value already in hand is what keeps leg 4 from costing a
    second ``get_agent_runtime``. Additive: every earlier caller ignores it."""
    control = _control_client_with_authorizer_and_env(existing_env={"LOG_LEVEL": "INFO"})

    svc = AgentIdentityService(
        graph=AsyncMock(),
        registry=_registry_double(),
        control_client=control,
        region=REGION,
        tenant_id=TENANT_ID,
        login_base=LOGIN_BASE,
    )

    role_arn = svc.set_runtime_environment(RUNTIME_ARN, {"AWS_REGION": REGION})

    assert role_arn == "arn:aws:iam::123456789012:role/agent-runtime-role"
    assert control.update_agent_runtime.call_args.kwargs["roleArn"] == role_arn


def test_set_runtime_environment_honours_an_injected_control_client():
    """``control_client`` routes the GET, the UPDATE **and** the poll into the account
    that owns the runtime. All three, not just the write: a GET in one account and an UPDATE in
    another cannot be made to mean anything, and a poll under the ambient client answers
    ``ResourceNotFoundException`` forever — a successful write reported as a timeout."""
    ambient = _control_client_with_authorizer_and_env(existing_env={"LOG_LEVEL": "INFO"})
    stage = _control_client_with_authorizer_and_env(
        existing_env={"LOG_LEVEL": "INFO"}, statuses=["READY", "READY"]
    )

    svc = AgentIdentityService(
        graph=AsyncMock(),
        registry=_registry_double(),
        control_client=ambient,
        region=REGION,
        tenant_id=TENANT_ID,
        login_base=LOGIN_BASE,
    )

    svc.set_runtime_environment(RUNTIME_ARN, {"AWS_REGION": REGION}, control_client=stage)

    assert stage.get_agent_runtime.call_count == 2
    stage.update_agent_runtime.assert_called_once()
    ambient.get_agent_runtime.assert_not_called()
    ambient.update_agent_runtime.assert_not_called()


def test_set_runtime_environment_can_skip_the_poll_to_ready():
    """`wait_ready=False`: the update is sent and the method RETURNS, with
    no poll. The poll `time.sleep`s up to 300 s per runtime, which the reconcile-on-read caller
    would spend holding one of the ~40 shared `anyio` worker threads inside a GET handler. Only the
    OBSERVATION is skipped: the UpdateAgentRuntime is identical, and a heal that never converges
    leaves the env absent, so the next read re-triggers it."""
    control = _control_client_with_authorizer_and_env(
        existing_env={"LOG_LEVEL": "INFO"},
        statuses=["UPDATING", "UPDATING", "READY"],
    )

    svc = AgentIdentityService(
        graph=AsyncMock(),
        registry=_registry_double(),
        control_client=control,
        region=REGION,
        tenant_id=TENANT_ID,
        login_base=LOGIN_BASE,
    )

    svc.set_runtime_environment(RUNTIME_ARN, {"AWS_REGION": REGION}, wait_ready=False)

    assert control.get_agent_runtime.call_count == 1
    control.update_agent_runtime.assert_called_once()


def test_set_runtime_environment_does_not_raise_on_failed_when_not_waiting():
    """The corollary: not waiting means not observing, so a runtime that goes UPDATE_FAILED is not
    reported here. That is why `wait_ready` DEFAULTS to True — the grant path must fail loud (the
    test below); only the best-effort read-path heal opts out, and it re-triggers on the next read."""
    control = _control_client_with_authorizer_and_env(
        existing_env={"LOG_LEVEL": "INFO"},
        statuses=["READY", "UPDATE_FAILED"],
    )

    svc = AgentIdentityService(
        graph=AsyncMock(),
        registry=_registry_double(),
        control_client=control,
        region=REGION,
        tenant_id=TENANT_ID,
        login_base=LOGIN_BASE,
    )

    svc.set_runtime_environment(RUNTIME_ARN, {"AWS_REGION": REGION}, wait_ready=False)

    control.update_agent_runtime.assert_called_once()


def test_set_runtime_environment_raises_on_failed():
    control = _control_client_with_authorizer_and_env(
        existing_env={"LOG_LEVEL": "INFO"},
        statuses=["READY", "UPDATE_FAILED"],
    )

    svc = AgentIdentityService(
        graph=AsyncMock(),
        registry=_registry_double(),
        control_client=control,
        region=REGION,
        tenant_id=TENANT_ID,
        login_base=LOGIN_BASE,
    )

    with pytest.raises(RuntimeError):
        svc.set_runtime_environment(RUNTIME_ARN, {"AWS_REGION": REGION})


def _client_error(code: str, op: str = "DeleteAgentRuntime") -> ClientError:
    """Build a botocore ClientError carrying ``code`` as its Error/Code."""
    return ClientError({"Error": {"Code": code, "Message": "x"}}, op)


def test_delete_runtime_calls_control_with_rid_from_arn():
    graph = _graph_double()
    registry = _registry_double()
    control = _control_client()
    svc = _make_service(graph=graph, registry=registry, control=control)

    svc.delete_runtime(RUNTIME_ARN)

    svc._control.delete_agent_runtime.assert_called_once_with(agentRuntimeId=RUNTIME_RID)


def test_delete_runtime_noop_on_blank_arn():
    graph = _graph_double()
    registry = _registry_double()
    control = _control_client()
    svc = _make_service(graph=graph, registry=registry, control=control)

    svc.delete_runtime("")

    svc._control.delete_agent_runtime.assert_not_called()


def test_delete_runtime_idempotent_on_not_found():
    graph = _graph_double()
    registry = _registry_double()
    control = _control_client()
    control.delete_agent_runtime.side_effect = _client_error("ResourceNotFoundException")
    svc = _make_service(graph=graph, registry=registry, control=control)

    svc.delete_runtime(RUNTIME_ARN)

    svc._control.delete_agent_runtime.assert_called_once_with(agentRuntimeId=RUNTIME_RID)


def test_delete_runtime_reraises_other_client_error():
    graph = _graph_double()
    registry = _registry_double()
    control = _control_client()
    control.delete_agent_runtime.side_effect = _client_error("AccessDeniedException")
    svc = _make_service(graph=graph, registry=registry, control=control)

    with pytest.raises(ClientError):
        svc.delete_runtime(RUNTIME_ARN)


@pytest.mark.asyncio
async def test_delete_identity_delegates_to_graph():
    graph = _graph_double()
    graph.delete_agent_app.return_value = None
    registry = _registry_double()
    control = _control_client()
    svc = _make_service(graph=graph, registry=registry, control=control)

    agent = _make_agent()
    agent.entra_app_id = "app-guid-x"
    agent.entra_sp_id = "sp-obj-x"

    await svc.delete_identity(agent)

    graph.delete_agent_app.assert_awaited_once_with(entra_app_id="app-guid-x", entra_sp_id="sp-obj-x")


def test_delete_runtime_probe_access_denied_still_attempts_delete_and_propagates():
    graph = _graph_double()
    registry = _registry_double()
    control = _control_client()
    control.get_agent_runtime.side_effect = _client_error("AccessDeniedException", op="GetAgentRuntime")
    control.delete_agent_runtime.side_effect = _client_error("AccessDeniedException")
    svc = _make_service(graph=graph, registry=registry, control=control)

    with pytest.raises(ClientError):
        svc.delete_runtime(RUNTIME_ARN)

    svc._control.delete_agent_runtime.assert_called_once_with(agentRuntimeId=RUNTIME_RID)


def test_delete_runtime_skips_delete_when_probe_says_gone_not_found():
    graph = _graph_double()
    registry = _registry_double()
    control = _control_client()
    control.get_agent_runtime.side_effect = _client_error("ResourceNotFoundException", op="GetAgentRuntime")
    svc = _make_service(graph=graph, registry=registry, control=control)

    svc.delete_runtime(RUNTIME_ARN)

    svc._control.delete_agent_runtime.assert_not_called()


def test_delete_runtime_deletes_when_probe_says_present():
    graph = _graph_double()
    registry = _registry_double()
    control = _control_client()
    svc = _make_service(graph=graph, registry=registry, control=control)

    svc.delete_runtime(RUNTIME_ARN)

    svc._control.delete_agent_runtime.assert_called_once_with(agentRuntimeId=RUNTIME_RID)


def test_runtime_exists_true_when_get_succeeds():
    graph = _graph_double()
    registry = _registry_double()
    control = _control_client()
    svc = _make_service(graph=graph, registry=registry, control=control)

    assert svc.runtime_exists(RUNTIME_ARN) is True


def test_runtime_exists_false_on_blank_arn():
    graph = _graph_double()
    registry = _registry_double()
    control = _control_client()
    svc = _make_service(graph=graph, registry=registry, control=control)

    assert svc.runtime_exists("") is False
    svc._control.get_agent_runtime.assert_not_called()


def test_runtime_exists_false_when_gone():
    graph = _graph_double()
    registry = _registry_double()
    control = _control_client()
    control.get_agent_runtime.side_effect = _client_error("ResourceNotFoundException", op="GetAgentRuntime")
    svc = _make_service(graph=graph, registry=registry, control=control)

    assert svc.runtime_exists(RUNTIME_ARN) is False


def test_runtime_exists_raises_on_access_denied():
    graph = _graph_double()
    registry = _registry_double()
    control = _control_client()
    control.get_agent_runtime.side_effect = _client_error("AccessDeniedException", op="GetAgentRuntime")
    svc = _make_service(graph=graph, registry=registry, control=control)

    with pytest.raises(ClientError):
        svc.runtime_exists(RUNTIME_ARN)


DEV_RID = "agent_governance_test_agent_dev-aaaaaaaaaa"
PROD_RID = "agent_governance_test_agent_prod-bbbbbbbbbb"
DEV_ARN = f"arn:aws:bedrock-agentcore:{REGION}:123456789012:runtime/{DEV_RID}"
PROD_ARN = f"arn:aws:bedrock-agentcore:{REGION}:123456789012:runtime/{PROD_RID}"


@pytest.mark.asyncio
async def test_provision_runtime_wires_the_authorizer_on_EVERY_stage_runtime():
    """N runtimes ⇒ N authorizer configs. A runtime without the inbound JWT authorizer accepts
    UNAUTHENTICATED invocations, so wiring only one leaves the other born ungoverned — a
    security hole, not a cosmetic gap."""
    agent = _make_agent(agent_arns={"dev": DEV_ARN, "prod": PROD_ARN}, agent_arn=PROD_ARN)
    graph = _graph_double(agent.id)
    registry = _registry_double()
    control = _control_client()

    svc = _make_service(graph=graph, registry=registry, control=control)
    await svc.provision_runtime(agent)

    updated = {c.kwargs["agentRuntimeId"] for c in control.update_agent_runtime.call_args_list}
    assert updated == {DEV_RID, PROD_RID}
    for c in control.update_agent_runtime.call_args_list:
        assert "customJWTAuthorizer" in c.kwargs["authorizerConfiguration"]
    assert agent.identity_status == "provisioned"


@pytest.mark.asyncio
async def test_provision_runtime_wires_EXACTLY_ONE_authorizer_for_a_legacy_record():
    """The other half: a legacy scalar-only record still configures exactly its one runtime —
    not zero, and not a fabricated per-stage set."""
    agent = _make_agent(agent_arn=RUNTIME_ARN)
    graph = _graph_double(agent.id)
    registry = _registry_double()
    control = _control_client()

    svc = _make_service(graph=graph, registry=registry, control=control)
    await svc.provision_runtime(agent)

    updated = [c.kwargs["agentRuntimeId"] for c in control.update_agent_runtime.call_args_list]
    assert updated == [RUNTIME_RID]


@pytest.mark.asyncio
async def test_provision_runtime_requires_at_least_one_runtime():
    """No map AND no scalar ⇒ nothing to configure. Still a ValueError (the scalar-only contract),
    because the caller has sequenced the post-deploy half before a runtime exists."""
    graph = _graph_double()
    registry = _registry_double()
    control = _control_client()
    svc = _make_service(graph=graph, registry=registry, control=control)

    with pytest.raises(ValueError, match="agent_arn"):
        await svc.provision_runtime(_make_agent(agent_arn=None))


@pytest.mark.asyncio
async def test_provision_reaches_provision_runtime_for_a_MAP_ONLY_agent():
    """The gate in ``provision()`` must fire when EITHER the scalar or the MAP names a runtime.

    Before this it read ``if agent.agent_arn:``, so an agent whose runtimes are known only from
    ``agent_arns`` — no scalar — got ZERO authorizer calls and was stranded ``pending`` with no
    error. That is the worst shape of D-A4 defect 3: a runtime with no ``authorizerConfiguration``
    accepts UNAUTHENTICATED invocations, so it would be an ungoverned copy of a governed agent,
    and the silence made it invisible. ``provision_runtime`` already advertised map support in its
    docstring and its ``ValueError``; the caller's gate was the thing that did not deliver it."""
    agent = _make_agent(agent_arns={"dev": DEV_ARN, "prod": PROD_ARN}, agent_arn=None)
    graph = _graph_double(agent.id)
    registry = _registry_double()
    control = _control_client()

    svc = _make_service(graph=graph, registry=registry, control=control)
    await svc.provision(agent)

    updated = {c.kwargs["agentRuntimeId"] for c in control.update_agent_runtime.call_args_list}
    assert updated == {DEV_RID, PROD_RID}
    assert agent.identity_status == "provisioned"


@pytest.mark.asyncio
async def test_provision_still_SKIPS_the_runtime_half_when_the_agent_names_no_runtime():
    """The other half of the widened gate: pre-registration mints the identity BEFORE any
    runtime exists (no scalar, empty map). That must still skip the runtime half and leave the
    agent ``pending`` — widening the gate must not turn a legitimate deferral into the
    ``ValueError`` ``provision_runtime`` raises when asked to wire nothing."""
    agent = _make_agent(agent_arn=None)
    graph = _graph_double(agent.id)
    registry = _registry_double()
    control = _control_client()

    svc = _make_service(graph=graph, registry=registry, control=control)
    await svc.provision(agent)

    assert control.update_agent_runtime.call_args_list == []
    assert agent.identity_status == "pending"


@pytest.mark.asyncio
async def test_a_failure_on_one_runtime_does_NOT_leave_the_others_unwired(monkeypatch):
    """Per-runtime tolerance THEN raise. A dev runtime that cannot be reached must not stop
    prod from getting its authorizer — but the agent must still land 'failed', because an agent
    reported 'provisioned' while one of its runtimes is ungoverned is the dangerous outcome."""
    agent = _make_agent(agent_arns={"dev": DEV_ARN, "prod": PROD_ARN})
    graph = _graph_double(agent.id)
    registry = _registry_double()
    control = _control_client()

    def flaky(agentRuntimeId, **kwargs):
        if agentRuntimeId == DEV_RID:
            raise RuntimeError("AccessDenied")
        return {}

    control.update_agent_runtime.side_effect = flaky

    svc = _make_service(graph=graph, registry=registry, control=control)
    with pytest.raises(ProvisioningError):
        await svc.provision(agent)

    attempted = {c.kwargs["agentRuntimeId"] for c in control.update_agent_runtime.call_args_list}
    assert attempted == {DEV_RID, PROD_RID}
    assert agent.identity_status == "failed"


def _round_trip(agent: Agent) -> Agent:
    """Serialize an Agent to its envelope and hydrate it back, as a store write+read does."""
    import json

    envelope = json.loads(json.dumps(agent.to_envelope()))
    record = {
        "recordId": agent.id,
        "name": agent.name,
        "description": agent.purpose,
        "status": "APPROVED",
        "createdAt": agent.created_at,
        "updatedAt": agent.updated_at,
    }
    return Agent.from_record(record, envelope)


def test_the_envelope_round_trip_PRESERVES_a_populated_per_stage_map():
    """The load-bearing one. A drop here is exactly the failure the ordering constraint warns
    about — invisible, and only observable later as a leaked runtime."""
    agent = _make_agent(agent_arns={"dev": DEV_ARN, "prod": PROD_ARN}, agent_arn=PROD_ARN)

    out = _round_trip(agent)

    assert out.agent_arns == {"dev": DEV_ARN, "prod": PROD_ARN}
    assert out.agent_arn == PROD_ARN
    assert out.runtime_arns() == {"dev": DEV_ARN, "prod": PROD_ARN}


def test_the_envelope_round_trip_TOLERATES_a_missing_map():
    """A scalar-only envelope has no `agent_arns` key AT ALL. It must hydrate — a missing map is a
    legacy record, not an error — and resolve through the scalar."""
    agent = _make_agent()
    envelope = agent.to_envelope()
    del envelope["agent_arns"]

    record = {
        "recordId": agent.id,
        "name": agent.name,
        "description": agent.purpose,
        "status": "APPROVED",
        "createdAt": agent.created_at,
        "updatedAt": agent.updated_at,
    }
    out = Agent.from_record(record, envelope)

    assert out.agent_arns == {}
    assert out.runtime_arns() == {"unknown": RUNTIME_ARN}


def test_the_envelope_round_trip_tolerates_an_explicit_null_map():
    """`jq` can leave a JSON `null` behind. It must read as "no map", not crash validation."""
    agent = _make_agent()
    envelope = agent.to_envelope()
    envelope["agent_arns"] = None

    record = {
        "recordId": agent.id,
        "name": agent.name,
        "description": agent.purpose,
        "status": "APPROVED",
        "createdAt": agent.created_at,
        "updatedAt": agent.updated_at,
    }
    out = Agent.from_record(record, envelope)

    assert out.agent_arns == {}


def test_persist_identity_writes_the_map_into_the_envelope_it_stores():
    """The concrete path the buildspec's write must survive: the REAL `to_envelope` is what
    `persist_identity` serializes, so the map has to be in that dict."""
    import json

    agent = _make_agent(agent_arns={"dev": DEV_ARN, "prod": PROD_ARN})

    stored = json.loads(json.dumps(agent.to_envelope()))

    assert stored["agent_arns"] == {"dev": DEV_ARN, "prod": PROD_ARN}


def _credentials_double() -> AsyncMock:
    creds = AsyncMock(name="AgentCredentialService")
    creds.delete_agent_obo_provider.return_value = None
    return creds


@pytest.mark.asyncio
async def test_delete_identity_also_deletes_the_obo_credential_provider():
    graph = _graph_double()
    graph.delete_agent_app.return_value = None
    creds = _credentials_double()
    svc = AgentIdentityService(
        graph=graph,
        registry=_registry_double(),
        control_client=_control_client(),
        region=REGION,
        tenant_id=TENANT_ID,
        login_base=LOGIN_BASE,
        credentials=creds,
    )
    agent = _make_agent()
    agent.entra_app_id = "app-guid-x"
    agent.entra_sp_id = "sp-obj-x"

    manager = MagicMock()
    manager.attach_mock(creds.delete_agent_obo_provider, "provider")
    manager.attach_mock(graph.delete_agent_app, "app")

    await svc.delete_identity(agent)

    creds.delete_agent_obo_provider.assert_awaited_once_with(agent.id)
    assert [c[0] for c in manager.mock_calls] == ["provider", "app"]


@pytest.mark.asyncio
async def test_delete_identity_swallows_a_provider_failure_and_still_deletes_the_app():
    graph = _graph_double()
    graph.delete_agent_app.return_value = None
    creds = _credentials_double()
    creds.delete_agent_obo_provider.side_effect = RuntimeError("throttled")
    svc = AgentIdentityService(
        graph=graph,
        registry=_registry_double(),
        control_client=_control_client(),
        region=REGION,
        tenant_id=TENANT_ID,
        login_base=LOGIN_BASE,
        credentials=creds,
    )
    agent = _make_agent()
    agent.entra_app_id = "app-guid-x"

    await svc.delete_identity(agent)

    graph.delete_agent_app.assert_awaited_once()


@pytest.mark.asyncio
async def test_delete_identity_can_opt_out_of_the_bundled_provider_leg():
    """`include_obo_provider=False` leaves the vault entry to a caller
    that reports it as its OWN line-item. Only `DELETE /agents/{id}`'s registered-external
    cascade uses it: nothing is blocking there, so the swallow above would let the route report
    `identity: deleted` while the entry survived. It stops the double delete, not the delete."""
    graph = _graph_double()
    graph.delete_agent_app.return_value = None
    creds = _credentials_double()
    svc = AgentIdentityService(
        graph=graph,
        registry=_registry_double(),
        control_client=_control_client(),
        region=REGION,
        tenant_id=TENANT_ID,
        login_base=LOGIN_BASE,
        credentials=creds,
    )
    agent = _make_agent()
    agent.entra_app_id = "app-guid-x"

    await svc.delete_identity(agent, include_obo_provider=False)

    creds.delete_agent_obo_provider.assert_not_awaited()
    graph.delete_agent_app.assert_awaited_once()


@pytest.mark.asyncio
async def test_delete_obo_provider_propagates_the_failure_it_reports():
    """The un-swallowed public half. The route turns the raise into a `failed` `obo_provider`
    line-item; swallowing here would put the false `deleted` straight back."""
    creds = _credentials_double()
    creds.delete_agent_obo_provider.side_effect = RuntimeError("throttled")
    svc = AgentIdentityService(
        graph=_graph_double(),
        registry=_registry_double(),
        control_client=_control_client(),
        region=REGION,
        tenant_id=TENANT_ID,
        login_base=LOGIN_BASE,
        credentials=creds,
    )

    with pytest.raises(RuntimeError):
        await svc.delete_obo_provider("rec-123")

    creds.delete_agent_obo_provider.assert_awaited_once_with("rec-123")


@pytest.mark.asyncio
async def test_delete_identity_builds_its_own_credential_service_when_none_injected():
    """Wiring: every existing caller constructs the service without a credential service, so
    the provider deletion must not depend on a wiring change. It reuses this service's own
    Graph + control client (the provider name needs neither)."""
    graph = _graph_double()
    graph.delete_agent_app.return_value = None
    control = _control_client()

    class _NotFound(Exception):
        pass

    control.exceptions.ResourceNotFoundException = _NotFound
    svc = _make_service(graph=graph, registry=_registry_double(), control=control)

    agent = _make_agent()
    agent.entra_app_id = "app-guid-x"

    await svc.delete_identity(agent)

    control.delete_oauth2_credential_provider.assert_called_once()
    assert control.delete_oauth2_credential_provider.call_args.kwargs["name"].endswith(agent.id)


@contextmanager
def _route_wiring(identity, databricks=None):
    """Wire the doubles into ``api.routes.agents``' singletons for the duration of a test.

    The repo-wide idiom (tests patch a route module's lazy singleton rather than reaching into
    a service), and the reason ``agents.get_adapters()`` deliberately does NOT cache: a cached
    adapter dict would keep dispatching into whatever collaborator existed at first call, so a
    swapped double would be silently ignored and every test here would pass vacuously.
    """
    from api.routes import agents as agents_module

    previous = (agents_module._identity_svc, agents_module._databricks_identity_svc)
    agents_module._identity_svc = identity
    agents_module._databricks_identity_svc = databricks
    try:
        yield agents_module
    finally:
        (
            agents_module._identity_svc,
            agents_module._databricks_identity_svc,
        ) = previous


def _databricks_agent(**overrides) -> Agent:
    """A Databricks-governed agent: a runtime HANDLE (never an ARN), Entra, DATABRICKS."""
    base: dict[str, Any] = dict(
        agent_arn=None,
        platform=Platform.DATABRICKS,
        auth_type=AgentAuthType.ENTRA,
    )
    base.update(overrides)
    agent = _make_agent(**base)
    agent.runtime_handle = "https://claims-triage-1234.aws.databricksapps.com"
    agent.runtime_kind = "app"
    return agent


@pytest.mark.asyncio
async def test_provision_dispatches_a_databricks_agent_to_the_databricks_half():
    graph = _graph_double()
    registry = _registry_double()
    control = MagicMock(name="control")
    databricks = AsyncMock(name="DatabricksIdentityService")
    svc = _make_service(graph=graph, registry=registry, control=control)
    agent = _databricks_agent()

    with _route_wiring(svc, databricks) as routes:
        await svc.provision(agent, runtime_binding=routes.platform_runtime_binding)

    graph.create_agent_app.assert_awaited_once()
    graph.set_assignment_required.assert_awaited_once()
    graph.grant_backend_obo_consent.assert_awaited_once()
    databricks.provision_databricks_runtime.assert_awaited_once_with(agent)
    control.get_agent_runtime.assert_not_called()
    control.update_agent_runtime.assert_not_called()


@pytest.mark.asyncio
async def test_an_agentcore_agent_never_reaches_the_databricks_half():
    graph = _graph_double()
    registry = _registry_double()
    control = MagicMock(name="control")
    control.get_agent_runtime.return_value = {
        "agentRuntimeArtifact": {"containerConfiguration": {"containerUri": "repo:tag"}},
        "roleArn": "arn:aws:iam::123456789012:role/r",
        "networkConfiguration": {"networkMode": "PUBLIC"},
        "status": "READY",
    }
    databricks = AsyncMock(name="DatabricksIdentityService")
    svc = _make_service(graph=graph, registry=registry, control=control)

    with _route_wiring(svc, databricks) as routes:
        await svc.provision(_make_agent(), runtime_binding=routes.platform_runtime_binding)

    control.update_agent_runtime.assert_called_once()
    databricks.provision_databricks_runtime.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_databricks_agent_with_no_collaborator_wired_fails_loudly():
    """The dangerous alternative is a SILENT SKIP: provision_identity succeeds, no runtime
    half runs, and the agent sits at 'pending' with no error — indistinguishable from work
    in flight. It must be a persisted 'failed' instead.

    The adapter seam changed only WHERE the refusal comes from: the factory leaves an un-wired platform out
    of the adapter dict and ``adapter_for`` refuses it with ``AdapterError``. It is resolved
    LAZILY, inside ``provision``'s envelope, which is what keeps the outcome a persisted
    'failed' rather than an exception thrown past it."""
    registry = _registry_double()
    svc = _make_service(graph=_graph_double(), registry=registry, control=MagicMock(name="control"))
    agent = _databricks_agent()

    with _route_wiring(svc, None) as routes:
        with pytest.raises(ProvisioningError):
            await svc.provision(agent, runtime_binding=routes.platform_runtime_binding)

    assert agent.identity_status == "failed"
    assert registry.persist_identity.call_args.args[0].identity_status == "failed"


@pytest.mark.asyncio
async def test_a_databricks_half_failure_is_persisted_as_failed_by_the_envelope():
    """The Databricks half owns its own persist, but `provision`'s envelope must ALSO catch —
    a raise that escaped both would leave the record at 'pending'."""
    registry = _registry_double()
    databricks = AsyncMock(name="DatabricksIdentityService")
    databricks.provision_databricks_runtime.side_effect = RuntimeError("boom")
    svc = _make_service(graph=_graph_double(), registry=registry, control=MagicMock(name="control"))
    agent = _databricks_agent()

    with _route_wiring(svc, databricks) as routes:
        with pytest.raises(ProvisioningError):
            await svc.provision(agent, runtime_binding=routes.platform_runtime_binding)

    assert agent.identity_status == "failed"


@pytest.mark.asyncio
async def test_a_metadata_only_agent_still_gets_neither_runtime_half():
    """Neither gate: no ARN, no handle. The neutral half runs (pre-registration), and
    both platform halves stay untouched — the deferral this seam must not break.

    NO ``runtime_binding`` is passed, which is exactly what the routes do for this record: the
    registration hook and the reprovision affordance both gate on a runtime EXISTING, so a
    metadata-only agent is never handed a platform binding at all. ``provision`` then takes its
    default AgentCore arm, whose ``runtime_arns()`` gate is False — the deferral, unchanged."""
    control = MagicMock(name="control")
    databricks = AsyncMock(name="DatabricksIdentityService")
    svc = _make_service(graph=_graph_double(), registry=_registry_double(), control=control)

    with _route_wiring(svc, databricks):
        await svc.provision(_make_agent(agent_arn=None, platform=Platform.AWS_BEDROCK))

    control.update_agent_runtime.assert_not_called()
    databricks.provision_databricks_runtime.assert_not_awaited()


def _status_service(*, control) -> AgentIdentityService:
    return _make_service(graph=_graph_double(), registry=_registry_double(), control=control)


def _status_read(svc, databricks=None):
    """The route's status read over these doubles: ``status_adapter`` then ``runtime_status``.

    Returns a callable with the earlier ``svc.runtime_status`` shape, so the assertions below did
    not have to move — the two-step resolve-then-read IS the relocation under test."""

    def read(agent, stage=None):
        with _route_wiring(svc, databricks) as routes:
            return routes.status_adapter(agent).runtime_status(agent, stage)

    return read


def test_runtime_status_dispatches_a_databricks_agent_to_the_databricks_producer():
    from models.agent import RuntimeStatus

    control = _control_client()
    databricks = MagicMock(name="DatabricksIdentityService")
    databricks.runtime_status.return_value = RuntimeStatus(
        agent_id="rec-abc123", stage="dev", status="ready", checked_at="2026-08-06T00:00:00Z"
    )
    read = _status_read(_status_service(control=control), databricks)
    agent = _databricks_agent()

    result = read(agent, "dev")

    assert result.status == "ready"
    databricks.runtime_status.assert_called_once_with(agent, stage="dev")
    control.get_agent_runtime.assert_not_called()


def test_runtime_status_of_a_databricks_agent_carries_the_env_field_as_none():
    """THE RECONCILE-ON-READ AND DATABRICKS SEAM. `GET /agents/{id}/runtime` dereferences
    `status.environment_variables` unguarded on every producer path (the reconcile-on-read
    hand-off), so the Databricks producer's answer must ride the `RuntimeStatusWithEnv`
    carrier too — a plain `RuntimeStatus` here 500s the fleet view for every Databricks
    agent. `None` (never `{}`) is the pinned value: a Databricks runtime holds no
    backend-injected MCP env, so "no evidence" is the true claim and the reconciler no-ops."""
    from models.agent import RuntimeStatus

    control = _control_client()
    databricks = MagicMock(name="DatabricksIdentityService")
    databricks.runtime_status.return_value = RuntimeStatus(
        agent_id="rec-abc123", stage="dev", status="ready", checked_at="2026-08-06T00:00:00Z"
    )
    read = _status_read(_status_service(control=control), databricks)

    result = read(_databricks_agent(), "dev")

    assert result.environment_variables is None
    unwired = _status_read(_status_service(control=_control_client()), None)
    assert unwired(_databricks_agent(), "dev").environment_variables is None


def test_runtime_status_keeps_the_agentcore_path_on_boto3():
    """The fence. An AgentCore agent must still be answered by the boto3 producer even when a
    Databricks collaborator is wired."""
    control = _control_client()
    databricks = MagicMock(name="DatabricksIdentityService")
    read = _status_read(_status_service(control=control), databricks)

    result = read(_make_agent())

    assert result.status == "ready"
    control.get_agent_runtime.assert_called_once_with(agentRuntimeId=RUNTIME_RID)
    databricks.runtime_status.assert_not_called()


def test_runtime_status_of_a_databricks_agent_is_unknown_when_no_producer_is_wired():
    """A missing collaborator is not a silent "not_deployed". Reporting nothing-deployed for an
    agent we simply cannot ask about would be the same conflation `unknown` exists to prevent —
    and it is a READ, so it still may not raise.

    The adapter seam is where this stops being a branch: with no Databricks collaborator the factory leaves
    the platform OUT of the adapter dict, so ``status_adapter`` cannot dispatch to it — and it
    answers with that platform's collaborator-less adapter (whose degrade arm owns the honest
    text) rather than borrowing the AWS one, which would say ``not_deployed``."""
    control = _control_client()
    read = _status_read(_status_service(control=control), None)

    result = read(_databricks_agent())

    assert result.status == "unknown"
    assert result.detail
    control.get_agent_runtime.assert_not_called()


def test_a_raising_databricks_producer_degrades_to_unknown():
    """Defense in depth: the producer promises never to raise, but this seam is what the route
    calls, and a 5xx here blanks the fleet view."""
    control = _control_client()
    databricks = MagicMock(name="DatabricksIdentityService")
    databricks.runtime_status.side_effect = RuntimeError("boom")
    read = _status_read(_status_service(control=control), databricks)

    result = read(_databricks_agent())

    assert result.status == "unknown"
    assert "boom" not in (result.detail or "")


def test_an_ungoverned_databricks_labelled_agent_with_a_handle_reads_unknown():
    """THE ONE SHAPE THE ADAPTER SEAM CHANGED, pinned deliberately.

    A ``platform=DATABRICKS`` record with a runtime HANDLE but NOT Entra-governed: the read
    dispatches on the record's PLATFORM (the `GOVERNABLE_PLATFORMS` bridge), so it reaches the
    Databricks producer, which answers `unknown` — there is something running and AGP holds no
    credential path to ask about it. Before the seam it fell through to the AgentCore body and read
    `not_deployed`, MANUFACTURED from an empty ARN map: a positive claim that nothing is running,
    drawn from the absence of an AWS ARN on a record that never had one. That is exactly the
    conflation `unknown` exists to prevent, so the new answer is the honest one — and it is the
    reason the dispatch must not be re-gated on `is_databricks_governed_agent`.

    Contrast with the handle-LESS case below: no handle IS complete local evidence, and that one
    still answers `not_deployed`."""
    control = _control_client()
    databricks = MagicMock(name="DatabricksIdentityService")
    read = _status_read(_status_service(control=control), databricks)
    ungoverned = _databricks_agent(auth_type=AgentAuthType.API_KEY)
    assert not ungoverned.is_databricks_governed

    from services.databricks_identity_service import DatabricksIdentityService

    databricks.runtime_status.side_effect = lambda agent, stage=None: (
        DatabricksIdentityService.runtime_status(MagicMock(), agent, stage=stage)
    )

    result = read(ungoverned, "dev")

    assert result.status == "unknown"
    assert result.detail == ("runtime status could not be read (the agent has no governed runtime identity)")
    control.get_agent_runtime.assert_not_called()


def test_a_metadata_only_agent_is_still_answered_not_deployed_by_the_default_arm():
    """Neither gate. The dispatch must not be an if/elif chain that falls off the end — a
    metadata-only record (the ~18 seed agents) answers `not_deployed` locally, as it does today.

    Since the seam the same rule is spelled in ``status_adapter``: a record whose platform names no
    governed runtime resolves to the AWS adapter, which holds that answer LOCALLY."""
    control = _control_client()
    databricks = MagicMock(name="DatabricksIdentityService")
    read = _status_read(_status_service(control=control), databricks)

    result = read(_make_agent(agent_arn=None, platform=Platform.AWS_BEDROCK))

    assert result.status == "not_deployed"
    databricks.runtime_status.assert_not_called()
    control.get_agent_runtime.assert_not_called()


def test_provisioning_error_renders_the_fixed_502_delete_refusal():
    """The one kind is the delete route's refusal when the blocking federation audience could not be
    withdrawn; the message stays in the log."""
    err = ProvisioningError("agent rec-1's federation audience could not be removed (DatabricksError)")

    assert (err.status_code, err.kind, err.code) == (
        502,
        "artifact_removal_failed",
        "identity.artifact_removal_failed",
    )
    assert err.body() == {
        "detail": (
            "the agent's Databricks federation audience could not be removed, so the agent was not "
            "deleted — retry, or ask your Databricks account admin"
        ),
        "code": "identity.artifact_removal_failed",
    }
    assert "rec-1" not in str(err.detail)
