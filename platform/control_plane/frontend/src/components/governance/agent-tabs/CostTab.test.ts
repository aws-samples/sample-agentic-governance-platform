import { describe, it, expect } from 'vitest';
import type { AgentMetrics } from '../../../types';
import { CHART_COLORS } from '../../govern/mockData';
import { agentMetricsToSeries } from './agentObservability';

// A fully-populated per-agent metrics payload (C3 /agents/{id}/metrics shape:
// AgentMetrics — no by_agent). snake_case exactly as the backend emits.
const POPULATED: AgentMetrics = {
  totals: { traces: 24, cost_usd: 2.4, tokens: 6000 },
  daily: [
    { date: '2026-07-01', traces: 9, cost_usd: 0.9, tokens: 2200 },
    { date: '2026-07-02', traces: 15, cost_usd: 1.5, tokens: 3800 },
  ],
  by_model: [
    { model: 'claude-sonnet', cost_usd: 1.6, tokens: 4000 },
    { model: 'claude-haiku', cost_usd: 0.8, tokens: 2000 },
  ],
  by_user: [],
};

// A "provisioned but no traces yet" agent — every scalar zeroed, lists empty.
const EMPTY: AgentMetrics = {
  totals: { traces: 0, cost_usd: 0, tokens: 0 },
  daily: [],
  by_model: [],
  by_user: [],
};

describe('CostTab · agentMetricsToSeries', () => {
  it('surfaces the agent totals the KPI tiles render', () => {
    const series = agentMetricsToSeries(POPULATED);
    expect(series.totals).toEqual({ traces: 24, cost: 2.4, tokens: 6000 });
    expect(series.isEmpty).toBe(false);
  });

  it('maps daily rows into the cost/traces/token timeseries (order preserved)', () => {
    const series = agentMetricsToSeries(POPULATED);
    expect(series.timeseries).toEqual([
      { date: '2026-07-01', traces: 9, cost: 0.9, tokens: 2200 },
      { date: '2026-07-02', traces: 15, cost: 1.5, tokens: 3800 },
    ]);
  });

  it('maps by_model into pie slices with a stable palette color per index', () => {
    const series = agentMetricsToSeries(POPULATED);
    expect(series.byModel).toEqual([
      { model: 'claude-sonnet', cost: 1.6, tokens: 4000, color: CHART_COLORS[0] },
      { model: 'claude-haiku', cost: 0.8, tokens: 2000, color: CHART_COLORS[1] },
    ]);
  });

  it('a single agent carries no per-agent breakdown', () => {
    expect(agentMetricsToSeries(POPULATED).byAgent).toEqual([]);
  });

  it('empty-state: zeroed totals + empty lists is isEmpty (the calm no-data card)', () => {
    const series = agentMetricsToSeries(EMPTY);
    expect(series.totals).toEqual({ traces: 0, cost: 0, tokens: 0 });
    expect(series.timeseries).toEqual([]);
    expect(series.byModel).toEqual([]);
    expect(series.isEmpty).toBe(true);
  });

  it('empty-state: null/undefined (unprovisioned / failed read) yields isEmpty, never throws', () => {
    expect(() => agentMetricsToSeries(null)).not.toThrow();
    expect(() => agentMetricsToSeries(undefined)).not.toThrow();
    expect(agentMetricsToSeries(null).isEmpty).toBe(true);
  });
});
