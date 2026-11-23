import { useEffect, useRef, useState, type JSX } from 'react';
import { Link } from 'react-router-dom';

import { ComingSoonBanner } from '../shared/comingSoon';
import { OPS_CARD, OPS_BADGE, OPS_TABLE_HEAD, OPS_TABLE_DIVIDE } from './opsUi';
import { deploymentStages, advance, isComplete, type ProvStage } from './provisioning';

const STAGE_INTERVAL_MS = 600;

const DEFAULT_AGENT = 'Claims Triage Agent';
const DEFAULT_REPOSITORY = 'acme/claims-triage';
const DEFAULT_TEMPLATE = 'Python Agent';
const DEFAULT_ENTRA_APP = 'd4c3b2a1-…-registered';

interface RunRow {
  agent: string;
  template: string;
  repository: string;
  entraApp: string;
  status: string;
  when: string;
}

const SEED_RUNS: RunRow[] = [
  { agent: 'agent-foo', template: 'Python Agent', repository: 'acme/agent-foo', entraApp: 'a1b2c3d4-…-registered', status: 'ready', when: '12m ago' },
  { agent: 'onboarding-bot', template: 'TypeScript Agent', repository: 'acme/onboarding-bot', entraApp: 'e5f6a7b8-…-registered', status: 'ready', when: '1h ago' },
  { agent: 'triage-agent', template: 'Python Agent', repository: 'acme/triage-agent', entraApp: 'pending', status: 'pending', when: '3h ago' },
  { agent: 'fnol-agent', template: 'Go Agent', repository: 'acme/fnol-agent', entraApp: 'registered', status: 'ready', when: '6h ago' },
  { agent: 'fraud-agent', template: 'TypeScript Agent', repository: 'acme/fraud-agent', entraApp: 'registration failed', status: 'failed', when: '1d ago' },
];

const STATUS_LABEL: Record<string, string> = {
  ready: 'ready',
  running: 'running',
  pending: 'pending',
  failed: 'failed',
};

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

export default function Deployments(): JSX.Element {
  const agentName = DEFAULT_AGENT;
  const repository = DEFAULT_REPOSITORY;
  const template = DEFAULT_TEMPLATE;
  const buttonLabel = 'New deployment';

  const [extraRuns, setExtraRuns] = useState<RunRow[]>([]);

  const [stages, setStages] = useState<ProvStage[]>([]);
  const [deployingName, setDeployingName] = useState<string | null>(null);

  const intervalRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const clearTimer = () => {
    if (intervalRef.current !== null) {
      clearInterval(intervalRef.current);
      intervalRef.current = null;
    }
  };
  useEffect(() => clearTimer, []);

  const onDeploy = () => {
    if (deployingName) return;
    let current = deploymentStages();
    setDeployingName(agentName);
    setStages(current);

    clearTimer();
    intervalRef.current = setInterval(() => {
      current = advance(current);
      setStages(current);
      if (!isComplete(current)) return;
      clearTimer();
      const row: RunRow = {
        agent: agentName,
        template,
        repository,
        entraApp: DEFAULT_ENTRA_APP,
        status: 'running',
        when: 'just now',
      };
      setExtraRuns((prev) => [row, ...prev]);
      setStages([]);
      setDeployingName(null);
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
