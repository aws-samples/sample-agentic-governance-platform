// McpConnectedAgentsTab — the MCP detail "Connected Agents" tab (Epic 7, T-FE-MCP-ACCESS).
//
// The MCP-side mirror of E6's AccessTab: a dense, scannable table of the
// principals granted access to this MCP server — except here the principals are
// AGENTS (service principals from our registry) and the resource is the MCP.
// This is the visual proof that "agent→MCP is the same grant as user→agent":
// the same live Entra app-role-assignment plumbing (read straight from Graph via
// mcpGrantsApi, no DynamoDB), the same optimistic-add/remove with a reconciling
// refetch to absorb Graph's eventual consistency, and the same unprovisioned
// status banners.
//
// Cloned structure-for-structure from AccessTab so the two tabs stay visually
// and behaviourally identical; the swaps are: grantsApi→mcpGrantsApi, the
// resource id is mcp.id, the picker opens in agent mode, and the grant POST
// carries principal_type:'agent' (Graph returns the assignment as a
// 'ServicePrincipal' on the read side).

import { useCallback, useEffect, useRef, useState } from 'react';
import client, { mcpGrantsApi } from '../../api/client';
import type { Grant, McpServer, PrincipalHit } from '../../api/client';
import { AgentAvatar } from './agentUi';
import PrincipalPicker from './PrincipalPicker';

// Card chrome — identical to McpServerDetail's CARD so the tab body sits in the
// same surface family as Overview/Tools.
const CARD = 'bg-white/70 backdrop-blur rounded-xl border border-slate-200/60 shadow-sm';

// A pending identity older than this is treated as stranded (a mid-provision
// crash with no other recovery path), unlocking the Re-provision button.
// Mirrors AccessTab's STALE_PENDING_MS exactly.
const STALE_PENDING_MS = 2.5 * 60 * 1000;

// Graph app-role assignments are eventually consistent: a just-written grant
// may not show on an immediate re-list. After a mutate we wait this long, then
// refetch to reconcile the optimistic state with Graph's truth. Mirrors
// AccessTab's REPLICATION_RECONCILE_MS.
const REPLICATION_RECONCILE_MS = 1200;

// principalType is capitalized in the READ shape ('ServicePrincipal' for an
// agent). Every grant in this tab is an agent (indigo — same tint as the
// 'Agent' pill in AccessTab's principalTypeBadge + the picker).
function agentTypeBadge(): { cls: string; label: string } {
  return { cls: 'bg-indigo-50 text-indigo-700', label: 'Agent' };
}

// Invoker=slate (the common case), Admin=amber (elevated — reads as the
// noteworthy one without alarming like red). Copied verbatim from AccessTab.
function roleBadge(role: 'Invoker' | 'Admin'): { cls: string; label: string } {
  return role === 'Admin'
    ? { cls: 'bg-amber-50 text-amber-700', label: 'Admin' }
    : { cls: 'bg-slate-100 text-slate-600', label: 'Invoker' };
}

// Whether a `pending` identity has been pending long enough to look stranded.
function isPendingStranded(updatedAt: string): boolean {
  const t = new Date(updatedAt).getTime();
  if (Number.isNaN(t)) return false; // unparseable → don't offer recovery yet
  return Date.now() - t > STALE_PENDING_MS;
}

// A small CSS-only spinner (no icon library) — slate ring with a blue arc.
// Copied verbatim from AccessTab's Spinner.
function Spinner() {
  return (
    <span
      aria-hidden="true"
      className="inline-block h-4 w-4 rounded-full border-2 border-slate-200 border-t-blue-600 animate-spin"
    />
  );
}

// ---------------------------------------------------------------------------
// Status banner (shown when identity_status !== 'provisioned')
// ---------------------------------------------------------------------------

function StatusBanner({
  mcp,
  canManage,
  onReprovision,
}: {
  mcp: McpServer;
  canManage: boolean;
  onReprovision: () => void;
}) {
  const status = mcp.identity_status;

  // none → metadata-only MCP server; the grants UI is hidden entirely (no
  // identity to assign roles against). A standard MCP server with no provisioned
  // identity lands here too.
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

  // failed → red banner + Re-provision (Operator+).
  if (status === 'failed') {
    return (
      <div className={`${CARD} border-red-200/70 p-6`}>
        <h2 className="text-sm font-semibold text-red-700">Identity provisioning failed</h2>
        <p className="text-sm text-slate-600 mt-1">
          The Entra identity for this MCP server could not be provisioned. Connected-agent grants
          are unavailable until provisioning succeeds.
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
  const stranded = isPendingStranded(mcp.updated_at);
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
// McpConnectedAgentsTab
// ---------------------------------------------------------------------------

export default function McpConnectedAgentsTab({
  mcp,
  canManage,
}: {
  mcp: McpServer;
  canManage: boolean;
}) {
  // The dispatch contract passes only { mcp, canManage } (no parent refresh
  // callback, unlike E6's AccessTab which receives onReprovision). So this tab
  // owns a local copy of the server's identity fields, seeded from the prop and
  // refreshed in place after a re-provision, so the banner's identity_status /
  // updated_at reflect the now-pending re-config without a page reload.
  const [serverIdentity, setServerIdentity] = useState<McpServer>(mcp);
  // Resync the local copy whenever the parent prop changes (e.g. a lifecycle
  // action upstream re-fetched the server).
  useEffect(() => {
    setServerIdentity(mcp);
  }, [mcp]);

  const [grants, setGrants] = useState<Grant[]>([]);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState<string | null>(null);

  // Mutation (add/remove/reprovision) UI state — mirrors AccessTab's
  // actionPending / actionError idiom so the two tabs behave identically.
  const [actionPending, setActionPending] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);

  const [picking, setPicking] = useState(false);

  // Ref on the "+ Grant agent access" trigger so focus returns there when the
  // picker closes (both via the ✕ close and after a successful grant),
  // preventing focus from dropping to document start. Mirrors AccessTab.
  const addTriggerRef = useRef<HTMLButtonElement>(null);

  const closePicker = useCallback(() => {
    setPicking(false);
    setActionError(null);
    // Return focus to the trigger that opened the picker.
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

  const provisioned = serverIdentity.identity_status === 'provisioned';
  const mcpId = serverIdentity.id;

  // Guards against setState after unmount (the reconcile refetch fires on a
  // timer, so it can land after the user navigates away).
  const mountedRef = useRef(true);
  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
    };
  }, []);

  // Stranded-pending timer (mirrors AccessTab FIX 3): isPendingStranded() only
  // re-evaluates on render, so if the tab is already open when the ~2.5-min
  // threshold passes the Re-provision affordance would never appear without a
  // user-triggered re-render. Schedule a one-shot timeout for exactly when the
  // threshold crosses and bump a nonce to force one re-render at that moment.
  const [, setStaleNonce] = useState(0);
  useEffect(() => {
    if (serverIdentity.identity_status !== 'pending') return;
    const elapsed = Date.now() - new Date(serverIdentity.updated_at).getTime();
    if (Number.isNaN(elapsed) || elapsed >= STALE_PENDING_MS) return; // already stranded or unparseable
    const remaining = STALE_PENDING_MS - elapsed;
    const handle = window.setTimeout(() => {
      if (mountedRef.current) setStaleNonce((n) => n + 1);
    }, remaining);
    return () => window.clearTimeout(handle);
  }, [serverIdentity.identity_status, serverIdentity.updated_at]);

  // Silent reconcile fetch — used after mutate (to absorb Graph replication
  // lag); not surfaced as the blocking load spinner. Mirrors AccessTab.
  const reconcile = useCallback(async () => {
    try {
      const fresh = await mcpGrantsApi.list(mcpId);
      if (mountedRef.current) setGrants(fresh);
    } catch {
      // A failed reconcile leaves the optimistic state in place — the next
      // explicit action or remount will resync. Don't clobber the UI with a
      // background-refetch error.
    }
  }, [mcpId]);

  // Initial load (and on mcp/provisioned change). Skipped when unprovisioned —
  // there's nothing to list until an identity exists.
  useEffect(() => {
    if (!provisioned) {
      setLoading(false);
      return;
    }
    let cancelled = false;
    setLoading(true);
    setLoadError(null);
    mcpGrantsApi
      .list(mcpId)
      .then((res) => {
        if (!cancelled) setGrants(res);
      })
      .catch((err: unknown) => {
        if (!cancelled) setLoadError(err instanceof Error ? err.message : 'Failed to load grants.');
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [mcpId, provisioned]);

  // -- mutations ------------------------------------------------------------

  // Re-provision the MCP server's Entra identity (Epic 7, T-ROUTES owns the
  // route). Mirrors AgentDetail.handleReprovision: call POST /reprovision via
  // the shared axios instance (the response interceptor unwraps the backend
  // `detail` into err.message), then re-fetch the server so the local
  // identity_status flips to pending and the banner updates in place.
  const handleReprovision = async () => {
    setActionPending(true);
    setActionError(null);
    try {
      const { data } = await client.post<McpServer>(`/api/v1/mcp-servers/${mcpId}/reprovision`);
      if (mountedRef.current) setServerIdentity(data);
    } catch (err: unknown) {
      if (mountedRef.current) {
        setActionError(err instanceof Error ? err.message : 'Re-provision failed.');
      }
    } finally {
      if (mountedRef.current) setActionPending(false);
    }
  };

  const handlePick = async (hit: PrincipalHit, role: 'Invoker' | 'Admin') => {
    // Belt-and-suspenders re-entry guard (the picker button is also disabled via
    // the `pending` prop). Mirrors AccessTab.
    if (actionPending) return;
    // This (E7 agent→MCP) flow only ever surfaces agent principals; user/group
    // belong to the E6 user→agent flow and are unreachable here.
    if (hit.type !== 'agent') return;
    setActionPending(true);
    setActionError(null);
    try {
      const created = await mcpGrantsApi.add(mcpId, {
        principal_id: hit.id, // the agent's entra_sp_id (design-decision #6)
        principal_type: 'agent',
        role,
      });
      if (!mountedRef.current) return;
      // Optimistically insert the authoritative grant the server returned
      // (carries the real assignment_id + capitalized principal_type).
      setGrants((prev) => {
        // De-dupe in case a prior reconcile already pulled it in.
        if (prev.some((g) => g.assignment_id === created.assignment_id)) return prev;
        return [...prev, created];
      });
      // closePicker focuses the trigger and clears actionError, so the user
      // lands back at the "+ Grant agent access" button with the new row visible.
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

  const handleRemove = async (grant: Grant) => {
    setActionPending(true);
    setActionError(null);
    // Optimistically drop the row; restore it if the call fails.
    const prevGrants = grants;
    setGrants((prev) => prev.filter((g) => g.assignment_id !== grant.assignment_id));
    try {
      await mcpGrantsApi.remove(mcpId, grant.assignment_id);
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
    return <StatusBanner mcp={serverIdentity} canManage={canManage} onReprovision={handleReprovision} />;
  }

  const tBadge = agentTypeBadge();

  return (
    <div className="space-y-4">
      <div className={`${CARD} overflow-hidden`}>
        {/* Card header: title + Add. */}
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

        {/* Picker (inline, above the table) — opened in agent mode. */}
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

        {/* Mutation error (e.g. 409 already-assigned) — non-blocking. */}
        {actionError && (
          <p className="px-5 pt-3 text-sm text-red-600" role="alert">{actionError}</p>
        )}

        {/* Body: loading / error / empty / table. */}
        {loading ? (
          <div className="px-5 py-8 text-center">
            <p className="text-sm text-slate-400">Loading access…</p>
          </div>
        ) : loadError ? (
          <div className="px-5 py-8 text-center">
            <p className="text-sm text-red-600" role="alert">{loadError}</p>
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

                  {/* Type pill (Agent). */}
                  <span className={`shrink-0 inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium ${tBadge.cls}`}>
                    {tBadge.label}
                  </span>

                  {/* Role pill (Invoker / Admin). */}
                  <span className={`shrink-0 inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium ${rBadge.cls}`}>
                    {rBadge.label}
                  </span>

                  {/* Remove ✕ (Operator+). */}
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
