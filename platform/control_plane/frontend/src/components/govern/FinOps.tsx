import { Link } from 'react-router-dom';
import {
  AreaChart, Area, BarChart, Bar, LineChart, Line, XAxis, YAxis, CartesianGrid, Tooltip, ResponsiveContainer,
  PieChart, Pie, Cell, ComposedChart, Legend,
} from 'recharts';
import {
  COST_HEALTH, COST_KPIS, SPEND_VELOCITY, COST_BY_MODEL, COST_30DAY_TREND,
  ANOMALY_ALERTS, BU_BUDGETS, AGENT_COSTS, OPTIMIZATION_OPPS, TOTAL_POTENTIAL_SAVINGS,
  FORECAST_12M, UNIT_ECONOMICS, CHARGEBACK_STATEMENT, COMMITMENTS,
  tooltipStyle,
} from './mockData';
import { ComingSoonBanner } from '../shared/comingSoon';

const anomalySeverity: Record<string, string> = {
  warning: 'bg-amber-100 text-amber-700',
  primary: 'bg-blue-100 text-blue-700',
};

const trendArrow: Record<string, string> = {
  up:   '▲',
  down: '▼',
  flat: '▸',
};
const trendColor: Record<string, string> = {
  up:   'text-rose-600',
  down: 'text-emerald-600',
  flat: 'text-slate-400',
};

const commitStatusBg: Record<string, string> = {
  'Recommended': 'bg-blue-50 text-blue-700 border-blue-200',
  'Active':      'bg-emerald-50 text-emerald-700 border-emerald-200',
  'Evaluating':  'bg-amber-50 text-amber-700 border-amber-200',
};

export default function FinOps() {
  const totalBudget = BU_BUDGETS.reduce((s, b) => s + b.monthlyBudget, 0);
  const totalSpend = BU_BUDGETS.reduce((s, b) => s + b.currentSpend, 0);
  const totalChargeback = CHARGEBACK_STATEMENT.reduce((s, c) => s + c.total, 0);
  const totalCommitmentSavings = COMMITMENTS.reduce((s, c) => s + c.savingsIfCommitted, 0);

  return (
    <div className="min-h-[calc(100vh-4rem)] relative">
      <div className="relative max-w-7xl mx-auto px-6 py-10">
        <ComingSoonBanner />
        <Link to="/" className="text-sm text-slate-400 hover:text-slate-600 transition-colors font-medium">
          ← Home
        </Link>

        <div className="flex items-end justify-between mt-3 mb-6">
          <div>
            <h1 className="text-3xl font-semibold text-slate-900 tracking-tight">Cost & FinOps</h1>
            <p className="text-slate-500 mt-1 max-w-2xl">
              Spend posture, forecast, unit economics, chargeback, and commitment planning — everything the CFO and platform FinOps lead need in one place.
            </p>
          </div>
        </div>

        {/* Row 1: Health + KPIs */}
        <div className="grid grid-cols-1 lg:grid-cols-[220px_1fr] gap-4 mb-4">
          <div className="bg-white/80 backdrop-blur-sm rounded-2xl border-2 border-emerald-200 p-5 shadow-sm text-center">
            <div className="text-[11px] uppercase tracking-widest text-slate-400">FinOps Health</div>
            <div className={`text-5xl font-bold mt-2 ${COST_HEALTH.score >= 70 ? 'text-emerald-600' : COST_HEALTH.score >= 50 ? 'text-amber-600' : 'text-rose-600'}`}>
              {COST_HEALTH.score}
            </div>
            <div className={`inline-block mt-2 text-[11px] font-semibold px-2 py-0.5 rounded-full ${COST_HEALTH.trend === 'improving' ? 'bg-emerald-50 text-emerald-700' : 'bg-amber-50 text-amber-700'}`}>
              {COST_HEALTH.trend === 'improving' ? '▲ Improving' : '▼ Declining'}
            </div>
            <div className="mt-3 text-xs text-slate-500">
              Savings realized: <span className="font-semibold text-slate-700">${COST_HEALTH.savingsRealized.toLocaleString()}</span>
              <span className="text-slate-400"> / ${COST_HEALTH.savingsTarget.toLocaleString()}</span>
            </div>
          </div>
          <div className="grid grid-cols-2 md:grid-cols-5 gap-3">
            {COST_KPIS.map(k => (
              <div key={k.label} className="bg-white/80 backdrop-blur-sm rounded-xl border border-slate-200/60 p-4 shadow-sm">
                <div className="text-[11px] font-medium text-slate-500 uppercase tracking-wide">{k.label}</div>
                <div className="text-2xl font-semibold mt-1" style={{ color: k.color }}>{k.value}</div>
                <div className="text-[11px] text-slate-400 mt-0.5">{k.sub}</div>
              </div>
            ))}
          </div>
        </div>

        {/* Anomaly alerts */}
        {ANOMALY_ALERTS.length > 0 && (
          <div className="bg-white/80 backdrop-blur-sm rounded-xl border border-amber-200 p-4 shadow-sm mb-4">
            <div className="flex items-center gap-2 mb-2">
              <span className="text-[10px] font-bold px-2 py-0.5 rounded-full bg-amber-100 text-amber-700 uppercase">Cost Anomalies</span>
              <span className="text-xs text-slate-500">{ANOMALY_ALERTS.length} active alerts</span>
            </div>
            <div className="space-y-1.5">
              {ANOMALY_ALERTS.map(a => (
                <div key={a.id} className="flex items-center gap-2 text-xs">
                  <span className={`text-[10px] font-semibold px-1.5 py-0.5 rounded ${anomalySeverity[a.severity]}`}>{a.type}</span>
                  <span className="text-slate-700">{a.desc}</span>
                  <span className="text-slate-400">· {a.bu}</span>
                  <span className="text-slate-400 ml-auto">{a.time}</span>
                </div>
              ))}
            </div>
          </div>
        )}

        {/* Row 2: Velocity + model mix */}
        <div className="grid grid-cols-1 lg:grid-cols-2 gap-4 mb-4">
          <div className="bg-white/80 backdrop-blur-sm rounded-xl border border-slate-200/60 p-5 shadow-sm">
            <div className="text-sm font-semibold text-slate-900 mb-3">Spend Velocity (today)</div>
            <ResponsiveContainer width="100%" height={180}>
              <AreaChart data={SPEND_VELOCITY}>
                <defs>
                  <linearGradient id="spendGrad2" x1="0" y1="0" x2="0" y2="1">
                    <stop offset="0%" stopColor="#f59e0b" stopOpacity={0.3} />
                    <stop offset="100%" stopColor="#f59e0b" stopOpacity={0} />
                  </linearGradient>
                </defs>
                <CartesianGrid strokeDasharray="3 3" stroke="#e2e8f0" />
                <XAxis dataKey="hour" tick={{ fill: '#94a3b8', fontSize: 10 }} interval={2} />
                <YAxis tick={{ fill: '#94a3b8', fontSize: 10 }} unit="$" />
                <Tooltip contentStyle={tooltipStyle} formatter={(v) => `$${Number(v).toFixed(2)}`} />
                <Area type="monotone" dataKey="cost" stroke="#f59e0b" fill="url(#spendGrad2)" strokeWidth={2} />
              </AreaChart>
            </ResponsiveContainer>
          </div>

          <div className="bg-white/80 backdrop-blur-sm rounded-xl border border-slate-200/60 p-5 shadow-sm">
            <div className="text-sm font-semibold text-slate-900 mb-3">Model Mix (monthly)</div>
            <ResponsiveContainer width="100%" height={180}>
              <PieChart>
                <Pie data={COST_BY_MODEL} dataKey="cost" nameKey="model" cx="50%" cy="50%" outerRadius={70} innerRadius={42} paddingAngle={2}>
                  {COST_BY_MODEL.map((m, i) => <Cell key={i} fill={m.color} />)}
                </Pie>
                <Tooltip contentStyle={tooltipStyle} formatter={(v) => `$${Number(v).toLocaleString()}`} />
              </PieChart>
            </ResponsiveContainer>
            <div className="grid grid-cols-2 gap-x-3 gap-y-1 text-[11px] mt-2">
              {COST_BY_MODEL.map(m => (
                <div key={m.model} className="flex items-center gap-1.5">
                  <span className="w-2 h-2 rounded-full" style={{ background: m.color }} />
                  <span className="text-slate-700 truncate">{m.model}</span>
                  <span className="text-slate-400 ml-auto">${m.cost.toLocaleString()}</span>
                </div>
              ))}
            </div>
          </div>
        </div>

        {/* Row 3: 30d vs budget + BU budgets */}
        <div className="grid grid-cols-1 lg:grid-cols-[2fr_1fr] gap-4 mb-4">
          <div className="bg-white/80 backdrop-blur-sm rounded-xl border border-slate-200/60 p-5 shadow-sm">
            <div className="text-sm font-semibold text-slate-900 mb-3">30-Day Cost vs Budget</div>
            <ResponsiveContainer width="100%" height={200}>
              <ComposedChart data={COST_30DAY_TREND}>
                <defs>
                  <linearGradient id="costGrad2" x1="0" y1="0" x2="0" y2="1">
                    <stop offset="0%" stopColor="#f59e0b" stopOpacity={0.3} />
                    <stop offset="100%" stopColor="#f59e0b" stopOpacity={0} />
                  </linearGradient>
                </defs>
                <CartesianGrid strokeDasharray="3 3" stroke="#e2e8f0" />
                <XAxis dataKey="day" tick={{ fill: '#94a3b8', fontSize: 10 }} />
                <YAxis tick={{ fill: '#94a3b8', fontSize: 10 }} unit="$" />
                <Tooltip contentStyle={tooltipStyle} formatter={(v) => `$${Number(v).toFixed(0)}`} />
                <Area type="monotone" dataKey="cost" stroke="#f59e0b" fill="url(#costGrad2)" strokeWidth={2} name="Daily Cost" />
                <Line type="monotone" dataKey="budget" stroke="#ef4444" strokeDasharray="5 5" dot={false} name="Daily Budget" />
                <Legend wrapperStyle={{ fontSize: '11px', paddingTop: '6px' }} />
              </ComposedChart>
            </ResponsiveContainer>
          </div>

          <div className="bg-white/80 backdrop-blur-sm rounded-xl border border-slate-200/60 p-5 shadow-sm">
            <div className="text-sm font-semibold text-slate-900 mb-1">Budget by BU</div>
            <div className="text-[11px] text-slate-400 mb-3">
              ${totalSpend.toLocaleString()} of ${totalBudget.toLocaleString()} ({((totalSpend / totalBudget) * 100).toFixed(0)}%)
            </div>
            <div className="space-y-2.5">
              {BU_BUDGETS.map(b => {
                const pct = b.currentSpend / b.monthlyBudget;
                const color = pct > 0.9 ? 'bg-rose-500' : pct > 0.75 ? 'bg-amber-500' : 'bg-emerald-500';
                return (
                  <div key={b.bu}>
                    <div className="flex items-center justify-between text-xs mb-1">
                      <span className="text-slate-700 font-medium">{b.bu}</span>
                      <span className="text-slate-400">${b.currentSpend.toLocaleString()}/${b.monthlyBudget.toLocaleString()}</span>
                    </div>
                    <div className="h-1.5 rounded-full bg-slate-100 overflow-hidden">
                      <div className={`h-full rounded-full ${color}`} style={{ width: `${Math.min(pct * 100, 100)}%` }} />
                    </div>
                  </div>
                );
              })}
            </div>
          </div>
        </div>

        {/* Row 4: 12-month forecast */}
        <div className="bg-white/80 backdrop-blur-sm rounded-xl border border-slate-200/60 p-5 shadow-sm mb-4">
          <div className="flex items-center justify-between mb-3">
            <div className="text-sm font-semibold text-slate-900">12-Month Forecast</div>
            <div className="flex items-center gap-3 text-[11px]">
              <span className="flex items-center gap-1"><span className="w-2 h-2 rounded-full bg-emerald-500" /> Conservative (+8%/mo)</span>
              <span className="flex items-center gap-1"><span className="w-2 h-2 rounded-full bg-amber-500" /> Moderate (+15%/mo)</span>
              <span className="flex items-center gap-1"><span className="w-2 h-2 rounded-full bg-rose-500" /> Aggressive (+28%/mo)</span>
            </div>
          </div>
          <ResponsiveContainer width="100%" height={220}>
            <LineChart data={FORECAST_12M}>
              <CartesianGrid strokeDasharray="3 3" stroke="#e2e8f0" />
              <XAxis dataKey="month" tick={{ fill: '#94a3b8', fontSize: 10 }} />
              <YAxis tick={{ fill: '#94a3b8', fontSize: 10 }} unit="$" />
              <Tooltip contentStyle={tooltipStyle} formatter={(v) => `$${Number(v).toLocaleString()}`} />
              <Line type="monotone" dataKey="conservative" stroke="#10b981" strokeWidth={2} />
              <Line type="monotone" dataKey="moderate"      stroke="#f59e0b" strokeWidth={2} />
              <Line type="monotone" dataKey="aggressive"    stroke="#ef4444" strokeWidth={2} />
            </LineChart>
          </ResponsiveContainer>
        </div>

        {/* Row 5: Unit Economics */}
        <div className="bg-white/80 backdrop-blur-sm rounded-xl border border-slate-200/60 p-5 shadow-sm mb-4">
          <div className="text-sm font-semibold text-slate-900 mb-3">Unit Economics</div>
          <table className="w-full text-sm">
            <thead>
              <tr className="text-[11px] text-slate-400 uppercase tracking-wide border-b border-slate-100">
                <th className="text-left py-2 font-medium">Use Case</th>
                <th className="text-right py-2 font-medium">Cost / unit</th>
                <th className="text-left py-2 font-medium pl-4">Unit</th>
                <th className="text-right py-2 font-medium">Volume / mo</th>
                <th className="text-right py-2 font-medium">Monthly cost</th>
                <th className="text-center py-2 font-medium">Trend</th>
              </tr>
            </thead>
            <tbody>
              {UNIT_ECONOMICS.map(u => (
                <tr key={u.useCase} className="border-b border-slate-50 hover:bg-slate-50/40">
                  <td className="py-2.5 text-slate-700">{u.useCase}</td>
                  <td className="py-2.5 text-right font-semibold text-slate-900 tabular-nums">${u.cost.toFixed(u.cost < 1 ? 4 : 2)}</td>
                  <td className="py-2.5 text-slate-500 text-[11px] pl-4">{u.unit}</td>
                  <td className="py-2.5 text-right text-slate-500 tabular-nums">{u.volume.toLocaleString()}</td>
                  <td className="py-2.5 text-right font-semibold text-slate-900 tabular-nums">${(u.cost * u.volume).toFixed(2)}</td>
                  <td className={`py-2.5 text-center font-semibold ${trendColor[u.trend]}`}>{trendArrow[u.trend]}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>

        {/* Row 6: Chargeback Statement */}
        <div className="bg-white/80 backdrop-blur-sm rounded-xl border border-slate-200/60 p-5 shadow-sm mb-4">
          <div className="flex items-center justify-between mb-3">
            <div>
              <div className="text-sm font-semibold text-slate-900">Chargeback — Current Month</div>
              <div className="text-[11px] text-slate-400">Showback preview, pending finance sign-off</div>
            </div>
            <div className="text-sm font-semibold text-slate-900 tabular-nums">Total: ${totalChargeback.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}</div>
          </div>
          <div className="space-y-3">
            {CHARGEBACK_STATEMENT.map(c => (
              <div key={c.bu} className="border border-slate-100 rounded-lg p-3">
                <div className="flex items-center justify-between mb-2">
                  <div className="text-sm font-semibold text-slate-900">{c.bu}</div>
                  <div className="text-sm font-semibold text-slate-900 tabular-nums">${c.total.toFixed(2)}</div>
                </div>
                <div className="space-y-1">
                  {c.items.map(it => (
                    <div key={it.useCase} className="flex items-center justify-between text-xs pl-2">
                      <span className="text-slate-500">· {it.useCase}</span>
                      <span className="text-slate-700 tabular-nums">${it.cost.toFixed(2)}</span>
                    </div>
                  ))}
                </div>
              </div>
            ))}
          </div>
        </div>

        {/* Row 7: Commitments + Optimizations side-by-side */}
        <div className="grid grid-cols-1 lg:grid-cols-2 gap-4 mb-4">
          <div className="bg-white/80 backdrop-blur-sm rounded-xl border border-slate-200/60 p-5 shadow-sm">
            <div className="flex items-center justify-between mb-3">
              <div className="text-sm font-semibold text-slate-900">Commitment Planner</div>
              <span className="text-[11px] font-semibold text-emerald-600">${totalCommitmentSavings.toLocaleString()}/mo saveable</span>
            </div>
            <div className="space-y-2">
              {COMMITMENTS.map(c => (
                <div key={c.model} className="border border-slate-100 rounded-lg p-3">
                  <div className="flex items-center justify-between mb-1">
                    <div className="text-sm font-semibold text-slate-900">{c.model}</div>
                    <span className={`text-[10px] font-semibold px-1.5 py-0.5 rounded border ${commitStatusBg[c.status]}`}>{c.status}</span>
                  </div>
                  <div className="grid grid-cols-4 gap-2 text-[11px]">
                    <div>
                      <div className="text-slate-400 uppercase tracking-widest text-[9px]">Mode</div>
                      <div className="text-slate-700 font-medium mt-0.5">{c.mode}</div>
                    </div>
                    <div>
                      <div className="text-slate-400 uppercase tracking-widest text-[9px]">Spend / mo</div>
                      <div className="text-slate-700 font-medium tabular-nums mt-0.5">${c.monthlySpend}</div>
                    </div>
                    <div>
                      <div className="text-slate-400 uppercase tracking-widest text-[9px]">If committed</div>
                      <div className="text-emerald-600 font-semibold tabular-nums mt-0.5">-${c.savingsIfCommitted}/mo</div>
                    </div>
                    <div>
                      <div className="text-slate-400 uppercase tracking-widest text-[9px]">Break-even</div>
                      <div className="text-slate-700 font-medium mt-0.5">{c.breakEvenMo > 0 ? `${c.breakEvenMo} mo` : '—'}</div>
                    </div>
                  </div>
                </div>
              ))}
            </div>
          </div>

          <div className="bg-white/80 backdrop-blur-sm rounded-xl border border-slate-200/60 p-5 shadow-sm">
            <div className="flex items-center justify-between mb-3">
              <div className="text-sm font-semibold text-slate-900">Optimization Opportunities</div>
              <span className="text-[11px] font-semibold text-emerald-600">${TOTAL_POTENTIAL_SAVINGS}/mo potential</span>
            </div>
            <div className="space-y-2">
              {OPTIMIZATION_OPPS.map(o => (
                <div key={o.id} className="flex items-start gap-3 p-2.5 rounded-lg hover:bg-slate-50/60 transition-colors">
                  <div className="text-lg font-bold text-emerald-600 w-16 flex-shrink-0 text-right">${o.savings}</div>
                  <div className="flex-1 min-w-0">
                    <div className="text-xs text-slate-700 leading-tight">{o.rec}</div>
                    <div className="flex items-center gap-2 mt-1 text-[10px]">
                      <span className="text-slate-400">Effort: <span className="text-slate-600 font-medium">{o.effort}</span></span>
                      <span className="text-slate-300">·</span>
                      <span className="text-slate-400">Risk: <span className="text-slate-600 font-medium">{o.risk}</span></span>
                    </div>
                  </div>
                </div>
              ))}
            </div>
          </div>
        </div>

        {/* Row 8: Top cost drivers */}
        <div className="bg-white/80 backdrop-blur-sm rounded-xl border border-slate-200/60 p-5 shadow-sm">
          <div className="text-sm font-semibold text-slate-900 mb-3">Top Cost Drivers — Agents</div>
          <ResponsiveContainer width="100%" height={220}>
            <BarChart data={AGENT_COSTS.slice().sort((a, b) => b.monthlyCost - a.monthlyCost)} layout="vertical" margin={{ left: 10 }}>
              <CartesianGrid horizontal={false} strokeDasharray="3 3" stroke="#e2e8f0" />
              <XAxis type="number" tick={{ fill: '#94a3b8', fontSize: 10 }} unit="$" />
              <YAxis type="category" dataKey="agent" tick={{ fill: '#475569', fontSize: 10 }} width={130} />
              <Tooltip contentStyle={tooltipStyle} formatter={(v) => `$${Number(v).toFixed(2)}`} />
              <Bar dataKey="monthlyCost" fill="#3b82f6" radius={[0, 6, 6, 0]} />
            </BarChart>
          </ResponsiveContainer>
        </div>

      </div>
    </div>
  );
}
