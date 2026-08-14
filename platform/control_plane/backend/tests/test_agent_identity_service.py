"""Tests for ``services.agent_identity_service`` — the E6 provisioning orchestrator (T-IDENTITY).

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

Contract is pinned in the E6 plan, Task T-IDENTITY (the gate, ``provision`` step order,
``_configure_runtime_authorizer``, CRITIQUE-FIX-A/B/C, AUDIENCE-FORM, RID-NOT-ARN);
mechanics from research §1 (GET→replay→UpdateAgentRuntime→poll-to-READY; audience-only).
"""

from __future__ import annotations

import warnings
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, call

import pytest
from botocore.exceptions import ClientError
from fastapi.encoders import jsonable_encoder

from models.agent import Agent, AuthType, IdentityStatus, LifecycleState, Platform
from services.agent_identity_service import (
    AgentIdentityService,
    ProvisioningError,
    is_agentcore_agent,
)

# ---------------------------------------------------------------------------
# Fixtures / builders
# ---------------------------------------------------------------------------
TENANT_ID = "00000000-0000-0000-0000-000000000001"
LOGIN_BASE = "https://login.microsoftonline.com"
REGION = "us-east-1"

# A realistic AgentCore runtime ARN; the RID is the last "/"-segment.
RUNTIME_RID = "agent_governance_test_agent-wdOmsREOEj"
RUNTIME_ARN = (
    f"arn:aws:bedrock-agentcore:us-east-1:123456789012:runtime/{RUNTIME_RID}"
)


def _make_agent(
    *,
    agent_id: str = "rec-abc123",
    name: str = "Claims Triage DE",
    agent_arn: str | None = RUNTIME_ARN,
    # E28A/T1: per-stage runtime ARNs. Defaults to EMPTY — a legacy (scalar-only) record, the
    # shape every agent in the registry has today, so the pre-existing suite keeps exercising it.
    agent_arns: dict[str, str] | None = None,
    auth_type: AuthType = AuthType.ENTRA,
    platform: Platform | None = Platform.AWS_BEDROCK,
    entra_sp_id: str | None = None,
    entra_app_audience: str | None = None,
    identity_status: str = "none",
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


# ===========================================================================
# Gate
# ===========================================================================
def test_is_agentcore_agent_gate():
    # True only for arn + ENTRA + AWS_BEDROCK.
    assert is_agentcore_agent(_make_agent()) is True

    # No arn (a metadata seed agent) → False.
    assert is_agentcore_agent(_make_agent(agent_arn=None)) is False
    assert is_agentcore_agent(_make_agent(agent_arn="")) is False

    # Wrong auth_type → False.
    assert is_agentcore_agent(_make_agent(auth_type=AuthType.API_KEY)) is False
    assert is_agentcore_agent(_make_agent(auth_type=AuthType.NONE)) is False

    # Wrong platform → False.
    assert is_agentcore_agent(_make_agent(platform=Platform.AZURE)) is False
    assert is_agentcore_agent(_make_agent(platform=Platform.OTHER)) is False
    assert is_agentcore_agent(_make_agent(platform=None)) is False


# ===========================================================================
# Happy path — order + ids + status
# ===========================================================================
@pytest.mark.asyncio
async def test_provision_happy_path_calls_graph_then_authorizer_then_persists():
    agent = _make_agent()
    graph = _graph_double(agent.id)
    registry = _registry_double()
    control = _control_client()

    # A shared manager records the relative order of the collaborator calls.
    manager = MagicMock()
    manager.attach_mock(graph.create_agent_app, "create_agent_app")
    manager.attach_mock(graph.set_assignment_required, "set_assignment_required")
    manager.attach_mock(graph.grant_backend_obo_consent, "grant_backend_obo_consent")
    manager.attach_mock(registry.persist_identity, "persist_identity")
    manager.attach_mock(control.update_agent_runtime, "update_agent_runtime")

    svc = _make_service(graph=graph, registry=registry, control=control)
    result = await svc.provision(agent)

    # The exact call order: create → persist(pending) → set_assignment_required →
    # grant_backend_obo_consent → update_agent_runtime (authorizer) → persist(provisioned).
    order = [c[0] for c in manager.mock_calls]
    assert order == [
        "create_agent_app",
        "persist_identity",
        "set_assignment_required",
        "grant_backend_obo_consent",
        "update_agent_runtime",
        "persist_identity",
    ]

    # Final status + the 5 identity fields set on the agent.
    assert result.identity_status == "provisioned"
    assert result.entra_app_id == f"app-client-guid-{agent.id}"
    assert result.entra_sp_id == f"sp-obj-id-{agent.id}"
    assert result.invoker_role_id == f"invoker-{agent.id}"
    assert result.admin_role_id == f"admin-{agent.id}"
    assert result.entra_app_audience == f"api://agp-agent-{agent.id}"

    # create_agent_app was called with (agent.id, agent.name).
    graph.create_agent_app.assert_awaited_once_with(agent.id, agent.name)


# ===========================================================================
# The writers assign IdentityStatus MEMBERS (Epic 36/T20) — no serializer warnings
# ===========================================================================
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
# Authorizer — audience only, no allowedClients / customClaims
# ===========================================================================
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

    # AUDIENCE-FORM (live): accept BOTH the api:// URI and the app's client GUID, since
    # the OBO'd token's aud can be either form. Order = (URI, GUID).
    assert jwt["allowedAudience"] == [agent.entra_app_audience, agent.entra_app_id]
    # Audience ONLY — no allowedClients / customClaims / allowedScopes.
    assert "allowedClients" not in jwt
    assert "customClaims" not in jwt
    assert "allowedScopes" not in jwt
    # discoveryUrl is the Entra v2 well-known, per-tenant.
    assert jwt["discoveryUrl"] == (
        f"{LOGIN_BASE}/{TENANT_ID}/v2.0/.well-known/openid-configuration"
    )


# ===========================================================================
# Authorizer — replays artifact / role / network (full-replace UpdateAgentRuntime)
# ===========================================================================
@pytest.mark.asyncio
async def test_provision_replays_artifact_role_network():
    agent = _make_agent()
    graph = _graph_double(agent.id)
    registry = _registry_double()
    control = _control_client()

    # Capture what get_agent_runtime returned so we can compare the replay.
    read = control.get_agent_runtime.return_value

    svc = _make_service(graph=graph, registry=registry, control=control)
    await svc.provision(agent)

    _, kwargs = control.update_agent_runtime.call_args
    assert kwargs["agentRuntimeArtifact"] == read["agentRuntimeArtifact"]
    assert kwargs["roleArn"] == read["roleArn"]
    assert kwargs["networkConfiguration"] == read["networkConfiguration"]


# ===========================================================================
# Poll to READY
# ===========================================================================
@pytest.mark.asyncio
async def test_provision_polls_runtime_to_ready():
    agent = _make_agent()
    graph = _graph_double(agent.id)
    registry = _registry_double()
    # First get_agent_runtime read (for replay) + poll reads: UPDATING, UPDATING, READY.
    control = _control_client(statuses=["UPDATING", "UPDATING", "UPDATING", "READY"])

    svc = _make_service(graph=graph, registry=registry, control=control)
    result = await svc.provision(agent)

    # We waited until READY before flipping to 'provisioned'.
    assert result.identity_status == "provisioned"
    # get_agent_runtime called more than once (initial read + ≥1 poll until READY).
    assert control.get_agent_runtime.call_count >= 2


# ===========================================================================
# Idempotency — skip create when sp exists (re-provision)
# ===========================================================================
@pytest.mark.asyncio
async def test_provision_idempotent_skips_app_create_when_sp_exists():
    # The agent already carries its identity ids (a re-provision).
    agent = _make_agent(
        entra_sp_id="sp-existing",
        entra_app_audience="api://agp-agent-rec-abc123",
        identity_status="failed",
    )
    agent.invoker_role_id = "invoker-existing"
    agent.admin_role_id = "admin-existing"
    graph = _graph_double(agent.id)
    registry = _registry_double()
    control = _control_client()

    svc = _make_service(graph=graph, registry=registry, control=control)
    result = await svc.provision(agent)

    # create_agent_app NOT called (sp already known).
    graph.create_agent_app.assert_not_awaited()
    # Authorizer still re-configured (the point of re-provision).
    control.update_agent_runtime.assert_called_once()
    # Steps 2/3 still run (idempotent).
    graph.set_assignment_required.assert_awaited_once_with("sp-existing")
    graph.grant_backend_obo_consent.assert_awaited_once_with("sp-existing")
    assert result.identity_status == "provisioned"


# ===========================================================================
# CRITIQUE-FIX-A — persist ids IMMEDIATELY after create, before step 2
# ===========================================================================
@pytest.mark.asyncio
async def test_provision_persists_ids_immediately_after_app_create():
    agent = _make_agent()
    graph = _graph_double(agent.id)
    registry = _registry_double()
    control = _control_client()

    # Record the agent's identity_status + ids AT THE MOMENT set_assignment_required
    # is invoked. If the persist happened first, the snapshot must show 'pending' +
    # the ids set.
    snapshot: dict = {}

    async def _capture_on_step2(sp_id):
        snapshot["identity_status"] = agent.identity_status
        snapshot["entra_sp_id"] = agent.entra_sp_id
        snapshot["entra_app_id"] = agent.entra_app_id
        snapshot["invoker_role_id"] = agent.invoker_role_id
        snapshot["admin_role_id"] = agent.admin_role_id
        snapshot["entra_app_audience"] = agent.entra_app_audience
        # persist_identity must already have been called once (the pending persist).
        snapshot["persist_count_at_step2"] = registry.persist_identity.call_count
        return None

    graph.set_assignment_required.side_effect = _capture_on_step2

    svc = _make_service(graph=graph, registry=registry, control=control)
    await svc.provision(agent)

    # At step 2, the ids were already set and status was 'pending', and exactly one
    # persist had happened.
    assert snapshot["identity_status"] == "pending"
    assert snapshot["entra_sp_id"] == f"sp-obj-id-{agent.id}"
    assert snapshot["entra_app_id"] == f"app-client-guid-{agent.id}"
    assert snapshot["invoker_role_id"] == f"invoker-{agent.id}"
    assert snapshot["admin_role_id"] == f"admin-{agent.id}"
    assert snapshot["entra_app_audience"] == f"api://agp-agent-{agent.id}"
    assert snapshot["persist_count_at_step2"] == 1


# ===========================================================================
# CRITIQUE-FIX-A — mid-sequence failure persists ids; re-provision skips create
# ===========================================================================
@pytest.mark.asyncio
async def test_provision_midsequence_failure_persists_ids_then_reprovision_skips_create():
    agent = _make_agent()
    graph = _graph_double(agent.id)
    registry = _registry_double()
    control = _control_client()

    # set_assignment_required raises AFTER create_agent_app succeeded.
    graph.set_assignment_required.side_effect = RuntimeError("Graph 500 on PATCH")

    svc = _make_service(graph=graph, registry=registry, control=control)
    with pytest.raises(ProvisioningError):
        await svc.provision(agent)

    # The ids ARE persisted (create succeeded + the immediate persist landed), and
    # status is 'failed'.
    assert agent.entra_sp_id == f"sp-obj-id-{agent.id}"
    assert agent.entra_app_id == f"app-client-guid-{agent.id}"
    assert agent.entra_app_audience == f"api://agp-agent-{agent.id}"
    assert agent.identity_status == "failed"
    # create_agent_app was called exactly once so far.
    assert graph.create_agent_app.await_count == 1

    # --- A SUBSEQUENT provision on the SAME agent (re-provision) ---
    # Clear the failing side_effect so step 2 now succeeds.
    graph.set_assignment_required.side_effect = None
    graph.set_assignment_required.return_value = None

    result = await svc.provision(agent)

    # create_agent_app NOT called again (entra_sp_id already set → skip-guard holds).
    assert graph.create_agent_app.await_count == 1
    # Steps 2-4 re-ran.
    graph.set_assignment_required.assert_awaited_with(f"sp-obj-id-{agent.id}")
    graph.grant_backend_obo_consent.assert_awaited_with(f"sp-obj-id-{agent.id}")
    control.update_agent_runtime.assert_called_once()
    assert result.identity_status == "provisioned"


# ===========================================================================
# CRITIQUE-FIX-B — the blocking authorizer config runs OFF the event loop
# ===========================================================================
@pytest.mark.asyncio
async def test_provision_dispatches_authorizer_off_loop(monkeypatch):
    agent = _make_agent()
    graph = _graph_double(agent.id)
    registry = _registry_double()
    control = _control_client()

    import services.agent_identity_service as mod

    # Patch anyio.to_thread.run_sync with an AsyncMock that actually runs the target
    # (so the rest of provision still works) but records that it was awaited with the
    # blocking method + its args.
    real_run_sync = mod.anyio.to_thread.run_sync
    recorder = AsyncMock(side_effect=real_run_sync)
    monkeypatch.setattr(mod.anyio.to_thread, "run_sync", recorder)

    svc = _make_service(graph=graph, registry=registry, control=control)
    await svc.provision(agent)

    # run_sync was awaited with the bound fan-out + the agent. E28A/T1 moved the audience/arn
    # arguments INSIDE the fan-out (which resolves the agent's N runtimes and closes over the
    # audience per runtime), so the off-loop hand-off is now agent-shaped. The contract this
    # test exists for is unchanged and still asserted: the blocking boto3 work — a ~minute+
    # poll-to-READY, now once PER RUNTIME, so strictly MORE blocking than before — is handed to
    # a thread rather than run on the uvicorn loop where it would freeze health checks.
    recorder.assert_awaited_once()
    awaited_args = recorder.await_args.args
    assert awaited_args[0] == svc._configure_runtime_authorizers
    assert awaited_args[1] is agent


# ===========================================================================
# CRITIQUE-FIX-C — two agents get distinct audiences (cross-agent-replay safety)
# ===========================================================================
@pytest.mark.asyncio
async def test_two_agents_get_distinct_audiences():
    agent_a = _make_agent(agent_id="rec-aaa", name="Agent A")
    agent_b = _make_agent(agent_id="rec-bbb", name="Agent B")

    # Each agent's graph double derives app_uri from its own id → distinct audiences.
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

    # Each runtime's allowedAudience == that agent's (URI, GUID) pair.
    assert aud_a == [agent_a.entra_app_audience, agent_a.entra_app_id]
    assert aud_b == [agent_b.entra_app_audience, agent_b.entra_app_id]
    # And the two audiences are NOT equal (no cross-agent replay).
    assert aud_a != aud_b


# ===========================================================================
# Failure → identity_status='failed' + ProvisioningError
# ===========================================================================
@pytest.mark.asyncio
async def test_provision_failure_sets_identity_status_failed_and_raises():
    agent = _make_agent()
    graph = _graph_double(agent.id)
    registry = _registry_double()
    control = _control_client()

    # The boto3 authorizer step raises.
    control.get_agent_runtime.side_effect = RuntimeError("AccessDeniedException")

    svc = _make_service(graph=graph, registry=registry, control=control)
    with pytest.raises(ProvisioningError):
        await svc.provision(agent)

    assert agent.identity_status == "failed"
    # ...as the MEMBER (Epic 36/T20): a bare string here would warn on serialization.
    assert agent.identity_status is IdentityStatus.FAILED
    # The failed status was persisted.
    persisted_statuses = [
        c.args[0].identity_status for c in registry.persist_identity.call_args_list
    ]
    assert "failed" in persisted_statuses


@pytest.mark.asyncio
async def test_provision_raises_when_create_returns_no_sp_id():
    # FIX 1: create_agent_app's GET-or-create branch can return sp_id=None
    # (_resolve_existing_app finds the app but no SP). provision() must guard this:
    # persist 'failed' and raise, WITHOUT advancing to steps 2-4.
    agent = _make_agent()
    graph = _graph_double(agent.id)
    graph.create_agent_app.return_value = {
        "app_id": f"app-client-guid-{agent.id}",
        "sp_id": None,  # the half-resolved record
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

    # Status persisted as 'failed'.
    assert agent.identity_status == "failed"
    # The APP ids we DID get are kept intact (so the duplicate-resolve works next time).
    assert agent.entra_app_id == f"app-client-guid-{agent.id}"
    assert agent.entra_app_audience == f"api://agp-agent-{agent.id}"
    # Steps 2-4 were NOT reached.
    graph.set_assignment_required.assert_not_awaited()
    graph.grant_backend_obo_consent.assert_not_awaited()
    control.update_agent_runtime.assert_not_called()


@pytest.mark.asyncio
async def test_provision_graph_failure_sets_identity_status_failed_and_raises():
    # A graph (step-1 create) failure also lands at 'failed' + ProvisioningError.
    agent = _make_agent()
    graph = _graph_double(agent.id)
    graph.create_agent_app.side_effect = RuntimeError("Graph 403")
    registry = _registry_double()
    control = _control_client()

    svc = _make_service(graph=graph, registry=registry, control=control)
    with pytest.raises(ProvisioningError):
        await svc.provision(agent)

    assert agent.identity_status == "failed"


# ===========================================================================
# RID-NOT-ARN — boto3 control calls get the ARN's last segment, not the ARN
# ===========================================================================
@pytest.mark.asyncio
async def test_configure_runtime_authorizer_parses_rid_from_arn():
    agent = _make_agent()
    graph = _graph_double(agent.id)
    registry = _registry_double()
    control = _control_client()

    svc = _make_service(graph=graph, registry=registry, control=control)
    await svc.provision(agent)

    # get_agent_runtime + update_agent_runtime both receive agentRuntimeId == RID,
    # NOT the full ARN.
    get_kwargs = control.get_agent_runtime.call_args.kwargs
    update_kwargs = control.update_agent_runtime.call_args.kwargs
    assert get_kwargs["agentRuntimeId"] == RUNTIME_RID
    assert update_kwargs["agentRuntimeId"] == RUNTIME_RID
    assert get_kwargs["agentRuntimeId"] != RUNTIME_ARN
    assert update_kwargs["agentRuntimeId"] != RUNTIME_ARN


# ===========================================================================
# Poll exhaustion / failed runtime status → ProvisioningError
# ===========================================================================
@pytest.mark.asyncio
async def test_provision_runtime_update_failed_status_raises():
    agent = _make_agent()
    graph = _graph_double(agent.id)
    registry = _registry_double()
    # Initial read READY, then the poll sees UPDATE_FAILED → must raise.
    control = _control_client(statuses=["READY", "UPDATE_FAILED"])

    svc = _make_service(graph=graph, registry=registry, control=control)
    with pytest.raises(ProvisioningError):
        await svc.provision(agent)

    assert agent.identity_status == "failed"


# ===========================================================================
# Task 4 — split seam: provision_identity (pre-deploy, identity-mint only) +
# provision_runtime (post-deploy, authorizer). provision() = provision_identity then
# provision_runtime WHEN agent_arn present (preserves the pre-split behavior). Adapted
# from the brief's sketch to the file's real idiom: async methods + the module-level
# _make_agent / _graph_double / _registry_double / _control_client / _make_service doubles
# (the brief's `identity_service_no_aws` fixture + sync `make_agent` don't exist here).
# ===========================================================================
@pytest.mark.asyncio
async def test_provision_identity_skips_runtime_calls_when_no_arn():
    # provision_identity mints the Entra identity ONLY — safe when agent_arn is None
    # (pre-registration): it must NOT touch the runtime boto3 (no update_agent_runtime /
    # get_agent_runtime) and must land the agent at 'pending' (or 'provisioned').
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
    # provision_runtime is the post-deploy half — it needs the runtime ARN and raises
    # a ValueError (not a bare KeyError/None-crash) when it is missing.
    graph = _graph_double()
    registry = _registry_double()
    control = _control_client()
    svc = _make_service(graph=graph, registry=registry, control=control)

    with pytest.raises(ValueError, match="agent_arn"):
        await svc.provision_runtime(_make_agent(agent_arn=None))


@pytest.mark.asyncio
async def test_provision_still_does_both_when_arn_present():
    # With an ARN present, provision() still runs identity-mint AND the runtime authorizer
    # (the pre-split behavior is preserved).
    agent = _make_agent(agent_arn=RUNTIME_ARN)
    graph = _graph_double(agent.id)
    registry = _registry_double()
    control = _control_client()

    svc = _make_service(graph=graph, registry=registry, control=control)
    await svc.provision(agent)

    control.update_agent_runtime.assert_called()


# ===========================================================================
# set_runtime_environment (Epic 7, Tier-2) — inject the reference agent's env at
# GRANT time. A SYNC boto3 sibling of _configure_runtime_authorizer: GET → replay
# all required + security-relevant (authorizer) + optional fields → MERGE env →
# UpdateAgentRuntime → poll READY. Mirrors the authorizer method's mechanics
# (RID-from-ARN, replay, poll-to-READY); MERGES env so it never clobbers env the
# runtime already needs and NEVER drops the inbound authorizer (the E6 gate, §12.7).
# ===========================================================================
def _control_client_with_authorizer_and_env(
    *,
    existing_env: dict | None = None,
    existing_authorizer: dict | None = None,
    statuses: list[str] | None = None,
) -> MagicMock:
    """A control client whose GetAgentRuntime ALSO returns an inbound authorizer +
    a pre-existing environmentVariables map (the live shape set_runtime_environment
    must preserve/merge). Defaults to a realistic E6 customJWTAuthorizer + one
    pre-existing env key. ``statuses`` sequences the poll reads (else every read READY).
    """
    control = _control_client(statuses=statuses)
    artifact = {"containerConfiguration": {"containerUri": "123.dkr.ecr/agent:latest"}}
    role_arn = "arn:aws:iam::123456789012:role/agent-runtime-role"
    network = {"networkMode": "PUBLIC"}
    authorizer = existing_authorizer or {
        "customJWTAuthorizer": {
            "discoveryUrl": (
                f"{LOGIN_BASE}/{TENANT_ID}/v2.0/.well-known/openid-configuration"
            ),
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
        control.get_agent_runtime.side_effect = [
            {**base_read, "status": s} for s in statuses
        ]
    return control


def test_set_runtime_environment_merges_env_and_preserves_authorizer():
    # GetAgentRuntime returns an existing authorizer + existing env; set_runtime_environment
    # must (a) MERGE the injected env with the pre-existing env (new keys win, existing
    # kept), (b) REPLAY the inbound authorizer unchanged (§12.7 — never drop the gate),
    # (c) replay artifact/roleArn/networkConfiguration.
    existing_authorizer = {
        "customJWTAuthorizer": {
            "discoveryUrl": (
                f"{LOGIN_BASE}/{TENANT_ID}/v2.0/.well-known/openid-configuration"
            ),
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
    # (a) MERGE: all injected keys present AND the pre-existing keys preserved.
    for k, v in injected.items():
        assert sent_env[k] == v
    assert sent_env["LOG_LEVEL"] == "INFO"
    assert sent_env["MODEL_ID"] == "some-existing-model"
    # (b) the inbound authorizer is REPLAYED unchanged (§12.7 — the E6 gate stays).
    assert kwargs["authorizerConfiguration"] == existing_authorizer
    # (c) artifact / roleArn / networkConfiguration replayed from the GET.
    assert kwargs["agentRuntimeArtifact"] == read["agentRuntimeArtifact"]
    assert kwargs["roleArn"] == read["roleArn"]
    assert kwargs["networkConfiguration"] == read["networkConfiguration"]
    # RID-not-ARN on both control calls.
    assert kwargs["agentRuntimeId"] == RUNTIME_RID
    assert control.get_agent_runtime.call_args.kwargs["agentRuntimeId"] == RUNTIME_RID


def test_set_runtime_environment_merges_when_runtime_has_no_existing_env():
    # A runtime with NO environmentVariables on the GET (the common case) → the injected
    # env IS the env; no KeyError on the absent key.
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
    # The authorizer is still replayed (the gate is preserved even on the no-env path).
    assert "authorizerConfiguration" in kwargs


def test_set_runtime_environment_polls_to_ready():
    # Initial read (replay) + poll reads UPDATING, UPDATING, READY → converges; the
    # method returns only after READY (it does not raise).
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

    # get_agent_runtime called more than once (initial read + ≥1 poll until READY).
    assert control.get_agent_runtime.call_count >= 2
    control.update_agent_runtime.assert_called_once()


def test_set_runtime_environment_returns_the_execution_role_arn():
    """E36/T13 — the method RETURNS the runtime's exec ``roleArn``, read from the GET it already
    performs. The Langfuse wiring has to grant that exact principal ``GetSecretValue`` on the
    per-agent secret, and returning the value already in hand is what keeps leg 4 from costing a
    second ``get_agent_runtime``. Additive: every pre-T13 caller ignores it."""
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
    # It is the SAME value replayed into the full-replace (one read, one truth).
    assert control.update_agent_runtime.call_args.kwargs["roleArn"] == role_arn


def test_set_runtime_environment_honours_an_injected_control_client():
    """E36/T13 — ``control_client`` routes the GET, the UPDATE **and** the poll into the account
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

    svc.set_runtime_environment(
        RUNTIME_ARN, {"AWS_REGION": REGION}, control_client=stage
    )

    assert stage.get_agent_runtime.call_count == 2  # the replay read + the poll read
    stage.update_agent_runtime.assert_called_once()
    ambient.get_agent_runtime.assert_not_called()
    ambient.update_agent_runtime.assert_not_called()


def test_set_runtime_environment_can_skip_the_poll_to_ready():
    """E36/T12 (fix round 1) — `wait_ready=False`: the update is sent and the method RETURNS, with
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

    # Exactly ONE read — the replay GET. No poll read followed it, even though the runtime is
    # UPDATING (the poll-to-READY variant above needs ≥2 on this same client).
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
    # The poll sees UPDATE_FAILED → raise the SAME error type the authorizer method
    # raises on a terminal/timeout status (a RuntimeError; the grant route converts it
    # to a 502). Initial read READY, then the poll sees UPDATE_FAILED.
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


# ===========================================================================
# Task 2 (E23/T2) — teardown: delete_identity (async, delegates to
# GraphService.delete_agent_app) + delete_runtime (sync, DeleteAgentRuntime).
# Both idempotent so a repo-teardown cascade can call them unconditionally:
# delete_runtime no-ops on a falsy ARN and swallows a not-found ClientError;
# delete_identity delegates to the T1 method (which handles blanks/404).
# ===========================================================================
def _client_error(code: str, op: str = "DeleteAgentRuntime") -> ClientError:
    """Build a botocore ClientError carrying ``code`` as its Error/Code."""
    return ClientError({"Error": {"Code": code, "Message": "x"}}, op)


def test_delete_runtime_calls_control_with_rid_from_arn():
    graph = _graph_double()
    registry = _registry_double()
    control = _control_client()
    svc = _make_service(graph=graph, registry=registry, control=control)

    svc.delete_runtime(RUNTIME_ARN)

    # RID-not-ARN: DeleteAgentRuntime gets the ARN's last "/"-segment.
    svc._control.delete_agent_runtime.assert_called_once_with(agentRuntimeId=RUNTIME_RID)


def test_delete_runtime_noop_on_blank_arn():
    graph = _graph_double()
    registry = _registry_double()
    control = _control_client()
    svc = _make_service(graph=graph, registry=registry, control=control)

    svc.delete_runtime("")  # nothing was provisioned → no control call at all.

    svc._control.delete_agent_runtime.assert_not_called()


def test_delete_runtime_idempotent_on_not_found():
    graph = _graph_double()
    registry = _registry_double()
    control = _control_client()
    control.delete_agent_runtime.side_effect = _client_error(
        "ResourceNotFoundException"
    )
    svc = _make_service(graph=graph, registry=registry, control=control)

    # A not-found is swallowed (already gone) — must NOT raise.
    svc.delete_runtime(RUNTIME_ARN)

    svc._control.delete_agent_runtime.assert_called_once_with(agentRuntimeId=RUNTIME_RID)


def test_delete_runtime_reraises_other_client_error():
    graph = _graph_double()
    registry = _registry_double()
    control = _control_client()
    control.delete_agent_runtime.side_effect = _client_error("AccessDeniedException")
    svc = _make_service(graph=graph, registry=registry, control=control)

    # Any non-not-found ClientError propagates (fail loud).
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

    svc._graph.delete_agent_app.assert_awaited_once_with(
        entra_app_id="app-guid-x", entra_sp_id="sp-obj-x"
    )


# ===========================================================================
# Task 11a (E23/T11) — delete_runtime probes get_agent_runtime FIRST so a delete
# on a GENUINELY-GONE runtime (ResourceNotFoundException) is skipped as success —
# never a stuck "failed" step. AccessDenied is AMBIGUOUS (NOT proof of gone): the
# delete is ATTEMPTED, and a persistent denial PROPAGATES (visible FAILED step,
# record kept for retry) instead of a silent skip that would orphan a live runtime.
# + runtime_exists (11b) — the reachability probe the delete-preview endpoint uses.
# ===========================================================================
def test_delete_runtime_probe_access_denied_still_attempts_delete_and_propagates():
    # AccessDenied on the probe is AMBIGUOUS (a live runtime behind an IAM/SCP/region
    # misconfig also returns it) — NOT proof of gone. delete_runtime must NOT skip: it
    # attempts the delete, and if that ALSO denies, the error PROPAGATES (a visible
    # FAILED teardown step). This is the anti-silent-orphan guarantee.
    graph = _graph_double()
    registry = _registry_double()
    control = _control_client()
    control.get_agent_runtime.side_effect = _client_error(
        "AccessDeniedException", op="GetAgentRuntime"
    )
    control.delete_agent_runtime.side_effect = _client_error("AccessDeniedException")
    svc = _make_service(graph=graph, registry=registry, control=control)

    with pytest.raises(ClientError):
        svc.delete_runtime(RUNTIME_ARN)

    svc._control.delete_agent_runtime.assert_called_once_with(agentRuntimeId=RUNTIME_RID)


def test_delete_runtime_skips_delete_when_probe_says_gone_not_found():
    graph = _graph_double()
    registry = _registry_double()
    control = _control_client()
    control.get_agent_runtime.side_effect = _client_error(
        "ResourceNotFoundException", op="GetAgentRuntime"
    )
    svc = _make_service(graph=graph, registry=registry, control=control)

    svc.delete_runtime(RUNTIME_ARN)  # must not raise

    svc._control.delete_agent_runtime.assert_not_called()


def test_delete_runtime_deletes_when_probe_says_present():
    # The probe succeeds (runtime exists) → the delete IS issued (RID-not-ARN).
    graph = _graph_double()
    registry = _registry_double()
    control = _control_client()  # get_agent_runtime returns READY by default
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
    # ONLY a genuine ResourceNotFoundException means "gone" → False.
    graph = _graph_double()
    registry = _registry_double()
    control = _control_client()
    control.get_agent_runtime.side_effect = _client_error(
        "ResourceNotFoundException", op="GetAgentRuntime"
    )
    svc = _make_service(graph=graph, registry=registry, control=control)

    assert svc.runtime_exists(RUNTIME_ARN) is False


def test_runtime_exists_raises_on_access_denied():
    # AccessDenied is AMBIGUOUS — it must NOT be inferred as gone (that would let a
    # live runtime be shown "gone"). It propagates so the caller maps it to "unknown".
    graph = _graph_double()
    registry = _registry_double()
    control = _control_client()
    control.get_agent_runtime.side_effect = _client_error(
        "AccessDeniedException", op="GetAgentRuntime"
    )
    svc = _make_service(graph=graph, registry=registry, control=control)

    with pytest.raises(ClientError):
        svc.runtime_exists(RUNTIME_ARN)


# ===========================================================================
# E28A/T1 — the authorizer and the grant env must reach EVERY per-stage runtime
# ===========================================================================
# T1b stage-scopes the runtime module's resource names, so two runtimes genuinely co-exist per
# agent. Both of these paths previously operated on the ONE stored scalar ARN, which meant the
# other runtime was born UNAUTHORIZED (defect 3) and kept stale-or-absent grant env (defect 4).
#
# WHY THE ANSWER IS "ALL OF THEM" IN BOTH CASES. An Entra identity is minted per AGENT and a
# grant is a governance fact about the AGENT — neither is a property of a stage. So a runtime
# that carries the agent's name and not the agent's authorizer is not "a dev-only concern", it
# is an ungoverned copy of a governed agent, which is the exact failure a governance product
# cannot ship. The same reasoning makes partial env injection wrong: it leaves one runtime
# acting on a revoked or superseded MCP set.

# Per-stage RIDs/ARNs for a map-bearing (post-T1b) record. Obviously-fake 12-digit account.
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

    # Both RIDs were updated — RID-not-ARN still holds for each.
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
    """No map AND no scalar ⇒ nothing to configure. Still a ValueError (the pre-E28A contract),
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
    """The other half of the widened gate: E20 pre-registration mints the identity BEFORE any
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

    def flaky(agentRuntimeId, **kwargs):  # noqa: N803 — boto3 param name
        if agentRuntimeId == DEV_RID:
            raise RuntimeError("AccessDenied")
        return {}

    control.update_agent_runtime.side_effect = flaky

    svc = _make_service(graph=graph, registry=registry, control=control)
    with pytest.raises(ProvisioningError):
        await svc.provision(agent)

    # prod was still attempted despite dev failing.
    attempted = {c.kwargs["agentRuntimeId"] for c in control.update_agent_runtime.call_args_list}
    assert attempted == {DEV_RID, PROD_RID}
    assert agent.identity_status == "failed"


# The grant-env fan-out lives in ``agent_mcp_env.rebuild_runtime_mcp_env``, NOT here: this
# service's ``set_runtime_environment`` is deliberately env-AGNOSTIC (a per-runtime MERGE
# primitive — WHAT it is called with belongs to the caller), and that module already owns the
# off-loop dispatch, which must stay one ``run_sync`` per SYNC boto3 call. Its per-stage tests
# are in ``test_agent_mcp_env.py``.


# ===========================================================================
# E28A/T1 — the ENVELOPE ROUND-TRIP (the silent-loss risk, C-A2)
# ===========================================================================
# THIS IS WHY THE PLAN ORDERS BACKEND READ SUPPORT BEFORE OR WITH THE BUILDSPEC WRITER.
# T1b's buildspec patches the STORED envelope JSON directly (`jq '.agent_arns[$s]=$arn'`), but
# every subsequent write goes through `persist_identity` / `update`, which re-serialize the
# envelope from `Agent.to_envelope()`. So if `to_envelope` omitted the key — or `from_record`
# dropped it — a map the buildspec had just written would be silently discarded by the very next
# identity persist, and no offline gate anywhere else in the suite would notice: the agent would
# simply be back to one runtime, and the E23 delete would leak the other again.
#
# `persist_identity` is exercised here rather than mocked away, because the mock used everywhere
# else in this file cannot catch a serialization gap.

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
    agent = _make_agent(
        agent_arns={"dev": DEV_ARN, "prod": PROD_ARN}, agent_arn=PROD_ARN
    )

    out = _round_trip(agent)

    assert out.agent_arns == {"dev": DEV_ARN, "prod": PROD_ARN}
    assert out.agent_arn == PROD_ARN          # the scalar survives alongside it (C-A2)
    assert out.runtime_arns() == {"dev": DEV_ARN, "prod": PROD_ARN}


def test_the_envelope_round_trip_TOLERATES_a_missing_map():
    """A pre-E28A envelope has no `agent_arns` key AT ALL. It must hydrate — a missing map is a
    legacy record, not an error — and resolve through the scalar."""
    agent = _make_agent()
    envelope = agent.to_envelope()
    del envelope["agent_arns"]          # exactly what every stored envelope looks like today

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


# ===========================================================================
# E36/T16 (research item 5B) — identity teardown also deletes the agent's OBO
# credential provider (``agp-agent-obo-{id}``).
#
# The Entra app delete cascades the client secret Entra holds; the AgentCore Token Vault
# entry survived it, one per agent ever granted an MCP. `delete_identity` is the ONE identity
# teardown both delete paths reach, so it is where the deletion belongs.
#
# The provider leg is SWALLOWED on failure ON PURPOSE: in the repo cascade the ``identity``
# line-item is BLOCKING (``project_service._NON_BLOCKING_ITEMS`` holds only ``langfuse`` and
# ``exec_role``), so a raise here would flip an already-deleted Entra app to ``failed`` and
# trap the DDB row behind a vault entry no retry of the cascade can fix. It is idempotent, so
# a later teardown/retry still reclaims it.
# ===========================================================================
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

    # By AGENT ID (the provider name is derived from the id alone), and BEFORE the Entra app
    # delete — the vault entry is the thing that outlives the app.
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

    # No raise: the repo cascade's ``identity`` item is BLOCKING, and trapping the row
    # behind a Token Vault entry is worse than reporting the app teardown that DID happen.
    await svc.delete_identity(agent)

    graph.delete_agent_app.assert_awaited_once()


@pytest.mark.asyncio
async def test_delete_identity_can_opt_out_of_the_bundled_provider_leg():
    """`include_obo_provider=False` (E36/T16 fix round 1) leaves the vault entry to a caller
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

    # The lazily-built AgentCredentialService drove the control client we injected.
    control.delete_oauth2_credential_provider.assert_called_once()
    assert control.delete_oauth2_credential_provider.call_args.kwargs["name"].endswith(
        agent.id
    )


# ===========================================================================
# E29/T6 — the platform dispatch seam in provision()
#
# provision() is now `provision_identity` (UNCHANGED, platform-neutral) followed by a
# runtime half chosen by platform. These pin the seam itself; the Databricks half's own
# behaviour lives in tests/test_databricks_identity_service.py. Everything above this
# comment is the AgentCore fence — it passes unmodified, which is the contract.
# ===========================================================================

def _databricks_agent(**overrides) -> Agent:
    """A Databricks-governed agent: a runtime HANDLE (never an ARN), Entra, DATABRICKS."""
    base = dict(
        agent_arn=None,
        platform=Platform.DATABRICKS,
        auth_type=AuthType.ENTRA,
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
    svc = AgentIdentityService(
        graph=graph,
        registry=registry,
        control_client=control,
        region=REGION,
        tenant_id=TENANT_ID,
        login_base=LOGIN_BASE,
        databricks_identity=databricks,
    )
    agent = _databricks_agent()

    await svc.provision(agent)

    # The NEUTRAL half ran identically to the AgentCore path...
    graph.create_agent_app.assert_awaited_once()
    graph.set_assignment_required.assert_awaited_once()
    graph.grant_backend_obo_consent.assert_awaited_once()
    # ...the platform half was the Databricks one...
    databricks.provision_databricks_runtime.assert_awaited_once_with(agent)
    # ...and NOTHING on the AgentCore runtime path was touched. A Databricks agent carries
    # no ARN, so an authorizer call would have been aimed at a parsed URL.
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
    svc = AgentIdentityService(
        graph=graph,
        registry=registry,
        control_client=control,
        region=REGION,
        tenant_id=TENANT_ID,
        login_base=LOGIN_BASE,
        databricks_identity=databricks,
    )

    await svc.provision(_make_agent())

    control.update_agent_runtime.assert_called_once()
    databricks.provision_databricks_runtime.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_databricks_agent_with_no_collaborator_wired_fails_loudly():
    """The dangerous alternative is a SILENT SKIP: provision_identity succeeds, no runtime
    half runs, and the agent sits at 'pending' with no error — indistinguishable from work
    in flight. It must be a persisted 'failed' instead."""
    registry = _registry_double()
    svc = _make_service(
        graph=_graph_double(), registry=registry, control=MagicMock(name="control")
    )
    agent = _databricks_agent()

    with pytest.raises(ProvisioningError):
        await svc.provision(agent)

    assert agent.identity_status == "failed"
    assert registry.persist_identity.call_args.args[0].identity_status == "failed"


@pytest.mark.asyncio
async def test_a_databricks_half_failure_is_persisted_as_failed_by_the_envelope():
    """The Databricks half owns its own persist, but `provision`'s envelope must ALSO catch —
    a raise that escaped both would leave the record at 'pending'."""
    registry = _registry_double()
    databricks = AsyncMock(name="DatabricksIdentityService")
    databricks.provision_databricks_runtime.side_effect = RuntimeError("boom")
    svc = AgentIdentityService(
        graph=_graph_double(),
        registry=registry,
        control_client=MagicMock(name="control"),
        region=REGION,
        tenant_id=TENANT_ID,
        login_base=LOGIN_BASE,
        databricks_identity=databricks,
    )
    agent = _databricks_agent()

    with pytest.raises(ProvisioningError):
        await svc.provision(agent)

    assert agent.identity_status == "failed"


@pytest.mark.asyncio
async def test_a_metadata_only_agent_still_gets_neither_runtime_half():
    """Neither gate: no ARN, no handle. The neutral half runs (E20 pre-registration), and
    both platform halves stay untouched — the deferral this seam must not break."""
    control = MagicMock(name="control")
    databricks = AsyncMock(name="DatabricksIdentityService")
    svc = AgentIdentityService(
        graph=_graph_double(),
        registry=_registry_double(),
        control_client=control,
        region=REGION,
        tenant_id=TENANT_ID,
        login_base=LOGIN_BASE,
        databricks_identity=databricks,
    )

    await svc.provision(_make_agent(agent_arn=None, platform=Platform.AWS_BEDROCK))

    control.update_agent_runtime.assert_not_called()
    databricks.provision_databricks_runtime.assert_not_awaited()


# ===========================================================================
# E29/T10 — the runtime_status platform dispatch
#
# `runtime_status` is the ONLY producer of RuntimeStatus, and it now has two producers behind
# it. These pin the SEAM: a Databricks-governed agent is answered by the Databricks producer
# with ZERO boto3, and everything else keeps today's body byte for byte. The Databricks
# producer's own mapping/redaction behaviour lives in tests/test_databricks_identity_service.py.
# ===========================================================================

def _status_service(*, control, databricks=None) -> AgentIdentityService:
    return AgentIdentityService(
        graph=_graph_double(),
        registry=_registry_double(),
        control_client=control,
        region=REGION,
        tenant_id=TENANT_ID,
        login_base=LOGIN_BASE,
        databricks_identity=databricks,
    )


def test_runtime_status_dispatches_a_databricks_agent_to_the_databricks_producer():
    from models.agent import RuntimeStatus

    control = _control_client()
    databricks = MagicMock(name="DatabricksIdentityService")
    databricks.runtime_status.return_value = RuntimeStatus(
        agent_id="rec-abc123", stage="dev", status="ready", checked_at="2026-08-06T00:00:00Z"
    )
    svc = _status_service(control=control, databricks=databricks)
    agent = _databricks_agent()

    result = svc.runtime_status(agent, "dev")

    assert result.status == "ready"
    databricks.runtime_status.assert_called_once_with(agent, stage="dev")
    # ZERO boto3: a Databricks agent carries no ARN, and asking AgentCore about one would
    # produce a NotFound that reads as "not_deployed" — a wrong answer wearing a confident one.
    control.get_agent_runtime.assert_not_called()


def test_runtime_status_of_a_databricks_agent_carries_the_env_field_as_none():
    """THE E36/T12 × E29/T10 SEAM. `GET /agents/{id}/runtime` dereferences
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
    svc = _status_service(control=control, databricks=databricks)

    result = svc.runtime_status(_databricks_agent(), "dev")

    assert result.environment_variables is None
    # The degraded arms ride the same carrier — no producer path may lack the field.
    unwired = _status_service(control=_control_client(), databricks=None)
    assert unwired.runtime_status(_databricks_agent(), "dev").environment_variables is None


def test_runtime_status_keeps_the_agentcore_path_on_boto3():
    """The fence. An AgentCore agent must still be answered by the boto3 producer even when a
    Databricks collaborator is wired."""
    control = _control_client()
    databricks = MagicMock(name="DatabricksIdentityService")
    svc = _status_service(control=control, databricks=databricks)

    result = svc.runtime_status(_make_agent())

    assert result.status == "ready"
    control.get_agent_runtime.assert_called_once_with(agentRuntimeId=RUNTIME_RID)
    databricks.runtime_status.assert_not_called()


def test_runtime_status_of_a_databricks_agent_is_unknown_when_no_producer_is_wired():
    """A missing collaborator is not a silent "not_deployed". Reporting nothing-deployed for an
    agent we simply cannot ask about would be the same conflation `unknown` exists to prevent —
    and it is a READ, so it still may not raise."""
    control = _control_client()
    svc = _status_service(control=control, databricks=None)

    result = svc.runtime_status(_databricks_agent())

    assert result.status == "unknown"
    assert result.detail
    control.get_agent_runtime.assert_not_called()


def test_a_raising_databricks_producer_degrades_to_unknown():
    """Defense in depth: the producer promises never to raise, but this seam is what the route
    calls, and a 5xx here blanks the fleet view."""
    control = _control_client()
    databricks = MagicMock(name="DatabricksIdentityService")
    databricks.runtime_status.side_effect = RuntimeError("boom")
    svc = _status_service(control=control, databricks=databricks)

    result = svc.runtime_status(_databricks_agent())

    assert result.status == "unknown"
    assert "boom" not in (result.detail or "")


def test_a_metadata_only_agent_is_still_answered_not_deployed_by_the_default_arm():
    """Neither gate. The dispatch must not be an if/elif chain that falls off the end — a
    metadata-only record (the ~18 seed agents) answers `not_deployed` locally, as it does today."""
    control = _control_client()
    databricks = MagicMock(name="DatabricksIdentityService")
    svc = _status_service(control=control, databricks=databricks)

    result = svc.runtime_status(_make_agent(agent_arn=None, platform=Platform.AWS_BEDROCK))

    assert result.status == "not_deployed"
    databricks.runtime_status.assert_not_called()
    control.get_agent_runtime.assert_not_called()
