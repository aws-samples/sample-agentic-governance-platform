// CostTab — the per-agent "Cost" tab of AgentDetail (Epic 26 / Task 9).
//
// Renders one agent's Langfuse-backed cost + token story: KPI tiles, a cost &
// traces timeseries, and a cost-by-model donut, with a date-range selector.
// This is the per-agent sibling of the platform ObservabilityDashboard (T8) — it
// reuses the very same chart recipe (agentMetricsToSeries → buildDashboardSeries,
// the shared CHART_COLORS/tooltipStyle, the glass CARD token), so the two read as
// one design. The backend enforces tenant scope (a foreign agent 404s); this tab
// adds no role gating — VIEWER-readable like the rest of AgentDetail.
import { useEffect, useMemo, useState } from 'react';
import {
  ResponsiveContainer, ComposedChart, Area, Line, XAxis, YAxis, CartesianGrid, Tooltip, Legend,
  PieChart, Pie, Cell,
} from 'recharts';
import { observabilityApi } from '../../../api/client';
import type { AgentMetrics } from '../../../types';
import { tooltipStyle } from '../../govern/mockData';
import { defaultDateRange } from '../observability/observabilityMetrics';
import { agentMetricsToSeries, fmtUsd } from './agentObservability';
import { ObsEmptyCard } from './ObsEmptyCard';

const CARD = 'bg-white/70 backdrop-blur rounded-xl border border-slate-200/60 shadow-sm p-5';

const fmtInt = (n: number) => n.toLocaleString();

export default function CostTab({ agentId }: { agentId: string }) {
  const [range, setRange] = useState(() => defaultDateRange());
  const [metrics, setMetrics] = useState<AgentMetrics | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError('');
    observabilityApi
      .getAgentMetrics(agentId, { dateFrom: range.dateFrom, dateTo: range.dateTo })
      .then((m) => { if (!cancelled) setMetrics(m); })
      .catch((e) => { if (!cancelled) setError(e instanceof Error ? e.message : 'Failed to load cost metrics'); })
      .finally(() => { if (!cancelled) setLoading(false); });
    return () => { cancelled = true; };
  }, [agentId, range.dateFrom, range.dateTo]);

  const series = useMemo(() => agentMetricsToSeries(metrics), [metrics]);

  return (
    <div className="space-y-4">
      {/* Date-range selector (mirrors the dashboard controls) */}
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div className="flex items-end gap-4">
          <div>
            <label className="block text-[11px] font-medium text-slate-500 uppercase tracking-wide mb-1">From</label>
            <input
              type="date"
              value={range.dateFrom}
              max={range.dateTo}
              onChange={(e) => setRange((r) => ({ ...r, dateFrom: e.target.value }))}
              className="px-3 py-1.5 border border-slate-200 rounded-lg text-sm text-slate-700 bg-white"
            />
          </div>
          <div>
            <label className="block text-[11px] font-medium text-slate-500 uppercase tracking-wide mb-1">To</label>
            <input
              type="date"
              value={range.dateTo}
              min={range.dateFrom}
              onChange={(e) => setRange((r) => ({ ...r, dateTo: e.target.value }))}
              className="px-3 py-1.5 border border-slate-200 rounded-lg text-sm text-slate-700 bg-white"
            />
          </div>
        </div>
        {loading && (
          <div className="flex items-center gap-2 text-xs text-slate-400">
            <span className="w-3.5 h-3.5 border-2 border-slate-300 border-t-slate-500 rounded-full animate-spin" />
            Loading cost metrics…
          </div>
        )}
      </div>

      {error ? (
        <ObsEmptyCard title="Couldn't load cost metrics" body={error} tone="amber" />
      ) : series.isEmpty && !loading ? (
        <ObsEmptyCard
          title="No cost data in this window"
          body="This agent has no recorded Langfuse activity for the selected dates. Once it's provisioned and invoked, its cost and token usage will appear here — try widening the date range."
        />
      ) : (
        <>
          {/* KPI tiles */}
          <div className="grid grid-cols-1 sm:grid-cols-3 gap-3">
            <Kpi label="Total Cost" value={fmtUsd(series.totals.cost)} sub="model spend (USD)" color="#f59e0b" />
            <Kpi label="Total Tokens" value={fmtInt(series.totals.tokens)} sub="input + output" color="#6366f1" />
            <Kpi label="Total Traces" value={fmtInt(series.totals.traces)} sub="invocations" color="#3b82f6" />
          </div>

          {/* Cost & traces timeseries + cost by model */}
          <div className="grid grid-cols-1 lg:grid-cols-[2fr_1fr] gap-4">
            <div className={CARD}>
              <div className="text-sm font-semibold text-slate-900 mb-3">Cost &amp; Traces</div>
              {series.timeseries.length > 0 ? (
                <ResponsiveContainer width="100%" height={220}>
                  <ComposedChart data={series.timeseries}>
                    <defs>
                      <linearGradient id="agentCostGrad" x1="0" y1="0" x2="0" y2="1">
                        <stop offset="0%" stopColor="#f59e0b" stopOpacity={0.3} />
                        <stop offset="100%" stopColor="#f59e0b" stopOpacity={0} />
                      </linearGradient>
                    </defs>
                    <CartesianGrid strokeDasharray="3 3" stroke="#e2e8f0" />
                    <XAxis dataKey="date" tick={{ fill: '#94a3b8', fontSize: 10 }} />
                    <YAxis yAxisId="cost" tick={{ fill: '#94a3b8', fontSize: 10 }} unit="$" />
                    <YAxis yAxisId="traces" orientation="right" tick={{ fill: '#94a3b8', fontSize: 10 }} />
                    <Tooltip
                      contentStyle={tooltipStyle}
                      formatter={(v, name) => (name === 'Cost' ? fmtUsd(Number(v)) : fmtInt(Number(v)))}
                    />
                    <Area yAxisId="cost" type="monotone" dataKey="cost" name="Cost" stroke="#f59e0b" fill="url(#agentCostGrad)" strokeWidth={2} />
                    <Line yAxisId="traces" type="monotone" dataKey="traces" name="Traces" stroke="#3b82f6" strokeWidth={2} dot={false} />
                    <Legend wrapperStyle={{ fontSize: '11px', paddingTop: '6px' }} />
                  </ComposedChart>
                </ResponsiveContainer>
              ) : (
                <div className="h-[220px] flex items-center justify-center text-sm text-slate-400">No daily breakdown for this range.</div>
              )}
            </div>

            <div className={CARD}>
              <div className="text-sm font-semibold text-slate-900 mb-3">Cost by Model</div>
              {series.byModel.length > 0 ? (
                <>
                  <ResponsiveContainer width="100%" height={180}>
                    <PieChart>
                      <Pie data={series.byModel} dataKey="cost" nameKey="model" cx="50%" cy="50%" outerRadius={70} innerRadius={42} paddingAngle={2}>
                        {series.byModel.map((m) => <Cell key={m.model} fill={m.color} />)}
                      </Pie>
                      <Tooltip contentStyle={tooltipStyle} formatter={(v) => fmtUsd(Number(v))} />
                    </PieChart>
                  </ResponsiveContainer>
                  <div className="grid grid-cols-1 gap-y-1 text-[11px] mt-2">
                    {series.byModel.map((m) => (
                      <div key={m.model} className="flex items-center gap-1.5">
                        <span className="w-2 h-2 rounded-full flex-shrink-0" style={{ background: m.color }} />
                        <span className="text-slate-700 truncate">{m.model}</span>
                        <span className="text-slate-400 ml-auto tabular-nums">{fmtUsd(m.cost)}</span>
                      </div>
                    ))}
                  </div>
                </>
              ) : (
                <div className="h-[180px] flex items-center justify-center text-sm text-slate-400">No model breakdown yet.</div>
              )}
            </div>
          </div>

          {/* Cost by User — spend attributed to the Entra identity the agent stamps on
              each trace, so it answers "who is driving this agent's spend". */}
          <div className={CARD}>
            <div className="text-sm font-semibold text-slate-900 mb-3">Cost by User</div>
            {(metrics?.by_user?.length ?? 0) > 0 ? (
              <table className="w-full text-sm">
                <thead>
                  <tr className="text-[11px] uppercase tracking-wide text-slate-400 border-b border-slate-200/70">
                    <th className="text-left py-2 font-medium">User</th>
                    <th className="text-right py-2 font-medium">Invocations</th>
                    <th className="text-right py-2 font-medium">Cost</th>
                  </tr>
                </thead>
                <tbody>
                  {metrics!.by_user.map((u) => (
                    <tr key={u.user_id || '__nouser__'} className="border-b border-slate-100 last:border-0">
                      <td className="py-2.5 text-slate-700">
                        {u.user_id || <span className="text-slate-400">No user (direct invoke)</span>}
                      </td>
                      <td className="py-2.5 text-right text-slate-600 tabular-nums">{fmtInt(u.traces)}</td>
                      <td className="py-2.5 text-right font-semibold text-slate-900 tabular-nums">{fmtUsd(u.cost_usd)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            ) : (
              <div className="py-8 text-center text-sm text-slate-400">
                No per-user spend yet. Invocations carrying a caller identity will appear here.
              </div>
            )}
          </div>
        </>
      )}
    </div>
  );
}

function Kpi({ label, value, sub, color }: { label: string; value: string; sub: string; color: string }) {
  return (
    <div className={CARD}>
      <div className="text-[11px] font-medium text-slate-500 uppercase tracking-wide">{label}</div>
      <div className="text-2xl font-semibold mt-1 tabular-nums" style={{ color }}>{value}</div>
      <div className="text-[11px] text-slate-400 mt-0.5">{sub}</div>
    </div>
  );
}
