"""ContainmentStore (mutable) + AuditStore (append-only) on the memory double of ``core.state_store``.

The ``table`` fixture stands in for ``core.aws_clients.dynamodb_table`` with a spy around a memory
table, so a named store proves its key layout, its consistent reads and the conditions it sends.
"""

from typing import Literal
from unittest.mock import MagicMock

import pytest
from boto3.dynamodb.conditions import Attr

from core import aws_clients
from core.state_store import ABSENT, StateStore
from models.containment import (
    STEP_ORDER,
    AuditEvent,
    ContainmentState,
    ContainmentStateValue,
    Signal,
    StepResult,
)
from services import containment_store as store_module
from services.containment_store import AuditStore, ContainmentStore

NOW = 1_800_000_000.0


class _Clock:
    def __init__(self, t=NOW):
        self.t = t

    def __call__(self):
        return self.t


@pytest.fixture
def clock():
    return _Clock()


@pytest.fixture
def table(monkeypatch):
    spy = MagicMock(wraps=StateStore("")._table)
    monkeypatch.setattr(aws_clients, "dynamodb_table", lambda name, *, region=None: spy)
    return spy


@pytest.fixture
def cstore(clock):
    return ContainmentStore(table_name="", clock=clock)


@pytest.fixture
def astore():
    return AuditStore(table_name="")


def _row(table, pk, sk):
    return table.get_item(Key={"pk": pk, "sk": sk}).get("Item")


def _state(agent_id="a1", state: ContainmentStateValue = "CONTAINED", **kw):
    return ContainmentState(
        agent_id=agent_id,
        state=state,
        job=kw.pop("job", "running"),
        since="2026-10-13T09:00:00+00:00",
        by_oid="oid-1",
        by_name="Lars Svensson",
        reason="tool misuse on fnol",
        steps=[StepResult(id=s) for s in STEP_ORDER],
        **kw,
    )


def _signal(span_id="s1", span_end="2026-10-13T09:00:00+00:00", agent_id="a1"):
    return Signal(
        agent_id=agent_id,
        session_id="sess-1",
        trace_id="t1",
        span_id=span_id,
        tool="delete_claim",
        label="FAIL",
        explanation="deleted a claim it was asked to read",
        span_end=span_end,
        detected_at="2026-10-13T09:00:05+00:00",
    )


def _event(
    at="2026-10-13T09:00:00.000+00:00",
    action: Literal["contain", "restore", "monitor-on", "monitor-off", "signal"] = "contain",
    agent_id="a1",
):
    return AuditEvent(
        agent_id=agent_id, at=at, actor_oid="oid-1", actor_name="Lars", action=action, result="requested"
    )


def test_step_order_and_defaults():
    assert STEP_ORDER == ("refuse-invokes", "cedar-forbid", "runtime-deny")
    step = StepResult(id="cedar-forbid")
    assert (step.status, step.detail, step.native_ref, step.at) == ("pending", "", None, None)
    assert _signal().source == "agentcore-evaluations"


def test_state_round_trip(cstore):
    assert cstore.get_state("a1") is None
    s = _state(policies=[{"mcp_id": "m1", "policy_id": "p1"}], runtime_policy_before={"arn:rt": None})
    cstore.put_state(s, {"state": {ABSENT}})
    assert cstore.get_state("a1") == s
    assert cstore.get_state("other") is None


def test_is_contained(cstore):
    assert cstore.is_contained("a1") is False
    cstore.put_state(_state(state="CONTAINED"), {"state": {ABSENT}})
    assert cstore.is_contained("a1") is True
    cstore.put_state(_state(state="RESTORED", job="restored"), {"job": {"running"}})
    assert cstore.is_contained("a1") is False


def test_put_state_writes_only_from_the_expected_row(cstore):
    assert cstore.put_state(_state(), {"state": {ABSENT}}) is True
    assert cstore.put_state(_state(job="contained"), {"state": {ABSENT}}) is False
    assert cstore.get_state("a1").job == "running"
    assert cstore.put_state(_state(job="contained"), {"job": {"running"}}) is True
    assert cstore.put_state(_state(job="failed"), {"job": {"running"}}) is False
    assert cstore.get_state("a1").job == "contained"


def test_state_key_layout(clock, table):
    ContainmentStore(table_name="t", clock=clock).put_state(_state(), {"state": {ABSENT}})
    row = _row(table, "AGENT#a1", "STATE")
    assert (row["state"], row["job"]) == ("CONTAINED", "running")


def test_monitor_on_off_maintains_index(cstore):
    assert cstore.get_monitor("a1") == {"enabled": False, "cursor_ms": 0}
    assert cstore.set_monitor("a1", enabled=True, cursor_ms=1000) == {"enabled": True, "cursor_ms": 1000}
    cstore.set_monitor("a2", enabled=True)
    assert sorted(cstore.list_monitored()) == ["a1", "a2"]
    assert cstore.set_monitor("a1", enabled=True) == {"enabled": True, "cursor_ms": 1000}
    cstore.set_monitor("a1", enabled=False)
    assert cstore.get_monitor("a1") == {"enabled": False, "cursor_ms": 1000}
    assert cstore.list_monitored() == ["a2"]


def test_set_monitor_without_a_cursor_leaves_the_cursor_attribute_alone(clock, table):
    s = ContainmentStore(table_name="t", clock=clock)
    s.set_monitor("a1", enabled=True)
    assert "cursor_ms" not in _row(table, "AGENT#a1", "MONITOR")
    s.save_cursor("a1", 700)
    s.set_monitor("a1", enabled=False)
    assert _row(table, "AGENT#a1", "MONITOR")["cursor_ms"] == 700
    sets = [c.kwargs["ExpressionAttributeValues"] for c in table.update_item.call_args_list]
    assert [sorted(v.values(), key=str) for v in sets] == [[True], [700], [False]]


def test_save_cursor_only_moves_the_cursor(cstore):
    cstore.save_cursor("a1", 500)
    assert cstore.get_monitor("a1") == {"enabled": False, "cursor_ms": 0}
    assert cstore.list_monitored() == []
    cstore.set_monitor("a1", enabled=True, cursor_ms=100)
    cstore.set_monitor("a1", enabled=False)
    cstore.save_cursor("a1", 900)
    assert cstore.get_monitor("a1") == {"enabled": False, "cursor_ms": 900}
    assert cstore.list_monitored() == []


def test_save_cursor_missing_row_creates_nothing(clock, table):
    ContainmentStore(table_name="t", clock=clock).save_cursor("a1", 5)
    assert table.scan()["Items"] == []


def test_reads_are_strongly_consistent(clock, table):
    s = ContainmentStore(table_name="t", clock=clock)
    s.put_state(_state(), {"state": {ABSENT}})
    s.set_monitor("a1", enabled=True)
    s.put_signal(_signal())
    table.reset_mock()
    s.get_state("a1")
    s.is_contained("a1")
    s.get_monitor("a1")
    s.list_monitored()
    s.list_signals("a1")
    reads = [c for c in table.method_calls if c[0] in ("get_item", "query")]
    assert {c[0] for c in reads} == {"get_item", "query"}
    assert all(c.kwargs["ConsistentRead"] is True for c in reads)


def test_monitor_index_rows(clock, table):
    s = ContainmentStore(table_name="t", clock=clock)
    s.set_monitor("a1", enabled=True)
    assert _row(table, "MONITORED", "a1") and _row(table, "AGENT#a1", "MONITOR")
    s.set_monitor("a1", enabled=True)
    assert s.list_monitored() == ["a1"]
    s.set_monitor("a1", enabled=False)
    assert _row(table, "MONITORED", "a1") is None


def test_put_signal_dedupes_on_span_id(cstore):
    assert cstore.put_signal(_signal("s1")) is True
    assert cstore.put_signal(_signal("s1")) is False
    assert len(cstore.list_signals("a1")) == 1


def test_list_signals_newest_first(cstore):
    for i in range(5):
        cstore.put_signal(_signal(f"s{i}", span_end=f"2026-10-13T09:00:0{i}+00:00"))
    cstore.put_signal(_signal("other", agent_id="a2"))
    got = cstore.list_signals("a1")
    assert [s.span_id for s in got] == ["s4", "s3", "s2", "s1", "s0"]
    assert [s.span_id for s in cstore.list_signals("a1", limit=3)] == ["s4", "s3", "s2"]


def test_signal_row_key_and_ttl(clock, table):
    ContainmentStore(table_name="t", clock=clock).put_signal(_signal("s1"))
    row = _row(table, "AGENT#a1", "SIGNAL#2026-10-13T09:00:00+00:00#s1")
    assert row["ttl"] == int(NOW) + 7 * 24 * 3600


def test_acquire_lease(cstore, clock):
    assert cstore.acquire_lease("monitor", "h1") is True
    assert cstore.acquire_lease("monitor", "h2") is False
    assert cstore.acquire_lease("monitor", "h1") is True
    assert cstore.acquire_lease("other", "h2") is True
    clock.t += 31
    assert cstore.acquire_lease("monitor", "h2") is True
    assert cstore.acquire_lease("monitor", "h1") is False


def test_lease_row(clock, table):
    ContainmentStore(table_name="t", clock=clock).acquire_lease("monitor", "h1", ttl_s=30)
    row = _row(table, "LEASE", "monitor")
    assert row["holder"] == "h1" and row["ttl"] == row["expires_at"] == int(NOW) + 30


def test_audit_append_is_conditional_and_never_mutates(table):
    a = AuditStore(table_name="audit")
    a.append(_event())
    a.append(_event(action="restore"))
    puts = table.put_item.call_args_list
    assert len(puts) == 2
    assert all(c.kwargs["ConditionExpression"] == Attr("sk").not_exists() for c in puts)
    assert not table.update_item.called and not table.delete_item.called
    sks = [c.kwargs["Item"]["sk"] for c in puts]
    assert all(sk.startswith("EVENT#2026-10-13T09:00:00.000+00:00#") for sk in sks)
    assert all(len(sk.rsplit("#", 1)[1]) == 32 for sk in sks)
    assert all(c.kwargs["Item"]["pk"] == "AGENT#a1" for c in puts)


def test_audit_same_millisecond_distinct_sk(astore):
    astore.append(_event())
    astore.append(_event())
    assert len(astore.list("a1")) == 2


def test_audit_list_oldest_first(astore):
    astore.append(_event(at="2026-10-13T09:00:02.000+00:00", action="restore"))
    astore.append(_event(at="2026-10-13T09:00:00.000+00:00", action="contain"))
    astore.append(_event(at="2026-10-13T09:00:01.000+00:00", action="signal"))
    astore.append(_event(agent_id="a2"))
    assert [e.action for e in astore.list("a1")] == ["contain", "signal", "restore"]
    assert [e.action for e in astore.list("a1", limit=2)] == ["signal", "restore"]
    assert astore.list("a1")[0] == _event(at="2026-10-13T09:00:00.000+00:00", action="contain")


def test_audit_list_limit_below_row_count(astore):
    for i in range(5):
        astore.append(
            _event(at=f"2026-10-13T09:00:0{i}.000+00:00", action="signal", agent_id="a1").model_copy(
                update={"detail": str(i)}
            )
        )
    assert [e.detail for e in astore.list("a1", limit=2)] == ["3", "4"]
    assert [e.detail for e in astore.list("a1")] == ["0", "1", "2", "3", "4"]


def test_audit_same_timestamp_orders_by_sk(astore, monkeypatch):
    hexes = iter(["c" * 32, "b" * 32, "a" * 32])
    monkeypatch.setattr(store_module.uuid, "uuid4", lambda: type("U", (), {"hex": next(hexes)})())
    for d in ("first", "second", "third"):
        astore.append(_event().model_copy(update={"detail": d}))
    assert [e.detail for e in astore.list("a1")] == ["third", "second", "first"]


def test_audit_append_refuses_a_row_it_would_overwrite(astore, monkeypatch):
    monkeypatch.setattr(store_module.uuid, "uuid4", lambda: type("U", (), {"hex": "f" * 32})())
    astore.append(_event())
    with pytest.raises(RuntimeError):
        astore.append(_event())
    assert len(astore.list("a1")) == 1


def test_lazy_singletons_in_memory_when_unconfigured(monkeypatch):
    monkeypatch.setattr(store_module, "_containment_store", None)
    monkeypatch.setattr(store_module, "_audit_store", None)
    monkeypatch.setattr(store_module.settings, "CONTAINMENT_TABLE_NAME", "")
    monkeypatch.setattr(store_module.settings, "AUDIT_TABLE_NAME", "")
    c = store_module.get_containment_store()
    assert c is store_module.get_containment_store()
    a = store_module.get_audit_store()
    assert a is store_module.get_audit_store()
    c.put_state(_state(), {"state": {ABSENT}})
    assert c.is_contained("a1")
    a.append(_event())
    assert len(a.list("a1")) == 1
