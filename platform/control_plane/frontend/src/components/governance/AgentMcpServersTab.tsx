import { useCallback, useEffect, useRef, useState } from 'react';
import { Link } from 'react-router-dom';
import client, { agentMcpGrantsApi, mcpGrantsApi, mcpServersApi } from '../../api/client';
import type { Agent, AgentMcpGrant, McpServer } from '../../api/client';
import { useLoad } from '../../api/useLoad';
import { AgentAvatar } from './agentUi';
import { mcpDeliveryNote } from './platformLabels';
import { computeDrift, declaredServerStatus } from './mcpDrift';
import type { DriftState } from './mcpDrift';
import { REPLICATION_RECONCILE_MS, STALE_PENDING_MS } from './accessTiming';
import { useReturnFocus } from '../shared/useReturnFocus';

const CARD = 'bg-white/70 backdrop-blur rounded-xl border border-slate-200/60 shadow-sm';

function roleBadge(role: string): { cls: string; label: string } {
  if (role === 'Admin') return { cls: 'bg-amber-50 text-amber-700', label: 'Admin' };
  if (role === 'Invoker') return { cls: 'bg-slate-100 text-slate-600', label: 'Invoker' };
  return { cls: 'bg-slate-50 text-slate-400', label: role || 'Unknown' };
}

function Spinner() {
  return (
    <span
      aria-hidden="true"
      className="inline-block h-4 w-4 rounded-full border-2 border-slate-200 border-t-blue-600 animate-spin"
    />
  );
}

function StatusBanner({
  agent,
  stranded,
  canManage,
  onReprovision,
}: {
  agent: Agent;
  stranded: boolean;
  canManage: boolean;
  onReprovision: () => void;
}) {
  const status = agent.identity_status;

  if (status === 'none') {
    return (
      <div className={`${CARD} p-6`}>
        <h2 className="text-sm font-semibold text-slate-800">No Entra identity</h2>
        <p className="text-sm text-slate-500 mt-1">
          This agent has no Entra identity yet. MCP-server grants are available once the agent is
          provisioned with a Microsoft Entra identity.
        </p>
      </div>
    );
  }

  if (status === 'failed') {
    return (
      <div className={`${CARD} border-red-200/70 p-6`}>
        <h2 className="text-sm font-semibold text-red-700">Identity provisioning failed</h2>
        <p className="text-sm text-slate-600 mt-1">
          The Entra identity for this agent could not be provisioned. MCP-server grants are
          unavailable until provisioning succeeds.
        </p>
        {canManage && (
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

  return (
    <div className={`${CARD} p-6`}>
      <div className="flex items-center gap-2.5">
        <Spinner />
        <h2 className="text-sm font-semibold text-slate-800">Provisioning identity…</h2>
      </div>
      <p className="text-sm text-slate-500 mt-1">
        The agent's Microsoft Entra identity is being provisioned. MCP-server grants will be
        available once it completes.
      </p>
      {stranded && (
        <div className="mt-3">
          <p className="text-sm text-amber-700">
            This has been pending for a while. If provisioning stalled, you can re-provision to
            recover.
          </p>
          {canManage && (
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

function McpPicker({
  onPick,
  onClose,
  pending,
  excludeMcpIds,
  initialMcpId,
}: {
  onPick: (mcp: McpServer, role: 'Invoker' | 'Admin') => void;
  onClose: () => void;
  pending: boolean;
  excludeMcpIds: Set<string>;
  initialMcpId?: string | null;
}) {
  const [query, setQuery] = useState('');
  const [picked, setPicked] = useState<McpServer | null | undefined>(undefined);
  const [role, setRole] = useState<'Invoker' | 'Admin'>('Invoker');

  const inputRef = useRef<HTMLInputElement>(null);
  useEffect(() => {
    inputRef.current?.focus();
  }, []);

  const { state } = useLoad(
    () =>
      mcpServersApi
        .list()
        .then((all) => all.filter((m) => m.identity_status === 'provisioned' && !!m.entra_sp_id)),
    [],
    'Failed to load MCP servers.',
  );
  const servers = state.data ?? [];
  const initial = initialMcpId ? (servers.find((m) => m.id === initialMcpId) ?? null) : null;
  const selected = picked === undefined ? initial : picked;

  const q = query.trim().toLowerCase();
  const grantable = servers.filter((m) => !excludeMcpIds.has(m.id));
  const filtered = q ? grantable.filter((m) => m.name.toLowerCase().includes(q)) : grantable;

  return (
    <div className={`${CARD} p-4`} role="dialog" aria-label="Grant MCP access">
      <div className="flex items-center justify-between gap-3 mb-3">
        <h3 className="text-sm font-semibold text-slate-800">Grant MCP access</h3>
        <button
          type="button"
          onClick={onClose}
          aria-label="Close picker"
          className="shrink-0 inline-flex items-center justify-center h-6 w-6 rounded-md text-slate-400 hover:text-slate-700 hover:bg-slate-100 transition-colors"
        >
          <span aria-hidden="true" className="text-base leading-none">×</span>
        </button>
      </div>

      <label htmlFor="mcp-search" className="sr-only">
        Filter MCP servers
      </label>
      <input
        id="mcp-search"
        ref={inputRef}
        type="text"
        value={query}
        onChange={(e) => {
          setQuery(e.target.value);
          setPicked(null);
        }}
        placeholder="Filter MCP servers by name…"
        className="w-full px-3 py-2 text-sm rounded-lg border border-slate-300 focus:outline-none focus:ring-2 focus:ring-blue-500/40"
        autoComplete="off"
      />

      <div className="mt-3">
        {state.kind === 'error' && (
          <p className="text-sm text-red-600" role="alert">{state.message}</p>
        )}

        {state.kind === 'loading' && (
          <p className="text-sm text-slate-400">Loading MCP servers…</p>
        )}

        {state.kind === 'ready' && grantable.length === 0 && (
          <p className="text-sm text-slate-400">
            No provisioned MCP servers are available to grant.
          </p>
        )}
        {state.kind === 'ready' && grantable.length > 0 && filtered.length === 0 && (
          <p className="text-sm text-slate-400">No MCP servers match “{query.trim()}”.</p>
        )}

        {state.kind === 'ready' && filtered.length > 0 && (
          <ul className="max-h-64 overflow-y-auto -mx-1 divide-y divide-slate-100">
            {filtered.map((mcp) => {
              const isSelected = selected?.id === mcp.id;
              return (
                <li key={mcp.id}>
                  <button
                    type="button"
                    onClick={() => {
                      setPicked(mcp);
                      setRole('Invoker');
                    }}
                    aria-pressed={isSelected}
                    className={`w-full flex items-center gap-3 px-2 py-2 rounded-lg text-left transition-colors ${
                      isSelected ? 'bg-blue-50/70 ring-1 ring-inset ring-blue-200' : 'hover:bg-slate-50'
                    }`}
                  >
                    <AgentAvatar name={mcp.name} size="sm" />
                    <span className="min-w-0 flex-1">
                      <span className="block text-sm font-medium text-slate-800 truncate">
                        {mcp.name}
                      </span>
                      {mcp.endpoint_url && (
                        <span className="block text-xs text-slate-400 truncate">{mcp.endpoint_url}</span>
                      )}
                    </span>
                  </button>
                </li>
              );
            })}
          </ul>
        )}
      </div>

      {selected && (
        <div className="mt-3 pt-3 border-t border-slate-200/70">
          <div className="flex items-center gap-2 mb-3 text-sm text-slate-600">
            <span className="text-slate-400">Granting access to</span>
            <span className="font-medium text-slate-800 truncate">{selected.name}</span>
          </div>

          <div className="flex items-end gap-2 flex-wrap">
            <div>
              <label htmlFor="mcp-grant-role" className="block text-[11px] uppercase tracking-wide text-slate-400 font-medium mb-1">
                Role
              </label>
              <select
                id="mcp-grant-role"
                value={role}
                onChange={(e) => setRole(e.target.value as 'Invoker' | 'Admin')}
                className="px-3 py-1.5 text-sm rounded-lg border border-slate-300 bg-white focus:outline-none focus:ring-2 focus:ring-blue-500/40"
              >
                <option value="Invoker">Invoker</option>
                <option value="Admin">Admin</option>
              </select>
            </div>
            <button
              type="button"
              onClick={() => onPick(selected, role)}
              disabled={pending}
              className="px-3.5 py-1.5 rounded-lg bg-blue-600 text-white text-sm font-medium hover:bg-blue-700 transition-colors disabled:opacity-40"
            >
              {pending ? 'Granting…' : 'Grant access'}
            </button>
            <button
              type="button"
              onClick={() => setPicked(null)}
              className="px-3.5 py-1.5 rounded-lg bg-white border border-slate-300 text-slate-700 text-sm font-medium hover:bg-slate-50 transition-colors"
            >
              Back
            </button>
          </div>
        </div>
      )}
    </div>
  );
}

export default function AgentMcpServersTab({
  agent,
  canManage,
  onGrantsChanged,
}: {
  agent: Agent;
  canManage: boolean;
  onGrantsChanged?: () => void;
}) {
  const [reprovisioned, setReprovisioned] = useState<{ from: Agent; to: Agent } | null>(null);
  const agentIdentity = reprovisioned !== null && reprovisioned.from === agent ? reprovisioned.to : agent;

  const [actionPending, setActionPending] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);

  const [picking, setPicking] = useState(false);
  const [pickInitialId, setPickInitialId] = useState<string | null>(null);

  const addTriggerRef = useRef<HTMLButtonElement>(null);
  const returnFocus = useReturnFocus(addTriggerRef);

  const closePicker = useCallback(() => {
    setPicking(false);
    setPickInitialId(null);
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

  const provisioned = agentIdentity.identity_status === 'provisioned';
  const agentId = agentIdentity.id;

  const mountedRef = useRef(true);
  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
    };
  }, []);

  const [strandedAt, setStrandedAt] = useState<string | null>(null);
  useEffect(() => {
    if (agentIdentity.identity_status !== 'pending') return;
    const updatedAt = agentIdentity.updated_at;
    const elapsed = Date.now() - new Date(updatedAt).getTime();
    if (Number.isNaN(elapsed)) return;
    const handle = window.setTimeout(() => {
      if (mountedRef.current) setStrandedAt(updatedAt);
    }, Math.max(0, STALE_PENDING_MS - elapsed));
    return () => window.clearTimeout(handle);
  }, [agentIdentity.identity_status, agentIdentity.updated_at]);
  const stranded =
    agentIdentity.identity_status === 'pending' && strandedAt === agentIdentity.updated_at;

  const { state: grantsState, patch: patchGrants } = useLoad(
    () => (provisioned ? agentMcpGrantsApi.list(agentId) : Promise.resolve<AgentMcpGrant[]>([])),
    [agentId, provisioned],
    'Failed to load MCP grants.',
  );
  const grants = grantsState.data ?? [];

  const reconcile = async () => {
    const fresh = await agentMcpGrantsApi.list(agentId).catch(() => null);
    if (fresh && mountedRef.current) patchGrants(() => fresh);
  };

  const declared = agentIdentity.declared_mcp_server_ids;
  const needsNames = !!declared?.some((id) => !grants.some((g) => g.mcp_id === id));
  const registryLoad = useLoad(
    () => (provisioned && needsNames ? mcpServersApi.list() : Promise.resolve<McpServer[] | null>(null)),
    [provisioned, needsNames],
  );
  const registry = registryLoad.state.data ?? null;

  const openGrantFor = (mcpId: string) => {
    setPickInitialId(mcpId);
    setPicking(true);
    setActionError(null);
  };

  const handleReprovision = async () => {
    setActionPending(true);
    setActionError(null);
    try {
      await client.post(`/api/v1/agents/${agentId}/reprovision`);
      if (mountedRef.current) {
        setReprovisioned({
          from: agent,
          to: { ...agentIdentity, identity_status: 'pending', updated_at: new Date().toISOString() },
        });
      }
    } catch (err: unknown) {
      if (mountedRef.current) {
        setActionError(err instanceof Error ? err.message : 'Re-provision failed.');
      }
    } finally {
      if (mountedRef.current) setActionPending(false);
    }
  };

  const handlePick = async (mcp: McpServer, role: 'Invoker' | 'Admin') => {
    if (actionPending) return;
    const principalId = agentIdentity.entra_sp_id;
    if (!principalId) {
      setActionError('This agent has no service-principal id to grant.');
      return;
    }
    setActionPending(true);
    setActionError(null);
    try {
      const created = await mcpGrantsApi.add(mcp.id, {
        principal_id: principalId,
        principal_type: 'agent',
        role,
      });
      onGrantsChanged?.();
      if (!mountedRef.current) return;
      const optimistic: AgentMcpGrant = {
        mcp_id: mcp.id,
        mcp_name: mcp.name,
        role: created.role,
        assignment_id: created.assignment_id,
      };
      patchGrants((prev) => {
        if (prev.some((g) => g.assignment_id === optimistic.assignment_id)) return prev;
        return [...prev, optimistic];
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

  const handleRemove = async (grant: AgentMcpGrant) => {
    setActionPending(true);
    setActionError(null);
    const prevGrants = grants;
    patchGrants((prev) => prev.filter((g) => g.assignment_id !== grant.assignment_id));
    try {
      await mcpGrantsApi.remove(grant.mcp_id, grant.assignment_id);
      onGrantsChanged?.();
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
        agent={agentIdentity}
        stranded={stranded}
        canManage={canManage}
        onReprovision={handleReprovision}
      />
    );
  }

  const grantedMcpIds = new Set(grants.map((g) => g.mcp_id));

  const drift = computeDrift(declared, grants.map((g) => g.mcp_id));
  const driftState = new Map<string, DriftState>(drift.map((d) => [d.id, d.state]));
  const notGranted = drift.filter((d) => d.state === 'not_granted').map((d) => d.id);
  const grantableDeclared = notGranted.filter((id) => declaredServerStatus(id, registry).status === 'grantable');
  const soleDeclaredId = grantableDeclared.length === 1 ? grantableDeclared[0] : null;

  const deliveryNote = mcpDeliveryNote(agentIdentity.platform);

  return (
    <div className="space-y-4">
      {deliveryNote && (
        <div
          role="note"
          className="rounded-xl border border-amber-200/70 bg-amber-50/70 px-5 py-3 text-sm text-amber-800"
        >
          {deliveryNote}
        </div>
      )}

      <div className={`${CARD} overflow-hidden`}>
        <div className="flex items-center justify-between gap-3 px-5 py-4 border-b border-slate-200/60">
          <div>
            <h2 className="text-sm font-semibold text-slate-800">MCP servers</h2>
            <p className="text-xs text-slate-400 mt-0.5">
              Servers this agent is granted (Entra app-role assignments) and servers its code declares it uses.
            </p>
          </div>
          {canManage && !picking && (
            <button
              ref={addTriggerRef}
              type="button"
              onClick={() => {
                setPickInitialId(soleDeclaredId);
                setPicking(true);
                setActionError(null);
              }}
              className="shrink-0 px-3.5 py-1.5 rounded-lg bg-blue-600 text-white text-sm font-medium hover:bg-blue-700 transition-colors"
            >
              + Grant MCP access
            </button>
          )}
        </div>

        {picking && (
          <div className="px-5 py-4 border-b border-slate-200/60 bg-slate-50/50">
            <McpPicker
              key={pickInitialId ?? 'all'}
              initialMcpId={pickInitialId}
              onPick={handlePick}
              onClose={closePicker}
              pending={actionPending}
              excludeMcpIds={grantedMcpIds}
            />
          </div>
        )}

        {actionError && (
          <p className="px-5 pt-3 text-sm text-red-600" role="alert">{actionError}</p>
        )}

        {grantsState.kind === 'loading' ? (
          <div className="px-5 py-8 text-center">
            <p className="text-sm text-slate-400">Loading MCP servers…</p>
          </div>
        ) : grantsState.kind === 'error' ? (
          <div className="px-5 py-8 text-center">
            <p className="text-sm text-red-600" role="alert">{grantsState.message}</p>
          </div>
        ) : grants.length === 0 && notGranted.length === 0 ? (
          <div className="px-5 py-10 text-center">
            <p className="text-sm font-medium text-slate-600">
              This agent has not been granted access to any MCP servers yet.
            </p>
            <p className="text-xs text-slate-400 mt-1">
              {canManage
                ? 'Use “Grant MCP access” to let this agent call an MCP server.'
                : 'An operator can grant this agent access to MCP servers.'}
            </p>
          </div>
        ) : (
          <ul className="divide-y divide-slate-100">
            {grants.map((grant) => {
              const rBadge = roleBadge(grant.role);
              const unused = driftState.get(grant.mcp_id) === 'unused_grant';
              return (
                <li
                  key={grant.assignment_id}
                  className={`flex items-center gap-3 px-5 py-3 ${unused ? 'bg-slate-50/70' : ''}`}
                >
                  <AgentAvatar name={grant.mcp_name} size="sm" />
                  <div className="min-w-0 flex-1">
                    <div className="flex items-center gap-2 min-w-0">
                      <Link
                        to={`/mcp-servers/${grant.mcp_id}`}
                        className="text-sm font-medium text-blue-700 hover:underline truncate"
                      >
                        {grant.mcp_name}
                      </Link>
                    </div>
                    {unused && (
                      <p className="text-xs text-slate-500 mt-0.5">
                        Granted but not used by the agent. Consider revoking.
                      </p>
                    )}
                  </div>

                  <span className={`shrink-0 inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium ${rBadge.cls}`}>
                    {rBadge.label}
                  </span>

                  {canManage && (
                    <button
                      type="button"
                      onClick={() => handleRemove(grant)}
                      disabled={actionPending}
                      aria-label={`Remove ${grant.mcp_name} access`}
                      className="shrink-0 inline-flex items-center justify-center h-7 w-7 rounded-md text-slate-400 hover:text-red-600 hover:bg-red-50 transition-colors disabled:opacity-40"
                    >
                      <span aria-hidden="true" className="text-base leading-none">×</span>
                    </button>
                  )}
                </li>
              );
            })}

            {notGranted.map((mcpId) => {
              const { name, status } = declaredServerStatus(mcpId, registry);
              return (
                <li key={`declared-${mcpId}`} className="flex items-center gap-3 px-5 py-3 bg-amber-50/60">
                  <AgentAvatar name={name} size="sm" />
                  <div className="min-w-0 flex-1">
                    {status === 'missing' ? (
                      <span className="text-sm font-medium text-slate-700 truncate">
                        {name} <span className="font-normal text-slate-400">not found in the registry</span>
                      </span>
                    ) : (
                      <Link
                        to={`/mcp-servers/${mcpId}`}
                        className="text-sm font-medium text-blue-700 hover:underline truncate"
                      >
                        {name}
                      </Link>
                    )}
                    <p className="text-xs text-amber-700 mt-0.5">
                      This agent will try to use {name}, but it isn't granted.
                    </p>
                    {status === 'unprovisioned' && (
                      <p className="text-xs text-slate-500 mt-0.5">
                        {name} isn't provisioned yet, so it can't be granted.
                      </p>
                    )}
                  </div>
                  <span className="shrink-0 inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium bg-amber-100 text-amber-800">
                    Not granted
                  </span>
                  {canManage && (
                    <button
                      type="button"
                      onClick={() => openGrantFor(mcpId)}
                      disabled={actionPending || status === 'unprovisioned' || status === 'missing'}
                      aria-label={`Grant ${name} access`}
                      className="shrink-0 px-3 py-1 rounded-lg bg-white border border-amber-300 text-amber-800 text-sm font-medium hover:bg-amber-50 transition-colors disabled:opacity-40"
                    >
                      Grant
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
