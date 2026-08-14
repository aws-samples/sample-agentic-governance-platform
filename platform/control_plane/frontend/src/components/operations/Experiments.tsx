// Experiments — the frictionless multi-tenant experimentation surface (Epic 18,
// Task 6). Maria's #1 unmet want: spin up an isolated sandbox AWS account and
// collaborate with no CLI and no waiting. "New experiment" instantly provisions a
// sandbox — an animated stage checklist driven by the pure provisioning stage
// machine — then surfaces a ready card with a "Promote to use-case →" link into
// the Studio.
//
// All pure logic (the stage machine + deterministic account ids) is provisioning.ts;
// the seed data + shared store are Task 3; the page frame + emerald-on-glass tokens
// are Task 1. This file is only the React composition + the staged provisioning
// interaction on top of those.
//
// House style: emerald-on-glass Ops tokens (opsUi.ts), inline-SVG glyphs (no icon
// lib), Tailwind v4 utility strings, 2-space indent — matching the other Ops pages.

import { useEffect, useRef, useState, type JSX, type ReactNode } from 'react';
import { Link } from 'react-router-dom';

import { ComingSoonBanner } from '../shared/comingSoon';
import OpsPage from './OpsPage';
import { OPS_CARD, OPS_BADGE, OPS_PRIMARY_BTN } from './opsUi';
import { TENANTS, type Tenant } from './demoData';
import { useDemoStore } from './demoStore';
import { experimentStages, advance, isComplete, mockAccountId, type ProvStage } from './provisioning';

// ───────────────────────────── constants ─────────────────────────────

// Reuse the exact business-unit + region strings the seed tenants use, so the
// form selects reconcile with the rest of the demo world (no invented values).
const BUSINESS_UNITS = Array.from(new Set(TENANTS.map((t) => t.businessUnit)));
const REGIONS = Array.from(new Set(TENANTS.map((t) => t.region)));

const STAGE_INTERVAL_MS = 600; // gap between provisioning beats

// ───────────────────────────── inline glyphs (no icon lib) ─────────────────────────────

function CheckGlyph({ className }: { className?: string }) {
  return (
    <svg viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth={2.2} aria-hidden="true" className={className}>
      <path strokeLinecap="round" strokeLinejoin="round" d="M3.5 8.5 6.5 11.5 12.5 5" />
    </svg>
  );
}

function PlusGlyph({ className }: { className?: string }) {
  return (
    <svg viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth={2.2} aria-hidden="true" className={className}>
      <path strokeLinecap="round" d="M8 3.5v9M3.5 8h9" />
    </svg>
  );
}

function Spinner(): JSX.Element {
  return (
    <svg viewBox="0 0 16 16" aria-hidden="true" className="h-3.5 w-3.5 animate-spin text-emerald-600">
      <circle cx="8" cy="8" r="6" fill="none" stroke="currentColor" strokeWidth="2.5" strokeOpacity="0.25" />
      <path d="M8 2a6 6 0 0 1 6 6" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" />
    </svg>
  );
}

// ───────────────────────────── helpers ─────────────────────────────

/** Up-to-2-letter initials for a collaborator avatar. */
function initials(name: string): string {
  const parts = name.trim().split(/\s+/).filter(Boolean);
  if (parts.length === 0) return '?';
  if (parts.length === 1) return parts[0].slice(0, 2).toUpperCase();
  return (parts[0][0] + parts[parts.length - 1][0]).toUpperCase();
}

/** Parse the comma/newline-separated collaborator field into trimmed names. */
function parseCollaborators(raw: string): string[] {
  return raw
    .split(/[,\n]/)
    .map((s) => s.trim())
    .filter(Boolean);
}

// ───────────────────────────── the page ─────────────────────────────

export default function Experiments(): JSX.Element {
  const { state, dispatch } = useDemoStore();

  // Collaborators are demo-only metadata (not on the Tenant), so we track them in
  // a local map keyed by account so the cards can render avatars.
  const [collaboratorsByAccount, setCollaboratorsByAccount] = useState<Record<string, string[]>>({});

  // --- new-experiment form ------------------------------------------------
  const [formOpen, setFormOpen] = useState(false);
  const [name, setName] = useState('');
  const [businessUnit, setBusinessUnit] = useState(BUSINESS_UNITS[0]);
  const [region, setRegion] = useState(REGIONS[0]);
  const [collaboratorsRaw, setCollaboratorsRaw] = useState('');

  // --- provisioning animation ---------------------------------------------
  // `pending` is the tenant being provisioned; `stages` is its live checklist.
  // Both are null/empty when no provisioning is in flight.
  const [pending, setPending] = useState<Tenant | null>(null);
  const [stages, setStages] = useState<ProvStage[]>([]);
  // The just-finished experiment, surfaced as a ready card with the Promote link.
  const [readyTenant, setReadyTenant] = useState<Tenant | null>(null);

  const intervalRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const clearTimer = () => {
    if (intervalRef.current !== null) {
      clearInterval(intervalRef.current);
      intervalRef.current = null;
    }
  };
  // Cancel any in-flight interval on unmount.
  useEffect(() => clearTimer, []);

  // Completion side-effects live here (NOT in the setStages updater) so they fire
  // exactly once. React StrictMode double-invokes state updaters in dev; running
  // dispatch(ADD_EXPERIMENT) inside the updater would append the tenant twice.
  // `pending` is the source of truth for the in-flight tenant and doubles as a
  // run-once guard: when the stages finish we commit `pending`, then clear it, so
  // this effect's body runs a single time per provisioning run.
  useEffect(() => {
    if (!pending || stages.length === 0 || !isComplete(stages)) return;
    clearTimer();
    dispatch({ type: 'ADD_EXPERIMENT', tenant: pending });
    setReadyTenant(pending);
    setPending(null);
    setStages([]);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [stages, pending]);

  const resetForm = () => {
    setName('');
    setBusinessUnit(BUSINESS_UNITS[0]);
    setRegion(REGIONS[0]);
    setCollaboratorsRaw('');
  };

  const onSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    if (!name.trim() || pending) return;

    // Deterministic account id seeded from the current experiment count so a
    // fresh run of the demo always yields the same sequence (no Math.random).
    const seed = state.experiments.length + 1;
    const account = mockAccountId(seed);
    const collaborators = parseCollaborators(collaboratorsRaw);
    const tenant: Tenant = {
      id: account,
      account,
      name: name.trim(),
      businessUnit,
      region,
      kind: 'sandbox',
      status: 'active',
    };

    setCollaboratorsByAccount((prev) => ({ ...prev, [account]: collaborators }));
    setReadyTenant(null);
    setPending(tenant);
    setStages(experimentStages());
    setFormOpen(false);
    resetForm();

    // Drive the checklist forward one beat at a time. The updater is PURE — it
    // only advances the stages. The completion side-effects (commit the tenant +
    // reveal the ready card + stop the timer) live in the StrictMode-safe effect
    // above, which watches `stages`/`pending` and fires exactly once.
    clearTimer();
    intervalRef.current = setInterval(() => {
      setStages((prev) => advance(prev));
    }, STAGE_INTERVAL_MS);
  };

  // Don't double-render the pending tenant — it joins `state.experiments` only on
  // completion, so the seed grid is just the committed experiments.
  const experiments = state.experiments;
  const newExperimentDisabled = pending !== null;

  return (
    <OpsPage
      backTo="/ops"
      title="Experiments"
      subtitle="Spin up an isolated sandbox account and collaborate — no CLI, no waiting."
      action={
        <button
          type="button"
          onClick={() => setFormOpen((v) => !v)}
          disabled={newExperimentDisabled}
          className={`${OPS_PRIMARY_BTN} inline-flex items-center gap-1.5 disabled:opacity-60 disabled:cursor-not-allowed`}
        >
          <PlusGlyph className="h-4 w-4" />
          New experiment
        </button>
      }
    >
      <ComingSoonBanner />

      {/* ── New-experiment form ─────────────────────────────────────────── */}
      {formOpen && (
        <form onSubmit={onSubmit} className={`${OPS_CARD} p-5 mb-6`}>
          <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
            <Field label="Experiment name" htmlFor="exp-name">
              <input
                id="exp-name"
                value={name}
                onChange={(e) => setName(e.target.value)}
                placeholder="e.g. Claims Sandbox v2"
                className="w-full rounded-lg border border-emerald-200/70 bg-white/70 px-3.5 py-2 text-sm text-slate-800 placeholder:text-slate-400 focus:outline-none focus:ring-2 focus:ring-emerald-400/50 focus:border-emerald-400 transition-shadow"
              />
            </Field>

            <Field label="Collaborators" htmlFor="exp-collaborators">
              <input
                id="exp-collaborators"
                value={collaboratorsRaw}
                onChange={(e) => setCollaboratorsRaw(e.target.value)}
                placeholder="Comma-separated, e.g. Maria Chen, David Okafor"
                className="w-full rounded-lg border border-emerald-200/70 bg-white/70 px-3.5 py-2 text-sm text-slate-800 placeholder:text-slate-400 focus:outline-none focus:ring-2 focus:ring-emerald-400/50 focus:border-emerald-400 transition-shadow"
              />
            </Field>

            <Field label="Business unit" htmlFor="exp-bu">
              <select
                id="exp-bu"
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

            <Field label="Region" htmlFor="exp-region">
              <select
                id="exp-region"
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

          <div className="mt-4 flex items-center justify-end gap-3">
            <button
              type="button"
              onClick={() => {
                setFormOpen(false);
                resetForm();
              }}
              className="px-3.5 py-1.5 rounded-lg text-sm font-medium text-slate-600 hover:text-slate-900 transition-colors"
            >
              Cancel
            </button>
            <button type="submit" disabled={!name.trim()} className={`${OPS_PRIMARY_BTN} disabled:opacity-60 disabled:cursor-not-allowed`}>
              Provision sandbox
            </button>
          </div>
        </form>
      )}

      {/* ── Provisioning checklist (in flight) ──────────────────────────── */}
      {pending && (
        <div className={`${OPS_CARD} p-5 mb-6`}>
          <div className="flex items-center justify-between gap-4 mb-4">
            <div>
              <div className="text-sm font-semibold text-slate-900">Provisioning “{pending.name}”</div>
              <p className="text-xs text-slate-500 mt-0.5">
                <span className="font-mono">{pending.account}</span> · {pending.businessUnit} · {pending.region}
              </p>
            </div>
            <span
              className={`inline-flex items-center gap-1.5 text-[11px] font-semibold px-2 py-0.5 rounded-full ${OPS_BADGE.provisioning}`}
            >
              <Spinner />
              Provisioning
            </span>
          </div>

          <ul className="divide-y divide-emerald-100/70">
            {stages.map((stage) => (
              <StageRow key={stage.key} stage={stage} />
            ))}
          </ul>
        </div>
      )}

      {/* ── Ready card (just finished) ──────────────────────────────────── */}
      {readyTenant && (
        <div className={`${OPS_CARD} p-5 mb-6 border-emerald-300 ring-1 ring-emerald-200/60`}>
          <div className="flex items-start justify-between gap-4">
            <div className="flex items-start gap-3">
              <span aria-hidden="true" className="shrink-0 h-8 w-8 rounded-full bg-emerald-100 text-emerald-700 flex items-center justify-center">
                <CheckGlyph className="h-4 w-4" />
              </span>
              <div>
                <div className="text-sm font-semibold text-slate-900">“{readyTenant.name}” is ready</div>
                <p className="text-xs text-slate-500 mt-0.5">
                  Sandbox account <span className="font-mono">{readyTenant.account}</span> provisioned in {readyTenant.region}.
                </p>
              </div>
            </div>
            <Link
              to="/ops/studio"
              className="shrink-0 inline-flex items-center gap-1.5 px-3 py-1.5 rounded-lg border border-emerald-300 bg-white text-emerald-700 text-sm font-medium hover:bg-emerald-50 transition-colors"
            >
              Promote to use-case →
            </Link>
          </div>
        </div>
      )}

      {/* ── Experiments grid ────────────────────────────────────────────── */}
      <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
        {experiments.map((t) => (
          <ExperimentCard key={t.id} tenant={t} collaborators={collaboratorsByAccount[t.account] ?? []} />
        ))}
      </div>
    </OpsPage>
  );
}

// ───────────────────────────── sub-components ─────────────────────────────

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

function StageRow({ stage }: { stage: ProvStage }): JSX.Element {
  const done = stage.status === 'done';
  const running = stage.status === 'running';
  return (
    <li className="flex items-center gap-3 py-2.5">
      <span
        aria-hidden="true"
        className={`shrink-0 h-6 w-6 rounded-full flex items-center justify-center transition-colors ${
          done ? 'bg-emerald-100 text-emerald-700' : running ? 'bg-amber-100 text-amber-700' : 'bg-slate-100 text-slate-400'
        }`}
      >
        {done ? <CheckGlyph className="h-3.5 w-3.5" /> : running ? <Spinner /> : <span className="h-2 w-2 rounded-full bg-current" />}
      </span>
      <span className={`text-sm font-medium ${done ? 'text-slate-800' : running ? 'text-slate-900' : 'text-slate-400'}`}>
        {stage.label}
      </span>
    </li>
  );
}

function ExperimentCard({ tenant, collaborators }: { tenant: Tenant; collaborators: string[] }): JSX.Element {
  const badge = tenant.status === 'provisioning' ? OPS_BADGE.provisioning : OPS_BADGE.ready;
  const badgeLabel = tenant.status === 'provisioning' ? 'Provisioning' : 'Active';
  return (
    <div className={`${OPS_CARD} p-5 flex flex-col`}>
      <div className="flex items-start justify-between gap-3">
        <h2 className="text-base font-semibold text-slate-900">{tenant.name}</h2>
        <span className={`inline-flex items-center gap-1.5 text-[11px] font-semibold px-2 py-0.5 rounded-full shrink-0 ${badge}`}>
          <span aria-hidden="true">●</span>
          {badgeLabel}
        </span>
      </div>

      <div className="font-mono text-xs text-slate-500 mt-2">{tenant.account}</div>
      <div className="text-sm text-slate-500 mt-1 flex-1">
        {tenant.businessUnit} · {tenant.region}
      </div>

      <div className="mt-4 flex items-center gap-1.5">
        {collaborators.length > 0 ? (
          collaborators.map((c) => (
            <span
              key={c}
              title={c}
              className="h-7 w-7 rounded-full bg-emerald-100 text-emerald-700 text-[11px] font-semibold flex items-center justify-center ring-2 ring-white"
            >
              {initials(c)}
            </span>
          ))
        ) : (
          <span className="text-xs text-slate-400">No collaborators yet</span>
        )}
      </div>
    </div>
  );
}
