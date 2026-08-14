import {
  LineChart, Line, BarChart, Bar, XAxis, YAxis, CartesianGrid, Tooltip, ResponsiveContainer, Legend,
} from 'recharts';
import Drawer from './Drawer';
import { MODELS, MODEL_DETAILS, tooltipStyle } from './mockData';

interface Props {
  modelId: string | null;
  onClose: () => void;
}

const tierBg: Record<string, string> = {
  'Tier 1': 'bg-rose-50 text-rose-700 ring-rose-200',
  'Tier 2': 'bg-amber-50 text-amber-700 ring-amber-200',
  'Tier 3': 'bg-emerald-50 text-emerald-700 ring-emerald-200',
};

const stepStatusBg: Record<string, string> = {
  approved: 'bg-emerald-100 text-emerald-700',
  pending:  'bg-amber-100 text-amber-700',
  'n/a':    'bg-slate-100 text-slate-500',
};

export default function ModelDrawer({ modelId, onClose }: Props) {
  const model = modelId ? MODELS.find(m => m.id === modelId) : null;
  const detail = modelId ? MODEL_DETAILS[modelId] : null;

  return (
    <Drawer
      open={!!model && !!detail}
      onClose={onClose}
      title={model?.name ?? ''}
      subtitle={model ? `${model.provider} · ${model.owner} · ${model.tier}` : undefined}
      width="xl"
    >
      {model && detail && (
        <div className="space-y-6">
          {/* Summary pills */}
          <div className="flex flex-wrap items-center gap-2">
            <span className={`text-xs font-semibold px-2.5 py-1 rounded-full ring-1 ${tierBg[model.tier]}`}>{model.tier}</span>
            <span className="text-xs font-semibold px-2.5 py-1 rounded-full bg-slate-100 text-slate-700">{model.status}</span>
            <span className="text-xs text-slate-500">Context: {detail.contextWindow}</span>
            <span className="text-xs text-slate-500">· Input ${detail.pricing.input}/1K · Output ${detail.pricing.output}/1K</span>
          </div>

          <p className="text-sm text-slate-600 leading-relaxed">{detail.description}</p>

          {/* KPI row */}
          <div className="grid grid-cols-4 gap-3">
            <div className="bg-slate-50/80 rounded-xl p-3">
              <div className="text-[10px] uppercase tracking-widest text-slate-400">Eval score</div>
              <div className="text-2xl font-semibold text-emerald-600">{model.evalScore}</div>
            </div>
            <div className="bg-slate-50/80 rounded-xl p-3">
              <div className="text-[10px] uppercase tracking-widest text-slate-400">Use cases</div>
              <div className="text-2xl font-semibold text-slate-900">{model.useCases}</div>
            </div>
            <div className="bg-slate-50/80 rounded-xl p-3">
              <div className="text-[10px] uppercase tracking-widest text-slate-400">Monthly cost</div>
              <div className="text-2xl font-semibold text-slate-900">${model.monthlyCost.toLocaleString()}</div>
            </div>
            <div className="bg-slate-50/80 rounded-xl p-3">
              <div className="text-[10px] uppercase tracking-widest text-slate-400">Last validated</div>
              <div className="text-sm font-semibold text-slate-900 mt-1.5">{model.lastValidated || '—'}</div>
            </div>
          </div>

          {/* Eval history */}
          <div>
            <div className="text-sm font-semibold text-slate-900 mb-2">Evaluation History</div>
            <div className="bg-white rounded-xl border border-slate-200 p-4">
              <ResponsiveContainer width="100%" height={180}>
                <LineChart data={detail.evalHistory}>
                  <CartesianGrid strokeDasharray="3 3" stroke="#e2e8f0" />
                  <XAxis dataKey="date" tick={{ fill: '#94a3b8', fontSize: 10 }} />
                  <YAxis domain={[0, 100]} tick={{ fill: '#94a3b8', fontSize: 10 }} />
                  <Tooltip contentStyle={tooltipStyle} />
                  <Legend wrapperStyle={{ fontSize: '11px' }} />
                  <Line type="monotone" dataKey="safety"  stroke="#10b981" strokeWidth={2} />
                  <Line type="monotone" dataKey="quality" stroke="#3b82f6" strokeWidth={2} />
                  <Line type="monotone" dataKey="latency" stroke="#f59e0b" strokeWidth={2} />
                </LineChart>
              </ResponsiveContainer>
            </div>
          </div>

          {/* Drift */}
          <div>
            <div className="text-sm font-semibold text-slate-900 mb-2">Drift Signals (6-week)</div>
            <div className="bg-white rounded-xl border border-slate-200 p-4">
              <ResponsiveContainer width="100%" height={160}>
                <BarChart data={detail.driftSignals}>
                  <CartesianGrid strokeDasharray="3 3" stroke="#e2e8f0" />
                  <XAxis dataKey="week" tick={{ fill: '#94a3b8', fontSize: 10 }} />
                  <YAxis yAxisId="left"  tick={{ fill: '#94a3b8', fontSize: 10 }} />
                  <YAxis yAxisId="right" orientation="right" tick={{ fill: '#94a3b8', fontSize: 10 }} />
                  <Tooltip contentStyle={tooltipStyle} />
                  <Legend wrapperStyle={{ fontSize: '11px' }} />
                  <Bar yAxisId="left"  dataKey="quality"       fill="#3b82f6" name="Quality" />
                  <Bar yAxisId="right" dataKey="hallucination" fill="#ef4444" name="Hallucination %" />
                </BarChart>
              </ResponsiveContainer>
            </div>
          </div>

          {/* Attestation */}
          <div>
            <div className="text-sm font-semibold text-slate-900 mb-2">Attestation & Documentation</div>
            <div className="grid grid-cols-3 gap-3">
              <div className="bg-white rounded-xl border border-slate-200 p-3">
                <div className="text-[10px] uppercase tracking-widest text-slate-400">SR 11-7</div>
                {detail.attestation.sr11_7.attested ? (
                  <>
                    <div className="text-sm font-semibold text-emerald-600 mt-1">✓ Attested</div>
                    <div className="text-[11px] text-slate-500 mt-0.5">{detail.attestation.sr11_7.date} · {detail.attestation.sr11_7.attester}</div>
                  </>
                ) : (
                  <div className="text-sm font-semibold text-amber-600 mt-1">Pending</div>
                )}
              </div>
              <div className="bg-white rounded-xl border border-slate-200 p-3">
                <div className="text-[10px] uppercase tracking-widest text-slate-400">EU AI Act</div>
                <div className="text-xs font-semibold text-slate-900 mt-1 leading-tight">{detail.attestation.euAiAct.classification}</div>
                <div className={`text-[11px] mt-0.5 ${detail.attestation.euAiAct.documented ? 'text-emerald-600' : 'text-rose-600'}`}>
                  {detail.attestation.euAiAct.documented ? '✓ Documented' : '✗ Missing'}
                </div>
              </div>
              <div className="bg-white rounded-xl border border-slate-200 p-3">
                <div className="text-[10px] uppercase tracking-widest text-slate-400">Model Card</div>
                <div className={`text-sm font-semibold mt-1 ${detail.attestation.modelCard.complete ? 'text-emerald-600' : 'text-rose-600'}`}>
                  {detail.attestation.modelCard.complete ? '✓ Complete' : 'Incomplete'}
                </div>
                <a href={detail.attestation.modelCard.url} className="text-[11px] text-blue-600 hover:underline mt-0.5 inline-block">View card →</a>
              </div>
            </div>
          </div>

          {/* Approval chain */}
          <div>
            <div className="text-sm font-semibold text-slate-900 mb-2">Approval Chain</div>
            <div className="bg-white rounded-xl border border-slate-200 divide-y divide-slate-100">
              {detail.approvalChain.map((a, i) => (
                <div key={i} className="flex items-center justify-between px-4 py-2.5">
                  <div>
                    <div className="text-sm text-slate-900 font-medium">{a.step}</div>
                    <div className="text-[11px] text-slate-500">{a.approver}{a.date ? ` · ${a.date}` : ''}</div>
                  </div>
                  <span className={`text-[10px] font-semibold px-2 py-0.5 rounded uppercase ${stepStatusBg[a.status]}`}>
                    {a.status}
                  </span>
                </div>
              ))}
            </div>
          </div>

          {/* Use cases */}
          <div>
            <div className="text-sm font-semibold text-slate-900 mb-2">Use Cases ({detail.useCasesList.length})</div>
            <div className="bg-white rounded-xl border border-slate-200 divide-y divide-slate-100">
              {detail.useCasesList.map((u, i) => (
                <div key={i} className="flex items-center justify-between px-4 py-2.5">
                  <div>
                    <div className="text-sm text-slate-900">{u.name}</div>
                    <div className="text-[11px] text-slate-500">{u.owner}</div>
                  </div>
                  <div className="text-xs text-slate-500 tabular-nums">{u.invocations.toLocaleString()} / mo</div>
                </div>
              ))}
            </div>
          </div>
        </div>
      )}
    </Drawer>
  );
}
