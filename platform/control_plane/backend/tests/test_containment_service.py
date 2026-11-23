"""ContainmentService: contain / run_contain / restore / run_restore / view.

Fakes for the cedar service, the runtime levers and the registries; the stores run in-memory.
"""

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest
from botocore.exceptions import ClientError

from core.rbac import Principal, Role
from core.state_store import StateStore
from models.containment import STEP_ORDER, ContainmentState, Signal
from models.mcp_server import Kind
from services.containment_service import ContainmentConflict, ContainmentService
from services.containment_store import AuditStore, ContainmentStore
from services.mcp_cedar_service import McpCedarError

pytestmark = pytest.mark.asyncio

RT = "arn:aws:bedrock-agentcore:us-east-1:000000000000:runtime/fraud_triage_dev-AbC123"
GW1 = "arn:aws:bedrock-agentcore:us-east-1:000000000000:gateway/claims-1"
GW2 = "arn:aws:bedrock-agentcore:us-east-1:000000000000:gateway/fnol-2"


def _agent(**kw):
    base: dict[str, Any] = dict(
        id="agent-1",
        name="Fraud triage",
        entra_app_id="app-123",
        tenant_id=None,
        mcp_server_ids=["mcp-gw1", "mcp-gw2", "mcp-std"],
        arns={"dev": RT},
    )
    base.update(kw)
    arns = base.pop("arns")
    return SimpleNamespace(**base, runtime_arns=lambda: dict(arns))


MCPS = {
    "mcp-gw1": SimpleNamespace(id="mcp-gw1", kind=Kind.GATEWAY, gateway_arn=GW1),
    "mcp-gw2": SimpleNamespace(id="mcp-gw2", kind=Kind.GATEWAY, gateway_arn=GW2),
    "mcp-std": SimpleNamespace(id="mcp-std", kind=Kind.STANDARD, gateway_arn=None),
}


class FakeCedar:
    def __init__(self, results=None):
        self.results = results or {}  # mcp id -> dict | Exception
        self.forbids = []
        self.deleted = []
        self.delete_raises: Exception | None = None
        self.delete_fail = {}  # mcp id -> Exception, per gateway

    async def add_agent_forbid(self, mcp, *, agent_app_id, agent_label):
        self.forbids.append((mcp.id, agent_app_id, agent_label))
        r = self.results.get(mcp.id, {"status": "ok", "policy_id": f"pol-{mcp.id}"})
        if isinstance(r, Exception):
            raise r
        return r

    async def delete_policy(self, mcp, policy_id):
        if self.delete_raises:
            raise self.delete_raises
        if mcp.id in self.delete_fail:
            raise self.delete_fail[mcp.id]
        self.deleted.append((mcp.id, policy_id))


class FakeLevers:
    def __init__(self, deny=None):
        self.deny = deny or {"status": "ok", "before": None}
        self.denied = []
        self.restored = []

    def deny_invokes(self, agent, runtime_arn):
        self.denied.append(runtime_arn)
        return dict(self.deny)

    def restore_invokes(self, agent, runtime_arn, before):
        self.restored.append((runtime_arn, before))
        return {"status": "ok"}


ALICE = Principal(oid="oid-alice", email="alice@example.com", role=Role.ADMIN, raw_claims={})
BOB = Principal(oid="oid-bob", email="bob@example.com", role=Role.ADMIN, raw_claims={})


def _svc(agent=None, cedar=None, levers=None):
    agent = agent or _agent()
    store = ContainmentStore(table_name="")
    audit = AuditStore(table_name="")
    svc = ContainmentService(
        store=store,
        audit=audit,
        cedar=cedar or FakeCedar(),
        levers=levers or FakeLevers(),
        agents_loader=lambda aid: agent if aid == agent.id else None,
        mcp_loader=MCPS.get,
        now=lambda: datetime(2026, 10, 13, 9, 0, 0, tzinfo=UTC),
    )
    return svc, store, audit, agent


def _sig(detected_at, span_id="s1"):
    return Signal(
        agent_id="agent-1",
        session_id="sess",
        trace_id=None,
        span_id=span_id,
        tool="send_email",
        label="FAIL",
        explanation="leaked",
        span_end=detected_at,
        detected_at=detected_at,
    )


def _state(store: ContainmentStore) -> ContainmentState:
    state = store.get_state("agent-1")
    assert state is not None
    return state


def _steps(state):
    return {s.id: s for s in state.steps}


async def test_contain_short_reason_raises_value_error():
    svc, *_ = _svc()
    with pytest.raises(ValueError):
        await svc.contain(_agent(), ALICE, "short")


async def test_contain_writes_running_state_and_requested_audit():
    svc, store, audit, agent = _svc()
    store.put_signal(_sig("2026-10-13T08:00:00+00:00", "a"))
    store.put_signal(_sig("2026-10-13T08:30:00+00:00", "b"))
    state = await svc.contain(agent, ALICE, "tool misuse seen")
    assert (state.state, state.job, state.by_oid, state.by_name, state.reason) == (
        "CONTAINED",
        "running",
        "oid-alice",
        "alice@example.com",
        "tool misuse seen",
    )
    assert [s.id for s in state.steps] == list(STEP_ORDER)
    assert all(s.status == "pending" for s in state.steps)
    assert state.signal_at == "2026-10-13T08:30:00+00:00"
    assert _state(store).job == "running"
    [ev] = audit.list("agent-1")
    assert (ev.action, ev.result, ev.step, ev.actor_oid) == ("contain", "requested", None, "oid-alice")


async def test_contain_twice_conflicts():
    svc, _, _, agent = _svc()
    await svc.contain(agent, ALICE, "tool misuse seen")
    with pytest.raises(ContainmentConflict) as e:
        await svc.contain(agent, BOB, "tool misuse seen")
    assert e.value.detail == "agent is already contained"


async def test_contain_by_email_when_no_oid():
    svc, _, _, agent = _svc()
    p = Principal(oid=None, email="dev@example.com", role=Role.OPERATOR, raw_claims={})
    state = await svc.contain(agent, p, "tool misuse seen")
    assert state.by_oid == "dev@example.com"


async def test_run_contain_all_ok():
    cedar, levers = FakeCedar(), FakeLevers(deny={"status": "ok", "before": '{"old": 1}'})
    svc, store, audit, agent = _svc(cedar=cedar, levers=levers)
    await svc.contain(agent, ALICE, "tool misuse seen")
    await svc.run_contain("agent-1")
    state = _state(store)
    steps = _steps(state)
    assert [s.status for s in state.steps] == ["ok", "ok", "ok"]
    assert all(s.at for s in state.steps)
    assert state.job == "contained" and state.state == "CONTAINED"
    # Only GATEWAY-kind MCPs get a forbid, with the agent's app id + label.
    assert cedar.forbids == [("mcp-gw1", "app-123", "Fraud triage"), ("mcp-gw2", "app-123", "Fraud triage")]
    assert state.policies == [
        {"mcp_id": "mcp-gw1", "policy_id": "pol-mcp-gw1"},
        {"mcp_id": "mcp-gw2", "policy_id": "pol-mcp-gw2"},
    ]
    assert steps["cedar-forbid"].native_ref is not None
    assert "pol-mcp-gw1" in steps["cedar-forbid"].native_ref
    assert levers.denied == [RT]
    assert state.runtime_policy_before == {RT: '{"old": 1}'}
    assert steps["runtime-deny"].native_ref == RT
    rows = audit.list("agent-1")
    assert [(r.action, r.step, r.result) for r in rows] == [
        ("contain", None, "requested"),
        ("contain", "refuse-invokes", "ok"),
        ("contain", "cedar-forbid", "ok"),
        ("contain", "runtime-deny", "ok"),
    ]
    # Each audit row has its own `at`, strictly increasing.
    ats = [r.at for r in rows]
    assert ats == sorted(ats) and len(set(ats)) == len(ats)


async def test_run_contain_gateway_error_caught_per_gateway():
    cedar = FakeCedar(
        results={"mcp-gw1": McpCedarError("agent forbid create failed", "policy_create_failed")}
    )
    svc, store, _, agent = _svc(cedar=cedar)
    await svc.contain(agent, ALICE, "tool misuse seen")
    await svc.run_contain("agent-1")
    state = _state(store)
    step = _steps(state)["cedar-forbid"]
    assert step.status == "ok"  # one ok is enough
    assert "mcp-gw1" in step.detail and "failed" in step.detail and "mcp-gw2" in step.detail
    assert state.policies == [{"mcp_id": "mcp-gw2", "policy_id": "pol-mcp-gw2"}]


async def test_run_contain_all_gateways_skipped_and_runtime_failed_is_failed_job():
    skip = {"status": "skipped", "detail": "gateway not enforcing Cedar"}
    cedar = FakeCedar(results={"mcp-gw1": skip, "mcp-gw2": skip})
    levers = FakeLevers(deny={"status": "failed", "detail": "AccessDenied: nope"})
    svc, store, audit, agent = _svc(cedar=cedar, levers=levers)
    await svc.contain(agent, ALICE, "tool misuse seen")
    await svc.run_contain("agent-1")
    state = _state(store)
    steps = _steps(state)
    assert steps["refuse-invokes"].status == "ok"
    assert steps["cedar-forbid"].status == "skipped"
    assert steps["runtime-deny"].status == "failed"  # still ran after cedar
    assert "AccessDenied" in steps["runtime-deny"].detail
    assert state.job == "failed" and state.state == "CONTAINED"
    assert [r.result for r in audit.list("agent-1")][-2:] == ["skipped", "failed"]


async def test_run_contain_all_gateways_failed_is_failed_step():
    err = RuntimeError("boom")
    cedar = FakeCedar(results={"mcp-gw1": err, "mcp-gw2": err})
    svc, store, _, agent = _svc(cedar=cedar)
    await svc.contain(agent, ALICE, "tool misuse seen")
    await svc.run_contain("agent-1")
    state = _state(store)
    assert _steps(state)["cedar-forbid"].status == "failed"
    assert state.job == "contained"  # runtime-deny ok


async def test_run_contain_harness_runtime_skipped():
    agent = _agent(mcp_server_ids=["mcp-gw1"])
    levers = FakeLevers(deny={"status": "skipped", "detail": "not a runtime"})
    svc, store, _, _ = _svc(agent=agent, levers=levers)
    await svc.contain(agent, ALICE, "tool misuse seen")
    await svc.run_contain("agent-1")
    state = _state(store)
    steps = _steps(state)
    assert steps["runtime-deny"].status == "skipped" and "not a runtime" in steps["runtime-deny"].detail
    assert state.runtime_policy_before == {}
    assert state.job == "contained"


async def test_run_contain_no_gateways_no_runtimes_skipped():
    agent = _agent(mcp_server_ids=["mcp-std"], arns={})
    svc, store, _, _ = _svc(agent=agent)
    await svc.contain(agent, ALICE, "tool misuse seen")
    await svc.run_contain("agent-1")
    state = _state(store)
    assert [s.status for s in state.steps] == ["ok", "skipped", "skipped"]
    assert state.job == "failed"


async def test_restore_not_contained_conflicts():
    svc, _, _, agent = _svc()
    with pytest.raises(ContainmentConflict) as e:
        await svc.restore(agent, BOB, "false positive")
    assert e.value.detail == "agent is not contained"


async def test_restore_short_reason_raises_value_error():
    svc, _, _, agent = _svc()
    await svc.contain(agent, ALICE, "tool misuse seen")
    await svc.run_contain("agent-1")
    with pytest.raises(ValueError):
        await svc.restore(agent, BOB, "short")


async def test_restore_same_person_conflicts():
    svc, _, _, agent = _svc()
    await svc.contain(agent, ALICE, "tool misuse seen")
    await svc.run_contain("agent-1")
    with pytest.raises(ContainmentConflict) as e:
        await svc.restore(agent, ALICE, "false positive")
    assert e.value.detail == "restore requires a different person than the one who contained the agent"


async def test_restore_same_person_by_email_conflicts():
    svc, _, _, agent = _svc()
    await svc.contain(
        agent, Principal(oid=None, email="x@example.com", role=Role.ADMIN, raw_claims={}), "tool misuse seen"
    )
    await svc.run_contain("agent-1")
    with pytest.raises(ContainmentConflict):
        await svc.restore(
            agent,
            Principal(oid=None, email="x@example.com", role=Role.ADMIN, raw_claims={}),
            "false positive",
        )


async def test_restore_without_identity_fails_closed():
    svc, _, _, agent = _svc()
    await svc.contain(agent, ALICE, "tool misuse seen")
    await svc.run_contain("agent-1")
    with pytest.raises(ContainmentConflict) as e:
        await svc.restore(
            agent, Principal(oid=None, email=None, role=Role.ADMIN, raw_claims={}), "false positive"
        )
    assert e.value.detail == "restore requires a different person than the one who contained the agent"


async def test_restore_and_run_restore_undo_everything():
    cedar, levers = FakeCedar(), FakeLevers(deny={"status": "ok", "before": None})
    svc, store, audit, agent = _svc(cedar=cedar, levers=levers)
    await svc.contain(agent, ALICE, "tool misuse seen")
    await svc.run_contain("agent-1")
    state = await svc.restore(agent, BOB, "false positive")
    assert state.job == "restoring" and state.state == "CONTAINED"  # AGP keeps refusing until undone
    assert all(s.status == "pending" for s in state.steps)
    await svc.run_restore("agent-1")
    state = _state(store)
    assert (state.state, state.job, state.restored_by_name) == ("RESTORED", "restored", "bob@example.com")
    assert state.restored_at
    assert [s.status for s in state.steps] == ["ok", "ok", "ok"]
    assert cedar.deleted == [("mcp-gw1", "pol-mcp-gw1"), ("mcp-gw2", "pol-mcp-gw2")]
    assert levers.restored == [(RT, None)]
    restore_rows = [(r.step, r.result, r.actor_oid) for r in audit.list("agent-1") if r.action == "restore"]
    assert restore_rows == [
        (None, "requested", "oid-bob"),
        ("cedar-forbid", "ok", "oid-bob"),
        ("runtime-deny", "ok", "oid-bob"),
        ("refuse-invokes", "ok", "oid-bob"),  # AGP stops refusing only after the native undo
    ]
    assert not store.is_contained("agent-1")
    # Contain is allowed again after a restore.
    await svc.contain(agent, ALICE, "tool misuse again")


async def test_run_restore_policy_delete_error_stays_contained_and_is_retryable():
    cedar = FakeCedar()
    svc, store, _audit, agent = _svc(cedar=cedar)
    await svc.contain(agent, ALICE, "tool misuse seen")
    await svc.run_contain("agent-1")
    await svc.restore(agent, BOB, "false positive")
    cedar.delete_raises = McpCedarError("delete failed", "policy_delete_failed")
    await svc.run_restore("agent-1")
    state = _state(store)
    steps = _steps(state)
    assert (steps["cedar-forbid"].status, steps["runtime-deny"].status, steps["refuse-invokes"].status) == (
        "failed",
        "ok",
        "skipped",
    )
    assert (state.state, state.job) == ("CONTAINED", "failed")
    assert len(state.policies) == 2  # the forbids are still on the gateways
    assert state.runtime_policy_before == {}  # that undo succeeded: not retried
    assert store.is_contained("agent-1")
    # Retry succeeds once the gateway cooperates.
    cedar.delete_raises = None
    await svc.restore(agent, BOB, "false positive retry")
    await svc.run_restore("agent-1")
    state = _state(store)
    assert (state.state, state.job, state.policies) == ("RESTORED", "restored", [])
    assert cedar.deleted == [("mcp-gw1", "pol-mcp-gw1"), ("mcp-gw2", "pol-mcp-gw2")]


async def test_run_restore_runtime_failure_keeps_arn_for_retry(monkeypatch):
    levers = FakeLevers(deny={"status": "ok", "before": '{"old": 1}'})
    svc, store, _, agent = _svc(levers=levers)
    await svc.contain(agent, ALICE, "tool misuse seen")
    await svc.run_contain("agent-1")
    await svc.restore(agent, BOB, "false positive")
    monkeypatch.setattr(
        levers, "restore_invokes", lambda a, arn, before: {"status": "failed", "detail": "AccessDenied"}
    )
    await svc.run_restore("agent-1")
    state = _state(store)
    assert (state.state, state.job) == ("CONTAINED", "failed")
    assert state.runtime_policy_before == {RT: '{"old": 1}'}


async def test_restore_refused_while_contain_running():
    svc, _, _, agent = _svc()
    await svc.contain(agent, ALICE, "tool misuse seen")  # job running, run_contain not yet done
    with pytest.raises(ContainmentConflict) as e:
        await svc.restore(agent, BOB, "false positive")
    assert e.value.detail == "agent containment is still running"


async def test_restore_and_contain_refused_while_restoring():
    svc, _, _, agent = _svc()
    await svc.contain(agent, ALICE, "tool misuse seen")
    await svc.run_contain("agent-1")
    await svc.restore(agent, BOB, "false positive")
    with pytest.raises(ContainmentConflict) as e:
        await svc.restore(agent, BOB, "false positive")
    assert e.value.detail == "agent containment is still running"
    with pytest.raises(ContainmentConflict) as e:
        await svc.contain(agent, ALICE, "tool misuse seen")
    assert e.value.detail == "agent containment is still running"


async def test_restore_allowed_after_failed_contain_job():
    agent = _agent(mcp_server_ids=[], arns={})
    svc, store, _, _ = _svc(agent=agent)
    await svc.contain(agent, ALICE, "tool misuse seen")
    await svc.run_contain("agent-1")
    assert _state(store).job == "failed"
    await svc.restore(agent, BOB, "false positive")
    await svc.run_restore("agent-1")
    assert _state(store).state == "RESTORED"


@pytest.mark.parametrize(
    "restorer",
    [
        Principal(oid=None, email="alice@example.com", role=Role.ADMIN, raw_claims={}),  # email only
        Principal(oid=None, email="Alice@Example.COM", role=Role.ADMIN, raw_claims={}),  # case differs
        Principal(oid="OID-ALICE", email="other@example.com", role=Role.ADMIN, raw_claims={}),  # oid case
    ],
)
async def test_restore_same_person_by_any_identifier(restorer):
    svc, _, _, agent = _svc()
    await svc.contain(agent, ALICE, "tool misuse seen")
    await svc.run_contain("agent-1")
    with pytest.raises(ContainmentConflict) as e:
        await svc.restore(agent, restorer, "false positive")
    assert e.value.detail == "restore requires a different person than the one who contained the agent"


async def test_contain_stores_both_identities():
    svc, _, _, agent = _svc()
    state = await svc.contain(agent, ALICE, "tool misuse seen")
    assert (state.by_oid, state.by_email) == ("oid-alice", "alice@example.com")


async def test_contain_without_identity_refused():
    svc, _, _, agent = _svc()
    with pytest.raises(ContainmentConflict):
        await svc.contain(
            agent, Principal(oid=None, email=None, role=Role.ADMIN, raw_claims={}), "tool misuse seen"
        )


async def test_run_contain_unexpected_error_fails_job_and_audits():
    agent = _agent()
    svc, store, audit, _ = _svc(agent=agent)
    await svc.contain(agent, ALICE, "tool misuse seen")

    def boom(_):
        raise RuntimeError("registry down")

    svc._agents = boom
    await svc.run_contain("agent-1")
    state = _state(store)
    assert (state.state, state.job) == ("CONTAINED", "failed")
    assert all(s.status == "failed" for s in state.steps)
    last = audit.list("agent-1")[-1]
    assert (last.action, last.step, last.result) == ("contain", None, "failed")
    assert "RuntimeError" in last.detail and "registry down" in last.detail


async def test_run_restore_unexpected_error_fails_job_and_audits():
    svc, store, audit, agent = _svc()
    await svc.contain(agent, ALICE, "tool misuse seen")
    await svc.run_contain("agent-1")
    await svc.restore(agent, BOB, "false positive")

    def boom(_):
        raise RuntimeError("registry down")

    svc._agents = boom
    await svc.run_restore("agent-1")
    state = _state(store)
    assert (state.state, state.job) == ("CONTAINED", "failed")
    last = audit.list("agent-1")[-1]
    assert (last.action, last.result, last.actor_oid) == ("restore", "failed", "oid-bob")


async def _contained_then_restoring(svc, agent):
    await svc.contain(agent, ALICE, "tool misuse seen")
    await svc.run_contain("agent-1")
    await svc.restore(agent, BOB, "false positive")


async def test_run_restore_one_of_two_gateways_fails_keeps_only_that_ref():
    cedar = FakeCedar()
    svc, store, _, agent = _svc(cedar=cedar)
    await _contained_then_restoring(svc, agent)
    cedar.delete_fail = {"mcp-gw2": McpCedarError("policy delete failed", "policy_delete_failed")}
    await svc.run_restore("agent-1")
    state = _state(store)
    assert (state.state, state.job) == ("CONTAINED", "failed")
    assert _steps(state)["cedar-forbid"].status == "failed"  # partial undo is not done
    assert state.policies == [{"mcp_id": "mcp-gw2", "policy_id": "pol-mcp-gw2"}]
    cedar.delete_fail = {}
    await svc.restore(agent, BOB, "false positive retry")
    await svc.run_restore("agent-1")
    state = _state(store)
    assert (state.state, state.job, state.policies) == ("RESTORED", "restored", [])
    assert cedar.deleted == [("mcp-gw1", "pol-mcp-gw1"), ("mcp-gw2", "pol-mcp-gw2")]  # gw1 not re-deleted


async def test_run_restore_one_of_two_runtimes_fails_keeps_only_that_arn(monkeypatch):
    rt2 = RT.replace("fraud_triage_dev-AbC123", "fraud_triage_prod-XyZ789")
    agent = _agent(arns={"dev": RT, "prod": rt2})
    levers = FakeLevers(deny={"status": "ok", "before": None})
    svc, store, _, _ = _svc(agent=agent, levers=levers)
    await _contained_then_restoring(svc, agent)
    calls = []

    def flaky(a, arn, before):
        calls.append(arn)
        return (
            {"status": "failed", "detail": "AccessDenied"}
            if arn == rt2 and len(calls) <= 2
            else {"status": "ok"}
        )

    monkeypatch.setattr(levers, "restore_invokes", flaky)
    await svc.run_restore("agent-1")
    state = _state(store)
    assert (state.state, state.job) == ("CONTAINED", "failed")
    assert _steps(state)["runtime-deny"].status == "failed"
    assert state.runtime_policy_before == {rt2: None}
    await svc.restore(agent, BOB, "false positive retry")
    await svc.run_restore("agent-1")
    state = _state(store)
    assert (state.state, state.job, state.runtime_policy_before) == ("RESTORED", "restored", {})
    assert calls == [RT, rt2, rt2]


async def test_run_restore_policy_already_gone_counts_as_undone():
    cedar = FakeCedar()
    svc, store, _, agent = _svc(cedar=cedar)
    await _contained_then_restoring(svc, agent)
    try:
        try:
            raise ClientError(
                {"Error": {"Code": "ResourceNotFoundException", "Message": "no"}}, "DeletePolicy"
            )
        except ClientError as err:
            raise McpCedarError("policy delete failed", "policy_not_found") from err
    except McpCedarError as wrapped:
        gone = wrapped
    cedar.delete_fail = {"mcp-gw1": gone}
    await svc.run_restore("agent-1")
    state = _state(store)
    assert (state.state, state.job, state.policies) == ("RESTORED", "restored", [])
    step = _steps(state)["cedar-forbid"]
    assert step.status == "ok" and "already gone" in step.detail


async def test_run_restore_other_cedar_error_is_not_treated_as_gone():
    cedar = FakeCedar()
    svc, store, _, agent = _svc(cedar=cedar)
    await _contained_then_restoring(svc, agent)
    try:
        try:
            raise ClientError({"Error": {"Code": "ThrottlingException", "Message": "slow"}}, "DeletePolicy")
        except ClientError as err:
            raise McpCedarError("policy delete failed", "policy_delete_failed") from err
    except McpCedarError as wrapped:
        cedar.delete_fail = {"mcp-gw1": wrapped}
    await svc.run_restore("agent-1")
    assert _state(store).state == "CONTAINED"


async def test_run_restore_mcp_record_gone_counts_as_undone():
    svc, store, audit, agent = _svc()
    await _contained_then_restoring(svc, agent)
    svc._mcp = lambda mcp_id: None if mcp_id == "mcp-gw1" else MCPS.get(mcp_id)
    await svc.run_restore("agent-1")
    state = _state(store)
    assert (state.state, state.job, state.policies) == ("RESTORED", "restored", [])
    row = next(r for r in audit.list("agent-1") if r.action == "restore" and r.step == "cedar-forbid")
    assert "mcp server record gone; policy pol-mcp-gw1 not verified" in row.detail


async def test_view_without_row(monkeypatch):
    from services import containment_service

    monkeypatch.setattr(containment_service.settings, "AWS_REGION", "eu-central-1")
    svc, *_ = _svc()
    v = svc.view("agent-1")
    assert v["agent_id"] == "agent-1"
    assert v["state"] is None and v["job"] is None and v["by_oid"] is None and v["since"] is None
    assert v["steps"] == [] and v["signals"] == [] and v["audit"] == []
    assert v["monitor"] == {"enabled": False}
    assert v["cloudtrail_url"] == (
        "https://eu-central-1.console.aws.amazon.com/cloudtrailv2/home"
        "?region=eu-central-1#/events?EventName=CreatePolicy"
    )


async def test_view_with_state_signals_and_audit():
    svc, store, _, agent = _svc()
    store.put_signal(_sig("2026-10-13T08:30:00+00:00"))
    store.set_monitor("agent-1", enabled=True)
    await svc.contain(agent, ALICE, "tool misuse seen")
    v = svc.view("agent-1")
    assert (v["state"], v["job"], v["by_name"], v["by_oid"], v["reason"]) == (
        "CONTAINED",
        "running",
        "alice@example.com",
        "oid-alice",
        "tool misuse seen",
    )
    assert v["signal_at"] == "2026-10-13T08:30:00+00:00"
    assert [s["id"] for s in v["steps"]] == list(STEP_ORDER)
    assert set(v["steps"][0]) == {"id", "status", "detail", "native_ref", "at"}
    assert v["signals"][0]["span_id"] == "s1"
    assert set(v["audit"][0]) == {"at", "actor_name", "action", "step", "result", "detail", "native_ref"}
    assert v["monitor"] == {"enabled": True}


@pytest.fixture
def transitions(monkeypatch):
    outcomes = []
    transition = StateStore.transition

    def spied(self, item, expected):
        outcomes.append(transition(self, item, expected))
        return outcomes[-1]

    monkeypatch.setattr(StateStore, "transition", spied)
    return outcomes


async def test_contain_refused_by_the_write_not_the_read(transitions):
    svc, store, audit, agent = _svc()
    await svc.contain(agent, ALICE, "tool misuse seen")
    with pytest.raises(ContainmentConflict) as e:
        await svc.contain(agent, BOB, "tool misuse seen")
    assert e.value.detail == "agent is already contained"
    assert transitions == [True, False]
    assert _state(store).by_oid == "oid-alice"
    assert [ev.actor_oid for ev in audit.list("agent-1")] == ["oid-alice"]


async def test_restore_refused_by_the_write_not_the_read(transitions, monkeypatch):
    svc, store, audit, agent = _svc()
    await svc.contain(agent, ALICE, "tool misuse seen")
    await svc.run_contain("agent-1")
    idle = _state(store)
    carol = Principal(oid="oid-carol", email="carol@example.com", role=Role.ADMIN, raw_claims={})
    await svc.restore(agent, BOB, "false positive")
    monkeypatch.setattr(store, "get_state", lambda agent_id: idle.model_copy(deep=True))
    with pytest.raises(ContainmentConflict) as e:
        await svc.restore(agent, carol, "false positive too")
    assert e.value.detail == "agent containment is still running"
    assert transitions[-2:] == [True, False]
    monkeypatch.undo()
    state = _state(store)
    assert (state.job, state.restored_by_oid) == ("restoring", "oid-bob")
    assert [ev.actor_oid for ev in audit.list("agent-1") if ev.action == "restore"] == ["oid-bob"]


async def test_run_contain_leaves_a_row_another_job_took_over():
    cedar = FakeCedar()
    svc, store, audit, agent = _svc(cedar=cedar)
    await svc.contain(agent, ALICE, "tool misuse seen")

    async def take_over(mcp, *, agent_app_id, agent_label):
        other = _state(store)
        other.job, other.reason = "failed", "taken over"
        assert store.put_state(other, {"job": {"running"}})
        return {"status": "ok", "policy_id": "pol"}

    cedar.add_agent_forbid = take_over
    await svc.run_contain("agent-1")
    state = _state(store)
    assert (state.state, state.job, state.reason) == ("CONTAINED", "failed", "taken over")
    assert [s.status for s in state.steps] == ["ok", "pending", "pending"]
    assert [(r.step, r.result) for r in audit.list("agent-1")] == [
        (None, "requested"),
        ("refuse-invokes", "ok"),
    ]
