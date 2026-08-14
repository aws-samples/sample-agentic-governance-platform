// Deployments — the mock-up payoff of the Operations Build & Run golden thread
// (Epic 18, Task 7). NOTHING HERE DEPLOYS ANYTHING (E31F/T4). This header used to
// claim the page "*performs* the live CI/CD provisioning"; it does not, and the
// claim was the most misleading sentence in the Operations tree — a reader took the
// checklist for a real pipeline. What actually happens on "New deployment" (or
// "Promote {agent}" when the user arrived from the Studio) is a SIMULATED stage
// machine: a `setInterval` walks the fixed `deploymentStages()` list — pick
// template → create repository → register Entra app → build → scan → deploy — one
// 600ms beat at a time through the pure `advance` reducer, flipping each label
// idle→running→done. No repository is created, no Entra app is registered, no
// image is built or scanned, and no API is called from this file at all. On
// completion a demo row is prepended to a table whose other rows are the hardcoded
// SEED_RUNS constant below.
//
// The REAL deployment records live on RepositoryDetail.tsx, which reads them from
// the backend via `agentOpsApi.deployments(agentId)` — that is the page to look at
// (or extend) for actual delivery state. This one is a demo surface, and says so in
// the UI through the ComingSoonBanner below.
//
// All pure logic (the stage machine) is provisioning.ts (Task 6); the shared store
// (`builtAgent` set by the Studio's Deploy) is Task 3; the emerald-on-glass tokens
// are Task 1. This file is only the React composition + the staged simulation on
// top of those — mirroring the Experiments.tsx animation idiom: a ref-tracked
// setInterval driving a PURE `advance` updater, with the completion side-effect
// (prepend the row) living in a StrictMode-safe, once-guarded effect.
//
// House style: emerald-on-glass Ops tokens (opsUi.ts), inline-SVG glyphs (no icon
// lib), Tailwind v4 utility strings, 2-space indent — matching the other Ops pages.

import { useEffect, useRef, useState, type JSX } from 'react';
import { Link } from 'react-router-dom';

import { ComingSoonBanner } from '../shared/comingSoon';
import { OPS_CARD, OPS_BADGE, OPS_TABLE_HEAD, OPS_TABLE_DIVIDE } from './opsUi';
import { useDemoStore } from './demoStore';
import { deploymentStages, advance, isComplete, type ProvStage } from './provisioning';

// ───────────────────────────── constants ─────────────────────────────

const STAGE_INTERVAL_MS = 600; // gap between provisioning beats

// Claims-Triage defaults — reconcile with the demo scenario (repo acme/claims-triage)
// when the user lands here directly (no Studio-built agent in the store).
const DEFAULT_AGENT = 'Claims Triage Agent';
const DEFAULT_REPOSITORY = 'acme/claims-triage';
const DEFAULT_TEMPLATE = 'Python Agent';
// Realistic Entra app (object) id assigned to the freshly-registered app.
const DEFAULT_ENTRA_APP = 'd4c3b2a1-…-registered';

interface RunRow {
  agent: string;
  template: string;
  repository: string;
  entraApp: string;
  status: string;
  when: string;
}

// Seed runs reconciled with the demo world. The two healthy provisioned rows map
// to the OPS_BADGE `ready` key (there is no `provisioned` key); `pending`/`failed`
// keys exist as-is.
const SEED_RUNS: RunRow[] = [
  { agent: 'agent-foo', template: 'Python Agent', repository: 'acme/agent-foo', entraApp: 'a1b2c3d4-…-registered', status: 'ready', when: '12m ago' },
  { agent: 'onboarding-bot', template: 'TypeScript Agent', repository: 'acme/onboarding-bot', entraApp: 'e5f6a7b8-…-registered', status: 'ready', when: '1h ago' },
  { agent: 'triage-agent', template: 'Python Agent', repository: 'acme/triage-agent', entraApp: 'pending', status: 'pending', when: '3h ago' },
  { agent: 'fnol-agent', template: 'Go Agent', repository: 'acme/fnol-agent', entraApp: 'registered', status: 'ready', when: '6h ago' },
  { agent: 'fraud-agent', template: 'TypeScript Agent', repository: 'acme/fraud-agent', entraApp: 'registration failed', status: 'failed', when: '1d ago' },
];

// Human label for each status pill (OPS_BADGE keys are lowercase; the pill text
// reads naturally). `running` is the freshly-deployed in-flight state.
const STATUS_LABEL: Record<string, string> = {
  ready: 'ready',
  running: 'running',
  pending: 'pending',
  failed: 'failed',
};

// ───────────────────────────── inline glyphs (no icon lib) ─────────────────────────────

function CheckGlyph({ className }: { className?: string }) {
  return (
    <svg viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth={2.2} aria-hidden="true" className={className}>
      <path strokeLinecap="round" strokeLinejoin="round" d="M3.5 8.5 6.5 11.5 12.5 5" />
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

// ───────────────────────────── the page ─────────────────────────────

export default function Deployments(): JSX.Element {
  const { state } = useDemoStore();
  const built = state.builtAgent;

  // Defaults flip to the Studio-built agent when the user arrived via Deploy —
  // label the button "Promote {name}" and prefill the repo/account/template.
  const agentName = built?.name ?? DEFAULT_AGENT;
  const repository = built ? `acme/${slug(built.name)}` : DEFAULT_REPOSITORY;
  const template = built ? templateForModel(built.model) : DEFAULT_TEMPLATE;
  const buttonLabel = built ? `Promote ${built.name}` : 'New deployment';

  // Rows added by completed live deployments, prepended to the seed runs.
  const [extraRuns, setExtraRuns] = useState<RunRow[]>([]);

  // --- provisioning animation ---------------------------------------------
  // `stages` is the live CI/CD checklist; non-empty only while a deployment is in
  // flight. `deployingName` is the agent being deployed and doubles as the
  // run-once guard for the completion effect below.
  const [stages, setStages] = useState<ProvStage[]>([]);
  const [deployingName, setDeployingName] = useState<string | null>(null);

  const intervalRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const clearTimer = () => {
    if (intervalRef.current !== null) {
      clearInterval(intervalRef.current);
      intervalRef.current = null;
    }
  };
  // Cancel any in-flight interval on unmount.
  useEffect(() => clearTimer, []);

  // Snapshot the deployment's repo/template at click time so the completion effect
  // builds the row from the values that were live when the run started (not whatever
  // the store reads later).
  const pendingMeta = useRef<{ template: string; repository: string }>({
    template: DEFAULT_TEMPLATE,
    repository: DEFAULT_REPOSITORY,
  });

  // Completion side-effects live HERE (not in the setStages updater) so they fire
  // exactly once. React StrictMode double-invokes state updaters in dev; prepending
  // the new row inside the updater would insert it twice. `deployingName` is the
  // source of truth for the in-flight agent and doubles as a run-once guard: when
  // the stages finish we prepend the row, then clear it, so this body runs a single
  // time per deployment.
  useEffect(() => {
    if (!deployingName || stages.length === 0 || !isComplete(stages)) return;
    clearTimer();
    const row: RunRow = {
      agent: deployingName,
      template: pendingMeta.current.template,
      repository: pendingMeta.current.repository,
      entraApp: DEFAULT_ENTRA_APP,
      status: 'running',
      when: 'just now',
    };
    setExtraRuns((prev) => [row, ...prev]);
    setStages([]);
    setDeployingName(null);
  }, [stages, deployingName]);

  const onDeploy = () => {
    if (deployingName) return; // a run is already in flight
    pendingMeta.current = { template, repository };
    setDeployingName(agentName);
    setStages(deploymentStages());

    // Drive the checklist forward one beat at a time. The updater is PURE — it
    // only advances the stages. The completion side-effect (prepend the running
    // row + stop the timer) lives in the StrictMode-safe effect above, which
    // watches `stages`/`deployingName` and fires exactly once.
    clearTimer();
    intervalRef.current = setInterval(() => {
      setStages((prev) => advance(prev));
    }, STAGE_INTERVAL_MS);
  };

  const deploying = deployingName !== null;
  const runs = [...extraRuns, ...SEED_RUNS];

  return (
    <div className="min-h-[calc(100vh-4rem)] relative">
      <div className="relative max-w-7xl mx-auto px-6 py-10">
        <Link to="/ops" className="text-sm text-slate-400 hover:text-slate-600 transition-colors font-medium">
          ← Operations
        </Link>

        <div className="flex items-end justify-between gap-4 mt-3 mb-6">
          <div>
            <h1 className="text-3xl font-semibold text-slate-900 tracking-tight">Deployments</h1>
            <p className="text-slate-500 mt-1 max-w-2xl">
              Will provision new agents end-to-end — scaffold a repository from a template, register its Entra app,
              build, scan, and deploy in one flow.
            </p>
          </div>
          <button
            type="button"
            onClick={onDeploy}
            disabled={deploying}
            className="shrink-0 px-3.5 py-1.5 rounded-lg bg-emerald-600 text-white text-sm font-medium hover:bg-emerald-700 transition-colors disabled:opacity-60 disabled:cursor-not-allowed"
          >
            {buttonLabel}
          </button>
        </div>

        <ComingSoonBanner />

        {/* New deployment flow — static stepper preview at rest, live CI/CD checklist while deploying */}
        <div className={`${OPS_CARD} p-5 mb-6`}>
          <div className="flex items-center justify-between gap-4 mb-4">
            <div className="text-[11px] font-medium text-slate-500 uppercase tracking-wide">
              {deploying ? `Deploying ${deployingName}` : 'New deployment flow'}
            </div>
            {deploying && (
              <span className={`inline-flex items-center gap-1.5 text-[11px] font-semibold px-2 py-0.5 rounded-full ${OPS_BADGE.running}`}>
                <Spinner />
                Provisioning
              </span>
            )}
          </div>

          {deploying ? (
            <ul className={OPS_TABLE_DIVIDE}>
              {stages.map((stage) => (
                <StageRow key={stage.key} stage={stage} />
              ))}
            </ul>
          ) : (
            <div className="flex items-center gap-2 sm:gap-4">
              {deploymentStages().map((s, i, arr) => (
                <div key={s.key} className="flex items-center gap-2 sm:gap-4">
                  <div className="flex items-center gap-3">
                    <span className="flex items-center justify-center w-8 h-8 rounded-full bg-emerald-600 text-white text-sm font-semibold tabular-nums shrink-0">
                      {i + 1}
                    </span>
                    <span className="text-sm font-medium text-slate-700 whitespace-nowrap">{s.label}</span>
                  </div>
                  {i < arr.length - 1 && (
                    <span aria-hidden="true" className="text-slate-300 text-lg">→</span>
                  )}
                </div>
              ))}
            </div>
          )}
        </div>

        {/* Provisioning runs */}
        <div className={`${OPS_CARD} overflow-hidden`}>
          <table className="w-full text-sm">
            <thead className={OPS_TABLE_HEAD}>
              <tr>
                <th className="text-left font-medium px-4 py-2.5">Agent</th>
                <th className="text-left font-medium px-4 py-2.5">Template</th>
                <th className="text-left font-medium px-4 py-2.5">Repository</th>
                <th className="text-left font-medium px-4 py-2.5">Entra App</th>
                <th className="text-left font-medium px-4 py-2.5">Status</th>
                <th className="text-right font-medium px-4 py-2.5">When</th>
              </tr>
            </thead>
            <tbody className={OPS_TABLE_DIVIDE}>
              {runs.map((r, i) => (
                <tr key={`${r.agent}-${i}`} className="hover:bg-emerald-50/40 transition-colors">
                  <td className="px-4 py-3 font-medium text-slate-900">{r.agent}</td>
                  <td className="px-4 py-3 text-slate-600">{r.template}</td>
                  <td className="px-4 py-3 font-mono text-[12px] text-slate-600">{r.repository}</td>
                  <td className="px-4 py-3 font-mono text-[12px] text-slate-500">{r.entraApp}</td>
                  <td className="px-4 py-3">
                    <span className={`inline-flex items-center gap-1.5 text-[11px] font-semibold px-2 py-0.5 rounded-full capitalize ${OPS_BADGE[r.status as keyof typeof OPS_BADGE]}`}>
                      {r.status === 'running' ? <Spinner /> : <span aria-hidden="true">●</span>}
                      {STATUS_LABEL[r.status] ?? r.status}
                    </span>
                  </td>
                  <td className="px-4 py-3 text-right text-[11px] text-slate-400">{r.when}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>
    </div>
  );
}

// ───────────────────────────── sub-components ─────────────────────────────

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

// ───────────────────────────── helpers ─────────────────────────────

/** Slugify an agent name into a repo-name segment, e.g. "Claims Triage Agent" → "claims-triage". */
function slug(name: string): string {
  const s = name
    .toLowerCase()
    .replace(/\bagent\b/g, '')
    .trim()
    .replace(/[^a-z0-9]+/g, '-')
    .replace(/^-+|-+$/g, '');
  return s || 'agent';
}

/** Map the built agent's model provider to the closest scaffold template. */
function templateForModel(model: string): string {
  return model.startsWith('Claude') ? 'Python Agent' : 'TypeScript Agent';
}
