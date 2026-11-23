import { useEffect, useMemo, useState } from 'react';
import { Link } from 'react-router-dom';
import { observabilityApi } from '../../../api/client';
import { useLoad } from '../../../api/useLoad';
import type { Agent, AgentRepositoryView, TraceRow } from '../../../api/client';
import { mapTraceRows, deriveTraceUrl, totalPages, tracesEmptyState } from './agentObservability';
import { ObsEmptyCard } from './ObsEmptyCard';

const CARD = 'bg-white/70 backdrop-blur rounded-xl border border-slate-200/60 shadow-sm';
const PAGE_SIZE = 20;
const NO_ROWS: TraceRow[] = [];

export default function TracesTab({
  agent,
  repo,
}: {
  agent: Agent;
  /** undefined = the repository lookup is still running; null = the agent has none. */
  repo: AgentRepositoryView | null | undefined;
}) {
  const agentId = agent.id;
  const [page, setPage] = useState(1);
  const [langfuseHost, setLangfuseHost] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    observabilityApi
      .getSettings()
      .then((s) => { if (!cancelled) setLangfuseHost(s.langfuse_host); })
      .catch(() => {});
    return () => { cancelled = true; };
  }, []);

  const { state } = useLoad(
    () => observabilityApi.getAgentTraces(agentId, { page, limit: PAGE_SIZE }),
    [agentId, page],
    'Failed to load traces',
  );
  const rows = state.data?.data ?? NO_ROWS;
  const total = state.data?.total ?? 0;
  const loading = state.kind === 'loading';
  const error = state.kind === 'error' ? state.message : '';

  const display = useMemo(() => mapTraceRows(rows), [rows]);
  const pages = totalPages(total, PAGE_SIZE);

  if (error) {
    return <ObsEmptyCard title="Couldn't load traces" body={error} tone="amber" />;
  }

  if (!loading && rows.length === 0 && page === 1) {
    const empty = tracesEmptyState(agent, repo);
    return (
      <div className="space-y-3">
        <ObsEmptyCard title={empty.title} body={empty.body} />
        {empty.opsLink && (
          <div>
            <Link
              to={empty.opsLink.to}
              className="inline-flex items-center gap-1 text-xs font-medium text-blue-600 hover:text-blue-700 transition-colors"
            >
              Open Observability for {empty.opsLink.name} in Operations
              <svg viewBox="0 0 20 20" fill="none" className="h-3 w-3" aria-hidden="true">
                <path d="M7 4h9v9M16 4l-9 9M4 8v8h8" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" />
              </svg>
            </Link>
          </div>
        )}
      </div>
    );
  }

  return (
    <div className={`${CARD} p-5`}>
      <div className="flex items-center justify-between mb-3">
        <div className="text-sm font-semibold text-slate-900">Recent Traces</div>
        {loading && (
          <div className="flex items-center gap-2 text-xs text-slate-400">
            <span className="w-3.5 h-3.5 border-2 border-slate-300 border-t-slate-500 rounded-full animate-spin" />
            Loading…
          </div>
        )}
      </div>

      <div className="overflow-x-auto">
        <table className="w-full text-sm">
          <thead>
            <tr className="text-[11px] text-slate-400 uppercase tracking-wide border-b border-slate-100">
              <th className="text-left py-2 font-medium">Timestamp</th>
              <th className="text-left py-2 font-medium pl-4">Name</th>
              <th className="text-right py-2 font-medium">Latency</th>
              <th className="text-right py-2 font-medium">Cost</th>
              <th className="text-left py-2 font-medium pl-4">User</th>
              <th className="text-right py-2 font-medium" aria-label="Open trace" />
            </tr>
          </thead>
          <tbody>
            {display.map((r) => {
              const url = deriveTraceUrl(langfuseHost, r.id);
              return (
                <tr key={r.id} className="border-b border-slate-50 hover:bg-slate-50/40">
                  <td className="py-2.5 text-slate-700 tabular-nums whitespace-nowrap">{r.timestamp}</td>
                  <td className="py-2.5 pl-4 text-slate-700">{r.name}</td>
                  <td className="py-2.5 text-right text-slate-500 tabular-nums whitespace-nowrap">{r.latency}</td>
                  <td className="py-2.5 text-right font-semibold text-slate-900 tabular-nums">{r.cost}</td>
                  <td className="py-2.5 pl-4 text-slate-500 text-[13px] truncate max-w-[16rem]">{r.user}</td>
                  <td className="py-2.5 text-right whitespace-nowrap">
                    {url ? (
                      <a
                        href={url}
                        target="_blank"
                        rel="noopener noreferrer"
                        className="inline-flex items-center gap-1 text-xs font-medium text-blue-600 hover:text-blue-700 transition-colors"
                      >
                        View
                        <svg viewBox="0 0 20 20" fill="none" className="h-3 w-3" aria-hidden="true">
                          <path d="M7 4h9v9M16 4l-9 9M4 8v8h8" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" />
                        </svg>
                      </a>
                    ) : (
                      <span className="text-[11px] text-slate-300 font-mono" title={r.id}>
                        {r.id.slice(0, 8)}
                      </span>
                    )}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>

      {pages > 1 && (
        <div className="flex items-center justify-between mt-4 pt-3 border-t border-slate-100">
          <div className="text-xs text-slate-400 tabular-nums">
            Page {page} of {pages} · {total.toLocaleString()} traces
          </div>
          <div className="flex items-center gap-2">
            <button
              type="button"
              onClick={() => setPage((p) => Math.max(1, p - 1))}
              disabled={page <= 1 || loading}
              className="px-3 py-1.5 rounded-lg bg-white border border-slate-300 text-slate-700 text-xs font-medium hover:bg-slate-50 transition-colors disabled:opacity-40 disabled:cursor-not-allowed"
            >
              Previous
            </button>
            <button
              type="button"
              onClick={() => setPage((p) => Math.min(pages, p + 1))}
              disabled={page >= pages || loading}
              className="px-3 py-1.5 rounded-lg bg-white border border-slate-300 text-slate-700 text-xs font-medium hover:bg-slate-50 transition-colors disabled:opacity-40 disabled:cursor-not-allowed"
            >
              Next
            </button>
          </div>
        </div>
      )}
    </div>
  );
}
