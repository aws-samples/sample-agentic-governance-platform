"""monitor_tick (lease, scan, dedup, cursor) and run_monitor_forever."""

import asyncio
from itertools import pairwise
from types import SimpleNamespace

import pytest

from models.containment import Signal
from services import containment_monitor
from services.containment_monitor import monitor_tick
from services.containment_store import AuditStore, ContainmentStore

pytestmark = pytest.mark.asyncio


def _sig(agent_id, span_id):
    return Signal(
        agent_id=agent_id,
        session_id="sess-1",
        trace_id="t",
        span_id=span_id,
        tool="send_email",
        label="FAIL",
        explanation="hidden instruction",
        span_end="2026-10-13T08:00:00+00:00",
        detected_at="2026-10-13T08:00:01+00:00",
    )


class FakeSource:
    name = "fake"

    def __init__(self, results):
        self.results = results  # agent id -> (signals, sessions, cursor) | Exception
        self.calls = []

    def scan(self, agent, since_ms):
        self.calls.append((agent.id, since_ms))
        r = self.results[agent.id]
        if isinstance(r, Exception):
            raise r
        return r


def _agents(*ids):
    table = {i: SimpleNamespace(id=i) for i in ids}
    return table.get


def _stores():
    return ContainmentStore(table_name=""), AuditStore(table_name="")


async def test_tick_without_lease_returns_zero():
    store, audit = _stores()
    store.set_monitor("a1", enabled=True)
    assert store.acquire_lease("containment-monitor", "other-task")
    source = FakeSource({"a1": ([_sig("a1", "s1")], [], 5)})
    n = await monitor_tick(store=store, source=source, agents_loader=_agents("a1"), holder="me", audit=audit)
    assert n == 0 and source.calls == []


async def test_tick_writes_new_signals_audits_once_and_saves_cursor():
    store, audit = _stores()
    store.set_monitor("a1", enabled=True, cursor_ms=100)
    source = FakeSource({"a1": ([_sig("a1", "s1"), _sig("a1", "s2")], [("sess-1", 900)], 1000)})
    n = await monitor_tick(store=store, source=source, agents_loader=_agents("a1"), holder="me", audit=audit)
    assert n == 2
    assert source.calls == [("a1", 100)]
    assert store.get_monitor("a1") == {"enabled": True, "cursor_ms": 1000}
    assert {s.span_id for s in store.list_signals("a1")} == {"s1", "s2"}
    rows = audit.list("a1")
    assert [(r.action, r.result, r.native_ref) for r in rows] == [
        ("signal", "ok", "s1"),
        ("signal", "ok", "s2"),
    ]

    # A re-scan returning the same span writes nothing new and audits nothing.
    source.results["a1"] = ([_sig("a1", "s1")], [], 1200)
    n = await monitor_tick(store=store, source=source, agents_loader=_agents("a1"), holder="me", audit=audit)
    assert n == 0
    assert len(audit.list("a1")) == 2
    assert store.get_monitor("a1")["cursor_ms"] == 1200


async def test_tick_uses_save_cursor_not_set_monitor(monkeypatch):
    store, audit = _stores()
    store.set_monitor("a1", enabled=True)
    calls = []
    monkeypatch.setattr(store, "set_monitor", lambda *a, **k: calls.append("set_monitor"))
    source = FakeSource({"a1": ([], [], 7)})
    await monitor_tick(store=store, source=source, agents_loader=_agents("a1"), holder="me", audit=audit)
    assert calls == []
    assert store.get_monitor("a1")["cursor_ms"] == 7


async def test_tick_scan_error_for_one_agent_does_not_stop_the_next():
    store, audit = _stores()
    store.set_monitor("a1", enabled=True, cursor_ms=1)
    store.set_monitor("a2", enabled=True, cursor_ms=2)
    source = FakeSource({"a1": RuntimeError("logs down"), "a2": ([_sig("a2", "x")], [], 50)})
    n = await monitor_tick(
        store=store, source=source, agents_loader=_agents("a1", "a2"), holder="me", audit=audit
    )
    assert n == 1
    assert store.get_monitor("a1")["cursor_ms"] == 1  # unchanged on error
    assert store.get_monitor("a2")["cursor_ms"] == 50


async def test_tick_skips_unknown_agent():
    store, audit = _stores()
    store.set_monitor("gone", enabled=True)
    source = FakeSource({})
    n = await monitor_tick(store=store, source=source, agents_loader=_agents(), holder="me", audit=audit)
    assert n == 0 and source.calls == []


async def test_run_monitor_forever_loops_until_stop_and_never_raises(monkeypatch):
    ticks = []

    async def boom(**kw):
        ticks.append(1)
        if len(ticks) >= 2:
            stop.set()
        raise RuntimeError("tick failed")

    stop = asyncio.Event()
    monkeypatch.setattr(containment_monitor, "monitor_tick", boom)
    monkeypatch.setattr(
        containment_monitor,
        "_default_deps",
        lambda: {"store": None, "source": None, "agents_loader": None, "holder": "h"},
    )
    monkeypatch.setattr(containment_monitor, "TICK_S", 0.01)
    await asyncio.wait_for(containment_monitor.run_monitor_forever(stop), timeout=2)
    assert len(ticks) == 2


async def test_run_monitor_forever_survives_wiring_failure(monkeypatch):
    stop = asyncio.Event()

    def bad_deps():
        stop.set()
        raise RuntimeError("no registry")

    monkeypatch.setattr(containment_monitor, "_default_deps", bad_deps)
    monkeypatch.setattr(containment_monitor, "TICK_S", 0.01)
    await asyncio.wait_for(containment_monitor.run_monitor_forever(stop), timeout=2)


async def test_tick_audit_rows_keep_their_scan_order_within_one_tick():
    store, audit = _stores()
    store.set_monitor("a1", enabled=True)
    signals = [_sig("a1", f"s{i}") for i in range(5)]
    source = FakeSource({"a1": (signals, [], 10)})
    await monitor_tick(store=store, source=source, agents_loader=_agents("a1"), holder="me", audit=audit)
    rows = audit.list("a1")
    assert [r.native_ref for r in rows] == ["s0", "s1", "s2", "s3", "s4"]
    assert all(earlier.at < later.at for earlier, later in pairwise(rows))
