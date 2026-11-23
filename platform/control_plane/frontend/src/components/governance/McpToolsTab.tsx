import { useState } from 'react';
import { mcpServersApi } from '../../api/client';
import type { McpServer } from '../../api/client';

const CARD = 'bg-white/70 backdrop-blur rounded-xl border border-slate-200/60 shadow-sm';

function Spinner() {
  return (
    <span
      aria-hidden="true"
      className="inline-block h-3.5 w-3.5 rounded-full border-2 border-blue-200 border-t-white animate-spin"
    />
  );
}

export default function McpToolsTab({
  mcp,
  canManage,
  onRefreshed,
}: {
  mcp: McpServer;
  canManage: boolean;
  onRefreshed: (next: McpServer) => void;
}) {
  const canRefresh = mcp.kind === 'gateway' && canManage;

  const [refreshing, setRefreshing] = useState(false);
  const [refreshError, setRefreshError] = useState<string | null>(null);
  const [justUpdated, setJustUpdated] = useState(false);

  const runRefresh = async () => {
    setRefreshing(true);
    setRefreshError(null);
    setJustUpdated(false);
    try {
      const next = await mcpServersApi.refreshTools(mcp.id);
      onRefreshed(next);
      setJustUpdated(true);
    } catch (err: unknown) {
      setRefreshError(err instanceof Error ? err.message : 'Failed to refresh tools.');
    } finally {
      setRefreshing(false);
    }
  };

  const RefreshBar = canRefresh ? (
    <div className="flex items-center justify-between gap-3 flex-wrap">
      <div className="flex items-center gap-2.5 min-w-0">
        <span className="text-[11px] uppercase tracking-wide text-slate-400 font-medium">
          {mcp.available_tools.length} {mcp.available_tools.length === 1 ? 'tool' : 'tools'}
        </span>
        {justUpdated && !refreshing && !refreshError && (
          <span className="text-xs text-emerald-600">Updated just now</span>
        )}
      </div>
      <button
        type="button"
        onClick={runRefresh}
        disabled={refreshing}
        className="shrink-0 inline-flex items-center gap-1.5 px-3.5 py-1.5 rounded-lg bg-blue-600 text-white text-sm font-medium hover:bg-blue-700 transition-colors disabled:opacity-50"
      >
        {refreshing ? (
          <>
            <Spinner />
            Refreshing…
          </>
        ) : (
          <>
            <span aria-hidden="true">↻</span>
            Refresh tools
          </>
        )}
      </button>
    </div>
  ) : null;

  const RefreshError = refreshError ? (
    <p className="text-sm text-red-600" role="alert">{refreshError}</p>
  ) : null;

  if (mcp.available_tools.length === 0) {
    return (
      <div className="space-y-3">
        {RefreshBar}
        {RefreshError}
        <div className="bg-white/60 backdrop-blur rounded-xl border border-slate-200/60 p-8 text-center text-slate-400 text-sm">
          No tools declared.
          {canRefresh && (
            <span className="block mt-1 text-slate-500">
              Refresh tools to re-read this gateway's catalog.
            </span>
          )}
        </div>
      </div>
    );
  }

  return (
    <div className="space-y-3">
      {RefreshBar}
      {RefreshError}
      {mcp.available_tools.map((tool, i) => (
        <div key={`${tool.name}-${i}`} className={`${CARD} p-5`}>
          <div className="flex items-baseline justify-between gap-3 flex-wrap">
            <code className="text-sm font-mono font-semibold text-slate-800 break-all">{tool.name}</code>
          </div>
          {tool.description && (
            <p className="text-sm text-slate-600 mt-1 whitespace-pre-wrap">{tool.description}</p>
          )}
          <div className="mt-3">
            <dt className="text-[11px] uppercase tracking-wide text-slate-400 font-medium mb-1">Input schema</dt>
            <pre className="text-xs font-mono text-slate-700 bg-slate-50 border border-slate-200/60 rounded-lg p-3 overflow-x-auto">
              {JSON.stringify(tool.input_schema, null, 2)}
            </pre>
          </div>
        </div>
      ))}
    </div>
  );
}
