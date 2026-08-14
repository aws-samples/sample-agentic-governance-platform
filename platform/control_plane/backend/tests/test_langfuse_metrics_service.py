"""Tests for the read-only Langfuse metrics service (Epic 26, Task 5 — contract C3).

Executable spec for ``LangfuseMetricsService``: per-agent daily metrics + trace list
read from Langfuse's public API, plus server-side fan-out aggregation for
project/tenant/platform scopes. Pure reads — no external mutations.

ALL HTTP is mocked (no live Langfuse): every test builds an ``httpx.AsyncClient`` over
an ``httpx.MockTransport`` whose handler routes on ``request.url.path`` and returns an
``httpx.Response`` — mirroring ``test_graph_service.py``. Secrets Manager is a
``MagicMock`` returning the per-agent ``{public_key, secret_key}`` JSON (the C1 SM
shape), so no boto3/AWS is touched. The TTL cache clock is INJECTED (a ``FakeClock``)
so the 60s window is advanced deterministically instead of sleeping.

The repo is not in pytest-asyncio ``auto`` mode, so each async test is decorated with
``@pytest.mark.asyncio`` explicitly (as in ``test_graph_service.py``).

SECRET-SAFETY: the pk/sk key VALUES are used only to build the Basic-auth header — the
tests never assert them on any returned shape (they must not leak into metrics/traces).
"""

from __future__ import annotations

import json
from datetime import date, datetime, timezone
from typing import Callable, Optional
from unittest.mock import MagicMock

import httpx
import pytest

from models.agent import Agent, AuthType, LifecycleState, Platform
from services.langfuse_metrics_service import (
    AgentMetrics,
    LangfuseMetricsService,
    TraceRow,
)

HOST = "https://langfuse.example.com"
REGION = "us-east-1"
DATE_FROM = date(2026, 7, 9)
DATE_TO = date(2026, 7, 16)


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------
class FakeClock:
    """A monotonic-style clock whose value only moves when the test advances it."""

    def __init__(self, start: float = 1000.0) -> None:
        self.t = start

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


def _agent(
    *,
    agent_id: str = "rec-abc123",
    name: str = "Claims Triage DE",
    secret_name: Optional[str] = "langfuse-agent-rec-abc123-keys",
    project_id: Optional[str] = "clx-proj-1",
) -> Agent:
    now = datetime.now(timezone.utc)
    return Agent(
        id=agent_id,
        name=name,
        purpose="Triage inbound motor claims",
        lifecycle_state=LifecycleState.APPROVED,
        platform=Platform.AWS_BEDROCK,
        auth_type=AuthType.ENTRA,
        tenant_id="tenant-de",
        langfuse_project_id=project_id,
        langfuse_key_secret_name=secret_name,
        created_at=now,
        updated_at=now,
    )


def _sm_mock(public_key: str = "pk-lf-abc", secret_key: str = "sk-lf-xyz") -> MagicMock:
    """A Secrets Manager client mock returning the per-agent key pair JSON."""
    sm = MagicMock(name="secretsmanager")
    sm.get_secret_value.return_value = {
        "SecretString": json.dumps(
            {
                "public_key": public_key,
                "secret_key": secret_key,
                "project_name": "agp-Claims Triage DE",
                "project_id": "clx-proj-1",
            }
        )
    }
    return sm


def _svc(
    handler: Callable[[httpx.Request], httpx.Response],
    *,
    sm: Optional[MagicMock] = None,
    clock: Optional[Callable[[], float]] = None,
) -> LangfuseMetricsService:
    transport = httpx.MockTransport(handler)
    client = httpx.AsyncClient(transport=transport)
    return LangfuseMetricsService(
        langfuse_host=HOST,
        region=REGION,
        secrets_client=sm if sm is not None else _sm_mock(),
        http_client=client,
        clock=clock or FakeClock(),
    )


def _daily_payload() -> dict:
    """A two-day ``/metrics/daily`` payload (research PART B.2 shape)."""
    return {
        "data": [
            {
                "date": "2026-07-15",
                "countTraces": 10,
                "countObservations": 20,
                "totalCost": 2.5,
                "usage": [
                    {
                        "model": "claude-sonnet",
                        "inputUsage": 100,
                        "outputUsage": 100,
                        "totalUsage": 200,
                        "totalCost": 1.5,
                    },
                    {
                        "model": "titan-embed",
                        "inputUsage": 50,
                        "outputUsage": 0,
                        "totalUsage": 50,
                        "totalCost": 1.0,
                    },
                ],
            },
            {
                "date": "2026-07-16",
                "countTraces": 5,
                "countObservations": 8,
                "totalCost": 1.0,
                "usage": [
                    {
                        "model": "claude-sonnet",
                        "inputUsage": 40,
                        "outputUsage": 60,
                        "totalUsage": 100,
                        "totalCost": 1.0,
                    }
                ],
            },
        ],
        "meta": {"page": 1, "limit": 50, "totalItems": 2, "totalPages": 1},
    }


# ===========================================================================
# 1) get_agent_metrics normalizes the daily payload → totals + daily + by_model
# ===========================================================================
@pytest.mark.asyncio
async def test_get_agent_metrics_normalizes_daily():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/public/metrics/daily"
        assert request.url.params.get("fromTimestamp") == "2026-07-09T00:00:00Z"
        assert request.url.params.get("toTimestamp") == "2026-07-16T23:59:59Z"
        # Basic auth header must be present (built from pk:sk).
        assert request.headers.get("authorization", "").lower().startswith("basic ")
        return httpx.Response(200, json=_daily_payload())

    svc = _svc(handler)
    metrics = await svc.get_agent_metrics(_agent(), DATE_FROM, DATE_TO)

    assert isinstance(metrics, AgentMetrics)
    # totals = sum across the two days.
    assert metrics.totals.traces == 15
    assert metrics.totals.cost_usd == 3.5
    assert metrics.totals.tokens == 350  # 200 + 50 + 100

    # daily preserves per-day rows (tokens summed from that day's usage[]).
    assert [d.date for d in metrics.daily] == ["2026-07-15", "2026-07-16"]
    assert metrics.daily[0].traces == 10
    assert metrics.daily[0].cost_usd == 2.5
    assert metrics.daily[0].tokens == 250  # 200 + 50
    assert metrics.daily[1].tokens == 100

    # by_model aggregates per-model across all days.
    by_model = {m.model: m for m in metrics.by_model}
    assert set(by_model) == {"claude-sonnet", "titan-embed"}
    assert by_model["claude-sonnet"].cost_usd == 2.5  # 1.5 + 1.0
    assert by_model["claude-sonnet"].tokens == 300  # 200 + 100
    assert by_model["titan-embed"].cost_usd == 1.0
    assert by_model["titan-embed"].tokens == 50


# ===========================================================================
# 2) unprovisioned agent (no key) ⇒ zeroed AgentMetrics, NO HTTP call, no raise
# ===========================================================================
@pytest.mark.asyncio
async def test_unprovisioned_agent_returns_zeroed():
    called = {"http": False}

    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        called["http"] = True
        return httpx.Response(200, json=_daily_payload())

    sm = _sm_mock()
    svc = _svc(handler, sm=sm)

    metrics = await svc.get_agent_metrics(
        _agent(secret_name=None, project_id=None), DATE_FROM, DATE_TO
    )

    assert isinstance(metrics, AgentMetrics)
    assert metrics.totals.traces == 0
    assert metrics.totals.cost_usd == 0.0
    assert metrics.totals.tokens == 0
    assert metrics.daily == []
    assert metrics.by_model == []
    # No HTTP call and NO Secrets Manager read for an unprovisioned agent.
    assert called["http"] is False
    sm.get_secret_value.assert_not_called()


# ===========================================================================
# 3) get_scope_metrics fans out + sums; one unprovisioned agent degrades to zero
# ===========================================================================
@pytest.mark.asyncio
async def test_get_scope_metrics_fans_out_and_sums():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        # Count ONLY the daily reads: get_agent_metrics also issues a v2 by-user query,
        # so counting every request would conflate the two and hide the cache behavior.
        if request.url.path.endswith("/metrics/daily"):
            calls["n"] += 1
        return httpx.Response(200, json=_daily_payload())

    svc = _svc(handler)
    agents = [
        _agent(agent_id="a1", secret_name="langfuse-agent-a1-keys"),
        _agent(agent_id="a2", secret_name="langfuse-agent-a2-keys"),
        _agent(agent_id="a3", secret_name=None, project_id=None),  # unprovisioned
    ]

    scope = await svc.get_scope_metrics(agents, DATE_FROM, DATE_TO)

    # Only the 2 provisioned agents hit Langfuse (distinct secret names ⇒ 2 calls).
    assert calls["n"] == 2
    # totals = 2× a single agent's totals (the unprovisioned one contributed zero).
    assert scope.totals.traces == 30  # 15 * 2
    assert scope.totals.cost_usd == 7.0  # 3.5 * 2
    assert scope.totals.tokens == 700  # 350 * 2

    # daily merged by date across the 2 agents.
    daily = {d.date: d for d in scope.daily}
    assert daily["2026-07-15"].traces == 20
    assert daily["2026-07-15"].cost_usd == 5.0
    assert daily["2026-07-16"].traces == 10

    # by_model merged by model across the 2 agents.
    by_model = {m.model: m for m in scope.by_model}
    assert by_model["claude-sonnet"].cost_usd == 5.0  # 2.5 * 2
    assert by_model["titan-embed"].tokens == 100  # 50 * 2


# ===========================================================================
# 3b) degrade-and-continue: a provisioned agent whose read FAILS contributes zero
# ===========================================================================
@pytest.mark.asyncio
async def test_get_scope_metrics_failed_read_degrades():
    def handler(request: httpx.Request) -> httpx.Response:
        # The a2 key raises (500); a1 succeeds.
        auth = request.headers.get("authorization", "")
        if "bad" in auth or request.url.params.get("fail") == "1":
            return httpx.Response(500, json={"error": "boom"})
        return httpx.Response(200, json=_daily_payload())

    # a2's secret returns a pk that flags the handler to 500 via the auth header.
    sm = MagicMock(name="secretsmanager")

    def _get(SecretId: str):  # noqa: N803 — boto3 kwarg name
        pk = "pk-bad" if "a2" in SecretId else "pk-ok"
        return {
            "SecretString": json.dumps({"public_key": pk, "secret_key": "sk"})
        }

    sm.get_secret_value.side_effect = _get

    # Route the failing call by inspecting the Basic-auth (pk-bad → 500).
    def handler2(request: httpx.Request) -> httpx.Response:
        import base64

        raw = request.headers.get("authorization", "").split(" ", 1)[-1]
        decoded = base64.b64decode(raw).decode() if raw else ""
        if decoded.startswith("pk-bad:"):
            return httpx.Response(500, json={"error": "boom"})
        return httpx.Response(200, json=_daily_payload())

    svc = _svc(handler2, sm=sm)
    agents = [
        _agent(agent_id="a1", secret_name="langfuse-agent-a1-keys"),
        _agent(agent_id="a2", secret_name="langfuse-agent-a2-keys"),
    ]

    scope = await svc.get_scope_metrics(agents, DATE_FROM, DATE_TO)

    # a2 failed ⇒ contributes zero; totals == just a1.
    assert scope.totals.traces == 15
    assert scope.totals.cost_usd == 3.5
    assert scope.totals.tokens == 350


# ===========================================================================
# 4) ~60s TTL cache: two calls in the window ⇒ ONE HTTP call; past 60s ⇒ refetch
# ===========================================================================
@pytest.mark.asyncio
async def test_ttl_cache_reuses_within_window():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        # Count ONLY the daily reads: get_agent_metrics also issues a v2 by-user query,
        # so counting every request would conflate the two and hide the cache behavior.
        if request.url.path.endswith("/metrics/daily"):
            calls["n"] += 1
        return httpx.Response(200, json=_daily_payload())

    clock = FakeClock()
    svc = _svc(handler, clock=clock)
    agent = _agent()

    first = await svc.get_agent_metrics(agent, DATE_FROM, DATE_TO)
    # Second call, 30s later (inside the 60s window) ⇒ served from cache, NO HTTP.
    clock.advance(30)
    second = await svc.get_agent_metrics(agent, DATE_FROM, DATE_TO)
    assert calls["n"] == 1
    assert second.totals.traces == first.totals.traces

    # Past the 60s TTL ⇒ a fresh HTTP call.
    clock.advance(31)  # now 61s from the first fetch
    await svc.get_agent_metrics(agent, DATE_FROM, DATE_TO)
    assert calls["n"] == 2


# ===========================================================================
# 4b) cache key includes the date range ⇒ a different window is a distinct fetch
# ===========================================================================
@pytest.mark.asyncio
async def test_ttl_cache_keyed_on_date_range():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        # Count ONLY the daily reads: get_agent_metrics also issues a v2 by-user query,
        # so counting every request would conflate the two and hide the cache behavior.
        if request.url.path.endswith("/metrics/daily"):
            calls["n"] += 1
        return httpx.Response(200, json=_daily_payload())

    svc = _svc(handler, clock=FakeClock())
    agent = _agent()

    await svc.get_agent_metrics(agent, DATE_FROM, DATE_TO)
    await svc.get_agent_metrics(agent, date(2026, 6, 1), date(2026, 6, 30))
    assert calls["n"] == 2  # different (secret, from, to) key ⇒ not a cache hit


# ===========================================================================
# 5) get_agent_traces maps the traces payload → TraceRow[] + total
# ===========================================================================
@pytest.mark.asyncio
async def test_get_agent_traces_shape():
    payload = {
        "data": [
            {
                "id": "trace-1",
                "timestamp": "2026-07-15T10:00:00Z",
                "name": "fnol-agent-invocation",
                "userId": "user-oid-1",
                "latency": 1.25,  # seconds (Langfuse) → 1250 ms
                "totalCost": 0.42,
            },
            {
                "id": "trace-2",
                "timestamp": "2026-07-15T09:00:00Z",
                "name": "fnol-agent-invocation",
                "userId": None,
                "latency": None,
                "totalCost": 0.0,
            },
        ],
        "meta": {"page": 1, "limit": 50, "totalItems": 137, "totalPages": 3},
    }

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/public/traces"
        assert request.url.params.get("page") == "2"
        assert request.url.params.get("limit") == "25"
        assert request.headers.get("authorization", "").lower().startswith("basic ")
        return httpx.Response(200, json=payload)

    svc = _svc(handler)
    result = await svc.get_agent_traces(_agent(), page=2, limit=25)

    assert result["total"] == 137
    rows = result["data"]
    assert len(rows) == 2
    assert isinstance(rows[0], TraceRow)
    assert rows[0].id == "trace-1"
    assert rows[0].timestamp == "2026-07-15T10:00:00Z"
    assert rows[0].name == "fnol-agent-invocation"
    assert rows[0].user_id == "user-oid-1"
    assert rows[0].latency_ms == 1250.0
    assert rows[0].cost_usd == 0.42
    # tolerant of null userId / latency.
    assert rows[1].user_id is None
    assert rows[1].latency_ms is None


# ===========================================================================
# 5b) unprovisioned agent ⇒ empty traces, NO HTTP call, no raise
# ===========================================================================
@pytest.mark.asyncio
async def test_get_agent_traces_unprovisioned_empty():
    called = {"http": False}

    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        called["http"] = True
        return httpx.Response(200, json={"data": [], "meta": {"totalItems": 0}})

    sm = _sm_mock()
    svc = _svc(handler, sm=sm)
    result = await svc.get_agent_traces(_agent(secret_name=None, project_id=None))

    assert result == {"data": [], "total": 0}
    assert called["http"] is False
    sm.get_secret_value.assert_not_called()


# ===========================================================================
# Cost by user — the v2 metrics query DSL grouped on ``userId``. ``/metrics/daily``
# has no user dimension, so this is a SECOND call per get_agent_metrics.
# ===========================================================================
@pytest.mark.asyncio
async def test_get_agent_metrics_includes_cost_by_user():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/metrics/daily"):
            return httpx.Response(200, json=_daily_payload())
        if request.url.path.endswith("/api/public/metrics"):
            # Shape verified against the live instance (Langfuse v3.174.1 OSS).
            return httpx.Response(
                200,
                json={
                    "data": [
                        {"userId": "lars.svensson", "sum_totalCost": 0.02, "count_count": 3},
                        {"userId": "maria.gomez", "sum_totalCost": 0.05, "count_count": 7},
                        {"userId": None, "sum_totalCost": 0.001, "count_count": 1},
                    ]
                },
            )
        return httpx.Response(404)

    metrics = await _svc(handler).get_agent_metrics(_agent(), DATE_FROM, DATE_TO)

    # Sorted by spend, descending — the biggest spender leads.
    assert [u.user_id for u in metrics.by_user] == ["maria.gomez", "lars.svensson", ""]
    assert metrics.by_user[0].cost_usd == 0.05
    assert metrics.by_user[0].traces == 7
    # A trace with no caller (e.g. a direct runtime invoke) buckets under "".
    assert metrics.by_user[2].user_id == ""
    # The rest of the metrics are unaffected by the extra call.
    assert metrics.totals.traces == 15


@pytest.mark.asyncio
async def test_by_user_failure_leaves_the_rest_of_the_metrics_intact():
    """The by-user read is BEST-EFFORT: the daily read already succeeded, so one failing
    breakdown must not zero the whole Cost tab."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/metrics/daily"):
            return httpx.Response(200, json=_daily_payload())
        return httpx.Response(500)  # the v2 by-user query fails

    metrics = await _svc(handler).get_agent_metrics(_agent(), DATE_FROM, DATE_TO)

    assert metrics.by_user == []
    assert metrics.totals.traces == 15  # daily metrics survive
    assert len(metrics.by_model) > 0
