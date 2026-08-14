// TracesTab — the per-agent "Traces" tab of AgentDetail (Epic 26 / Task 9).
//
// A paged table of this agent's recent Langfuse traces (timestamp, name,
// latency, cost, user). Rows drill down to the trace in the deployed Langfuse UI
// when a host is derivable from the observability settings; otherwise the row is
// a plain detail row (no dead link). Every Optional field the backend TraceRow
// emits (timestamp/name/latency_ms/user_id) renders as "—" via the pure
// mapTraceRows helper — no null.toFixed() crash. VIEWER-readable; the backend
// enforces tenant scope (a foreign agent 404s). Table chrome mirrors the FinOps /
// dashboard idiom: uppercase-tracked headers, tabular-nums cells, hover rows.
import { useEffect, useMemo, useState } from 'react';
import { observabilityApi } from '../../../api/client';
import type { TraceRow } from '../../../types';
import { mapTraceRows, deriveTraceUrl, totalPages } from './agentObservability';
import { ObsEmptyCard } from './ObsEmptyCard';

const CARD = 'bg-white/70 backdrop-blur rounded-xl border border-slate-200/60 shadow-sm';
const PAGE_SIZE = 20;

export default function TracesTab({ agentId }: { agentId: string }) {
  const [page, setPage] = useState(1);
  const [rows, setRows] = useState<TraceRow[]>([]);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  // Best-effort Langfuse host for the per-row deep link. A failure here never
  // blocks the table — rows just fall back to a detail affordance (deriveTraceUrl
  // returns null for a null host).
  const [langfuseHost, setLangfuseHost] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    observabilityApi
      .getSettings()
      .then((s) => { if (!cancelled) setLangfuseHost(s.langfuse_host); })
      .catch(() => { /* no host → detail-only rows */ });
    return () => { cancelled = true; };
  }, []);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError('');
    observabilityApi
      .getAgentTraces(agentId, { page, limit: PAGE_SIZE })
      .then((res) => { if (!cancelled) { setRows(res.data); setTotal(res.total); } })
      .catch((e) => { if (!cancelled) setError(e instanceof Error ? e.message : 'Failed to load traces'); })
      .finally(() => { if (!cancelled) setLoading(false); });
    return () => { cancelled = true; };
  }, [agentId, page]);

  const display = useMemo(() => mapTraceRows(rows), [rows]);
  const pages = totalPages(total, PAGE_SIZE);

  if (error) {
    return <ObsEmptyCard title="Couldn't load traces" body={error} tone="amber" />;
  }

  // Empty-state: only when the first page came back empty (not mid-pagination).
  if (!loading && rows.length === 0 && page === 1) {
    return (
      <ObsEmptyCard
        title="No traces recorded yet"
        body="This agent has no Langfuse traces. Once it's provisioned and invoked, each run will appear here with its latency, cost and calling user — newest first."
      />
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

      {/* Pager — only when there's more than one page. */}
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
