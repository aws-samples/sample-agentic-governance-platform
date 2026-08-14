import { useMemo, useState } from 'react';
import { Link } from 'react-router-dom';
import {
  AreaChart, Area, XAxis, YAxis, CartesianGrid, Tooltip, ResponsiveContainer, BarChart, Bar,
} from 'recharts';
import { AUDIT_EVENTS, INCIDENT_SUMMARY, RISK_TREND_30D, tooltipStyle } from './mockData';
import { ComingSoonBanner } from '../shared/comingSoon';

const catBg: Record<string, string> = {
  guardrail:  'bg-blue-50 text-blue-700 border-blue-200',
  incident:   'bg-rose-50 text-rose-700 border-rose-200',
  approval:   'bg-emerald-50 text-emerald-700 border-emerald-200',
  deployment: 'bg-violet-50 text-violet-700 border-violet-200',
  config:     'bg-slate-50 text-slate-700 border-slate-200',
};

const sevBg: Record<string, string> = {
  low:      'bg-slate-100 text-slate-600',
  medium:   'bg-amber-100 text-amber-700',
  high:     'bg-rose-100 text-rose-700',
  critical: 'bg-rose-200 text-rose-900',
};

type CatFilter = 'all' | keyof typeof catBg;
type SevFilter = 'all' | keyof typeof sevBg;

export default function AuditIncidents() {
  const [catFilter, setCatFilter] = useState<CatFilter>('all');
  const [sevFilter, setSevFilter] = useState<SevFilter>('all');
  const [search, setSearch] = useState('');
  const [selected, setSelected] = useState<typeof AUDIT_EVENTS[0] | null>(null);

  const filtered = useMemo(() => AUDIT_EVENTS.filter(e => {
    const catOk = catFilter === 'all' || e.category === catFilter;
    const sevOk = sevFilter === 'all' || e.severity === sevFilter;
    const q = search.toLowerCase();
    const searchOk = !q
      || e.summary.toLowerCase().includes(q)
      || (e.agent?.toLowerCase().includes(q) ?? false)
      || e.actor.toLowerCase().includes(q)
      || e.id.toLowerCase().includes(q);
    return catOk && sevOk && searchOk;
  }), [catFilter, sevFilter, search]);

  const countByCategory = useMemo(() => {
    const acc: Record<string, number> = {};
    AUDIT_EVENTS.forEach(e => { acc[e.category] = (acc[e.category] ?? 0) + 1; });
    return acc;
  }, []);

  const categories: { id: keyof typeof catBg; label: string }[] = [
    { id: 'guardrail',  label: 'Guardrail' },
    { id: 'incident',   label: 'Incident' },
    { id: 'approval',   label: 'Approval' },
    { id: 'deployment', label: 'Deployment' },
    { id: 'config',     label: 'Config' },
  ];

  const severities: SevFilter[] = ['all', 'critical', 'high', 'medium', 'low'];

  return (
    <div className="min-h-[calc(100vh-4rem)] relative">
      <div className="relative max-w-7xl mx-auto px-6 py-10">
        <ComingSoonBanner />
        <Link to="/" className="text-sm text-slate-400 hover:text-slate-600 transition-colors font-medium">
          ← Home
        </Link>

        <div className="flex items-end justify-between mt-3 mb-6">
          <div>
            <h1 className="text-3xl font-semibold text-slate-900 tracking-tight">Audit & Incidents</h1>
            <p className="text-slate-500 mt-1 max-w-2xl">
              Full timeline of guardrail events, incidents, approvals, deployments, and config changes. Every event links to its trace, CloudTrail record, or ticket — exportable as an evidence bundle.
            </p>
          </div>
        </div>

        {/* Summary KPIs */}
        <div className="grid grid-cols-2 md:grid-cols-5 gap-3 mb-4">
          <div className="bg-white/80 backdrop-blur-sm rounded-xl border border-slate-200/60 p-4 shadow-sm">
            <div className="text-[11px] font-medium text-slate-500 uppercase tracking-wide">Open Incidents</div>
            <div className="text-2xl font-semibold text-rose-600 mt-1">{INCIDENT_SUMMARY.open}</div>
            <div className="text-[11px] text-slate-400 mt-0.5">{INCIDENT_SUMMARY.critical} critical</div>
          </div>
          <div className="bg-white/80 backdrop-blur-sm rounded-xl border border-slate-200/60 p-4 shadow-sm">
            <div className="text-[11px] font-medium text-slate-500 uppercase tracking-wide">Resolved 7d</div>
            <div className="text-2xl font-semibold text-emerald-600 mt-1">{INCIDENT_SUMMARY.resolved7d}</div>
            <div className="text-[11px] text-slate-400 mt-0.5">↑ 2 vs last week</div>
          </div>
          <div className="bg-white/80 backdrop-blur-sm rounded-xl border border-slate-200/60 p-4 shadow-sm">
            <div className="text-[11px] font-medium text-slate-500 uppercase tracking-wide">MTTR</div>
            <div className="text-2xl font-semibold text-slate-900 mt-1">{INCIDENT_SUMMARY.mttrMin}m</div>
            <div className="text-[11px] text-slate-400 mt-0.5">Mean time to resolve</div>
          </div>
          <div className="bg-white/80 backdrop-blur-sm rounded-xl border border-slate-200/60 p-4 shadow-sm">
            <div className="text-[11px] font-medium text-slate-500 uppercase tracking-wide">Events Today</div>
            <div className="text-2xl font-semibold text-slate-900 mt-1">{AUDIT_EVENTS.filter(e => e.ts.startsWith('2026-05-08')).length}</div>
            <div className="text-[11px] text-slate-400 mt-0.5">All categories</div>
          </div>
          <div className="bg-white/80 backdrop-blur-sm rounded-xl border border-slate-200/60 p-4 shadow-sm">
            <div className="text-[11px] font-medium text-slate-500 uppercase tracking-wide">Evidence Bundles</div>
            <div className="text-2xl font-semibold text-slate-900 mt-1">12</div>
            <div className="text-[11px] text-slate-400 mt-0.5">Exportable · signed</div>
          </div>
        </div>

        {/* Row 2: charts */}
        <div className="grid grid-cols-1 lg:grid-cols-[2fr_1fr] gap-4 mb-4">
          <div className="bg-white/80 backdrop-blur-sm rounded-xl border border-slate-200/60 p-5 shadow-sm">
            <div className="flex items-center justify-between mb-3">
              <div className="text-sm font-semibold text-slate-900">30-Day Event Trend</div>
              <div className="flex items-center gap-3 text-[11px]">
                <span className="flex items-center gap-1"><span className="w-2 h-2 rounded-full bg-amber-500" /> Guardrail hits</span>
                <span className="flex items-center gap-1"><span className="w-2 h-2 rounded-full bg-rose-500" /> Violations</span>
              </div>
            </div>
            <ResponsiveContainer width="100%" height={180}>
              <AreaChart data={RISK_TREND_30D}>
                <defs>
                  <linearGradient id="hitsGrad2" x1="0" y1="0" x2="0" y2="1">
                    <stop offset="0%" stopColor="#f59e0b" stopOpacity={0.3} />
                    <stop offset="100%" stopColor="#f59e0b" stopOpacity={0} />
                  </linearGradient>
                </defs>
                <CartesianGrid strokeDasharray="3 3" stroke="#e2e8f0" />
                <XAxis dataKey="day" tick={{ fill: '#94a3b8', fontSize: 10 }} />
                <YAxis tick={{ fill: '#94a3b8', fontSize: 10 }} />
                <Tooltip contentStyle={tooltipStyle} />
                <Area type="monotone" dataKey="guardrailHits" stroke="#f59e0b" fill="url(#hitsGrad2)" strokeWidth={2} />
                <Area type="monotone" dataKey="violations"     stroke="#ef4444" fill="none" strokeWidth={2} />
              </AreaChart>
            </ResponsiveContainer>
          </div>

          <div className="bg-white/80 backdrop-blur-sm rounded-xl border border-slate-200/60 p-5 shadow-sm">
            <div className="text-sm font-semibold text-slate-900 mb-3">Events by Category</div>
            <ResponsiveContainer width="100%" height={180}>
              <BarChart
                data={categories.map(c => ({ category: c.label, count: countByCategory[c.id] ?? 0 }))}
                layout="vertical"
                margin={{ left: 10 }}
              >
                <CartesianGrid horizontal={false} strokeDasharray="3 3" stroke="#e2e8f0" />
                <XAxis type="number" tick={{ fill: '#94a3b8', fontSize: 10 }} />
                <YAxis type="category" dataKey="category" tick={{ fill: '#475569', fontSize: 10 }} width={80} />
                <Tooltip contentStyle={tooltipStyle} />
                <Bar dataKey="count" fill="#6366f1" radius={[0, 6, 6, 0]} />
              </BarChart>
            </ResponsiveContainer>
          </div>
        </div>

        {/* Filters */}
        <div className="flex flex-wrap items-center gap-3 mb-4">
          <input
            type="text"
            placeholder="Search events, agents, or trace IDs..."
            value={search}
            onChange={e => setSearch(e.target.value)}
            className="flex-1 min-w-[240px] py-2 px-3 bg-white border border-slate-200 rounded-lg text-sm outline-none focus:border-slate-400"
          />
          <div className="flex gap-1 flex-wrap">
            <button
              onClick={() => setCatFilter('all')}
              className={`px-3 py-1.5 rounded-lg text-xs font-medium transition ${catFilter === 'all' ? 'bg-slate-900 text-white' : 'bg-white text-slate-500 border border-slate-200 hover:border-slate-300'}`}
            >
              All ({AUDIT_EVENTS.length})
            </button>
            {categories.map(c => (
              <button
                key={c.id}
                onClick={() => setCatFilter(catFilter === c.id ? 'all' : c.id)}
                className={`px-3 py-1.5 rounded-lg text-xs font-medium transition ${
                  catFilter === c.id
                    ? `${catBg[c.id]} border`
                    : 'bg-white text-slate-500 border border-slate-200 hover:border-slate-300'
                }`}
              >
                {c.label} ({countByCategory[c.id] ?? 0})
              </button>
            ))}
          </div>
          <div className="flex gap-1">
            {severities.map(s => (
              <button
                key={s}
                onClick={() => setSevFilter(s)}
                className={`px-3 py-1.5 rounded-lg text-xs font-medium transition ${
                  sevFilter === s
                    ? 'bg-slate-900 text-white'
                    : 'bg-white text-slate-500 border border-slate-200 hover:border-slate-300'
                }`}
              >
                {s === 'all' ? 'All sev' : s}
              </button>
            ))}
          </div>
        </div>

        {/* Timeline */}
        <div className="bg-white/80 backdrop-blur-sm rounded-xl border border-slate-200/60 shadow-sm overflow-hidden">
          <div className="divide-y divide-slate-100">
            {filtered.map(e => (
              <button
                key={e.id}
                onClick={() => setSelected(e)}
                className="w-full text-left px-5 py-3 hover:bg-slate-50/60 transition flex items-center gap-3"
              >
                <span className="text-[11px] font-mono text-slate-400 w-32 flex-shrink-0">{e.ts}</span>
                <span className={`text-[10px] font-semibold px-1.5 py-0.5 rounded border ${catBg[e.category]} uppercase`}>
                  {e.category}
                </span>
                <span className={`text-[10px] font-semibold px-1.5 py-0.5 rounded uppercase ${sevBg[e.severity]}`}>
                  {e.severity}
                </span>
                <span className="text-sm text-slate-900 flex-1 truncate">{e.summary}</span>
                {e.agent && <span className="text-[11px] text-slate-400 hidden md:inline">{e.agent}</span>}
                <svg className="w-4 h-4 text-slate-300" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={1.5}>
                  <path strokeLinecap="round" strokeLinejoin="round" d="M4.5 19.5l15-15m0 0H8.25m11.25 0v11.25" />
                </svg>
              </button>
            ))}
          </div>
          {filtered.length === 0 && (
            <div className="text-center py-16 text-slate-400 text-sm">No events match your filter.</div>
          )}
        </div>

        {/* Event detail drawer */}
        {selected && (
          <div className="fixed inset-0 z-50">
            <div className="absolute inset-0 bg-slate-900/40 backdrop-blur-sm animate-fade-in" onClick={() => setSelected(null)} />
            <div className="absolute right-0 top-0 h-full w-full max-w-xl bg-white shadow-2xl border-l border-slate-200 flex flex-col animate-slide-in-right">
              <div className="flex items-start justify-between px-6 py-5 border-b border-slate-100">
                <div>
                  <h2 className="text-lg font-semibold text-slate-900">Event {selected.id}</h2>
                  <p className="text-sm text-slate-500 mt-0.5">{selected.ts}</p>
                </div>
                <button onClick={() => setSelected(null)} className="w-8 h-8 rounded-lg hover:bg-slate-100 flex items-center justify-center text-slate-400 hover:text-slate-600">
                  <svg className="w-5 h-5" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={1.5}><path strokeLinecap="round" strokeLinejoin="round" d="M6 18L18 6M6 6l12 12" /></svg>
                </button>
              </div>
              <div className="flex-1 overflow-y-auto px-6 py-5 space-y-5">
                <div className="flex flex-wrap gap-2">
                  <span className={`text-[10px] font-semibold px-2 py-0.5 rounded border uppercase ${catBg[selected.category]}`}>{selected.category}</span>
                  <span className={`text-[10px] font-semibold px-2 py-0.5 rounded uppercase ${sevBg[selected.severity]}`}>{selected.severity}</span>
                </div>

                <div>
                  <div className="text-[10px] uppercase tracking-widest text-slate-400">Summary</div>
                  <div className="text-sm text-slate-900 mt-1">{selected.summary}</div>
                </div>

                <div className="grid grid-cols-2 gap-3">
                  <div>
                    <div className="text-[10px] uppercase tracking-widest text-slate-400">Actor</div>
                    <div className="text-sm text-slate-900 mt-1">{selected.actor}</div>
                  </div>
                  {selected.agent && (
                    <div>
                      <div className="text-[10px] uppercase tracking-widest text-slate-400">Agent</div>
                      <div className="text-sm text-slate-900 mt-1">{selected.agent}</div>
                    </div>
                  )}
                </div>

                <div>
                  <div className="text-[10px] uppercase tracking-widest text-slate-400">Action Taken</div>
                  <div className="text-sm text-slate-900 mt-1">{selected.action}</div>
                </div>

                {selected.evidence && (
                  <div>
                    <div className="text-[10px] uppercase tracking-widest text-slate-400">Evidence</div>
                    <div className="text-sm text-slate-900 mt-1 font-mono">{selected.evidence}</div>
                    <div className="text-[11px] text-slate-500 mt-2">
                      Links to Langfuse trace, CloudTrail record, and (if incident) Jira ticket. Exportable as signed evidence bundle for audit.
                    </div>
                  </div>
                )}

                <div className="pt-3 border-t border-slate-100 flex gap-2">
                  <button className="flex-1 px-3 py-2 rounded-lg bg-slate-900 text-white text-sm font-medium hover:bg-slate-800 transition">
                    Open Trace
                  </button>
                  <button className="flex-1 px-3 py-2 rounded-lg bg-white text-slate-700 border border-slate-200 text-sm font-medium hover:bg-slate-50 transition">
                    Export Evidence
                  </button>
                </div>
              </div>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
