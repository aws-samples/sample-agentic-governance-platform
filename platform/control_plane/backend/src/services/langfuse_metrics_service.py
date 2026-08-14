"""Read-only Langfuse metrics service (Epic 26, Task 5 — contract C3).

Fetches per-agent cost / token / trace metrics and trace lists FROM Langfuse's public
read API and aggregates them server-side (fan-out) for project / tenant / platform
dashboards. Langfuse has NO cross-project aggregation endpoint, so scope metrics are
built by calling one per-agent read and summing here.

This service is PURE READ — it never mutates Langfuse or the registry. It is the
observability read path the platform renders its own dashboards from (E26 replaces the
bare Langfuse iframe embed).

Attribution is STRUCTURAL (E26 architecture): one agent = one Langfuse project = one
API key. The agent authenticates with its own project key so its traces land in its own
project (no trace tags). This service reads that project's data using the SAME per-agent
key pair the agent ingests with — fetched from Secrets Manager via the C1 join field
``agent.langfuse_key_secret_name`` (the SM secret JSON is ``{public_key, secret_key,
...}``, the ``langfuse_provisioning`` convention).

Endpoints (research ``…-e26-langfuse-data-api.md`` PART B, self-hosted Langfuse v3):
  - ``GET /api/public/metrics/daily?fromTimestamp&toTimestamp`` → ``data[]`` per-day rows
    (``countTraces``, ``totalCost``, per-model ``usage[]`` with ``totalUsage``/``totalCost``).
  - ``GET /api/public/traces?page&limit`` → ``data[]`` trace list (``id``, ``timestamp``,
    ``name``, ``userId``, ``latency`` [seconds], ``totalCost``) + ``meta.totalItems``.
Auth is HTTP **Basic** (username = public key, password = secret key), project-scoped.

SECURITY: the pk/sk secret VALUES are used ONLY to build the Basic-auth header. They are
NEVER logged, never returned, and never placed on any returned shape or in an exception
message. On a read failure only the HTTP status is logged.

DEGRADE-AND-CONTINUE: an agent with no ``langfuse_key_secret_name`` (unprovisioned), a
missing secret, or a failed read contributes ZEROED metrics and NEVER raises — so a
single bad agent can never break a whole dashboard's fan-out.

TESTABILITY: the httpx client, the Secrets Manager client, and the TTL-cache CLOCK are
all injectable (mirroring ``connection_service``'s injected ``now`` + ``graph_service``'s
injected ``http_client``). Tests pass an ``httpx.MockTransport`` client, a ``MagicMock``
Secrets Manager, and a fake clock they advance to exercise the 60s cache window without
sleeping.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from datetime import date
from typing import Callable, Optional

import boto3
import httpx
from pydantic import BaseModel

logger = logging.getLogger(__name__)

# The public read endpoints (same origin the agents export OTLP to).
_METRICS_DAILY_PATH = "/api/public/metrics/daily"
_METRICS_PATH = "/api/public/metrics"  # v2 query-DSL endpoint — the only one that groups by user
_TRACES_PATH = "/api/public/traces"

# In-process TTL for the daily-metrics call so dashboard refreshes don't hammer Langfuse.
# Keyed on (secret_name, date_from, date_to) — the daily read is deterministic for a
# given key + window, so a ~60s reuse is safe and cheap.
_CACHE_TTL_SECONDS = 60.0

# httpx timeout for a single read (mirrors the provisioner's 10s reads).
_HTTP_TIMEOUT_SECONDS = 10.0


# ===========================================================================
# DTOs (Pydantic v2, mirroring the models/agent.py DTO idiom) — contract C3
# ===========================================================================
class MetricTotals(BaseModel):
    traces: int = 0
    cost_usd: float = 0.0
    tokens: int = 0


class DailyMetric(BaseModel):
    date: str  # "YYYY-MM-DD"
    traces: int = 0
    cost_usd: float = 0.0
    tokens: int = 0


class ModelMetric(BaseModel):
    model_config = {"protected_namespaces": ()}  # ``model`` is a data field, not pydantic internal

    model: str
    cost_usd: float = 0.0
    tokens: int = 0


class UserMetric(BaseModel):
    user_id: str  # the caller's Entra identity as stamped on the trace; "" ⇒ no user (direct invoke)
    cost_usd: float = 0.0
    traces: int = 0


class AgentMetrics(BaseModel):
    totals: MetricTotals = MetricTotals()
    daily: list[DailyMetric] = []
    by_model: list[ModelMetric] = []
    by_user: list[UserMetric] = []


class TraceRow(BaseModel):
    id: str
    timestamp: Optional[str] = None
    name: Optional[str] = None
    user_id: Optional[str] = None
    latency_ms: Optional[float] = None
    cost_usd: float = 0.0


def _zeroed() -> AgentMetrics:
    """A zeroed AgentMetrics (unprovisioned / failed read) — the degrade-and-continue value."""
    return AgentMetrics(totals=MetricTotals(), daily=[], by_model=[], by_user=[])


class LangfuseMetricsService:
    """Read per-agent Langfuse metrics/traces and aggregate across agents (C3)."""

    def __init__(
        self,
        *,
        langfuse_host: str,
        region: str = "us-east-1",
        secrets_client=None,
        http_client: Optional[httpx.AsyncClient] = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._host = (langfuse_host or "").rstrip("/")
        self._region = region
        # Secrets Manager — injectable for tests (MagicMock / moto), lazily built otherwise.
        self._sm = secrets_client or boto3.client("secretsmanager", region_name=region)
        # INJECTED client (tests' single-loop MockTransport) is caller-owned and reused; a
        # non-injected service opens a fresh AsyncClient per read (routes run on the uvicorn
        # loop — no asyncio.run teardown races here, so no per-loop map is needed).
        self._injected_client = http_client
        # Injected clock for the TTL cache (tests advance a fake clock; prod = monotonic).
        self._clock = clock
        # TTL cache: {(secret_name, from_iso, to_iso): (expiry_epoch, AgentMetrics)}.
        self._daily_cache: dict[tuple[str, str, str], tuple[float, AgentMetrics]] = {}

    # -- http client lifecycle ---------------------------------------------
    @contextlib.asynccontextmanager
    async def _client(self):
        """Yield an httpx client: the injected one (caller-owned) or a fresh per-call one."""
        if self._injected_client is not None:
            yield self._injected_client
        else:  # pragma: no cover - exercised only in the live/route path, not unit tests
            async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT_SECONDS) as client:
                yield client

    # -- secrets (never logged) --------------------------------------------
    def _load_keys(self, secret_name: str) -> tuple[str, str]:
        """Fetch the per-agent ``{public_key, secret_key}`` pair from Secrets Manager.

        Returns ``(public_key, secret_key)`` — the VALUES used only to build the Basic-auth
        header (never logged, never returned to a caller)."""
        raw = self._sm.get_secret_value(SecretId=secret_name)["SecretString"]
        body = json.loads(raw)
        return body["public_key"], body["secret_key"]

    # -- per-agent metrics (C3) --------------------------------------------
    async def get_agent_metrics(
        self, agent, date_from: date, date_to: date
    ) -> AgentMetrics:
        """Per-agent cost / tokens / trace-count metrics over ``[date_from, date_to]``.

        Reads the agent's project key from Secrets Manager and calls
        ``/api/public/metrics/daily``, mapping ``data[]`` rows into ``AgentMetrics``
        (per-day rows + summed totals + per-model aggregation). The daily call is TTL-cached
        (~60s) keyed on ``(secret_name, date_from, date_to)``.

        DEGRADE-AND-CONTINUE: an unprovisioned agent (no ``langfuse_key_secret_name``) makes
        NO HTTP call (and NO Secrets Manager read) and returns zeroed metrics; a missing
        secret or a failed read likewise returns zeroed. This NEVER raises."""
        secret_name = getattr(agent, "langfuse_key_secret_name", None)
        if not secret_name:
            return _zeroed()

        from_iso = f"{date_from.isoformat()}T00:00:00Z"
        to_iso = f"{date_to.isoformat()}T23:59:59Z"
        cache_key = (secret_name, from_iso, to_iso)

        cached = self._daily_cache.get(cache_key)
        if cached is not None and self._clock() < cached[0]:
            return cached[1]

        try:
            public_key, secret_key = self._load_keys(secret_name)
            params = {"fromTimestamp": from_iso, "toTimestamp": to_iso}
            async with self._client() as client:
                resp = await client.get(
                    f"{self._host}{_METRICS_DAILY_PATH}",
                    params=params,
                    auth=httpx.BasicAuth(public_key, secret_key),
                )
            if resp.status_code != 200:
                logger.warning(
                    "[langfuse_metrics] daily read for agent %s returned status %s — "
                    "degrading to zero",
                    getattr(agent, "id", "?"),
                    resp.status_code,
                )
                return _zeroed()
            metrics = _map_daily(resp.json())
            # Cost-by-user: /metrics/daily cannot group by user, so this is a second
            # (v2 query-DSL) call. Best-effort — a failure leaves by_user empty rather
            # than degrading the whole card set, since the daily read already succeeded.
            metrics.by_user = await self._fetch_by_user(
                agent, public_key, secret_key, from_iso, to_iso
            )
        except Exception:  # noqa: BLE001 — degrade-and-continue; never break a dashboard
            logger.warning(
                "[langfuse_metrics] daily read failed for agent %s — degrading to zero",
                getattr(agent, "id", "?"),
                exc_info=True,
            )
            return _zeroed()

        # Cache ONLY successful reads (never cache a degraded zero).
        self._daily_cache[cache_key] = (self._clock() + _CACHE_TTL_SECONDS, metrics)
        return metrics

    async def _fetch_by_user(
        self, agent, public_key: str, secret_key: str, from_iso: str, to_iso: str
    ) -> list[UserMetric]:
        """Cost + trace count grouped by the invoking user, via the v2 metrics query DSL.

        ``/metrics/daily`` has no user dimension, so cost-by-user needs this second call.
        Users come from the ``langfuse.user.id`` the agent stamps on each trace (the caller's
        Entra identity), so spend attributes to a governed principal. Traces with no user
        (e.g. a direct runtime invoke) group under ``user_id=""``.

        BEST-EFFORT: any failure returns ``[]`` — the daily metrics already succeeded, so one
        missing card must not zero the whole Cost tab. NEVER raises."""
        query = json.dumps(
            {
                "view": "traces",
                "metrics": [
                    {"measure": "totalCost", "aggregation": "sum"},
                    {"measure": "count", "aggregation": "count"},
                ],
                "dimensions": [{"field": "userId"}],
                "fromTimestamp": from_iso,
                "toTimestamp": to_iso,
            }
        )
        try:
            async with self._client() as client:
                resp = await client.get(
                    f"{self._host}{_METRICS_PATH}",
                    params={"query": query},
                    auth=httpx.BasicAuth(public_key, secret_key),
                )
            if resp.status_code != 200:
                logger.warning(
                    "[langfuse_metrics] by-user read for agent %s returned status %s — "
                    "omitting the cost-by-user breakdown",
                    getattr(agent, "id", "?"),
                    resp.status_code,
                )
                return []
            rows = (resp.json() or {}).get("data") or []
        except Exception:  # noqa: BLE001 — best-effort; the rest of the metrics stand
            logger.warning(
                "[langfuse_metrics] by-user read failed for agent %s — omitting the "
                "cost-by-user breakdown",
                getattr(agent, "id", "?"),
                exc_info=True,
            )
            return []

        out: list[UserMetric] = []
        for row in rows:
            out.append(
                UserMetric(
                    user_id=str(row.get("userId") or ""),
                    cost_usd=float(row.get("sum_totalCost") or 0.0),
                    traces=int(row.get("count_count") or 0),
                )
            )
        out.sort(key=lambda u: u.cost_usd, reverse=True)
        return out

    async def get_agent_traces(
        self, agent, page: int = 1, limit: int = 50
    ) -> dict:
        """Paged trace list for one agent → ``{"data": [TraceRow], "total": int}``.

        Reads the agent's project key from Secrets Manager and calls
        ``/api/public/traces?page&limit``, mapping the trace list into ``TraceRow[]`` and
        surfacing ``meta.totalItems`` as ``total``. NOT TTL-cached (trace lists change more
        often than daily aggregates and are paged).

        DEGRADE-AND-CONTINUE: an unprovisioned agent makes NO HTTP call and returns
        ``{"data": [], "total": 0}``; a failed read likewise. NEVER raises."""
        secret_name = getattr(agent, "langfuse_key_secret_name", None)
        if not secret_name:
            return {"data": [], "total": 0}

        try:
            public_key, secret_key = self._load_keys(secret_name)
            params = {"page": page, "limit": limit}
            async with self._client() as client:
                resp = await client.get(
                    f"{self._host}{_TRACES_PATH}",
                    params=params,
                    auth=httpx.BasicAuth(public_key, secret_key),
                )
            if resp.status_code != 200:
                logger.warning(
                    "[langfuse_metrics] traces read for agent %s returned status %s — "
                    "degrading to empty",
                    getattr(agent, "id", "?"),
                    resp.status_code,
                )
                return {"data": [], "total": 0}
            body = resp.json()
        except Exception:  # noqa: BLE001 — degrade-and-continue; never break a dashboard
            logger.warning(
                "[langfuse_metrics] traces read failed for agent %s — degrading to empty",
                getattr(agent, "id", "?"),
                exc_info=True,
            )
            return {"data": [], "total": 0}

        rows = [_map_trace(t) for t in body.get("data", [])]
        total = int((body.get("meta") or {}).get("totalItems", len(rows)))
        return {"data": rows, "total": total}

    async def get_scope_metrics(
        self, agents: list, date_from: date, date_to: date
    ) -> AgentMetrics:
        """Fan out one ``get_agent_metrics`` per agent and aggregate server-side (C3).

        Sums totals, merges ``daily`` by date, and merges ``by_model`` by model across all
        agents — the project / tenant / platform headline (Langfuse has no cross-project
        aggregation, so it is built here). DEGRADE-AND-CONTINUE: an unprovisioned agent or a
        failed read contributes zero (``get_agent_metrics`` already returns zeroed and never
        raises); a defensive guard here treats any stray exception as zero too."""
        if not agents:
            return _zeroed()

        results = await asyncio.gather(
            *(self.get_agent_metrics(a, date_from, date_to) for a in agents),
            return_exceptions=True,
        )

        totals = MetricTotals()
        daily_by_date: dict[str, DailyMetric] = {}
        by_model: dict[str, ModelMetric] = {}
        by_user: dict[str, UserMetric] = {}

        for res in results:
            if isinstance(res, BaseException):  # defensive — get_agent_metrics never raises
                continue
            totals.traces += res.totals.traces
            totals.cost_usd += res.totals.cost_usd
            totals.tokens += res.totals.tokens
            for d in res.daily:
                agg = daily_by_date.get(d.date)
                if agg is None:
                    daily_by_date[d.date] = DailyMetric(
                        date=d.date, traces=d.traces, cost_usd=d.cost_usd, tokens=d.tokens
                    )
                else:
                    agg.traces += d.traces
                    agg.cost_usd += d.cost_usd
                    agg.tokens += d.tokens
            for m in res.by_model:
                agg_m = by_model.get(m.model)
                if agg_m is None:
                    by_model[m.model] = ModelMetric(
                        model=m.model, cost_usd=m.cost_usd, tokens=m.tokens
                    )
                else:
                    agg_m.cost_usd += m.cost_usd
                    agg_m.tokens += m.tokens
            # Same user across several agents rolls up to one row — a scope view answers
            # "what has this person cost us", not "per agent".
            for u in res.by_user:
                agg_u = by_user.get(u.user_id)
                if agg_u is None:
                    by_user[u.user_id] = UserMetric(
                        user_id=u.user_id, cost_usd=u.cost_usd, traces=u.traces
                    )
                else:
                    agg_u.cost_usd += u.cost_usd
                    agg_u.traces += u.traces

        return AgentMetrics(
            totals=totals,
            daily=[daily_by_date[k] for k in sorted(daily_by_date)],
            by_model=list(by_model.values()),
            by_user=sorted(by_user.values(), key=lambda u: u.cost_usd, reverse=True),
        )


# ===========================================================================
# Pure mappers (payload → DTOs)
# ===========================================================================
def _map_daily(payload: dict) -> AgentMetrics:
    """Map a ``/metrics/daily`` payload into AgentMetrics (per-day rows + totals + by_model)."""
    daily: list[DailyMetric] = []
    totals = MetricTotals()
    by_model: dict[str, ModelMetric] = {}

    for row in payload.get("data", []):
        usage = row.get("usage") or []
        day_tokens = sum(int(u.get("totalUsage") or 0) for u in usage)
        traces = int(row.get("countTraces") or 0)
        cost = float(row.get("totalCost") or 0.0)

        daily.append(
            DailyMetric(
                date=row.get("date", ""),
                traces=traces,
                cost_usd=cost,
                tokens=day_tokens,
            )
        )
        totals.traces += traces
        totals.cost_usd += cost
        totals.tokens += day_tokens

        for u in usage:
            model = u.get("model") or "unknown"
            agg = by_model.get(model)
            u_cost = float(u.get("totalCost") or 0.0)
            u_tokens = int(u.get("totalUsage") or 0)
            if agg is None:
                by_model[model] = ModelMetric(
                    model=model, cost_usd=u_cost, tokens=u_tokens
                )
            else:
                agg.cost_usd += u_cost
                agg.tokens += u_tokens

    return AgentMetrics(totals=totals, daily=daily, by_model=list(by_model.values()))


def _map_trace(t: dict) -> TraceRow:
    """Map a Langfuse trace dict → TraceRow (``latency`` seconds → ``latency_ms`` ms)."""
    latency = t.get("latency")
    latency_ms = float(latency) * 1000.0 if latency is not None else None
    return TraceRow(
        id=t.get("id", ""),
        timestamp=t.get("timestamp"),
        name=t.get("name"),
        user_id=t.get("userId"),
        latency_ms=latency_ms,
        cost_usd=float(t.get("totalCost") or 0.0),
    )
