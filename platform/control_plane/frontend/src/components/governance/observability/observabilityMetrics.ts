import type { ScopeMetrics } from '../../../api/client';
import { CHART_COLORS } from '../../shared/chartTheme';

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
  isEmpty: boolean;
}

/** Inclusive window, both ends as YYYY-MM-DD. */
export interface DateRange {
  dateFrom: string;
  dateTo: string;
}

export function buildDashboardSeries(
  scope: ScopeMetrics | null | undefined,
  range?: DateRange,
): DashboardSeries {
  const daily = scope?.daily ?? [];
  const models = scope?.by_model ?? [];
  const agents = scope?.by_agent ?? [];

  const totals = {
    traces: scope?.totals?.traces ?? 0,
    cost: scope?.totals?.cost_usd ?? 0,
    tokens: scope?.totals?.tokens ?? 0,
  };

  // Langfuse's daily read can return a bucket dated 1970-01-01 for an observation with no usable timestamp.
  const inRange = (date: string) => {
    if (!range) return true;
    const day = (date ?? '').slice(0, 10);
    return day >= range.dateFrom && day <= range.dateTo;
  };
  const timeseries: TimeseriesPoint[] = daily.filter((d) => inRange(d.date)).map((d) => ({
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

export function defaultDateRange(now: Date = new Date(), days = 30): { dateFrom: string; dateTo: string } {
  const fmt = (d: Date) => d.toISOString().slice(0, 10);
  const from = new Date(now.getTime());
  from.setUTCDate(from.getUTCDate() - (days - 1));
  return { dateFrom: fmt(from), dateTo: fmt(now) };
}
