import { useEffect, useState } from 'react';
import { agentMcpGrantsApi } from '../../api/client';
import type { MissingMcpGrant } from '../../api/client';
import { missingMcpGrantsHint } from './missingMcpGrants';

export default function MissingMcpGrantsHint({
  agentId,
  refreshKey,
  canManage,
  onGrant,
}: {
  agentId: string;
  refreshKey: number;
  canManage: boolean;
  onGrant: () => void;
}) {
  const [missing, setMissing] = useState<MissingMcpGrant[]>([]);

  useEffect(() => {
    let cancelled = false;
    agentMcpGrantsApi
      .missing(agentId)
      .then((rows) => !cancelled && setMissing(rows))
      .catch(() => !cancelled && setMissing([]));
    return () => {
      cancelled = true;
    };
  }, [agentId, refreshKey]);

  const hint = missingMcpGrantsHint(missing);
  if (!hint) return null;
  return (
    <div
      role="note"
      className="mb-4 flex items-center justify-between gap-3 rounded-xl border border-amber-200/70 bg-amber-50/70 px-5 py-3 text-sm text-amber-800"
    >
      <span>{hint}</span>
      <button
        type="button"
        onClick={onGrant}
        className="shrink-0 px-3 py-1.5 rounded-lg bg-white border border-amber-300 text-amber-800 text-xs font-medium hover:bg-amber-100 transition-colors"
      >
        {canManage ? (missing.length > 1 ? 'Grant access' : 'Grant it') : 'View MCP servers'}
      </button>
    </div>
  );
}
