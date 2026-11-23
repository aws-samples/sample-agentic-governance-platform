import { useCallback, useEffect, useRef, useState } from 'react';
import { Link } from 'react-router-dom';
import client, { mcpGrantsApi } from '../../api/client';
import type { McpGrantRead, McpServer } from '../../api/client';
import { useLoad } from '../../api/useLoad';
import { AgentAvatar } from './agentUi';
import PrincipalPicker, { type PickerHit } from './PrincipalPicker';
import { mcpRepoActions } from './mcpRepoOwnership';
import { REPLICATION_RECONCILE_MS, STALE_PENDING_MS } from './accessTiming';
import { useReturnFocus } from '../shared/useReturnFocus';

const CARD = 'bg-white/70 backdrop-blur rounded-xl border border-slate-200/60 shadow-sm';

function agentTypeBadge(): { cls: string; label: string } {
  return { cls: 'bg-indigo-50 text-indigo-700', label: 'Agent' };
}

function roleBadge(role: string): { cls: string; label: string } {
  return role === 'Admin'
    ? { cls: 'bg-amber-50 text-amber-700', label: 'Admin' }
    : { cls: 'bg-slate-100 text-slate-600', label: 'Invoker' };
}

function Spinner() {
  return (
    <span
      aria-hidden="true"
      className="inline-block h-4 w-4 rounded-full border-2 border-slate-200 border-t-blue-600 animate-spin"
    />
  );
}

function RebuildInstead({ repoHref }: { repoHref: string }) {
  return (
    <p className="mt-3 text-sm text-slate-600">
      This MCP server is managed by a repository.{' '}
      <Link to={repoHref} className="text-blue-700 font-medium hover:underline">
        Rebuild the repository instead
      </Link>
      .
    </p>
  );
}

function StatusBanner({
  mcp,
  stranded,
  canManage,
  onReprovision,
  repoHref,
}: {
  mcp: McpServer;
  stranded: boolean;
  canManage: boolean;
  onReprovision: () => void;
  repoHref: string | null;
}) {
  const status = mcp.identity_status;
  const { reprovision } = mcpRepoActions(repoHref !== null);

  if (status === 'none') {
    return (
      <div className={`${CARD} p-6`}>
        <h2 className="text-sm font-semibold text-slate-800">No Entra identity</h2>
        <p className="text-sm text-slate-500 mt-1">
          This MCP server has no Entra identity (metadata-only). Connected-agent grants are
          available once the server is provisioned with a Microsoft Entra identity.
        </p>
      </div>
    );
  }

  if (status === 'failed') {
    return (
      <div className={`${CARD} border-red-200/70 p-6`}>
        <h2 className="text-sm font-semibold text-red-700">Identity provisioning failed</h2>
        <p className="text-sm text-slate-600 mt-1">
          The Entra identity for this MCP server could not be provisioned. Connected-agent grants
          are unavailable until provisioning succeeds.
        </p>
        {repoHref && <RebuildInstead repoHref={repoHref} />}
        {reprovision && canManage && (
          <button
            type="button"
            onClick={onReprovision}
            className="mt-3 px-3.5 py-1.5 rounded-lg bg-blue-600 text-white text-sm font-medium hover:bg-blue-700 transition-colors"
          >
            Re-provision identity
          </button>
        )}
      </div>
    );
  }

  if (repoHref) {
    return (
      <div className={`${CARD} p-6`}>
        <h2 className="text-sm font-semibold text-slate-800">Waiting for the repository build</h2>
        <p className="text-sm text-slate-500 mt-1">
          This MCP server's gateway is built by its repository. Connected-agent grants will be
          available once a build succeeds.
        </p>
        <RebuildInstead repoHref={repoHref} />
      </div>
    );
  }
  return (
    <div className={`${CARD} p-6`}>
      <div className="flex items-center gap-2.5">
        <Spinner />
        <h2 className="text-sm font-semibold text-slate-800">Provisioning identity…</h2>
      </div>
      <p className="text-sm text-slate-500 mt-1">
        The MCP server's Microsoft Entra identity is being provisioned. Connected-agent grants will
        be available once it completes.
      </p>
      {stranded && (
        <div className="mt-3">
          <p className="text-sm text-amber-700">
            This has been pending for a while. If provisioning stalled, you can re-provision to
            recover.
          </p>
          {reprovision && canManage && (
            <button
              type="button"
              onClick={onReprovision}
              className="mt-2 px-3.5 py-1.5 rounded-lg bg-white border border-slate-300 text-slate-700 text-sm font-medium hover:bg-slate-50 transition-colors"
            >
              Re-provision identity
            </button>
          )}
        </div>
      )}
    </div>
  );
}

export default function McpConnectedAgentsTab({
  mcp,
  canManage,
  repoHref = null,
}: {
  mcp: McpServer;
  canManage: boolean;
  repoHref?: string | null;
}) {
  const [reprovisioned, setReprovisioned] = useState<{ from: McpServer; to: McpServer } | null>(null);
  const serverIdentity = reprovisioned !== null && reprovisioned.from === mcp ? reprovisioned.to : mcp;

  const [actionPending, setActionPending] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);

  const [picking, setPicking] = useState(false);

  const addTriggerRef = useRef<HTMLButtonElement>(null);
  const returnFocus = useReturnFocus(addTriggerRef);

  const closePicker = useCallback(() => {
    setPicking(false);
    setActionError(null);
    returnFocus();
  }, [returnFocus]);

  useEffect(() => {
    if (!picking) return;
    const onKeyDown = (e: KeyboardEvent) => {
      if (e.key === 'Escape') {
        e.preventDefault();
        closePicker();
      }
    };
    document.addEventListener('keydown', onKeyDown);
    return () => document.removeEventListener('keydown', onKeyDown);
  }, [picking, closePicker]);

  const provisioned = serverIdentity.identity_status === 'provisioned';
  const mcpId = serverIdentity.id;

  const mountedRef = useRef(true);
  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
    };
  }, []);

  const [strandedAt, setStrandedAt] = useState<string | null>(null);
  useEffect(() => {
    if (serverIdentity.identity_status !== 'pending') return;
    const updatedAt = serverIdentity.updated_at;
    const elapsed = Date.now() - new Date(updatedAt).getTime();
    if (Number.isNaN(elapsed)) return;
    const handle = window.setTimeout(() => {
      if (mountedRef.current) setStrandedAt(updatedAt);
    }, Math.max(0, STALE_PENDING_MS - elapsed));
    return () => window.clearTimeout(handle);
  }, [serverIdentity.identity_status, serverIdentity.updated_at]);
  const stranded =
    serverIdentity.identity_status === 'pending' && strandedAt === serverIdentity.updated_at;

  const { state: grantsState, patch: patchGrants } = useLoad(
    () => (provisioned ? mcpGrantsApi.list(mcpId) : Promise.resolve<McpGrantRead[]>([])),
    [mcpId, provisioned],
    'Failed to load grants.',
  );
  const grants = grantsState.data ?? [];

  const reconcile = async () => {
    const fresh = await mcpGrantsApi.list(mcpId).catch(() => null);
    if (fresh === null || !mountedRef.current) return;
    patchGrants(() => fresh);
  };

  const handleReprovision = async () => {
    setActionPending(true);
    setActionError(null);
    try {
      const { data } = await client.post<McpServer>(`/api/v1/mcp-servers/${mcpId}/reprovision`);
      if (mountedRef.current) setReprovisioned({ from: mcp, to: data });
    } catch (err: unknown) {
      if (mountedRef.current) {
        setActionError(err instanceof Error ? err.message : 'Re-provision failed.');
      }
    } finally {
      if (mountedRef.current) setActionPending(false);
    }
  };

  const handlePick = async (hit: PickerHit, role: 'Invoker' | 'Admin') => {
    if (actionPending) return;
    if (hit.type !== 'agent') return;
    setActionPending(true);
    setActionError(null);
    try {
      const created = await mcpGrantsApi.add(mcpId, {
        principal_id: hit.id,
        principal_type: 'agent',
        role,
      });
      if (!mountedRef.current) return;
      patchGrants((prev) => {
        if (prev.some((g) => g.assignment_id === created.assignment_id)) return prev;
        return [...prev, created];
      });
      closePicker();
      window.setTimeout(() => {
        void reconcile();
      }, REPLICATION_RECONCILE_MS);
    } catch (err: unknown) {
      if (mountedRef.current) {
        setActionError(err instanceof Error ? err.message : 'Failed to grant access.');
      }
    } finally {
      if (mountedRef.current) setActionPending(false);
    }
  };

  const handleRemove = async (grant: McpGrantRead) => {
    setActionPending(true);
    setActionError(null);
    const prevGrants = grants;
    patchGrants((prev) => prev.filter((g) => g.assignment_id !== grant.assignment_id));
    try {
      await mcpGrantsApi.remove(mcpId, grant.assignment_id);
      window.setTimeout(() => {
        void reconcile();
      }, REPLICATION_RECONCILE_MS);
    } catch (err: unknown) {
      if (mountedRef.current) {
        patchGrants(() => prevGrants);
        setActionError(err instanceof Error ? err.message : 'Failed to remove access.');
      }
    } finally {
      if (mountedRef.current) setActionPending(false);
    }
  };

  if (!provisioned) {
    return (
      <StatusBanner
        mcp={serverIdentity}
        stranded={stranded}
        canManage={canManage}
        onReprovision={handleReprovision}
        repoHref={repoHref}
      />
    );
  }

  const tBadge = agentTypeBadge();

  return (
    <div className="space-y-4">
      <div className={`${CARD} overflow-hidden`}>
        <div className="flex items-center justify-between gap-3 px-5 py-4 border-b border-slate-200/60">
          <div>
            <h2 className="text-sm font-semibold text-slate-800">Agents with access</h2>
            <p className="text-xs text-slate-400 mt-0.5">
              Direct Entra app-role assignments granting agents access to this MCP server.
            </p>
          </div>
          {canManage && !picking && (
            <button
              ref={addTriggerRef}
              type="button"
              onClick={() => {
                setPicking(true);
                setActionError(null);
              }}
              className="shrink-0 px-3.5 py-1.5 rounded-lg bg-blue-600 text-white text-sm font-medium hover:bg-blue-700 transition-colors"
            >
              + Grant agent access
            </button>
          )}
        </div>

        {picking && (
          <div className="px-5 py-4 border-b border-slate-200/60 bg-slate-50/50">
            <PrincipalPicker
              onPick={handlePick}
              onClose={closePicker}
              pending={actionPending}
              agentMode
            />
          </div>
        )}

        {actionError && (
          <p className="px-5 pt-3 text-sm text-red-600" role="alert">{actionError}</p>
        )}

        {grantsState.kind === 'loading' ? (
          <div className="px-5 py-8 text-center">
            <p className="text-sm text-slate-400">Loading access…</p>
          </div>
        ) : grantsState.kind === 'error' ? (
          <div className="px-5 py-8 text-center">
            <p className="text-sm text-red-600" role="alert">{grantsState.message}</p>
          </div>
        ) : grants.length === 0 ? (
          <div className="px-5 py-10 text-center">
            <p className="text-sm font-medium text-slate-600">No agents have been granted access yet.</p>
            <p className="text-xs text-slate-400 mt-1">
              {canManage
                ? 'Use “Grant agent access” to let an agent call this MCP server.'
                : 'An operator can grant agents access to this MCP server.'}
            </p>
          </div>
        ) : (
          <ul className="divide-y divide-slate-100">
            {grants.map((grant) => {
              const rBadge = roleBadge(grant.role);
              return (
                <li
                  key={grant.assignment_id}
                  className="flex items-center gap-3 px-5 py-3"
                >
                  <AgentAvatar name={grant.principal_display} size="sm" />
                  <div className="min-w-0 flex-1">
                    <div className="flex items-center gap-2 min-w-0">
                      <span className="text-sm font-medium text-slate-800 truncate">
                        {grant.principal_display}
                      </span>
                    </div>
                  </div>

                  <span className={`shrink-0 inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium ${tBadge.cls}`}>
                    {tBadge.label}
                  </span>

                  <span className={`shrink-0 inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium ${rBadge.cls}`}>
                    {rBadge.label}
                  </span>

                  {canManage && (
                    <button
                      type="button"
                      onClick={() => handleRemove(grant)}
                      disabled={actionPending}
                      aria-label={`Remove access for ${grant.principal_display}`}
                      className="shrink-0 inline-flex items-center justify-center h-7 w-7 rounded-md text-slate-400 hover:text-red-600 hover:bg-red-50 transition-colors disabled:opacity-40"
                    >
                      <span aria-hidden="true" className="text-base leading-none">×</span>
                    </button>
                  )}
                </li>
              );
            })}
          </ul>
        )}
      </div>
    </div>
  );
}
