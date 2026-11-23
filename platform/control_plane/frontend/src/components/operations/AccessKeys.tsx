import { useEffect, useRef, useState, type JSX, type ReactNode } from 'react';

import { ComingSoonBanner } from '../shared/comingSoon';
import OpsPage from './OpsPage';
import { OPS_CARD, OPS_BADGE, OPS_PRIMARY_BTN } from './opsUi';
import { MODELS, TENANTS } from './demoData';

type Environment = 'Exploration' | 'Use case';

const BUSINESS_UNITS = Array.from(new Set(TENANTS.map((t) => t.businessUnit)));
const REGIONS = Array.from(new Set(TENANTS.map((t) => t.region)));

const SUBMIT_DELAY_MS = 700;

const MASKED_KEY = 'sk-live-7Qx9••••••••••••••••••••••••3aF2';

function autoApproves(environment: Environment): boolean {
  return environment === 'Exploration';
}

function CheckGlyph({ className }: { className?: string }): JSX.Element {
  return (
    <svg viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth={2.2} aria-hidden="true" className={className}>
      <path strokeLinecap="round" strokeLinejoin="round" d="M3.5 8.5 6.5 11.5 12.5 5" />
    </svg>
  );
}

function ClockGlyph({ className }: { className?: string }): JSX.Element {
  return (
    <svg viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth={2.2} aria-hidden="true" className={className}>
      <circle cx="8" cy="8" r="6" />
      <path strokeLinecap="round" strokeLinejoin="round" d="M8 5v3l2 1.5" />
    </svg>
  );
}

function Spinner(): JSX.Element {
  return (
    <svg viewBox="0 0 16 16" aria-hidden="true" className="h-3.5 w-3.5 animate-spin text-white/90">
      <circle cx="8" cy="8" r="6" fill="none" stroke="currentColor" strokeWidth="2.5" strokeOpacity="0.3" />
      <path d="M8 2a6 6 0 0 1 6 6" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" />
    </svg>
  );
}

type Result = { environment: Environment };

export default function AccessKeys(): JSX.Element {
  const [environment, setEnvironment] = useState<Environment>('Exploration');
  const [businessUnit, setBusinessUnit] = useState(BUSINESS_UNITS[0]);
  const [region, setRegion] = useState(REGIONS[0]);
  const [models, setModels] = useState<string[]>([MODELS[0].id]);

  const [submitting, setSubmitting] = useState(false);
  const [result, setResult] = useState<Result | null>(null);

  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const clearTimer = () => {
    if (timerRef.current !== null) {
      clearTimeout(timerRef.current);
      timerRef.current = null;
    }
  };
  useEffect(() => clearTimer, []);

  const toggleModel = (id: string) => {
    setModels((prev) => (prev.includes(id) ? prev.filter((m) => m !== id) : [...prev, id]));
  };

  const onSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    if (submitting || models.length === 0) return;

    const environmentAtSubmit = environment;
    setResult(null);
    setSubmitting(true);

    clearTimer();
    timerRef.current = setTimeout(() => {
      timerRef.current = null;
      setResult({ environment: environmentAtSubmit });
      setSubmitting(false);
    }, SUBMIT_DELAY_MS);
  };

  const submitDisabled = submitting || models.length === 0;

  return (
    <OpsPage
      backTo="/ops"
      title="Access Keys"
      subtitle="Request model access in seconds. Exploration is auto-approved instantly — the change that cut access tickets from 500 to 250 a month."
    >
      <ComingSoonBanner />

      <form onSubmit={onSubmit} className={`${OPS_CARD} p-5 mb-6`}>
        <Field label="Environment" htmlFor="ak-environment">
          <div className="flex flex-wrap gap-2">
            {(['Exploration', 'Use case'] as Environment[]).map((env) => {
              const selected = environment === env;
              return (
                <button
                  key={env}
                  type="button"
                  onClick={() => setEnvironment(env)}
                  aria-pressed={selected}
                  className={`px-3.5 py-1.5 rounded-lg border text-sm font-medium transition-colors ${
                    selected
                      ? 'border-emerald-400 bg-emerald-50 text-emerald-700'
                      : 'border-emerald-200/70 bg-white/70 text-slate-600 hover:border-emerald-300 hover:text-slate-800'
                  }`}
                >
                  {env}
                  <span className="ml-1.5 text-[11px] font-normal text-slate-400">
                    {autoApproves(env) ? 'auto-approved' : 'needs approval'}
                  </span>
                </button>
              );
            })}
          </div>
        </Field>

        <div className="grid grid-cols-1 md:grid-cols-2 gap-4 mt-4">
          <Field label="Business function" htmlFor="ak-bu">
            <select
              id="ak-bu"
              value={businessUnit}
              onChange={(e) => setBusinessUnit(e.target.value)}
              className="w-full rounded-lg border border-emerald-200/70 bg-white/70 px-3.5 py-2 text-sm text-slate-800 focus:outline-none focus:ring-2 focus:ring-emerald-400/50 focus:border-emerald-400 transition-shadow"
            >
              {BUSINESS_UNITS.map((bu) => (
                <option key={bu} value={bu}>
                  {bu}
                </option>
              ))}
            </select>
          </Field>

          <Field label="Region" htmlFor="ak-region">
            <select
              id="ak-region"
              value={region}
              onChange={(e) => setRegion(e.target.value)}
              className="w-full rounded-lg border border-emerald-200/70 bg-white/70 px-3.5 py-2 text-sm text-slate-800 focus:outline-none focus:ring-2 focus:ring-emerald-400/50 focus:border-emerald-400 transition-shadow"
            >
              {REGIONS.map((r) => (
                <option key={r} value={r}>
                  {r}
                </option>
              ))}
            </select>
          </Field>
        </div>

        <Field label="Models" htmlFor="ak-models">
          <div className="flex flex-wrap gap-2">
            {MODELS.map((model) => {
              const selected = models.includes(model.id);
              return (
                <button
                  key={model.id}
                  type="button"
                  onClick={() => toggleModel(model.id)}
                  aria-pressed={selected}
                  className={`inline-flex items-center gap-1.5 px-3 py-1.5 rounded-full border text-sm font-medium transition-colors ${
                    selected
                      ? 'border-emerald-400 bg-emerald-50 text-emerald-700'
                      : 'border-emerald-200/70 bg-white/70 text-slate-600 hover:border-emerald-300 hover:text-slate-800'
                  }`}
                >
                  {selected && <CheckGlyph className="h-3.5 w-3.5" />}
                  {model.label}
                  <span className="text-[11px] font-normal text-slate-400">{model.provider}</span>
                </button>
              );
            })}
          </div>
        </Field>

        <div className="mt-5 flex items-center justify-end">
          <button type="submit" disabled={submitDisabled} className={`${OPS_PRIMARY_BTN} inline-flex items-center gap-1.5 disabled:opacity-60 disabled:cursor-not-allowed`}>
            {submitting && <Spinner />}
            {submitting ? 'Submitting…' : 'Request access'}
          </button>
        </div>
      </form>

      {result && (autoApproves(result.environment) ? <ApprovedPanel /> : <PendingPanel />)}
    </OpsPage>
  );
}

function Field({ label, htmlFor, children }: { label: string; htmlFor: string; children: ReactNode }): JSX.Element {
  return (
    <div>
      <label htmlFor={htmlFor} className="block text-[11px] font-medium text-slate-500 uppercase tracking-wide mb-1.5">
        {label}
      </label>
      {children}
    </div>
  );
}

function ApprovedPanel(): JSX.Element {
  return (
    <div className={`${OPS_CARD} p-5 border-emerald-300 ring-1 ring-emerald-200/60`}>
      <div className="flex items-start gap-3">
        <span aria-hidden="true" className="shrink-0 h-8 w-8 rounded-full bg-emerald-100 text-emerald-700 flex items-center justify-center">
          <CheckGlyph className="h-4 w-4" />
        </span>
        <div className="min-w-0">
          <div className="flex items-center gap-2">
            <span className={`inline-flex items-center gap-1.5 text-[11px] font-semibold px-2 py-0.5 rounded-full ${OPS_BADGE.ok}`}>
              <span aria-hidden="true">✓</span>
              Auto-approved
            </span>
          </div>
          <div className="mt-2 text-sm font-semibold text-slate-900">Exploration access granted instantly</div>
          <div className="mt-2 flex items-center gap-2">
            <code className="font-mono text-xs text-slate-700 bg-emerald-50/70 border border-emerald-100 rounded-md px-2.5 py-1.5">
              {MASKED_KEY}
            </code>
          </div>
          <p className="text-xs text-slate-500 mt-2">
            Metering dashboard, cost tracking, and model catalog provisioned automatically.
          </p>
        </div>
      </div>
    </div>
  );
}

function PendingPanel(): JSX.Element {
  return (
    <div className={`${OPS_CARD} p-5`}>
      <div className="flex items-start gap-3">
        <span aria-hidden="true" className="shrink-0 h-8 w-8 rounded-full bg-amber-100 text-amber-700 flex items-center justify-center">
          <ClockGlyph className="h-4 w-4" />
        </span>
        <div className="min-w-0">
          <div className="flex items-center gap-2">
            <span className={`inline-flex items-center gap-1.5 text-[11px] font-semibold px-2 py-0.5 rounded-full ${OPS_BADGE.pending}`}>
              <span aria-hidden="true">●</span>
              Pending
            </span>
          </div>
          <div className="mt-2 text-sm font-semibold text-slate-900">Use-case access submitted for approval</div>
          <p className="text-xs text-slate-500 mt-2">
            Routed to cost owner + operations manager for approval.
          </p>
        </div>
      </div>
    </div>
  );
}
