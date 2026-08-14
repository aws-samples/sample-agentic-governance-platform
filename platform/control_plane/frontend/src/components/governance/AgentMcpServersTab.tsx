// AgentMcpServersTab — the agent detail "MCP Servers" tab (Epic 7, T-FE-AGENT-MCP).
//
// The agent-side mirror of the MCP detail "Connected Agents" tab
// (McpConnectedAgentsTab). Where that tab answers "which agents can reach THIS
// MCP server", this one answers the inverse — "which MCP servers can THIS agent
// reach" — so the two-direction agent↔MCP grant story reads as one design: same
// glass card chrome, the same identity-status banners, the same optimistic
// add/remove with a reconciling refetch to absorb Microsoft Graph's eventual
// consistency.
//
// The read is the agent-direction reverse read (agentMcpGrantsApi.list →
// GET /agents/{id}/mcp-grants, T-ROUTES' agent_mcp_router). Each row's
// app-role assignment physically lives on the MCP's service principal, so a
// REVOKE goes back through the MCP-side route (mcpGrantsApi.remove(mcp_id,
// assignment_id)) — there is no agent-side delete. A GRANT likewise targets the
// MCP SP (mcpGrantsApi.add(mcp_id, { principal_id: agent.entra_sp_id, … })),
// with the agent's SP object id as the principal.
//
// Cloned structure-for-structure from McpConnectedAgentsTab; the swaps are:
// resource = the agent (so the banner reflects the AGENT's identity_status), the
// list is agentMcpGrantsApi (returning MCP rows, not principal rows), and the
// "+ Grant" affordance opens an inline MCP picker (provisioned MCP servers +
// role) rather than the principal picker.

import { useCallback, useEffect, useRef, useState } from 'react';
import { Link } from 'react-router-dom';
import client, { agentMcpGrantsApi, mcpGrantsApi, mcpServersApi } from '../../api/client';
import type { Agent, AgentMcpGrant, McpServer } from '../../api/client';
import { AgentAvatar } from './agentUi';
import { mcpDeliveryNote } from './platformLabels';

// Card chrome — identical to AgentDetail's CARD so the tab body sits in the same
// surface family as Overview/Access. (Same constant McpConnectedAgentsTab uses.)
const CARD = 'bg-white/70 backdrop-blur rounded-xl border border-slate-200/60 shadow-sm';

// A pending identity older than this is treated as stranded (a mid-provision
// crash with no other recovery path), unlocking the Re-provision button.
// Mirrors McpConnectedAgentsTab / AccessTab's STALE_PENDING_MS exactly.
const STALE_PENDING_MS = 2.5 * 60 * 1000;

// Graph app-role assignments are eventually consistent: a just-written grant may
// not show on an immediate re-list. After a mutate we wait this long, then
// refetch to reconcile the optimistic state with Graph's truth. Mirrors
// McpConnectedAgentsTab's REPLICATION_RECONCILE_MS.
const REPLICATION_RECONCILE_MS = 1200;

// Invoker=slate (the common case), Admin=amber (elevated — reads as the
// noteworthy one without alarming like red). Unknown=slate-light: the
// agent-direction reverse read maps a foreign/legacy appRoleId to "Unknown"
// (tolerant — see the backend), so the agent side can surface it where the
// MCP side never would; render it low-key rather than dropping the row.
function roleBadge(role: string): { cls: string; label: string } {
  if (role === 'Admin') return { cls: 'bg-amber-50 text-amber-700', label: 'Admin' };
  if (role === 'Invoker') return { cls: 'bg-slate-100 text-slate-600', label: 'Invoker' };
  return { cls: 'bg-slate-50 text-slate-400', label: role || 'Unknown' };
}

// Whether a `pending` identity has been pending long enough to look stranded.
function isPendingStranded(updatedAt: string): boolean {
  const t = new Date(updatedAt).getTime();
  if (Number.isNaN(t)) return false; // unparseable → don't offer recovery yet
  return Date.now() - t > STALE_PENDING_MS;
}

// A small CSS-only spinner (no icon library) — slate ring with a blue arc.
// Copied verbatim from McpConnectedAgentsTab's Spinner.
function Spinner() {
  return (
    <span
      aria-hidden="true"
      className="inline-block h-4 w-4 rounded-full border-2 border-slate-200 border-t-blue-600 animate-spin"
    />
  );
}

// ---------------------------------------------------------------------------
// Status banner (shown when the AGENT's identity_status !== 'provisioned')
// ---------------------------------------------------------------------------

function StatusBanner({
  agent,
  canManage,
  onReprovision,
}: {
  agent: Agent;
  canManage: boolean;
  onReprovision: () => void;
}) {
  const status = agent.identity_status;

  // none → metadata-only agent; the grants UI is hidden entirely (no identity to
  // assign roles against).
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

  // failed → red banner + Re-provision (Operator+).
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

  // pending → spinner + "provisioning…"; if it looks stranded, also offer
  // Re-provision (the only recovery path for a mid-provision crash).
  const stranded = isPendingStranded(agent.updated_at);
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

// ---------------------------------------------------------------------------
// McpPicker — inline MCP-server picker (the inverse of PrincipalPicker)
// ---------------------------------------------------------------------------
//
// Lists provisioned MCP servers (the only grant-capable ones) and reveals an
// Invoker/Admin role select on selection before confirming. A focused inline
// picker rather than another PrincipalPicker mode: the entity here is an MCP
// SERVER (not a principal), so keeping it separate stops PrincipalPicker from
// accreting modes. The dialog chrome + a11y (role="dialog", aria-label, a search
// box, a single blue confirm, a ✕ close) mirror PrincipalPicker so the two
// pickers read as siblings.

function McpPicker({
  onPick,
  onClose,
  pending,
  // MCP ids already granted — filtered out of the pick list so an operator can't
  // re-pick a server the agent already reaches (the grant would 409).
  excludeMcpIds,
}: {
  onPick: (mcp: McpServer, role: 'Invoker' | 'Admin') => void;
  onClose: () => void;
  pending: boolean;
  excludeMcpIds: Set<string>;
}) {
  const [query, setQuery] = useState('');
  const [servers, setServers] = useState<McpServer[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const [selected, setSelected] = useState<McpServer | null>(null);
  const [role, setRole] = useState<'Invoker' | 'Admin'>('Invoker');

  const inputRef = useRef<HTMLInputElement>(null);
  useEffect(() => {
    inputRef.current?.focus();
  }, []);

  // MCP servers are a small, bounded set (the catalog), so — like the agent
  // picker — fetch the whole provisioned list once and filter client-side; no
  // debounce, no per-keystroke fetch.
  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError(null);
    mcpServersApi
      .list()
      .then((all) => {
        if (cancelled) return;
        setServers(all.filter((m) => m.identity_status === 'provisioned' && !!m.entra_sp_id));
      })
      .catch((err: unknown) => {
        if (cancelled) return;
        setError(err instanceof Error ? err.message : 'Failed to load MCP servers.');
        setServers([]);
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  // Provisioned servers not already granted, filtered by the query (name).
  const q = query.trim().toLowerCase();
  const grantable = servers.filter((m) => !excludeMcpIds.has(m.id));
  const filtered = q ? grantable.filter((m) => m.name.toLowerCase().includes(q)) : grantable;

  return (
    <div className={`${CARD} p-4`} role="dialog" aria-label="Grant MCP access">
      {/* Header: title + close. */}
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

      {/* Filter box. */}
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
          // Re-opening the search clears any prior pending selection.
          setSelected(null);
        }}
        placeholder="Filter MCP servers by name…"
        className="w-full px-3 py-2 text-sm rounded-lg border border-slate-300 focus:outline-none focus:ring-2 focus:ring-blue-500/40"
        autoComplete="off"
      />

      {/* Results region. */}
      <div className="mt-3">
        {error && (
          <p className="text-sm text-red-600" role="alert">{error}</p>
        )}

        {!error && loading && (
          <p className="text-sm text-slate-400">Loading MCP servers…</p>
        )}

        {!error && !loading && grantable.length === 0 && (
          <p className="text-sm text-slate-400">
            No provisioned MCP servers are available to grant.
          </p>
        )}
        {!error && !loading && grantable.length > 0 && filtered.length === 0 && (
          <p className="text-sm text-slate-400">No MCP servers match “{query.trim()}”.</p>
        )}

        {!error && !loading && filtered.length > 0 && (
          <ul className="max-h-64 overflow-y-auto -mx-1 divide-y divide-slate-100">
            {filtered.map((mcp) => {
              const isSelected = selected?.id === mcp.id;
              return (
                <li key={mcp.id}>
                  <button
                    type="button"
                    onClick={() => {
                      setSelected(mcp);
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

      {/* Role select + confirm — revealed once an MCP is chosen. */}
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
              onClick={() => setSelected(null)}
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

// ---------------------------------------------------------------------------
// AgentMcpServersTab
// ---------------------------------------------------------------------------

export default function AgentMcpServersTab({
  agent,
  canManage,
}: {
  agent: Agent;
  canManage: boolean;
}) {
  // The dispatch contract passes only { agent, canManage } (no parent refresh
  // callback). So — like McpConnectedAgentsTab — this tab owns a local copy of
  // the agent's identity fields, seeded from the prop and refreshed in place
  // after a re-provision, so the banner's identity_status / updated_at reflect
  // the now-pending re-config without a page reload.
  const [agentIdentity, setAgentIdentity] = useState<Agent>(agent);
  useEffect(() => {
    setAgentIdentity(agent);
  }, [agent]);

  const [grants, setGrants] = useState<AgentMcpGrant[]>([]);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState<string | null>(null);

  // Mutation (add/remove/reprovision) UI state — mirrors McpConnectedAgentsTab.
  const [actionPending, setActionPending] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);

  const [picking, setPicking] = useState(false);

  // Ref on the "+ Grant MCP access" trigger so focus returns there when the
  // picker closes (both via ✕ and after a successful grant). Mirrors the idiom.
  const addTriggerRef = useRef<HTMLButtonElement>(null);

  const closePicker = useCallback(() => {
    setPicking(false);
    setActionError(null);
    window.setTimeout(() => addTriggerRef.current?.focus(), 0);
  }, []);

  // Close picker on Escape while it is open.
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

  // Guards against setState after unmount (the reconcile refetch fires on a
  // timer, so it can land after the user navigates away).
  const mountedRef = useRef(true);
  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
    };
  }, []);

  // Stranded-pending timer (mirrors McpConnectedAgentsTab): isPendingStranded()
  // only re-evaluates on render, so if the tab is already open when the ~2.5-min
  // threshold passes the Re-provision affordance would never appear without a
  // user-triggered re-render. Schedule a one-shot timeout for exactly when the
  // threshold crosses and bump a nonce to force one re-render at that moment.
  const [, setStaleNonce] = useState(0);
  useEffect(() => {
    if (agentIdentity.identity_status !== 'pending') return;
    const elapsed = Date.now() - new Date(agentIdentity.updated_at).getTime();
    if (Number.isNaN(elapsed) || elapsed >= STALE_PENDING_MS) return; // already stranded or unparseable
    const remaining = STALE_PENDING_MS - elapsed;
    const handle = window.setTimeout(() => {
      if (mountedRef.current) setStaleNonce((n) => n + 1);
    }, remaining);
    return () => window.clearTimeout(handle);
  }, [agentIdentity.identity_status, agentIdentity.updated_at]);

  // Silent reconcile fetch — used after mutate (to absorb Graph replication
  // lag); not surfaced as the blocking load spinner. Mirrors McpConnectedAgentsTab.
  const reconcile = useCallback(async () => {
    try {
      const fresh = await agentMcpGrantsApi.list(agentId);
      if (mountedRef.current) setGrants(fresh);
    } catch {
      // A failed reconcile leaves the optimistic state in place — the next
      // explicit action or remount will resync. Don't clobber the UI with a
      // background-refetch error.
    }
  }, [agentId]);

  // Initial load (and on agent/provisioned change). Skipped when unprovisioned —
  // there's nothing to list until an identity exists.
  useEffect(() => {
    if (!provisioned) {
      setLoading(false);
      return;
    }
    let cancelled = false;
    setLoading(true);
    setLoadError(null);
    agentMcpGrantsApi
      .list(agentId)
      .then((res) => {
        if (!cancelled) setGrants(res);
      })
      .catch((err: unknown) => {
        if (!cancelled) setLoadError(err instanceof Error ? err.message : 'Failed to load MCP grants.');
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [agentId, provisioned]);

  // -- mutations ------------------------------------------------------------

  // Re-provision the AGENT's Entra identity (Epic 6, T-ROUTES owns the route).
  // Mirrors AgentDetail.handleReprovision / McpConnectedAgentsTab: call POST
  // /reprovision via the shared axios instance (the response interceptor unwraps
  // the backend `detail` into err.message), then update the local identity so the
  // banner's identity_status flips to pending in place. The agents reprovision
  // route returns 202/no body, so re-mark pending locally rather than trusting a
  // body shape.
  const handleReprovision = async () => {
    setActionPending(true);
    setActionError(null);
    try {
      await client.post(`/api/v1/agents/${agentId}/reprovision`);
      if (mountedRef.current) {
        setAgentIdentity((prev) => ({
          ...prev,
          identity_status: 'pending',
          updated_at: new Date().toISOString(),
        }));
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
    // Belt-and-suspenders re-entry guard (the picker button is also disabled via
    // the `pending` prop). Mirrors McpConnectedAgentsTab.
    if (actionPending) return;
    // The agent must have a service-principal id to be the grant principal — it
    // does whenever provisioned (this render path is gated on provisioned), but
    // guard so a malformed grant can never be issued.
    const principalId = agentIdentity.entra_sp_id;
    if (!principalId) {
      setActionError('This agent has no service-principal id to grant.');
      return;
    }
    setActionPending(true);
    setActionError(null);
    try {
      // The grant targets the MCP SP (assignment lives there); the agent SP is
      // the principal. The add returns the MCP-side Grant shape (assignment_id +
      // role), NOT an AgentMcpGrant — so synthesize the reverse-read row from the
      // picked MCP + the returned assignment_id, then reconcile against the
      // agent-direction list (the source of truth).
      const created = await mcpGrantsApi.add(mcp.id, {
        principal_id: principalId,
        principal_type: 'agent',
        role,
      });
      if (!mountedRef.current) return;
      const optimistic: AgentMcpGrant = {
        mcp_id: mcp.id,
        mcp_name: mcp.name,
        role: created.role,
        assignment_id: created.assignment_id,
      };
      setGrants((prev) => {
        // De-dupe in case a prior reconcile already pulled it in.
        if (prev.some((g) => g.assignment_id === optimistic.assignment_id)) return prev;
        return [...prev, optimistic];
      });
      // closePicker focuses the trigger and clears actionError, so the user lands
      // back at the "+ Grant MCP access" button with the new row visible.
      closePicker();
      // Graph is eventually consistent — reconcile shortly after so the row set
      // matches the source of truth (and de-dupes any lag artifacts).
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
    // Optimistically drop the row; restore it if the call fails.
    const prevGrants = grants;
    setGrants((prev) => prev.filter((g) => g.assignment_id !== grant.assignment_id));
    try {
      // The assignment lives on the MCP's SP, so revoke via the MCP-side route
      // (there is no agent-side delete).
      await mcpGrantsApi.remove(grant.mcp_id, grant.assignment_id);
      // Reconcile after replication settles (a removed assignment can briefly
      // still appear on an immediate re-list).
      window.setTimeout(() => {
        void reconcile();
      }, REPLICATION_RECONCILE_MS);
    } catch (err: unknown) {
      if (mountedRef.current) {
        setGrants(prevGrants); // rollback
        setActionError(err instanceof Error ? err.message : 'Failed to remove access.');
      }
    } finally {
      if (mountedRef.current) setActionPending(false);
    }
  };

  // -- render ---------------------------------------------------------------

  // Unprovisioned → banner instead of the grants UI.
  if (!provisioned) {
    return <StatusBanner agent={agentIdentity} canManage={canManage} onReprovision={handleReprovision} />;
  }

  const grantedMcpIds = new Set(grants.map((g) => g.mcp_id));

  // The C-6 "recorded, not delivered" caveat, or null when grants really are delivered to the
  // runtime (E29/T11). Decided in `platformLabels` — this file neither tests the platform nor owns
  // the sentence, which is the point: C-6 pins the copy VERBATIM and a second literal here is how
  // a pinned contract drifts.
  const deliveryNote = mcpDeliveryNote(agentIdentity.platform);

  return (
    <div className="space-y-4">
      {/* THE DELIVERY CAVEAT SITS ABOVE THE CARD, not inside the table, because it qualifies every
          row AND the act of adding one — an operator must read it before granting, not after
          scanning a list. Rendered as an amber note rather than a red error: nothing is broken and
          the grant is not refused. The assignment is a real, auditable, revocable Entra record;
          what it is not is configuration the app will ever be told about by AGP.

          THE RECORDING UI STAYS FULLY ENABLED, deliberately — see `mcpDeliveryNote`'s contract.
          Disabling the grant button would be tidier-looking and wrong twice: the assignment has
          independent governance value, and a disabled control teaches "not supported" where the
          truth is "recorded here, wired up by hand over there".

          `role="note"` rather than `role="alert"`: it is standing context about the platform, not
          an event, so it must not interrupt a screen reader mid-task on every tab visit. */}
      {deliveryNote && (
        <div
          role="note"
          className="rounded-xl border border-amber-200/70 bg-amber-50/70 px-5 py-3 text-sm text-amber-800"
        >
          {deliveryNote}
        </div>
      )}

      <div className={`${CARD} overflow-hidden`}>
        {/* Card header: title + Grant. */}
        <div className="flex items-center justify-between gap-3 px-5 py-4 border-b border-slate-200/60">
          <div>
            <h2 className="text-sm font-semibold text-slate-800">MCP servers this agent can reach</h2>
            <p className="text-xs text-slate-400 mt-0.5">
              Direct Entra app-role assignments granting this agent access to MCP servers.
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
              + Grant MCP access
            </button>
          )}
        </div>

        {/* Picker (inline, above the table). */}
        {picking && (
          <div className="px-5 py-4 border-b border-slate-200/60 bg-slate-50/50">
            <McpPicker
              onPick={handlePick}
              onClose={closePicker}
              pending={actionPending}
              excludeMcpIds={grantedMcpIds}
            />
          </div>
        )}

        {/* Mutation error (e.g. 409 already-assigned) — non-blocking. */}
        {actionError && (
          <p className="px-5 pt-3 text-sm text-red-600" role="alert">{actionError}</p>
        )}

        {/* Body: loading / error / empty / table. */}
        {loading ? (
          <div className="px-5 py-8 text-center">
            <p className="text-sm text-slate-400">Loading MCP servers…</p>
          </div>
        ) : loadError ? (
          <div className="px-5 py-8 text-center">
            <p className="text-sm text-red-600" role="alert">{loadError}</p>
          </div>
        ) : grants.length === 0 ? (
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
              return (
                <li
                  key={grant.assignment_id}
                  className="flex items-center gap-3 px-5 py-3"
                >
                  <AgentAvatar name={grant.mcp_name} size="sm" />
                  <div className="min-w-0 flex-1">
                    <div className="flex items-center gap-2 min-w-0">
                      {/* MCP name → MCP detail page (the app's table→detail link idiom). */}
                      <Link
                        to={`/mcp-servers/${grant.mcp_id}`}
                        className="text-sm font-medium text-blue-700 hover:underline truncate"
                      >
                        {grant.mcp_name}
                      </Link>
                    </div>
                  </div>

                  {/* Role pill (Invoker / Admin / Unknown). */}
                  <span className={`shrink-0 inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium ${rBadge.cls}`}>
                    {rBadge.label}
                  </span>

                  {/* Remove ✕ (Operator+). */}
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
          </ul>
        )}
      </div>
    </div>
  );
}
