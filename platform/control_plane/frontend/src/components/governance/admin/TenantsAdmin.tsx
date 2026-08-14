// TenantsAdmin — the line-of-business tenants admin panel (Epic 24), rendered as
// the "Tenants" tab inside AdminConsole.
//
// Admin-gated (role_level >= 2). Like UsersAdmin, the non-admin path returns
// null: the AdminConsole already shows the not-authorized panel, so this tab body
// never renders for non-admins. Skeleton cloned from UsersAdmin: the
// cancelled-guard loader keyed on [isAdmin, reloadNonce] calling
// tenantsAdminApi.list(), the refetch nonce-bump, the shared runAction(id, fn)
// mutation helper, the glass-CARD table, inline loading/error/empty states, the
// per-row disabled/… spinner idiom, and the top-level actionError <p role="alert">
// (which is how the backend's 409 "tenant is referenced by existing resources"
// literal surfaces on Delete).
// Layout:
//   • a header row — a short description <p> + a "Create tenant" primary button;
//   • the glass-CARD table of tenantsAdminApi.list() rows: Name (+ description),
//     LoB, platform + capability badges, linked-group count, the per-stage identifier,
//     and Edit/Delete actions.
//   • Create/Edit open TenantModal (create: tenant=null; edit: the row); on
//     submit it creates/updates then closes + refetches.
//
// E29 — a tenant is platform-typed, which changes this table in two ways. The stage columns
// show a platform-appropriate identifier (see StageCell), and a Databricks tenant carries
// badges for its binding mode and probed capabilities. Both are derived by pure functions in
// tenantsAdminForm.ts; this file renders their output and decides nothing.

import { useCallback, useEffect, useState } from 'react';
import { tenantsAdminApi } from '../../../api/client';
import type { TenantInfo } from '../../../api/client';
import { useUser } from '../../../contexts/UserContext';
import { bindingModeBadge } from '../platformLabels';
import {
  STAGE_KEYS,
  buildCreatePayload,
  buildUpdatePayload,
  capabilityBadges,
  isDatabricksStage,
  platformLabel,
} from './tenantsAdminForm';
import TenantModal from './TenantModal';

const CARD = 'bg-white/70 backdrop-blur rounded-xl border border-slate-200/60 shadow-sm';
const PILL = 'inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium';

// One stage cell. E29 made the stage column platform-dependent: an AWS tenant's stage is
// identified by its 12-digit account id, a Databricks tenant's by its workspace host. Both
// shapes carry an `account_id`, so printing that field blindly would render a Databricks
// ACCOUNT UUID under a column headed "account" — true to the field name and misleading to
// every reader. Narrow first, then show the identifier that actually locates the stage.
//
// A missing stage renders an em dash rather than throwing: `stages` is an open map and a
// single-stage tenant is a legitimate record (the crash this replaces is the OPEN SEAM noted
// on `TenantInfo` in client.ts).
function StageCell({ tenant, stage }: { tenant: TenantInfo; stage: string }) {
  const config = tenant.stages?.[stage];
  if (!config) return <span className="text-slate-300">—</span>;
  if (isDatabricksStage(config)) {
    const host = config.workspace_url.replace(/^https:\/\//, '');
    return (
      <span className="block truncate max-w-[16rem]" title={config.workspace_url}>
        {host || <span className="text-slate-300">—</span>}
      </span>
    );
  }
  return <>{config.account_id || <span className="text-slate-300">—</span>}</>;
}

export default function TenantsAdmin() {
  const { user } = useUser();
  const isAdmin = (user?.role_level ?? 0) >= 2;

  const [tenants, setTenants] = useState<TenantInfo[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [reloadNonce, setReloadNonce] = useState(0);

  // Per-row action state.
  const [actionPendingId, setActionPendingId] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);

  // null = closed; 'new' = create; a TenantInfo = edit that tenant.
  const [modal, setModal] = useState<'new' | TenantInfo | null>(null);

  // Load the tenants. Skipped entirely for non-admins (this tab body never
  // renders for them — the AdminConsole short-circuits to the not-authorized panel).
  useEffect(() => {
    if (!isAdmin) {
      setLoading(false);
      return;
    }
    let cancelled = false;
    setLoading(true);
    tenantsAdminApi
      .list()
      .then((rows) => {
        if (cancelled) return;
        setTenants(rows);
        setError(null);
      })
      .catch((err: unknown) => {
        if (cancelled) return;
        setError(err instanceof Error ? err.message : 'Failed to load tenants.');
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [isAdmin, reloadNonce]);

  const refetch = useCallback(() => setReloadNonce((n) => n + 1), []);

  const runAction = useCallback(
    async (id: string, fn: () => Promise<unknown>) => {
      if (actionPendingId) return;
      setActionPendingId(id);
      setActionError(null);
      try {
        await fn();
        refetch();
      } catch (err: unknown) {
        // The axios interceptor surfaces the backend `detail` as err.message —
        // e.g. the DELETE 409 "tenant is referenced by existing resources".
        setActionError(err instanceof Error ? err.message : 'Action failed.');
      } finally {
        setActionPendingId(null);
      }
    },
    [actionPendingId, refetch],
  );

  const handleDelete = useCallback(
    (t: TenantInfo) => {
      if (!window.confirm(`Delete tenant "${t.name}"? This cannot be undone.`)) {
        return;
      }
      void runAction(t.id, () => tenantsAdminApi.remove(t.id));
    },
    [runAction],
  );

  // The non-admin path returns null — the AdminConsole already renders the
  // not-authorized panel, so there is nothing to show here.
  if (!isAdmin) return null;

  return (
    <div className="space-y-3">
      {/* Header row: description + Create tenant. */}
      <div className="flex items-start justify-between gap-3">
        <p className="text-xs text-slate-500">
          Tenants are line-of-business units that own agents, MCP servers and projects. Each maps
          to one or more Entra groups and to the LoB’s dev/prod runtime — AWS accounts, or
          Databricks workspaces.
        </p>
        <button
          type="button"
          onClick={() => setModal('new')}
          className="shrink-0 px-3.5 py-1.5 rounded-lg bg-blue-600 text-white text-sm font-medium hover:bg-blue-700 transition-colors"
        >
          Create tenant
        </button>
      </div>

      {actionError && (
        <p className="text-sm text-red-600" role="alert">
          {actionError}
        </p>
      )}

      {/* Table. */}
      {error ? (
        <div className="bg-white/70 backdrop-blur rounded-xl border border-red-200/70 shadow-sm p-6">
          <h3 className="text-sm font-semibold text-red-700">Couldn’t load tenants</h3>
          <p className="text-sm text-slate-600 mt-1">{error}</p>
          <button
            onClick={refetch}
            className="mt-3 px-3 py-1.5 rounded-lg bg-white border border-slate-300 text-slate-700 text-xs font-medium hover:bg-slate-50 transition-colors"
          >
            Retry
          </button>
        </div>
      ) : (
        <div className={`${CARD} overflow-hidden`}>
          <table className="w-full text-sm">
            <thead className="bg-slate-50/80 text-slate-500 text-xs uppercase tracking-wide">
              <tr>
                <th className="text-left font-medium px-4 py-2">Name</th>
                <th className="text-left font-medium px-4 py-2">Line of business</th>
                <th className="text-left font-medium px-4 py-2">Platform</th>
                <th className="text-left font-medium px-4 py-2">Groups</th>
                {/* Headed neutrally, because the cell's content is platform-dependent: an
                    AWS account id or a Databricks workspace host. */}
                <th className="text-left font-medium px-4 py-2">Dev</th>
                <th className="text-left font-medium px-4 py-2">Prod</th>
                <th className="text-right font-medium px-4 py-2">Actions</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-slate-100">
              {loading && (
                <tr>
                  <td colSpan={7} className="px-4 py-8 text-center text-slate-400 text-sm">
                    Loading tenants…
                  </td>
                </tr>
              )}

              {!loading &&
                tenants.map((t) => {
                  const rowPending = actionPendingId === t.id;
                  // While ANY row's action is in flight, disable every row's controls:
                  // runAction is single-flight (it drops a second concurrent call).
                  const anyPending = actionPendingId !== null;
                  // Pre-E29 records carry no `platform` — default to aws, mirroring the
                  // backend's hydration rather than rendering an empty column.
                  const platform = t.platform ?? 'aws';
                  const modeBadge = bindingModeBadge(t.binding_mode);
                  const badges = capabilityBadges(platform, t.capabilities);
                  return (
                    <tr key={t.id} className="hover:bg-blue-50/40 transition-colors">
                      <td className="px-4 py-2">
                        <span className="block text-slate-700 font-medium truncate" title={t.name}>
                          {t.name}
                        </span>
                        {t.description && (
                          <span className="block text-xs text-slate-400 truncate" title={t.description}>
                            {t.description}
                          </span>
                        )}
                      </td>
                      <td className="px-4 py-2 text-slate-600">{t.line_of_business}</td>
                      <td className="px-4 py-2">
                        <div className="flex flex-wrap items-center gap-1.5">
                          <span className={`${PILL} bg-slate-100 text-slate-700`}>
                            {platformLabel(platform)}
                          </span>
                          {/* Binding-mode badge (C-6 copy, + T14's third mode). Absent for AWS
                              tenants and for a Databricks tenant that has not been probed — a
                              badge is a claim, and there is nothing to claim until the probe has
                              run. Label, consequence and tint all come from `bindingModeBadge`,
                              replacing a fixed blue pill and an inline two-value guard that had to
                              grow with the vocabulary; the sr-only span keeps the consequence off
                              the hover-only path, exactly as on agent detail. */}
                          {modeBadge && (
                            <span className={`${PILL} ${modeBadge.tint}`} title={modeBadge.hint}>
                              {modeBadge.label}
                              <span className="sr-only"> — {modeBadge.hint}</span>
                            </span>
                          )}
                          {/* Capability badges: only the ones that reported. An unprobed
                              capability is shown as nothing, never as a failure. */}
                          {badges
                            .filter((b) => b.on !== undefined)
                            .map((b) => (
                              <span
                                key={b.key}
                                className={`${PILL} ${
                                  b.on
                                    ? 'bg-emerald-50 text-emerald-700'
                                    : 'bg-slate-100 text-slate-500'
                                }`}
                                title={`${b.label}: ${b.on ? 'available' : 'unavailable'}`}
                              >
                                {b.label}
                              </span>
                            ))}
                        </div>
                      </td>
                      <td className="px-4 py-2">
                        <span className={`${PILL} bg-violet-50 text-violet-700`}>
                          {t.entra_group_ids.length}{' '}
                          {t.entra_group_ids.length === 1 ? 'group' : 'groups'}
                        </span>
                      </td>
                      {/* E36/T1 guard lives in StageCell: `stages` is an open map, so a
                          column's stage can be absent — em-dash, never an unguarded index
                          (which threw and blanked the whole table). */}
                      {STAGE_KEYS.map((stage) => (
                        <td key={stage} className="px-4 py-2 text-slate-600 font-mono text-xs">
                          <StageCell tenant={t} stage={stage} />
                        </td>
                      ))}
                      <td className="px-4 py-2">
                        <div className="flex items-center justify-end gap-1.5">
                          <button
                            type="button"
                            onClick={() => setModal(t)}
                            disabled={rowPending || anyPending}
                            className="px-2.5 py-1 rounded-md bg-white border border-slate-300 text-slate-700 text-xs font-medium hover:bg-slate-50 transition-colors disabled:opacity-40"
                          >
                            Edit
                          </button>
                          <button
                            type="button"
                            onClick={() => handleDelete(t)}
                            disabled={rowPending || anyPending}
                            className="px-2.5 py-1 rounded-md bg-white border border-rose-300 text-rose-600 text-xs font-medium hover:bg-rose-50 transition-colors disabled:opacity-40"
                          >
                            {rowPending ? '…' : 'Delete'}
                          </button>
                        </div>
                      </td>
                    </tr>
                  );
                })}

              {!loading && tenants.length === 0 && (
                <tr>
                  <td colSpan={7} className="px-4 py-8 text-center text-slate-400 text-sm">
                    No tenants yet.
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
      )}

      {modal !== null && (
        <TenantModal
          tenant={modal === 'new' ? null : modal}
          onClose={() => setModal(null)}
          onSubmit={async (draft) => {
            if (modal === 'new') {
              await tenantsAdminApi.create(buildCreatePayload(draft));
            } else {
              await tenantsAdminApi.update(modal.id, buildUpdatePayload(draft));
            }
            // Create/update don't go through runAction, so clear any stale banner
            // from a prior failed Delete before closing + refetching.
            setActionError(null);
            setModal(null);
            refetch();
          }}
        />
      )}
    </div>
  );
}
