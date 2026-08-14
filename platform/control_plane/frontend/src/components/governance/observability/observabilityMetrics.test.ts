import { describe, it, expect } from 'vitest';
import type { ScopeMetrics } from '../../../types';
import { CHART_COLORS } from '../../govern/mockData';
import { buildDashboardSeries, defaultDateRange } from './observabilityMetrics';

// A fully-populated scope payload (the shape C4 /observability/metrics returns:
// AgentMetrics ⊕ by_agent[]). snake_case exactly as the backend emits.
const POPULATED: ScopeMetrics = {
  totals: { traces: 30, cost_usd: 1.5, tokens: 4200 },
  daily: [
    { date: '2026-07-01', traces: 10, cost_usd: 0.5, tokens: 1400 },
    { date: '2026-07-02', traces: 20, cost_usd: 1.0, tokens: 2800 },
  ],
  by_model: [
    { model: 'claude-haiku', cost_usd: 0.9, tokens: 2500 },
    { model: 'claude-sonnet', cost_usd: 0.6, tokens: 1700 },
  ],
  by_user: [],
  by_agent: [
    { agent_id: 'a1', agent_name: 'KYC Banking', tenant_id: 't-retail', totals: { traces: 18, cost_usd: 0.9, tokens: 2500 } },
    { agent_id: 'a2', agent_name: 'FNOL', tenant_id: null, totals: { traces: 12, cost_usd: 0.6, tokens: 1700 } },
  ],
};

// A "configured but no data yet" scope — every scalar zeroed, every list empty.
const EMPTY_SCOPE: ScopeMetrics = {
  totals: { traces: 0, cost_usd: 0, tokens: 0 },
  daily: [],
  by_model: [],
  by_user: [],
  by_agent: [],
};

describe('buildDashboardSeries', () => {
  it('maps daily rows into cost/traces/token timeseries points (order preserved)', () => {
    const series = buildDashboardSeries(POPULATED);
    expect(series.timeseries).toEqual([
      { date: '2026-07-01', traces: 10, cost: 0.5, tokens: 1400 },
      { date: '2026-07-02', traces: 20, cost: 1.0, tokens: 2800 },
    ]);
  });

  it('maps by_model into pie slices with a stable color per index', () => {
    const series = buildDashboardSeries(POPULATED);
    expect(series.byModel).toEqual([
      { model: 'claude-haiku', cost: 0.9, tokens: 2500, color: CHART_COLORS[0] },
      { model: 'claude-sonnet', cost: 0.6, tokens: 1700, color: CHART_COLORS[1] },
    ]);
  });

  it('flattens by_agent totals into table rows (agent id/name/tenant preserved, null tenant tolerated)', () => {
    const series = buildDashboardSeries(POPULATED);
    expect(series.byAgent).toEqual([
      { agent_id: 'a1', agent_name: 'KYC Banking', tenant_id: 't-retail', traces: 18, cost: 0.9, tokens: 2500 },
      { agent_id: 'a2', agent_name: 'FNOL', tenant_id: null, traces: 12, cost: 0.6, tokens: 1700 },
    ]);
  });

  it('surfaces scope totals and marks a data-bearing scope as non-empty', () => {
    const series = buildDashboardSeries(POPULATED);
    expect(series.totals).toEqual({ traces: 30, cost: 1.5, tokens: 4200 });
    expect(series.isEmpty).toBe(false);
  });

  it('empty-state: null/undefined input yields empty arrays + zero totals + isEmpty, never throws', () => {
    expect(() => buildDashboardSeries(null)).not.toThrow();
    const series = buildDashboardSeries(null);
    expect(series.timeseries).toEqual([]);
    expect(series.byModel).toEqual([]);
    expect(series.byAgent).toEqual([]);
    expect(series.totals).toEqual({ traces: 0, cost: 0, tokens: 0 });
    expect(series.isEmpty).toBe(true);
  });

  it('empty-state: a configured scope with zeroed totals + empty lists is isEmpty', () => {
    const series = buildDashboardSeries(EMPTY_SCOPE);
    expect(series.byAgent).toEqual([]);
    expect(series.isEmpty).toBe(true);
  });

  it('a scope with agents but no daily points is still non-empty', () => {
    const series = buildDashboardSeries({
      ...EMPTY_SCOPE,
      totals: { traces: 5, cost_usd: 0.2, tokens: 300 },
      by_agent: [{ agent_id: 'a9', agent_name: 'Solo', tenant_id: 't-x', totals: { traces: 5, cost_usd: 0.2, tokens: 300 } }],
    });
    expect(series.timeseries).toEqual([]);
    expect(series.byAgent).toHaveLength(1);
    expect(series.isEmpty).toBe(false);
  });

  it('cycles the palette when there are more models than colors (no undefined color)', () => {
    const many: ScopeMetrics = {
      ...EMPTY_SCOPE,
      totals: { traces: 1, cost_usd: 1, tokens: 1 },
      by_model: Array.from({ length: CHART_COLORS.length + 2 }, (_, i) => ({ model: `m${i}`, cost_usd: 1, tokens: 1 })),
    };
    const series = buildDashboardSeries(many);
    expect(series.byModel[CHART_COLORS.length].color).toBe(CHART_COLORS[0]);
    expect(series.byModel.every((s) => typeof s.color === 'string' && s.color.length > 0)).toBe(true);
  });
});

describe('defaultDateRange', () => {
  it('returns a 30-day inclusive window ending today, formatted YYYY-MM-DD (UTC)', () => {
    const { dateFrom, dateTo } = defaultDateRange(new Date('2026-07-16T00:00:00Z'));
    expect(dateTo).toBe('2026-07-16');
    expect(dateFrom).toBe('2026-06-17');
  });

  it('honors a custom window length', () => {
    const { dateFrom, dateTo } = defaultDateRange(new Date('2026-07-16T00:00:00Z'), 7);
    expect(dateTo).toBe('2026-07-16');
    expect(dateFrom).toBe('2026-07-10');
  });
});
