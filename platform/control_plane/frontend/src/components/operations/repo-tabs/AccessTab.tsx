import { useMemo, type JSX } from 'react';

import type { ArtifactKind } from '../../../api/client';
import { NO_VALUE } from '../opsLabels';
import { OPS_CARD } from '../opsUi';
import { FIELD_LABEL } from '../RepoFields';

export type MaybeCount = number | null;

export default function AccessTab({
  kind,
  artifactId,
  inbound,
  outbound,
  onNavigateAway,
}: {
  kind: ArtifactKind;
  artifactId: string;
  inbound: MaybeCount;
  outbound: MaybeCount;
  onNavigateAway: () => void;
}): JSX.Element {
  const isMcp = kind === 'mcp_server';
  const rows = useMemo(
    () => isMcp ? [
      {
        label: 'Agent grants',
        hint: 'Agents granted to call this MCP server.',
        count: inbound,
      },
    ] : [
      {
        label: 'Inbound invoke grants',
        hint: 'Principals that may invoke this agent, as Entra app-role assignments.',
        count: inbound,
      },
      {
        label: 'Outbound MCP dependencies',
        hint: 'MCP servers this agent is granted to reach.',
        count: outbound,
      },
    ],
    [isMcp, inbound, outbound],
  );

  return (
    <div className={`${OPS_CARD} p-5`}>
      <div className="flex items-start justify-between gap-4 mb-4">
        <div>
          <h3 className="text-sm font-semibold text-slate-800">Access</h3>
          <p className="text-sm text-slate-500 mt-1 max-w-2xl">
            A read-only mirror of the {isMcp ? 'MCP server’s' : 'agent’s'} access shape. Who exactly
            holds a grant, and every change to one, lives on the governance record.
          </p>
        </div>
        <button
          type="button"
          onClick={onNavigateAway}
          className="shrink-0 px-3 py-1.5 rounded-lg bg-white border border-slate-300 text-slate-700 text-xs font-medium hover:bg-slate-50 transition-colors"
        >
          Open governance record
        </button>
      </div>
      <dl className="grid grid-cols-1 sm:grid-cols-2 gap-4">
        {rows.map((row) => (
          <div key={row.label} className="rounded-lg border border-emerald-100/70 px-4 py-3">
            <dt className={FIELD_LABEL}>{row.label}</dt>
            <dd className="text-2xl font-semibold text-slate-900 tabular-nums">
              {row.count === null ? (
                <span
                  className="text-slate-400 text-base font-normal"
                  title="This count could not be read — it is not known to be zero."
                >
                  {NO_VALUE}
                </span>
              ) : (
                row.count
              )}
            </dd>
            <p className="text-[11px] text-slate-400 mt-1">{row.hint}</p>
          </div>
        ))}
      </dl>
      <p className="text-[11px] text-slate-400 mt-4">
        {isMcp ? 'MCP server' : 'Agent'} <span className="font-mono">{artifactId}</span>
      </p>
    </div>
  );
}
