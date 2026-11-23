import asyncio
import json
from datetime import datetime

import pytest

from models.agent import Agent, LifecycleState
from models.mcp_server import McpServer
from services import agent_mcp_env

_DT = datetime(2026, 6, 17, 12, 0, 0)


def _mcp(id, name, aud, url):
    return McpServer(
        id=id,
        name=name,
        entra_app_audience=aud,
        gateway_url=url,
        lifecycle_state=LifecycleState.APPROVED,
        created_at=_DT,
        updated_at=_DT,
    )


def test_build_env_lists_all_mcps_with_provider():
    mcps = [
        _mcp("CZR2", "Contact Center", "api://agp-mcp-CZR2", "https://cc/mcp"),
        _mcp("FN01", "FNOL", "api://agp-mcp-FN01", "https://fnol/mcp"),
    ]
    env = agent_mcp_env.build_runtime_mcp_env(mcps, "agp-agent-obo-ABC")
    assert env["CREDENTIAL_PROVIDER_NAME"] == "agp-agent-obo-ABC"
    servers = json.loads(env["MCP_SERVERS"])
    assert [s["id"] for s in servers] == ["CZR2", "FN01"]
    assert servers[0]["audience"] == "api://agp-mcp-CZR2"
    assert servers[0]["gateway_url"] == "https://cc/mcp"
    assert servers[0]["label"] == "contact_center"
    assert env["MCP_AUDIENCE"] == ""
    assert env["MCP_GATEWAY_URL"] == ""


def test_build_env_dedupes_colliding_labels():
    mcps = [
        _mcp("AAAAAA11", "Support", "api://agp-mcp-AAAAAA11", "https://a/mcp"),
        _mcp("BBBBBB22", "Support", "api://agp-mcp-BBBBBB22", "https://b/mcp"),
    ]
    env = agent_mcp_env.build_runtime_mcp_env(mcps, "p")
    labels = [s["label"] for s in json.loads(env["MCP_SERVERS"])]
    assert len(set(labels)) == 2
    assert labels[0] == "support"
    assert labels[1].startswith("support_")


def test_build_env_dedupes_three_way_shared_prefix_collision():
    mcps = [
        _mcp("AAAAAA01", "Support", "api://agp-mcp-AA01", "https://a/mcp"),
        _mcp("AAAAAA02", "Support", "api://agp-mcp-AA02", "https://b/mcp"),
        _mcp("AAAAAA03", "Support", "api://agp-mcp-AA03", "https://c/mcp"),
    ]
    env = agent_mcp_env.build_runtime_mcp_env(mcps, "p")
    labels = [s["label"] for s in json.loads(env["MCP_SERVERS"])]
    assert len(set(labels)) == 3


def test_build_env_empty_set_is_prompt_only():
    env = agent_mcp_env.build_runtime_mcp_env([], None)
    assert env["MCP_SERVERS"] == "[]"
    assert "CREDENTIAL_PROVIDER_NAME" not in env
    assert env["MCP_AUDIENCE"] == "" and env["MCP_GATEWAY_URL"] == ""


@pytest.mark.asyncio
async def test_rebuild_resolves_registry_and_pushes_env(monkeypatch):
    agent = Agent(
        id="ImD",
        name="CC",
        mcp_server_ids=["CZR2", "GONE"],
        oauth2_credential_provider_name="agp-agent-obo-ImD",
        agent_arn="arn:…:runtime/cc-x",
        lifecycle_state=LifecycleState.APPROVED,
        created_at=_DT,
        updated_at=_DT,
    )
    pushed = {}

    class FakeMcpReg:
        def get(self, mid):
            if mid == "CZR2":
                return _mcp("CZR2", "Contact Center", "api://agp-mcp-CZR2", "https://cc/mcp")
            return None

    class FakeIdentity:
        def set_runtime_environment(self, arn, env):
            pushed["arn"] = arn
            pushed["env"] = env

    monkeypatch.setattr(agent_mcp_env, "_get_mcp_registry", lambda: FakeMcpReg())
    monkeypatch.setattr(agent_mcp_env, "_get_agent_identity_service", lambda: FakeIdentity())

    await agent_mcp_env.rebuild_runtime_mcp_env(agent)

    assert pushed["arn"] == "arn:…:runtime/cc-x"
    servers = json.loads(pushed["env"]["MCP_SERVERS"])
    assert [s["id"] for s in servers] == ["CZR2"]


@pytest.mark.asyncio
async def test_rebuild_noop_without_agent_arn(monkeypatch):
    agent = Agent(
        id="meta",
        name="m",
        mcp_server_ids=["CZR2"],
        agent_arn=None,
        lifecycle_state=LifecycleState.APPROVED,
        created_at=_DT,
        updated_at=_DT,
    )
    called = {"set": False}

    class FakeIdentity:
        def set_runtime_environment(self, arn, env):
            called["set"] = True

    monkeypatch.setattr(
        agent_mcp_env, "_get_mcp_registry", lambda: type("R", (), {"get": lambda self, m: None})()
    )
    monkeypatch.setattr(agent_mcp_env, "_get_agent_identity_service", lambda: FakeIdentity())

    await agent_mcp_env.rebuild_runtime_mcp_env(agent)
    assert called["set"] is False


@pytest.mark.asyncio
async def test_rebuild_pushes_to_every_per_stage_runtime(monkeypatch):
    agent = Agent(
        id="ImD",
        name="CC",
        mcp_server_ids=["CZR2"],
        oauth2_credential_provider_name="agp-agent-obo-ImD",
        agent_arn="arn:…:runtime/cc_prod",
        agent_arns={"dev": "arn:…:runtime/cc_dev", "prod": "arn:…:runtime/cc_prod"},
        lifecycle_state=LifecycleState.APPROVED,
        created_at=_DT,
        updated_at=_DT,
    )
    pushed = []

    class FakeMcpReg:
        def get(self, mid):
            return _mcp("CZR2", "Contact Center", "api://agp-mcp-CZR2", "https://cc/mcp")

    class FakeIdentity:
        def set_runtime_environment(self, arn, env):
            pushed.append((arn, env))

    monkeypatch.setattr(agent_mcp_env, "_get_mcp_registry", lambda: FakeMcpReg())
    monkeypatch.setattr(agent_mcp_env, "_get_agent_identity_service", lambda: FakeIdentity())

    await agent_mcp_env.rebuild_runtime_mcp_env(agent)

    assert sorted(arn for arn, _ in pushed) == ["arn:…:runtime/cc_dev", "arn:…:runtime/cc_prod"]
    for _, env in pushed:
        assert json.loads(env["MCP_SERVERS"])[0]["id"] == "CZR2"


@pytest.mark.asyncio
async def test_rebuild_injects_the_others_then_raises_when_one_runtime_fails(monkeypatch):
    """Attempt-all-then-raise. A dev runtime that cannot be reached must not stop prod from
    being brought into line — but the grant route turns the raise into a fail-loud 5xx, because
    a successful grant must imply a fully-wired agent."""
    agent = Agent(
        id="ImD",
        name="CC",
        mcp_server_ids=[],
        agent_arns={"dev": "arn:…:runtime/cc_dev", "prod": "arn:…:runtime/cc_prod"},
        lifecycle_state=LifecycleState.APPROVED,
        created_at=_DT,
        updated_at=_DT,
    )
    pushed = []

    class FakeIdentity:
        def set_runtime_environment(self, arn, env):
            if arn.endswith("cc_dev"):
                raise RuntimeError("AccessDenied")
            pushed.append(arn)

    monkeypatch.setattr(
        agent_mcp_env, "_get_mcp_registry", lambda: type("R", (), {"get": lambda self, m: None})()
    )
    monkeypatch.setattr(agent_mcp_env, "_get_agent_identity_service", lambda: FakeIdentity())

    with pytest.raises(RuntimeError):
        await agent_mcp_env.rebuild_runtime_mcp_env(agent)

    assert pushed == ["arn:…:runtime/cc_prod"]


@pytest.mark.asyncio
async def test_rebuild_still_pushes_for_a_legacy_scalar_only_record(monkeypatch):
    """A legacy record (no map) must still get its one injection — every agent in the registry
    is this shape today."""
    agent = Agent(
        id="leg",
        name="CC",
        mcp_server_ids=[],
        agent_arn="arn:…:runtime/cc-x",
        lifecycle_state=LifecycleState.APPROVED,
        created_at=_DT,
        updated_at=_DT,
    )
    calls = []

    class FakeIdentity:
        def set_runtime_environment(self, arn, env):
            calls.append(arn)

    monkeypatch.setattr(
        agent_mcp_env, "_get_mcp_registry", lambda: type("R", (), {"get": lambda self, m: None})()
    )
    monkeypatch.setattr(agent_mcp_env, "_get_agent_identity_service", lambda: FakeIdentity())

    await agent_mcp_env.rebuild_runtime_mcp_env(agent)

    assert calls == ["arn:…:runtime/cc-x"]


@pytest.mark.asyncio
async def test_rebuild_noop_when_the_agent_has_no_runtime_at_all(monkeypatch):
    """No map AND no scalar (metadata-only / pre-registration) stays a no-op, unchanged."""
    agent = Agent(
        id="meta",
        name="m",
        mcp_server_ids=[],
        agent_arn=None,
        lifecycle_state=LifecycleState.APPROVED,
        created_at=_DT,
        updated_at=_DT,
    )
    called = {"set": False}

    class FakeIdentity:
        def set_runtime_environment(self, arn, env):
            called["set"] = True

    monkeypatch.setattr(
        agent_mcp_env, "_get_mcp_registry", lambda: type("R", (), {"get": lambda self, m: None})()
    )
    monkeypatch.setattr(agent_mcp_env, "_get_agent_identity_service", lambda: FakeIdentity())

    await agent_mcp_env.rebuild_runtime_mcp_env(agent)
    assert called["set"] is False


def _granted_agent(**overrides):
    return Agent(
        id=overrides.pop("id", "ImD"),
        name="CC",
        mcp_server_ids=overrides.pop("mcp_server_ids", ["CZR2"]),
        agent_arn="arn:…:runtime/cc-x",
        lifecycle_state=LifecycleState.APPROVED,
        created_at=_DT,
        updated_at=_DT,
        **overrides,
    )


def _spy_rebuild(monkeypatch, *, boom: Exception | None = None):
    """Replace the rebuild with an async spy — reconcile's contract is DELEGATION, and the
    rebuild itself is already pinned by the tests above. Records `wait_ready` alongside the agent,
    because HOW the reconcile delegates is part of that contract."""
    seen: list = []

    async def _fake(agent, *, wait_ready: bool = True):
        seen.append((agent, wait_ready))
        if boom is not None:
            raise boom

    monkeypatch.setattr(agent_mcp_env, "rebuild_runtime_mcp_env", _fake)
    return seen


def _spy_set_env(monkeypatch):
    """Let the REAL rebuild run and record every `set_runtime_environment` it dispatches, with the
    `wait_ready` keyword it was (or was not) given — the poll-to-READY switch."""
    calls: list[dict] = []

    class FakeIdentity:
        def set_runtime_environment(self, arn, env, *, wait_ready: bool = True):
            calls.append({"arn": arn, "wait_ready": wait_ready})

    monkeypatch.setattr(
        agent_mcp_env, "_get_mcp_registry", lambda: type("R", (), {"get": lambda self, m: None})()
    )
    monkeypatch.setattr(agent_mcp_env, "_get_agent_identity_service", lambda: FakeIdentity())
    return calls


@pytest.mark.asyncio
async def test_reconcile_rebuilds_when_grants_exist_but_the_runtime_lost_mcp_servers(monkeypatch):
    """THE defect: the record says the agent is wired to an MCP, the live runtime has no
    `MCP_SERVERS` — a replacement wiped it. Reconcile re-applies the desired state."""
    agent = _granted_agent()
    seen = _spy_rebuild(monkeypatch)

    applied = await agent_mcp_env.reconcile_runtime_mcp_env(
        agent, {"CREDENTIAL_PROVIDER_NAME": "agp-agent-obo-ImD", "OTHER": "x"}
    )

    assert applied is True
    assert [a.id for a, _ in seen] == ["ImD"]


@pytest.mark.asyncio
async def test_reconcile_heals_a_runtime_whose_env_is_entirely_empty(monkeypatch):
    """A replacement typically leaves NO env at all, and an empty env from a read that REACHED the
    runtime is still evidence — `{}` is what a wipe looks like, and it must heal exactly like a
    partial env does."""
    agent = _granted_agent()
    seen = _spy_rebuild(monkeypatch)

    applied = await agent_mcp_env.reconcile_runtime_mcp_env(agent, {})

    assert applied is True
    assert len(seen) == 1


@pytest.mark.asyncio
async def test_reconcile_does_not_heal_when_the_read_holds_no_evidence(monkeypatch):
    """`live_env is None` means the read never got an answer about the env (not-deployed, the
    runtime gone, Throttling, AccessDenied, an unreachable endpoint). A wipe is only DETECTABLE
    from a read that reached the runtime, so this is NOT a trigger: healing off it never converged
    — it answered a throttle with an extra write to the same throttled API and re-attempted the
    same doomed write on every read of an unreachable runtime. Nothing is lost, because
    reconcile-on-read's premise is that another read is coming."""
    agent = _granted_agent()
    seen = _spy_rebuild(monkeypatch)

    applied = await agent_mcp_env.reconcile_runtime_mcp_env(agent, None)

    assert applied is False
    assert seen == []


@pytest.mark.asyncio
async def test_reconcile_is_a_noop_for_an_agent_with_no_grants(monkeypatch):
    """No grants ⇒ nothing to re-apply, EVER — the empty `MCP_SERVERS` the rebuild would push
    is not a fact the platform needs to assert on a read, and this is what keeps a write off
    the GET path for almost every agent."""
    agent = _granted_agent(mcp_server_ids=[])
    seen = _spy_rebuild(monkeypatch)

    applied = await agent_mcp_env.reconcile_runtime_mcp_env(agent, {})

    assert applied is False
    assert seen == []


@pytest.mark.asyncio
async def test_reconcile_is_a_noop_when_the_runtime_is_already_wired(monkeypatch):
    """`MCP_SERVERS` present ⇒ the runtime already carries the injection, so a read must not
    write. KEY PRESENCE is the whole test: the VALUE is desired state the grant flow owns, and
    diffing it here would make a read route the arbiter of a governance decision."""
    agent = _granted_agent()
    seen = _spy_rebuild(monkeypatch)

    applied = await agent_mcp_env.reconcile_runtime_mcp_env(agent, {"MCP_SERVERS": "[]"})

    assert applied is False
    assert seen == []


@pytest.mark.asyncio
async def test_reconcile_swallows_a_rebuild_failure(monkeypatch):
    """The rebuild RAISES on an unreachable runtime (attempt-all-then-raise) — correct for the
    grant route, which must fail loud. Here it must NEVER propagate: this is a best-effort heal
    hanging off a read surface that is contractually 200-or-a-status, and a cross-account tenant
    runtime raises on EVERY attempt , which would otherwise 5xx the fleet view."""
    agent = _granted_agent()
    seen = _spy_rebuild(monkeypatch, boom=RuntimeError("AccessDenied"))

    applied = await agent_mcp_env.reconcile_runtime_mcp_env(agent, {})

    assert applied is False
    assert len(seen) == 1


@pytest.mark.asyncio
async def test_reconcile_does_not_wait_for_the_runtime_to_come_back_ready(monkeypatch):
    """The heal runs inside a GET handler, on an `anyio` worker from a pool of ~40 shared by every
    sync boto3 call in the backend. `set_runtime_environment`'s poll sleeps up to 300 s PER runtime,
    so waiting would park that thread for minutes per unhealed runtime — and there is nothing to
    wait FOR: the next read observes whether the heal landed, which is what this feature is."""
    agent = _granted_agent()
    calls = _spy_set_env(monkeypatch)

    applied = await agent_mcp_env.reconcile_runtime_mcp_env(agent, {})

    assert applied is True
    assert [c["wait_ready"] for c in calls] == [False]


@pytest.mark.asyncio
async def test_the_grant_path_still_polls_the_runtime_to_ready(monkeypatch):
    """The other half of that switch: `wait_ready` DEFAULTS to True, so grant and revoke are
    unchanged. They report a governance fact to an operator and must fail loud rather than claim a
    wiring they never saw converge."""
    agent = _granted_agent()
    calls = _spy_set_env(monkeypatch)

    await agent_mcp_env.rebuild_runtime_mcp_env(agent)

    assert [c["wait_ready"] for c in calls] == [True]


@pytest.mark.asyncio
async def test_reconcile_drops_a_second_trigger_while_one_is_in_flight(monkeypatch):
    """Self-collision, guaranteed on a single page load: the repository page fires the agent-level
    probe AND one probe per stage concurrently, each sees the same missing key, and each rebuild
    full-replace-PUTs EVERY runtime the agent owns. The duplicates are dropped, not queued — the
    heal already running re-derives the same desired state from the registry."""
    agent = _granted_agent()
    started = asyncio.Event()
    release = asyncio.Event()
    seen: list = []

    async def _slow(agent, *, wait_ready: bool = True):
        seen.append(agent)
        started.set()
        await release.wait()

    monkeypatch.setattr(agent_mcp_env, "rebuild_runtime_mcp_env", _slow)

    first = asyncio.ensure_future(agent_mcp_env.reconcile_runtime_mcp_env(agent, {}))
    await started.wait()

    assert await agent_mcp_env.reconcile_runtime_mcp_env(agent, {}) is False
    assert len(seen) == 1

    release.set()
    assert await first is True

    release.clear()
    started.clear()
    later = asyncio.ensure_future(agent_mcp_env.reconcile_runtime_mcp_env(agent, {}))
    await started.wait()
    release.set()
    assert await later is True
    assert len(seen) == 2


@pytest.mark.asyncio
async def test_the_in_flight_guard_is_per_agent_not_global(monkeypatch):
    """It must not turn concurrent reads of DIFFERENT agents into a queue — the collision it exists
    to stop is one agent's own 1+N probes."""
    blocked = _granted_agent(id="ImD")
    other = _granted_agent(id="Oth")
    started = asyncio.Event()
    release = asyncio.Event()
    seen: list = []

    async def _slow(agent, *, wait_ready: bool = True):
        seen.append(agent.id)
        if agent.id == "ImD":
            started.set()
            await release.wait()

    monkeypatch.setattr(agent_mcp_env, "rebuild_runtime_mcp_env", _slow)

    first = asyncio.ensure_future(agent_mcp_env.reconcile_runtime_mcp_env(blocked, {}))
    await started.wait()

    assert await agent_mcp_env.reconcile_runtime_mcp_env(other, {}) is True

    release.set()
    assert await first is True
    assert sorted(seen) == ["ImD", "Oth"]


def _registry_of(*ids):
    class R:
        def get(self, mid):
            return (
                _mcp(mid, f"srv {mid}", f"api://agp-mcp-{mid}", f"https://{mid}/mcp") if mid in ids else None
            )

    return R()


def _capture_env(monkeypatch, *ids):
    pushed: list[dict] = []

    class FakeIdentity:
        def set_runtime_environment(self, arn, env, *, wait_ready: bool = True):
            pushed.append(env)

    monkeypatch.setattr(agent_mcp_env, "_get_mcp_registry", lambda: _registry_of(*ids))
    monkeypatch.setattr(agent_mcp_env, "_get_agent_identity_service", lambda: FakeIdentity())
    return pushed


def test_runtime_mcp_ids_is_granted_first_then_declared_deduped():
    agent = _granted_agent(mcp_server_ids=["G1", "D2"], declared_mcp_server_ids=["D1", "D2", "D1"])
    assert agent_mcp_env.runtime_mcp_ids(agent) == ["G1", "D2", "D1"]


def test_runtime_mcp_ids_tolerates_an_agent_without_the_field():
    from types import SimpleNamespace

    assert agent_mcp_env.runtime_mcp_ids(SimpleNamespace(mcp_server_ids=["G1"])) == ["G1"]
    assert agent_mcp_env.runtime_mcp_ids(SimpleNamespace(mcp_server_ids=[])) == []


@pytest.mark.asyncio
async def test_rebuild_wires_declared_and_granted_servers(monkeypatch):
    pushed = _capture_env(monkeypatch, "G1", "D1")
    agent = _granted_agent(mcp_server_ids=["G1"], declared_mcp_server_ids=["D1"])

    await agent_mcp_env.rebuild_runtime_mcp_env(agent)

    assert [s["id"] for s in json.loads(pushed[0]["MCP_SERVERS"])] == ["G1", "D1"]


@pytest.mark.asyncio
async def test_rebuild_with_neither_list_pushes_an_empty_server_list(monkeypatch):
    pushed = _capture_env(monkeypatch)
    agent = _granted_agent(mcp_server_ids=[])

    await agent_mcp_env.rebuild_runtime_mcp_env(agent)

    assert pushed[0]["MCP_SERVERS"] == "[]"


@pytest.mark.asyncio
async def test_reconcile_triggers_for_a_declared_only_agent(monkeypatch):
    agent = _granted_agent(mcp_server_ids=[], declared_mcp_server_ids=["D1"])
    seen = _spy_rebuild(monkeypatch)

    applied = await agent_mcp_env.reconcile_runtime_mcp_env(agent, {})

    assert applied is True
    assert [a.id for a, _ in seen] == ["ImD"]


@pytest.mark.asyncio
async def test_a_revoke_of_a_declared_server_keeps_it_wired(monkeypatch):
    """The revoke removes only the GRANT (`mcp_server_ids`); the rebuilt env still carries the
    declared server, so the runtime tries it and Entra refuses it (the Contain story)."""
    from unittest.mock import AsyncMock, MagicMock, patch

    from services.agent_mcp_grant import revoke_agent_mcp_grant

    pushed = _capture_env(monkeypatch, "D1")
    agent = _granted_agent(mcp_server_ids=["D1"], declared_mcp_server_ids=["D1"], entra_sp_id="agent-sp")
    mcp = _mcp("D1", "srv D1", "api://agp-mcp-D1", "https://D1/mcp")
    mcp.entra_sp_id = "mcp-sp"

    graph = MagicMock()
    graph.list_assignments = AsyncMock(side_effect=[[{"id": "as-1", "principalId": "agent-sp"}], []])
    graph.revoke_app_role = AsyncMock(return_value=None)
    graph.revoke_agent_obo_consent = AsyncMock(return_value=None)
    agents = MagicMock()
    agents.list.return_value = [agent]

    with (
        patch("api.routes.mcp_server_grants.get_mcp_graph_service", return_value=graph),
        patch("api.routes.agents.get_service", return_value=agents),
    ):
        await revoke_agent_mcp_grant(mcp, "as-1")

    assert agent.mcp_server_ids == []
    agents.persist_identity.assert_called_once_with(agent)
    graph.revoke_agent_obo_consent.assert_awaited_once_with("agent-sp", "mcp-sp")
    assert [s["id"] for s in json.loads(pushed[-1]["MCP_SERVERS"])] == ["D1"]
