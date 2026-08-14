// Pure metrics→chart-series shaping for the Observability dashboard (Epic 26 /
// Task 8). Framework-free so vitest (src/**/*.test.ts only) can unit-test it;
// the ObservabilityDashboard component keeps its JSX thin and delegates the
// snake_case→camelCase + palette + empty-state logic here.
//
// Input is the C4 /observability/metrics payload (ScopeMetrics = AgentMetrics ⊕
// by_agent[]). Everything is null-tolerant: a not-configured / no-data scope
// (or a null fetch result) yields empty arrays + zeroed totals + isEmpty, never
// throws — so the caller can render a calm empty-state instead of an error.
import type { ScopeMetrics } from '../../../types';
import { CHART_COLORS } from '../../govern/mockData';

export interface TimeseriesPoint {
  date: string; // YYYY-MM-DD
  traces: number;
  cost: number;
  tokens: number;
}

export interface ModelSlice {
  model: string;
  cost: number;
  tokens: number;
  color: string;
}

export interface AgentRow {
  agent_id: string;
  agent_name: string;
  tenant_id: string | null;
  traces: number;
  cost: number;
  tokens: number;
}

export interface DashboardSeries {
  timeseries: TimeseriesPoint[];
  byModel: ModelSlice[];
  byAgent: AgentRow[];
  totals: { traces: number; cost: number; tokens: number };
  /** True when the scope carries no traces, no cost, no tokens and no agents —
   *  the signal to render a calm "no data yet" card rather than empty charts. */
  isEmpty: boolean;
}

export function buildDashboardSeries(scope: ScopeMetrics | null | undefined): DashboardSeries {
  const daily = scope?.daily ?? [];
  const models = scope?.by_model ?? [];
  const agents = scope?.by_agent ?? [];

  const totals = {
    traces: scope?.totals?.traces ?? 0,
    cost: scope?.totals?.cost_usd ?? 0,
    tokens: scope?.totals?.tokens ?? 0,
  };

  const timeseries: TimeseriesPoint[] = daily.map((d) => ({
    date: d.date,
    traces: d.traces,
    cost: d.cost_usd,
    tokens: d.tokens,
  }));

  const byModel: ModelSlice[] = models.map((m, i) => ({
    model: m.model,
    cost: m.cost_usd,
    tokens: m.tokens,
    color: CHART_COLORS[i % CHART_COLORS.length],
  }));

  const byAgent: AgentRow[] = agents.map((a) => ({
    agent_id: a.agent_id,
    agent_name: a.agent_name,
    tenant_id: a.tenant_id,
    traces: a.totals.traces,
    cost: a.totals.cost_usd,
    tokens: a.totals.tokens,
  }));

  const isEmpty =
    totals.traces === 0 &&
    totals.cost === 0 &&
    totals.tokens === 0 &&
    byAgent.length === 0 &&
    timeseries.length === 0;

  return { timeseries, byModel, byAgent, totals, isEmpty };
}

// defaultDateRange — a `days`-wide inclusive window ending "today", formatted as
// YYYY-MM-DD in UTC (the wire format the Langfuse metrics API expects). Default
// 30 days. `now` is injectable so the range is deterministic under test.
export function defaultDateRange(now: Date = new Date(), days = 30): { dateFrom: string; dateTo: string } {
  const fmt = (d: Date) => d.toISOString().slice(0, 10);
  const from = new Date(now.getTime());
  from.setUTCDate(from.getUTCDate() - (days - 1));
  return { dateFrom: fmt(from), dateTo: fmt(now) };
}
