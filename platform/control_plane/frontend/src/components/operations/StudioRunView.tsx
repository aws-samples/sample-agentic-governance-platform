import { useState, type JSX } from 'react';
import { Link } from 'react-router-dom';

import { OPS_BADGE, OPS_CARD, OPS_PRIMARY_BTN } from './opsUi';
import { KindIcon, Spinner } from './StudioForms.tsx';
import type { BlockKind, RegistryMcp } from './studioModel';
import {
  awaitMergeCopy,
  compositionLabelOf,
  declaredNames,
  nextAction,
  runBadge,
  timeline,
  type RunItem,
  type RunState,
  type TimelineRow,
} from './studioRun';
import { LABEL, PILL, SECONDARY_BTN } from './studioStyles';

function RowNode({ status }: { status: TimelineRow['status'] }): JSX.Element {
  const base = 'relative z-10 shrink-0 inline-flex items-center justify-center w-6 h-6 rounded-full ring-4 ring-white';
  if (status === 'done') {
    return (
      <span className={`${base} bg-emerald-100 text-emerald-700`}>
        <svg className="w-3.5 h-3.5" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={3} aria-hidden="true">
          <path strokeLinecap="round" strokeLinejoin="round" d="M5 13l4 4L19 7" />
        </svg>
      </span>
    );
  }
  if (status === 'running') {
    return (
      <span className={`${base} bg-emerald-600 text-white`}>
        <Spinner className="w-3.5 h-3.5" />
      </span>
    );
  }
  if (status === 'waiting') {
    return (
      <span className={`${base} bg-amber-100 text-amber-700`}>
        <svg className="w-3.5 h-3.5" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2} aria-hidden="true">
          <path strokeLinecap="round" strokeLinejoin="round" d="M12 6v6l4 2m5-2a9 9 0 11-18 0 9 9 0 0118 0z" />
        </svg>
      </span>
    );
  }
  if (status === 'failed') {
    return (
      <span className={`${base} bg-rose-100 text-rose-700`}>
        <svg className="w-3.5 h-3.5" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={3} aria-hidden="true">
          <path strokeLinecap="round" strokeLinejoin="round" d="M6 18L18 6M6 6l12 12" />
        </svg>
      </span>
    );
  }
  return (
    <span className={`${base} bg-slate-100`}>
      <span className="w-1.5 h-1.5 rounded-full bg-slate-300" />
    </span>
  );
}

const ROW_TEXT: Record<TimelineRow['status'], string> = {
  done: 'text-slate-700',
  running: 'text-slate-900 font-semibold',
  waiting: 'text-slate-900 font-semibold',
  failed: 'text-rose-700 font-medium',
  pending: 'text-slate-400',
};

function ReenterKey(props: { onSubmit: (key: string) => void }): JSX.Element {
  const [key, setKey] = useState('');
  return (
    <form
      className="mt-2 flex flex-wrap items-center gap-2"
      onSubmit={(e) => {
        e.preventDefault();
        if (key) props.onSubmit(key);
      }}
    >
      <input
        type="password"
        autoComplete="off"
        aria-label="Re-enter API key"
        placeholder="Re-enter API key"
        value={key}
        onChange={(e) => setKey(e.target.value)}
        className="flex-1 min-w-[12rem] px-2.5 py-1 text-xs font-mono rounded-md border border-rose-300 bg-white focus:outline-none focus:ring-2 focus:ring-emerald-500/40"
      />
      <button type="submit" disabled={!key} className="px-2.5 py-1 rounded-md bg-white border border-rose-300 text-[11px] font-semibold text-rose-700 hover:bg-rose-50 disabled:opacity-40">
        Retry with this key
      </button>
    </form>
  );
}

function RunTimeline(props: {
  rows: TimelineRow[];
  onRetry: () => void;
  needsKey: (rowKey: string) => boolean;
  onKey: (rowKey: string, key: string) => void;
}): JSX.Element {
  return (
    <ol className="relative" aria-label="Create progress">
      {props.rows.map((r, i) => (
        <li key={r.key} className="relative flex gap-3 pb-4 last:pb-0">
          {i < props.rows.length - 1 && (
            <span aria-hidden="true" className={`absolute left-[11px] top-6 bottom-0 w-px ${r.status === 'done' ? 'bg-emerald-200' : 'bg-slate-200'}`} />
          )}
          <RowNode status={r.status} />
          <div className="min-w-0 flex-1 pt-0.5">
            <div className="flex items-center justify-between gap-2">
              <span className={`text-sm truncate ${ROW_TEXT[r.status]}`}>
                {r.href && r.status === 'done' ? (
                  <a href={r.href} target="_blank" rel="noreferrer" className="hover:underline">
                    {r.label}
                  </a>
                ) : (
                  r.label
                )}
              </span>
              {r.status === 'waiting' ? (
                <span className={`${PILL} bg-amber-50 text-amber-800 border border-amber-200/80 shrink-0`}>Waiting on reviewer</span>
              ) : (
                <span className={`text-[11px] shrink-0 ${r.status === 'failed' ? 'text-rose-600' : 'text-slate-400'}`}>
                  {r.status === 'pending' ? 'waiting' : r.status}
                </span>
              )}
            </div>
            {r.status === 'running' && r.detail && (
              <p className="mt-1 text-xs text-amber-800">
                {r.detail}{' '}
                {r.href && (
                  <a href={r.href} target="_blank" rel="noreferrer" className="font-medium text-emerald-700 hover:underline">
                    Open Actions ↗
                  </a>
                )}
              </p>
            )}
            {r.status === 'failed' && (
              <div className="mt-1.5 rounded-lg bg-rose-50 border border-rose-200/70 px-3 py-2">
                <p role="alert" className="text-xs text-rose-800 break-words">
                  {r.detail}
                </p>
                {props.needsKey(r.key) ? (
                  <ReenterKey onSubmit={(key) => props.onKey(r.key, key)} />
                ) : (
                  <button type="button" onClick={props.onRetry} className="mt-2 px-2.5 py-1 rounded-md bg-white border border-rose-300 text-[11px] font-semibold text-rose-700 hover:bg-rose-50">
                    {r.key.startsWith('build:') ? 'Check again' : 'Retry this step'}
                  </button>
                )}
              </div>
            )}
          </div>
        </li>
      ))}
    </ol>
  );
}

const BADGE: Record<ReturnType<typeof runBadge>, { cls: string; label: string }> = {
  running: { cls: OPS_BADGE.provisioning, label: 'creating' },
  waiting: { cls: 'bg-amber-50 text-amber-800 border border-amber-200/80', label: 'waiting on reviewer' },
  failed: { cls: OPS_BADGE.failed, label: 'stopped' },
  done: { cls: OPS_BADGE.ready, label: 'submitted' },
};

const KIND_OF: Record<RunItem['kind'], BlockKind> = { mcp_server: 'mcp', agent: 'agent' };

export default function RunView(props: {
  run: RunState;
  repoPath: (i: RunItem) => string;
  registry: RegistryMcp[];
  onRetry: () => void;
  onReset: () => void;
  needsKey: (rowKey: string) => boolean;
  onKey: (rowKey: string, key: string) => void;
}): JSX.Element {
  const { run } = props;
  const badge = BADGE[runBadge(run)];
  const action = nextAction(run);
  const waiting = action.type === 'await_merge';
  const done = action.type === 'done';
  const withPr = run.items.filter((i) => i.pr);
  const merged = run.items.filter((i) => i.merged).length;
  const agents = run.items.filter((i) => i.kind === 'agent');
  const mcps = run.items.filter((i) => i.kind === 'mcp_server');
  const label = compositionLabelOf(run);

  return (
    <div className="grid grid-cols-1 lg:grid-cols-[1fr_24rem] gap-4">
      <div className={`${OPS_CARD} p-5`}>
        <div className="flex items-center justify-between gap-3 mb-4">
          <div>
            <h2 className="text-sm font-semibold text-slate-900">{done ? 'Submitted for approval' : `Creating ${label}`}</h2>
            <p className="text-xs text-slate-500 mt-0.5">You can leave this page. The run resumes here when you come back.</p>
          </div>
          <span className={`${PILL} ${badge.cls} shrink-0`}>
            <span aria-hidden="true">●</span>
            {badge.label}
          </span>
        </div>
        <RunTimeline rows={timeline(run)} onRetry={props.onRetry} needsKey={props.needsKey} onKey={props.onKey} />
      </div>

      <div className="space-y-4">
        {withPr.length > 0 && (
          <div className={`${OPS_CARD} p-4`}>
            <div className="flex items-center justify-between gap-2 mb-2">
              <h3 className={LABEL}>Pull requests</h3>
              <span className="text-[11px] text-slate-400 tabular-nums">
                {merged} of {run.items.length} merged
              </span>
            </div>
            <ul className="space-y-2">
              {withPr.map((i) => (
                <li key={i.blockId} className="rounded-lg border border-slate-200 bg-white/70 px-3 py-2">
                  <div className="flex items-center justify-between gap-2">
                    <span className="flex items-center gap-2 min-w-0">
                      <KindIcon kind={KIND_OF[i.kind]} size="w-6 h-6" />
                      <span className="font-mono text-xs font-semibold text-slate-800 truncate" title={props.repoPath(i)}>
                        {props.repoPath(i)}
                      </span>
                    </span>
                    <span className={`${PILL} ${i.merged ? 'bg-violet-50 text-violet-700' : 'bg-amber-50 text-amber-800'}`}>{i.merged ? 'merged' : 'open'}</span>
                  </div>
                  <p className="text-xs text-slate-500 mt-0.5">
                    #{i.pr!.number} · Studio: {i.name} · <span className="font-mono">{i.pr!.branch}</span>
                  </p>
                  <a href={i.pr!.url} target="_blank" rel="noreferrer" className="inline-block mt-1 text-xs text-emerald-700 hover:underline">
                    Review in GitHub ↗
                  </a>
                </li>
              ))}
            </ul>
            {waiting && (
              <p className="mt-3 rounded-lg bg-amber-50 border border-amber-200/70 px-3 py-2 text-xs text-amber-800">
                {awaitMergeCopy(run.items.length)}
              </p>
            )}
          </div>
        )}

        {done && (
          <div className="rounded-xl bg-emerald-50 border border-emerald-200/70 p-4">
            <h3 className="text-sm font-semibold text-emerald-900">Next in Governance</h3>
            <ol className="mt-2 space-y-1.5 text-sm text-emerald-900 list-decimal list-inside">
              <li>Approve the {run.items.length === 1 ? 'listing' : `${run.items.length} listings`}.</li>
              {agents.map((a) => {
                const targets = declaredNames(a, run, props.registry);
                return (
                  <li key={a.blockId}>
                    Then grant <span className="font-mono whitespace-nowrap">{a.name}</span> access to{' '}
                    {targets.length === 0
                      ? 'the MCP servers it needs'
                      : targets.map((t, i) => (
                          <span key={t}>
                            {i > 0 && (i === targets.length - 1 ? ' and ' : ', ')}
                            <span className="font-mono whitespace-nowrap">{t}</span>
                          </span>
                        ))}
                    .
                  </li>
                );
              })}
              {agents.length === 0 && mcps.length > 0 && (
                <li>
                  Then grant the agents that need them access to <span className="font-mono">{mcps.map((m) => m.name).join(', ')}</span>.
                </li>
              )}
            </ol>
            <p className="text-xs text-emerald-800/80 mt-2">Studio never grants access or approves a listing. That is Governance's call.</p>
            <div className="flex items-center gap-2 mt-3">
              <Link to="/agents/all" className={OPS_PRIMARY_BTN}>
                Open Governance
              </Link>
              <button type="button" onClick={props.onReset} className={SECONDARY_BTN}>
                Start over
              </button>
            </div>
          </div>
        )}

        {run.error && (
          <div className={`${OPS_CARD} p-4`}>
            <p className="text-xs text-slate-600">
              The run stopped. Fix the cause and retry the step, or start over. Repositories already created stay in the project.
            </p>
            <button type="button" onClick={props.onReset} className={`${SECONDARY_BTN} mt-2`}>
              Start over
            </button>
          </div>
        )}
      </div>
    </div>
  );
}
