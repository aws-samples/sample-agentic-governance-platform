import { useCallback, useEffect, useMemo, useState, type JSX } from 'react';

import { evalsApi, observabilityApi, type Repository, type ScopeMetrics, type TraceList } from '../../../api/client';
import { useLoad } from '../../../api/useLoad';
import { NO_VALUE } from '../opsLabels';
import { OPS_BADGE, OPS_CARD, OPS_PRIMARY_BTN, OPS_TABLE_DIVIDE, OPS_TABLE_HEAD } from '../opsUi';
import AddToDatasetModal from './AddToDatasetModal';
import { evalsTabVisible } from './evaluationsLogic';
import {
  DEFAULT_TRACE_STAGE,
  TRACES_COPY,
  TRACE_LIMIT,
  TRACE_STAGES,
  committedFlash,
  selectable,
  traceRowView,
  type TraceStage,
} from './tracesToDataset';
import {
  AGENT_ZERO_NOTE,
  METRICS_STATE_COPY,
  PROJECT_SCOPE_NOTE,
  agentSlice,
  metricsState,
  metricsWindow,
} from './observabilityTabModel';

const FIELD_LABEL = 'block text-[11px] uppercase tracking-wide text-slate-400 font-medium mb-1';

const fmtInt = (n: number): string => n.toLocaleString();
const fmtUsd = (n: number): string => `$${n.toFixed(2)}`;

export default function ObservabilityTab({
  repo,
  mayWrite,
}: {
  repo: Repository;
  mayWrite: boolean;
}): JSX.Element {
  const range = useMemo(() => metricsWindow(), []);
  const metricsLoad = useLoad(
    async (): Promise<{ configured: boolean | null; metrics: ScopeMetrics | null; error: boolean }> => {
      let configured: boolean | null = null;
      try {
        configured = (await observabilityApi.getSettings()).configured;
      } catch {
        configured = null;
      }
      if (configured !== true) return { configured, metrics: null, error: false };
      try {
        const metrics = await observabilityApi.getMetrics('project', {
          projectId: repo.project_id,
          dateFrom: range.dateFrom,
          dateTo: range.dateTo,
        });
        return { configured, metrics, error: false };
      } catch {
        return { configured, metrics: null, error: true };
      }
    },
    [repo.project_id, range.dateFrom, range.dateTo],
  );
  const read = metricsLoad.state.kind === 'ready' ? metricsLoad.state.data : null;
  const metrics = read?.metrics ?? null;

  const state = metricsState({
    loading: metricsLoad.state.kind === 'loading',
    configured: read?.configured,
    error: read?.error ?? false,
    metrics,
  });
  const copy = METRICS_STATE_COPY[state];
  const slice = agentSlice(metrics, repo.agent_id ?? '');

  const traces = evalsTabVisible(repo) ? <TracesPanel repo={repo} mayWrite={mayWrite} /> : null;

  if (copy !== null) {
    return (
      <div className="space-y-6">
        <div className={`${OPS_CARD} p-6`}>
          <h3 className="text-sm font-semibold text-slate-800">{copy.headline}</h3>
          <p className="text-sm text-slate-500 mt-1 max-w-2xl">{copy.detail}</p>
          <p className="text-[11px] text-slate-400 mt-3">
            Window {range.dateFrom} to {range.dateTo}
          </p>
        </div>
        {traces}
      </div>
    );
  }

  return (
    <div className="space-y-6">
      <div className={`${OPS_CARD} p-5`}>
        <div className="flex flex-wrap items-baseline justify-between gap-2 mb-4">
          <h3 className="text-sm font-semibold text-slate-800">This repository’s agent</h3>
          <span className="text-[11px] text-slate-400">
            {range.dateFrom} to {range.dateTo}
          </span>
        </div>
        {slice.kind === 'present' && slice.totals !== null ? (
          <>
            <dl className="grid grid-cols-1 sm:grid-cols-3 gap-4">
              <Tile label="Traces" value={fmtInt(slice.totals.traces)} />
              <Tile label="Cost" value={fmtUsd(slice.totals.cost_usd)} />
              <Tile label="Tokens" value={fmtInt(slice.totals.tokens)} />
            </dl>
            <p className="text-[11px] text-slate-400 mt-3 max-w-2xl">{AGENT_ZERO_NOTE}</p>
          </>
        ) : (
          <p className="text-sm text-slate-500 max-w-2xl">
            This agent isn’t in the project’s metrics breakdown, so its own traces and cost are
            unknown — not zero. That happens when the agent isn’t visible to you or isn’t yet
            attributed to this project.
          </p>
        )}
      </div>

      {traces}

      <div className={`${OPS_CARD} p-5`}>
        <h3 className="text-sm font-semibold text-slate-800">Project totals</h3>
        <p className="text-sm text-slate-500 mt-1 max-w-2xl">{PROJECT_SCOPE_NOTE}</p>
        <dl className="grid grid-cols-1 sm:grid-cols-3 gap-4 mt-4">
          <Tile
            label="Traces"
            value={metrics === null ? null : fmtInt(metrics.totals.traces)}
          />
          <Tile
            label="Cost"
            value={metrics === null ? null : fmtUsd(metrics.totals.cost_usd)}
          />
          <Tile
            label="Tokens"
            value={metrics === null ? null : fmtInt(metrics.totals.tokens)}
          />
        </dl>
      </div>

      <div className={`${OPS_CARD} p-5`}>
        <h3 className="text-sm font-semibold text-slate-800 mb-4">Agents in this project</h3>
        {(metrics?.by_agent ?? []).length === 0 ? (
          <p className="text-sm text-slate-500">
            The read returned no per-agent breakdown for this project.
          </p>
        ) : (
          <ul className={`divide-y divide-emerald-100/70`}>
            {(metrics?.by_agent ?? []).map((a) => (
              <li key={a.agent_id} className="flex items-baseline justify-between gap-4 py-2">
                <span className="text-sm text-slate-700 truncate">
                  {a.agent_name || a.agent_id}
                  {a.agent_id === repo.agent_id && (
                    <span className="ml-2 text-[11px] text-emerald-700 font-medium">
                      this repository
                    </span>
                  )}
                </span>
                <span className="shrink-0 text-xs text-slate-500 tabular-nums">
                  {fmtInt(a.totals.traces)} traces · {fmtUsd(a.totals.cost_usd)}
                </span>
              </li>
            ))}
          </ul>
        )}
      </div>
    </div>
  );
}

function Tile({ label, value }: { label: string; value: string | null }): JSX.Element {
  return (
    <div className="rounded-lg border border-emerald-100/70 px-4 py-3">
      <dt className={FIELD_LABEL}>{label}</dt>
      <dd className="text-2xl font-semibold text-slate-900 tabular-nums">{value ?? NO_VALUE}</dd>
    </div>
  );
}

const PILL = 'inline-flex items-center gap-1.5 text-[11px] font-semibold px-2 py-0.5 rounded-full';
const SECONDARY_BTN =
  'px-3 py-1.5 rounded-lg border border-slate-300 bg-white text-sm font-medium text-slate-700 hover:bg-slate-50 transition-colors disabled:opacity-50 disabled:cursor-not-allowed';

function TracesPanel({ repo, mayWrite }: { repo: Repository; mayWrite: boolean }): JSX.Element {
  const [stage, setStage] = useState<TraceStage>(DEFAULT_TRACE_STAGE);
  const [read, setRead] = useState<TraceList | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [tick, setTick] = useState(0);
  const [selected, setSelected] = useState<ReadonlySet<string>>(new Set());
  const [adding, setAdding] = useState(false);
  const [flash, setFlash] = useState<string | null>(null);
  const [now, setNow] = useState(() => Date.now());

  useEffect(() => {
    let cancelled = false;
    evalsApi
      .traces(repo.project_id, repo.id, { stage, limit: TRACE_LIMIT })
      .then((r) => {
        if (cancelled) return;
        setRead(r);
        setNow(Date.now());
      })
      .catch((e: unknown) => {
        if (cancelled) return;
        setRead(null);
        setError(e instanceof Error ? e.message : 'Something went wrong');
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [repo.project_id, repo.id, stage, tick]);

  const rows = useMemo(() => read?.traces ?? [], [read]);
  const pickable = useMemo(() => rows.filter(selectable), [rows]);
  const picked = useMemo(() => rows.filter((t) => selected.has(t.session_id)), [rows, selected]);
  const allPicked = pickable.length > 0 && picked.length === pickable.length;

  const restart = () => {
    setLoading(true);
    setError(null);
    setSelected(new Set());
  };

  const toggle = useCallback((id: string) => {
    setSelected((s) => {
      const next = new Set(s);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }, []);
  const toggleAll = () =>
    setSelected(allPicked ? new Set() : new Set(pickable.map((t) => t.session_id)));

  const empty = read !== null && (read.source === 'none' || rows.length === 0);
  const emptyCopy = read?.source === 'none' ? TRACES_COPY.emptyNone : TRACES_COPY.emptyList;

  return (
    <div className={`${OPS_CARD} p-5`}>
      <div className="flex flex-wrap items-start justify-between gap-3 mb-4">
        <div className="min-w-0">
          <h3 className="text-sm font-semibold text-slate-800">{TRACES_COPY.title}</h3>
          <p className="text-sm text-slate-500 mt-1 max-w-2xl">{TRACES_COPY.subtitle}</p>
        </div>
        <div className="flex items-center gap-2">
          <div role="group" aria-label="Stage" className="inline-flex rounded-lg border border-slate-300 bg-white p-0.5">
            {TRACE_STAGES.map((s) => (
              <button
                key={s.value}
                type="button"
                aria-pressed={stage === s.value}
                onClick={() => {
                  if (s.value === stage) return;
                  restart();
                  setRead(null);
                  setStage(s.value);
                }}
                className={`px-3 py-1 rounded-md text-xs font-medium transition-colors ${
                  stage === s.value ? 'bg-emerald-600 text-white' : 'text-slate-600 hover:bg-slate-50'
                }`}
              >
                {s.label}
              </button>
            ))}
          </div>
          <button
            type="button"
            className={SECONDARY_BTN}
            onClick={() => {
              restart();
              setTick((n) => n + 1);
            }}
            disabled={loading}
          >
            {loading ? 'Reading…' : 'Refresh'}
          </button>
        </div>
      </div>

      {flash && (
        <div role="status" className="mb-4 flex items-start justify-between gap-3 rounded-lg bg-emerald-50 px-3 py-2 text-sm text-emerald-800">
          <span>{flash}</span>
          <button
            type="button"
            aria-label="Dismiss"
            className="shrink-0 text-emerald-700 hover:text-emerald-900"
            onClick={() => setFlash(null)}
          >
            <span aria-hidden="true">×</span>
          </button>
        </div>
      )}

      {error ? (
        <p role="alert" className="rounded-lg bg-rose-50 px-3 py-2 text-sm text-rose-700">
          Could not read traces: {error}
        </p>
      ) : read === null ? (
        <p className="text-sm text-slate-500" role="status">Reading traces…</p>
      ) : empty ? (
        <div className="rounded-lg border border-dashed border-emerald-200 px-4 py-5">
          <p className="text-sm font-medium text-slate-700">{emptyCopy.headline}</p>
          <p className="text-sm text-slate-500 mt-1 max-w-2xl">{emptyCopy.detail}</p>
        </div>
      ) : (
        <>
          <div className="flex flex-wrap items-center justify-between gap-2 mb-3">
            <span className="text-xs text-slate-500" aria-live="polite">
              {picked.length > 0 ? `${picked.length} selected` : `${rows.length} recent trace${rows.length === 1 ? '' : 's'}`}
              {picked.length > 0 && (
                <button
                  type="button"
                  className="ml-2 font-medium text-emerald-700 hover:text-emerald-800"
                  onClick={() => setSelected(new Set())}
                >
                  Clear
                </button>
              )}
            </span>
            {mayWrite ? (
              <button
                type="button"
                className={`${OPS_PRIMARY_BTN} disabled:opacity-50 disabled:cursor-not-allowed`}
                disabled={picked.length === 0}
                onClick={() => {
                  setFlash(null);
                  setAdding(true);
                }}
              >
                Add to dataset{picked.length > 0 ? ` (${picked.length})` : ''}
              </button>
            ) : (
              <span className="text-xs text-slate-400">Adding traces to the dataset needs the Maintainer role on this project.</span>
            )}
          </div>
          <div className="overflow-x-auto -mx-5">
            <table className="w-full text-sm">
              <thead className={OPS_TABLE_HEAD}>
                <tr>
                  <th className="w-10 pl-5 py-2 text-left">
                    <input
                      type="checkbox"
                      aria-label="Select all traces"
                      className="accent-emerald-600"
                      checked={allPicked}
                      ref={(el) => {
                        if (el) el.indeterminate = picked.length > 0 && !allPicked;
                      }}
                      disabled={!mayWrite || pickable.length === 0}
                      onChange={toggleAll}
                    />
                  </th>
                  <th className="py-2 text-left font-medium whitespace-nowrap">When</th>
                  <th className="py-2 pl-4 text-left font-medium">Input</th>
                  <th className="py-2 pl-4 text-left font-medium">Tools</th>
                  <th className="py-2 pl-4 pr-5 text-right font-medium">Latency</th>
                </tr>
              </thead>
              <tbody className={OPS_TABLE_DIVIDE}>
                {rows.map((t) => {
                  const v = traceRowView(t, now);
                  const can = selectable(t);
                  const on = selected.has(t.session_id);
                  return (
                    <tr key={t.session_id} className={on ? 'bg-emerald-50/50' : 'hover:bg-slate-50/60'}>
                      <td className="pl-5 py-2.5 align-top">
                        <input
                          type="checkbox"
                          className="accent-emerald-600 mt-0.5"
                          aria-label={`Select the trace from ${v.when ?? 'an unknown time'}`}
                          title={can ? undefined : TRACES_COPY.noInput}
                          checked={on}
                          disabled={!mayWrite || !can}
                          onChange={() => toggle(t.session_id)}
                        />
                      </td>
                      <td className="py-2.5 align-top text-slate-700 tabular-nums whitespace-nowrap" title={v.title}>
                        {v.when ?? NO_VALUE}
                      </td>
                      <td className="py-2.5 pl-4 align-top max-w-[28rem]">
                        {v.input ? (
                          <span className="block text-slate-700 truncate" title={t.input ?? undefined}>
                            {v.input}
                          </span>
                        ) : (
                          <span className="text-slate-400 italic">No input recorded</span>
                        )}
                      </td>
                      <td className="py-2.5 pl-4 align-top">
                        {v.tools === null ? (
                          <span className="text-xs text-slate-400 italic">{TRACES_COPY.noToolData}</span>
                        ) : v.tools.length === 0 ? (
                          <span className="text-xs text-slate-400">{TRACES_COPY.noTools}</span>
                        ) : (
                          <ol className="flex flex-wrap items-center gap-1" aria-label="Tool calls in order">
                            {v.tools.map((name, i) => (
                              <li key={i} className={`${PILL} ${OPS_BADGE.unknown} font-mono font-medium`}>
                                {name}
                              </li>
                            ))}
                          </ol>
                        )}
                      </td>
                      <td className="py-2.5 pl-4 pr-5 align-top text-right text-slate-500 tabular-nums whitespace-nowrap">
                        {v.latency ?? NO_VALUE}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        </>
      )}

      {adding && mayWrite && (
        <AddToDatasetModal
          repo={repo}
          traces={picked}
          onClose={() => setAdding(false)}
          onDone={(n) => {
            setAdding(false);
            setSelected(new Set());
            setFlash(committedFlash(n));
          }}
        />
      )}
    </div>
  );
}
