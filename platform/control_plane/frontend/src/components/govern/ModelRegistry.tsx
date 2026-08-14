import { useMemo, useState } from 'react';
import { Link } from 'react-router-dom';
import {
  LineChart, Line, XAxis, YAxis, CartesianGrid, Tooltip, ResponsiveContainer,
} from 'recharts';
import { MODELS, MODEL_DETAILS, tooltipStyle } from './mockData';
import ModelDrawer from './ModelDrawer';
import { ComingSoonBanner } from '../shared/comingSoon';

const tierBg: Record<string, string> = {
  'Tier 1': 'bg-rose-50 text-rose-700 ring-rose-200',
  'Tier 2': 'bg-amber-50 text-amber-700 ring-amber-200',
  'Tier 3': 'bg-emerald-50 text-emerald-700 ring-emerald-200',
};

const statusBg: Record<string, string> = {
  'Production':     'bg-emerald-50 text-emerald-700 border-emerald-200',
  'Pending Review': 'bg-amber-50 text-amber-700 border-amber-200',
};

export default function ModelRegistry() {
  const [filter, setFilter] = useState<'all' | 'Tier 1' | 'Tier 2' | 'Tier 3'>('all');
  const [statusFilter, setStatusFilter] = useState<'all' | 'Production' | 'Pending Review'>('all');
  const [search, setSearch] = useState('');
  const [openModel, setOpenModel] = useState<string | null>(null);

  const filtered = useMemo(() => MODELS.filter(m => {
    const tierOk = filter === 'all' || m.tier === filter;
    const statusOk = statusFilter === 'all' || m.status === statusFilter;
    const q = search.toLowerCase();
    const searchOk = !q
      || m.name.toLowerCase().includes(q)
      || m.provider.toLowerCase().includes(q)
      || m.owner.toLowerCase().includes(q);
    return tierOk && statusOk && searchOk;
  }), [filter, statusFilter, search]);

  const totalCost = MODELS.reduce((s, m) => s + m.monthlyCost, 0);
  const totalUseCases = MODELS.reduce((s, m) => s + m.useCases, 0);
  const attested = Object.values(MODEL_DETAILS).filter(d => d.attestation.sr11_7.attested).length;
  const pendingAttestation = MODELS.length - attested;

  // Aggregate eval history: for each date, mean score across all models that have it
  const aggregateEval = useMemo(() => {
    const byDate: Record<string, { safety: number[]; quality: number[]; latency: number[] }> = {};
    Object.values(MODEL_DETAILS).forEach(d => {
      d.evalHistory.forEach(e => {
        if (!byDate[e.date]) byDate[e.date] = { safety: [], quality: [], latency: [] };
        byDate[e.date].safety.push(e.safety);
        byDate[e.date].quality.push(e.quality);
        byDate[e.date].latency.push(e.latency);
      });
    });
    const mean = (xs: number[]) => Math.round(xs.reduce((a, b) => a + b, 0) / xs.length);
    return Object.entries(byDate).sort(([a], [b]) => a.localeCompare(b)).map(([date, xs]) => ({
      date,
      safety: mean(xs.safety),
      quality: mean(xs.quality),
      latency: mean(xs.latency),
    }));
  }, []);

  return (
    <div className="min-h-[calc(100vh-4rem)] relative">
      <div className="relative max-w-7xl mx-auto px-6 py-10">
        <ComingSoonBanner />
        <Link to="/" className="text-sm text-slate-400 hover:text-slate-600 transition-colors font-medium">
          ← Home
        </Link>

        <div className="flex items-end justify-between mt-3 mb-6">
          <div>
            <h1 className="text-3xl font-semibold text-slate-900 tracking-tight">Model Registry</h1>
            <p className="text-slate-500 mt-1 max-w-2xl">
              Every foundation model in use, with owner, risk tier, eval score, cost, attestation state, and approval chain. Click any row for the full Model 360.
            </p>
          </div>
        </div>

        {/* Summary KPIs */}
        <div className="grid grid-cols-2 md:grid-cols-5 gap-3 mb-6">
          {[
            { label: 'Models Registered', value: MODELS.length,                 sub: `${MODELS.filter(m => m.status === 'Production').length} in production` },
            { label: 'Use Cases',          value: totalUseCases,                 sub: 'across the fleet' },
            { label: 'Monthly Cost',       value: `$${totalCost.toLocaleString()}`, sub: `~$${(totalCost * 12 / 1000).toFixed(1)}k/yr` },
            { label: 'SR 11-7 Attested',   value: `${attested}/${MODELS.length}`,   sub: `${pendingAttestation} pending` },
            { label: 'Avg Eval Score',     value: Math.round(MODELS.reduce((s, m) => s + m.evalScore, 0) / MODELS.length), sub: 'quality/safety/latency' },
          ].map(k => (
            <div key={k.label} className="bg-white/80 backdrop-blur-sm rounded-xl border border-slate-200/60 p-4 shadow-sm">
              <div className="text-[11px] font-medium text-slate-500 uppercase tracking-wide">{k.label}</div>
              <div className="text-2xl font-semibold text-slate-900 mt-1">{k.value}</div>
              <div className="text-[11px] text-slate-400 mt-0.5">{k.sub}</div>
            </div>
          ))}
        </div>

        {/* Fleet eval trend */}
        <div className="bg-white/80 backdrop-blur-sm rounded-xl border border-slate-200/60 p-5 shadow-sm mb-6">
          <div className="flex items-center justify-between mb-3">
            <div className="text-sm font-semibold text-slate-900">Fleet-Wide Eval Trend</div>
            <div className="flex items-center gap-3 text-[11px]">
              <span className="flex items-center gap-1"><span className="w-2 h-2 rounded-full bg-emerald-500" /> Safety</span>
              <span className="flex items-center gap-1"><span className="w-2 h-2 rounded-full bg-blue-500" /> Quality</span>
              <span className="flex items-center gap-1"><span className="w-2 h-2 rounded-full bg-amber-500" /> Latency</span>
            </div>
          </div>
          <ResponsiveContainer width="100%" height={200}>
            <LineChart data={aggregateEval}>
              <CartesianGrid strokeDasharray="3 3" stroke="#e2e8f0" />
              <XAxis dataKey="date" tick={{ fill: '#94a3b8', fontSize: 10 }} />
              <YAxis domain={[50, 100]} tick={{ fill: '#94a3b8', fontSize: 10 }} />
              <Tooltip contentStyle={tooltipStyle} />
              <Line type="monotone" dataKey="safety"  stroke="#10b981" strokeWidth={2} />
              <Line type="monotone" dataKey="quality" stroke="#3b82f6" strokeWidth={2} />
              <Line type="monotone" dataKey="latency" stroke="#f59e0b" strokeWidth={2} />
            </LineChart>
          </ResponsiveContainer>
        </div>

        {/* Filters */}
        <div className="flex flex-wrap items-center gap-3 mb-4">
          <input
            type="text"
            placeholder="Search models, providers, owners..."
            value={search}
            onChange={e => setSearch(e.target.value)}
            className="flex-1 min-w-[240px] py-2 px-3 bg-white border border-slate-200 rounded-lg text-sm outline-none focus:border-slate-400"
          />
          <div className="flex gap-1">
            {(['all', 'Tier 1', 'Tier 2', 'Tier 3'] as const).map(t => (
              <button
                key={t}
                onClick={() => setFilter(t)}
                className={`px-3 py-1.5 rounded-lg text-xs font-medium transition ${
                  filter === t
                    ? 'bg-slate-900 text-white'
                    : 'bg-white text-slate-500 border border-slate-200 hover:border-slate-300'
                }`}
              >
                {t === 'all' ? 'All tiers' : t}
              </button>
            ))}
          </div>
          <div className="flex gap-1">
            {(['all', 'Production', 'Pending Review'] as const).map(s => (
              <button
                key={s}
                onClick={() => setStatusFilter(s)}
                className={`px-3 py-1.5 rounded-lg text-xs font-medium transition ${
                  statusFilter === s
                    ? 'bg-slate-900 text-white'
                    : 'bg-white text-slate-500 border border-slate-200 hover:border-slate-300'
                }`}
              >
                {s === 'all' ? 'All statuses' : s}
              </button>
            ))}
          </div>
        </div>

        {/* Registry table */}
        <div className="bg-white/80 backdrop-blur-sm rounded-xl border border-slate-200/60 shadow-sm overflow-hidden mb-6">
          <table className="w-full text-sm">
            <thead>
              <tr className="text-[11px] text-slate-400 uppercase tracking-wide bg-slate-50/50">
                <th className="text-left py-2.5 px-5 font-medium">Model</th>
                <th className="text-left py-2.5 px-3 font-medium">Owner</th>
                <th className="text-center py-2.5 px-3 font-medium">Tier</th>
                <th className="text-center py-2.5 px-3 font-medium">SR 11-7</th>
                <th className="text-right py-2.5 px-3 font-medium">Use Cases</th>
                <th className="text-right py-2.5 px-3 font-medium">Eval</th>
                <th className="text-right py-2.5 px-3 font-medium">Monthly Cost</th>
                <th className="text-left py-2.5 px-3 font-medium">Last Validated</th>
                <th className="text-left py-2.5 px-5 font-medium">Status</th>
              </tr>
            </thead>
            <tbody>
              {filtered.map(m => {
                const detail = MODEL_DETAILS[m.id];
                const attestedBadge = detail?.attestation.sr11_7.attested;
                return (
                  <tr key={m.id} onClick={() => setOpenModel(m.id)} className="border-t border-slate-100 hover:bg-slate-50/60 cursor-pointer transition-colors">
                    <td className="py-2.5 px-5">
                      <div className="font-semibold text-slate-900">{m.name}</div>
                      <div className="text-[11px] text-slate-400">{m.provider}</div>
                    </td>
                    <td className="py-2.5 px-3 text-slate-700">{m.owner}</td>
                    <td className="py-2.5 px-3 text-center">
                      <span className={`text-[10px] font-semibold px-2 py-0.5 rounded-full ring-1 ${tierBg[m.tier]}`}>{m.tier}</span>
                    </td>
                    <td className="py-2.5 px-3 text-center">
                      {attestedBadge ? <span className="text-emerald-600 font-semibold">✓</span> : <span className="text-amber-600 font-semibold">·</span>}
                    </td>
                    <td className="py-2.5 px-3 text-right text-slate-700 tabular-nums">{m.useCases}</td>
                    <td className="py-2.5 px-3 text-right">
                      <span className={`font-semibold tabular-nums ${m.evalScore >= 85 ? 'text-emerald-600' : m.evalScore >= 70 ? 'text-amber-600' : 'text-rose-600'}`}>{m.evalScore}</span>
                    </td>
                    <td className="py-2.5 px-3 text-right text-slate-700 tabular-nums">${m.monthlyCost.toLocaleString()}</td>
                    <td className="py-2.5 px-3 text-slate-500 text-[11px]">{m.lastValidated}</td>
                    <td className="py-2.5 px-5">
                      <span className={`text-[10px] font-semibold px-2 py-0.5 rounded border ${statusBg[m.status]}`}>{m.status}</span>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>

        {/* Attestation Board */}
        <div className="grid grid-cols-1 lg:grid-cols-2 gap-4 mb-6">
          <div className="bg-white/80 backdrop-blur-sm rounded-xl border border-slate-200/60 p-5 shadow-sm">
            <div className="text-sm font-semibold text-slate-900 mb-3">SR 11-7 Attestation Board</div>
            <div className="space-y-2">
              {MODELS.map(m => {
                const detail = MODEL_DETAILS[m.id];
                const ok = detail?.attestation.sr11_7.attested;
                return (
                  <div key={m.id} className="flex items-center gap-3 py-1.5">
                    <div className={`w-7 h-7 rounded-md flex items-center justify-center flex-shrink-0 font-semibold ${ok ? 'bg-emerald-100 text-emerald-700' : 'bg-amber-100 text-amber-700'}`}>
                      {ok ? '✓' : '·'}
                    </div>
                    <div className="flex-1 min-w-0">
                      <div className="text-sm font-medium text-slate-900">{m.name}</div>
                      <div className="text-[11px] text-slate-500">
                        {ok
                          ? `Attested ${detail.attestation.sr11_7.date} by ${detail.attestation.sr11_7.attester}`
                          : 'Pending — not yet attested'}
                      </div>
                    </div>
                  </div>
                );
              })}
            </div>
          </div>

          <div className="bg-white/80 backdrop-blur-sm rounded-xl border border-slate-200/60 p-5 shadow-sm">
            <div className="text-sm font-semibold text-slate-900 mb-3">EU AI Act Classification</div>
            <div className="space-y-2">
              {MODELS.map(m => {
                const detail = MODEL_DETAILS[m.id];
                const cls = detail?.attestation.euAiAct.classification ?? 'Not classified';
                const documented = detail?.attestation.euAiAct.documented;
                return (
                  <div key={m.id} className="flex items-start gap-3 py-1.5">
                    <div className={`w-7 h-7 rounded-md flex items-center justify-center flex-shrink-0 font-semibold flex-shrink-0 ${documented ? 'bg-blue-100 text-blue-700' : 'bg-rose-100 text-rose-700'}`}>
                      {documented ? '✓' : '✗'}
                    </div>
                    <div className="flex-1 min-w-0">
                      <div className="text-sm font-medium text-slate-900">{m.name}</div>
                      <div className="text-[11px] text-slate-500 leading-snug">{cls}</div>
                    </div>
                  </div>
                );
              })}
            </div>
          </div>
        </div>

        {/* Approval Pipeline (pending models) */}
        <div className="bg-white/80 backdrop-blur-sm rounded-xl border border-slate-200/60 p-5 shadow-sm mb-6">
          <div className="text-sm font-semibold text-slate-900 mb-3">Approval Pipeline — Pending Models</div>
          <div className="space-y-4">
            {MODELS.filter(m => {
              const detail = MODEL_DETAILS[m.id];
              return detail?.approvalChain.some(a => a.status === 'pending');
            }).map(m => {
              const detail = MODEL_DETAILS[m.id];
              return (
                <div key={m.id} className="border border-slate-100 rounded-lg p-4">
                  <div className="flex items-center justify-between mb-3">
                    <div>
                      <div className="text-sm font-semibold text-slate-900">{m.name}</div>
                      <div className="text-[11px] text-slate-500">{m.provider} · {m.owner}</div>
                    </div>
                    <button onClick={() => setOpenModel(m.id)} className="text-xs font-medium text-blue-600 hover:text-blue-700">
                      Open detail →
                    </button>
                  </div>
                  <div className="flex items-center gap-2 overflow-x-auto">
                    {detail.approvalChain.map((a, i) => (
                      <div key={i} className="flex items-center gap-2 flex-shrink-0">
                        <div className={`px-2.5 py-1.5 rounded-lg text-[11px] font-medium ${
                          a.status === 'approved' ? 'bg-emerald-50 text-emerald-700 border border-emerald-200' :
                          a.status === 'pending'  ? 'bg-amber-50 text-amber-700 border border-amber-200' :
                                                    'bg-slate-50 text-slate-500 border border-slate-200'
                        }`}>
                          <div className="font-semibold">{a.step}</div>
                          <div className="text-[10px] opacity-80 mt-0.5">{a.approver}</div>
                        </div>
                        {i < detail.approvalChain.length - 1 && (
                          <svg className="w-3 h-3 text-slate-300" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
                            <path strokeLinecap="round" strokeLinejoin="round" d="M13 7l5 5m0 0l-5 5m5-5H6" />
                          </svg>
                        )}
                      </div>
                    ))}
                  </div>
                </div>
              );
            })}
          </div>
        </div>

        <ModelDrawer modelId={openModel} onClose={() => setOpenModel(null)} />
      </div>
    </div>
  );
}
