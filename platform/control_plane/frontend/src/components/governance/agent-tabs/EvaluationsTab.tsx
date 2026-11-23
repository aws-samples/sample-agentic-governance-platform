import { useMemo } from 'react';
import { Link } from 'react-router-dom';
import { agentOpsApi } from '../../../api/client';
import type { AgentRepositoryView, EvalRun } from '../../../api/client';
import { useLoad } from '../../../api/useLoad';
import { useUser } from '../../../contexts/useUser';
import { currentBuild, gateCard, judgeLabel, runLabels, type GateCardState } from '../../operations/repo-tabs/evaluationsLogic';
import { GATE_TONE_CLS, promoteGateView } from '../../operations/promoteGate';
import { entraActorName, type ActorViewer } from '../../operations/repositoryDetailTabs';
import { ObsEmptyCard } from './ObsEmptyCard';

const CARD = 'bg-white/70 backdrop-blur rounded-xl border border-slate-200/60 shadow-sm';
const TH = 'text-left py-2 font-medium';
const RECENT = 5;

const GATE_DOT: Record<GateCardState, string> = {
  passed: 'bg-emerald-500',
  failed: 'bg-amber-500',
  running: 'bg-slate-400',
  none: 'bg-slate-300',
};

const STATUS_CLS: Record<EvalRun['status'], string> = {
  passed: 'bg-emerald-50 text-emerald-700 border-emerald-200',
  failed: 'bg-amber-50 text-amber-700 border-amber-200',
  error: 'bg-amber-50 text-amber-700 border-amber-200',
  running: 'bg-slate-50 text-slate-600 border-slate-200',
};

const when = (iso: string | null | undefined): string => (iso ? new Date(iso).toLocaleString() : '—');

export default function EvaluationsTab({
  agentId,
  repo,
  repoError,
}: {
  agentId: string;
  /** undefined = the repository lookup is still running; null = the agent has none. */
  repo: AgentRepositoryView | null | undefined;
  repoError: string;
}) {
  const { user } = useUser();
  const viewer: ActorViewer | null = user ? { oid: user.oid, name: user.name, email: user.email } : null;
  const { state } = useLoad(
    () => (repo ? agentOpsApi.evalRuns(agentId).then((runs) => ({ runs, at: Date.now() })) : Promise.resolve(null)),
    [repo, agentId],
    'Failed to load evaluation runs',
  );
  const loaded = state.data;
  const runsError = state.kind === 'error' ? state.message : '';

  const labels = useMemo(() => runLabels(loaded?.runs.runs ?? []), [loaded]);

  if (repoError) {
    return (
      <ObsEmptyCard
        title="Couldn't find this agent's repository"
        body="Evaluations are read from the agent's Ops repository, which could not be looked up just now. Reload to try again."
        tone="amber"
      />
    );
  }
  if (repo === null) {
    return (
      <ObsEmptyCard
        title="No evaluations"
        body="This agent isn't built from an AGP repository, so it has no evaluations."
      />
    );
  }
  if (repo === undefined || (!loaded && !runsError)) {
    return (
      <div className={`${CARD} p-5 flex items-center gap-2 text-xs text-slate-400`}>
        <span className="w-3.5 h-3.5 border-2 border-slate-300 border-t-slate-500 rounded-full animate-spin" />
        Loading…
      </div>
    );
  }

  const opsLink = (
    <Link
      to={`/ops/agents/${encodeURIComponent(repo.repo_id)}`}
      className="inline-flex items-center gap-1 text-xs font-medium text-blue-600 hover:text-blue-700 transition-colors"
    >
      Open {repo.name} in Operations
      <svg viewBox="0 0 20 20" fill="none" className="h-3 w-3" aria-hidden="true">
        <path d="M7 4h9v9M16 4l-9 9M4 8v8h8" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" />
      </svg>
    </Link>
  );

  if (runsError || !loaded) {
    return (
      <div className="space-y-3">
        <ObsEmptyCard title="Couldn't load evaluations" body={runsError} tone="amber" />
        <div>{opsLink}</div>
      </div>
    );
  }

  const { runs, at } = loaded;
  const card = gateCard({ currentBuild: currentBuild(runs), currentRun: runs.current_run, itemCount: 0, now: at });
  const candidate = repo.prod_candidate_status === 'pending' ? promoteGateView(repo.eval_gate) : null;
  const recent = runs.runs.slice(0, RECENT);
  const overrides = runs.overrides.slice(0, RECENT);

  return (
    <div className="space-y-4">
      <div className={`${CARD} p-5`}>
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div className="min-w-0">
            <div className="text-[11px] uppercase tracking-wide text-slate-400 font-medium">Eval gate · dev build</div>
            <div className="mt-1 flex items-center gap-2">
              <span className={`inline-block h-2.5 w-2.5 rounded-full ${GATE_DOT[card.state]}`} aria-hidden="true" />
              <span className="text-sm font-semibold text-slate-900">{card.headline}</span>
            </div>
            {card.facts.length > 0 && <div className="mt-1 text-xs text-slate-500 tabular-nums">{card.facts.join(' · ')}</div>}
            {card.regressions.length > 0 && (
              <div className="mt-1 text-xs text-amber-700">Regression vs prod: {card.regressions.join('; ')}</div>
            )}
            {card.reasons.length > 0 && (
              <ul className="mt-2 space-y-0.5 text-xs text-amber-700">
                {card.reasons.map((r) => <li key={r}>{r}</li>)}
              </ul>
            )}
            {candidate && (
              <div className="mt-2 text-xs text-slate-500">
                Prod candidate: <span className={`font-medium ${GATE_TONE_CLS[candidate.tone].text}`}>{candidate.label}</span>
                {candidate.needsOverride && <span> — promoting it needs an override with a reason.</span>}
              </div>
            )}
          </div>
          {opsLink}
        </div>
      </div>

      <div className={`${CARD} p-5`}>
        <div className="text-sm font-semibold text-slate-900 mb-3">Recent runs</div>
        {recent.length === 0 ? (
          <p className="text-sm text-slate-500">No evaluation has run for this agent yet.</p>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="text-[11px] text-slate-400 uppercase tracking-wide border-b border-slate-100">
                  <th className={TH}>Started</th>
                  <th className={`${TH} pl-4`}>Result</th>
                  <th className={`${TH} pl-4`}>Checks</th>
                  <th className={`${TH} pl-4`}>Judges (mean)</th>
                  <th className={`${TH} pl-4`}>Trigger</th>
                </tr>
              </thead>
              <tbody>
                {recent.map((run) => {
                  const judges = Object.entries(run.summary?.judge_means ?? {});
                  return (
                    <tr key={run.run_id} className="border-b border-slate-50 hover:bg-slate-50/40 align-top">
                      <td className="py-2.5 text-slate-700 whitespace-nowrap tabular-nums" title={`${run.run_id} · ${when(run.started_at)}`}>
                        <time dateTime={run.started_at}>{labels.get(run.run_id) ?? '—'}</time>
                      </td>
                      <td className="py-2.5 pl-4">
                        <span className={`inline-flex px-2 py-0.5 rounded-full border text-[11px] font-semibold capitalize ${STATUS_CLS[run.status]}`}>
                          {run.status}
                        </span>
                      </td>
                      <td className="py-2.5 pl-4 text-slate-600 tabular-nums whitespace-nowrap">
                        {run.summary ? `${run.summary.checks_passed} passed · ${run.summary.checks_failed} failed` : '—'}
                      </td>
                      <td className="py-2.5 pl-4 text-slate-600 tabular-nums">
                        {judges.length === 0
                          ? '—'
                          : judges.map(([id, m]) => (
                              <div key={id} className="whitespace-nowrap">
                                {judgeLabel(id)} <span className="text-slate-900 font-medium">{m === null ? 'not scored' : m.toFixed(2)}</span>
                              </div>
                            ))}
                      </td>
                      <td className="py-2.5 pl-4 text-slate-500 capitalize">{run.trigger}</td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
      </div>

      {overrides.length > 0 && (
        <div className={`${CARD} p-5`}>
          <div className="text-sm font-semibold text-slate-900">Gate overrides</div>
          <p className="text-xs text-slate-500 mt-0.5 mb-3">Prod promotions made without a passing evaluation, with the reason given.</p>
          <ul className="divide-y divide-slate-100">
            {overrides.map((o) => (
              <li key={`${o.at}-${o.by}`} className="py-2 text-sm">
                <div className="flex flex-wrap items-baseline justify-between gap-2">
                  <span className="font-medium text-slate-800" title={o.by}>{o.by_name || entraActorName(o.by, viewer)}</span>
                  <span className="text-xs text-slate-400 tabular-nums">{when(o.at)}</span>
                </div>
                <p className="text-slate-600 mt-0.5 break-words">{o.reason}</p>
              </li>
            ))}
          </ul>
        </div>
      )}
    </div>
  );
}
