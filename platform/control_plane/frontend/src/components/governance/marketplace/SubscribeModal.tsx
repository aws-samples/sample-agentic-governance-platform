import { useCallback, useEffect, useRef, useState } from 'react';
import type { AgentLite, ProductCard } from '../../../api/client';
import { AgentAvatar } from '../agentUi';

export default function SubscribeModal({
  card,
  eligibleAgents,
  onSubmit,
  onClose,
}: {
  card: ProductCard;
  eligibleAgents: AgentLite[];
  onSubmit: (opts: { agentId?: string; message?: string }) => Promise<void>;
  onClose: () => void;
}) {
  const isMcp = card.product_type === 'mcp';

  const [agentId, setAgentId] = useState('');
  const [message, setMessage] = useState('');
  const [actionPending, setActionPending] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);

  const mountedRef = useRef(true);
  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
    };
  }, []);

  useEffect(() => {
    const onKeyDown = (e: KeyboardEvent) => {
      if (e.key === 'Escape') {
        e.preventDefault();
        onClose();
      }
    };
    document.addEventListener('keydown', onKeyDown);
    return () => document.removeEventListener('keydown', onKeyDown);
  }, [onClose]);

  const canSubmit = !isMcp || agentId !== '';

  const handleSubmit = useCallback(async () => {
    if (actionPending || !canSubmit) return;
    setActionPending(true);
    setActionError(null);
    try {
      await onSubmit({
        agentId: isMcp ? agentId : undefined,
        message: message.trim() ? message.trim() : undefined,
      });
    } catch (err: unknown) {
      if (mountedRef.current) {
        setActionError(err instanceof Error ? err.message : 'Failed to subscribe.');
      }
    } finally {
      if (mountedRef.current) setActionPending(false);
    }
  }, [actionPending, canSubmit, onSubmit, isMcp, agentId, message]);

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-slate-900/30 backdrop-blur-sm p-4"
      role="dialog"
      aria-modal="true"
      aria-label={`Subscribe to ${card.name}`}
      onMouseDown={(e) => {
        if (e.target === e.currentTarget && !actionPending) onClose();
      }}
    >
      <div className="w-full max-w-lg bg-white rounded-2xl border border-slate-200 shadow-xl">
        <div className="flex items-start justify-between gap-3 px-5 py-4 border-b border-slate-200/60">
          <div className="flex items-start gap-3 min-w-0">
            <AgentAvatar name={card.name} size="md" />
            <div className="min-w-0">
              <h2 className="text-sm font-semibold text-slate-900 leading-tight truncate">
                Subscribe to {card.name}
              </h2>
              <p className="text-xs text-slate-500 mt-0.5">
                {isMcp
                  ? 'Request access to this MCP server on behalf of one of your agents. An admin approves before the grant is applied.'
                  : 'Request access to this published agent. An administrator reviews marketplace subscriptions; some products are approved automatically.'}
              </p>
            </div>
          </div>
          <button
            type="button"
            onClick={onClose}
            disabled={actionPending}
            aria-label="Close"
            className="shrink-0 inline-flex items-center justify-center h-7 w-7 rounded-md text-slate-400 hover:text-slate-700 hover:bg-slate-100 transition-colors disabled:opacity-40"
          >
            <span aria-hidden="true" className="text-base leading-none">×</span>
          </button>
        </div>

        <div className="px-5 py-4 space-y-4">
          {isMcp && (
            <div>
              <label
                htmlFor="subscribe-agent"
                className="block text-[11px] uppercase tracking-wide text-slate-400 font-medium mb-1"
              >
                For agent <span className="text-red-500">*</span>
              </label>
              {eligibleAgents.length === 0 ? (
                <p className="text-sm text-slate-500">
                  You have no provisioned agents that can hold this grant. Provision an agent's
                  identity first, then subscribe.
                </p>
              ) : (
                <select
                  id="subscribe-agent"
                  value={agentId}
                  onChange={(e) => setAgentId(e.target.value)}
                  className="w-full px-3 py-1.5 text-sm rounded-lg border border-slate-300 bg-white focus:outline-none focus:ring-2 focus:ring-blue-500/40"
                >
                  <option value="">Select an agent…</option>
                  {eligibleAgents.map((a) => (
                    <option key={a.id} value={a.id}>
                      {a.name}
                    </option>
                  ))}
                </select>
              )}
            </div>
          )}

          <div>
            <label
              htmlFor="subscribe-message"
              className="block text-[11px] uppercase tracking-wide text-slate-400 font-medium mb-1"
            >
              Message <span className="text-slate-400 normal-case font-normal">(optional)</span>
            </label>
            <textarea
              id="subscribe-message"
              value={message}
              onChange={(e) => setMessage(e.target.value)}
              rows={3}
              placeholder="Add context for the approver…"
              className="w-full px-3 py-2 text-sm rounded-lg border border-slate-300 focus:outline-none focus:ring-2 focus:ring-blue-500/40 resize-none"
            />
          </div>

          {actionError && (
            <p className="text-sm text-red-600" role="alert">
              {actionError}
            </p>
          )}
        </div>

        <div className="flex items-center justify-end gap-2 px-5 py-4 border-t border-slate-200/60">
          <button
            type="button"
            onClick={onClose}
            disabled={actionPending}
            className="px-3.5 py-1.5 rounded-lg bg-white border border-slate-300 text-slate-700 text-sm font-medium hover:bg-slate-50 transition-colors disabled:opacity-40"
          >
            Cancel
          </button>
          <button
            type="button"
            onClick={handleSubmit}
            disabled={actionPending || !canSubmit}
            className="px-3.5 py-1.5 rounded-lg bg-blue-600 text-white text-sm font-medium hover:bg-blue-700 transition-colors disabled:opacity-40"
          >
            {actionPending ? 'Submitting…' : 'Confirm'}
          </button>
        </div>
      </div>
    </div>
  );
}
