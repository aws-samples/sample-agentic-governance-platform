// UsersAdmin — the platform-users admin panel (Epic 16), rendered as the "Users"
// tab inside AdminConsole.
//
// Admin-gated (role_level >= 2). Unlike MarketplaceAdmin, the non-admin path
// returns null: the AdminConsole already shows the not-authorized panel, so this
// tab body never renders for non-admins (and duplicating the panel would be
// wrong). Skeleton cloned from MarketplaceAdmin: the cancelled-guard loader keyed
// on [isAdmin, reloadNonce] calling usersAdminApi.list(), the refetch nonce-bump,
// the shared runAction(id, fn) mutation helper, the glass-CARD table, inline
// loading/error/empty states, the per-row disabled/… spinner idiom, and the
// top-level actionError <p role="alert">.
// Layout:
//   • a header row — a short description <p> + an "Add user" primary button;
//   • the glass-CARD table of usersAdminApi.list() rows. Each row shows the
//     principal (avatar + display name), a slate principal-type pill, an inline
//     role <select> (changeRole on change), and a rose Remove action (mirrors
//     MarketplaceAdmin's Revoke button), with the click wrapped so it doesn't
//     interfere with the row.
//   • Add-user opens AddUserModal; on submit it adds the user then closes +
//     refetches.

import { useCallback, useEffect, useMemo, useState } from 'react';
import { usersAdminApi } from '../../../api/client';
import type { PlatformRole, PlatformUser } from '../../../api/client';
import { useUser } from '../../../contexts/UserContext';
import { AgentAvatar } from '../agentUi';
import { PLATFORM_ROLES, ROLE_BADGE } from './usersAdminForm';
import AddUserModal from './AddUserModal';

const CARD = 'bg-white/70 backdrop-blur rounded-xl border border-slate-200/60 shadow-sm';

export default function UsersAdmin() {
  const { user } = useUser();
  const isAdmin = (user?.role_level ?? 0) >= 2;

  const [users, setUsers] = useState<PlatformUser[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [reloadNonce, setReloadNonce] = useState(0);

  // Per-row action state.
  const [actionPendingId, setActionPendingId] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);

  const [showAdd, setShowAdd] = useState(false);

  // Load the platform users. Skipped entirely for non-admins (this tab body never
  // renders for them — the AdminConsole short-circuits to the not-authorized panel).
  useEffect(() => {
    if (!isAdmin) {
      setLoading(false);
      return;
    }
    let cancelled = false;
    setLoading(true);
    usersAdminApi
      .list()
      .then((rows) => {
        if (cancelled) return;
        setUsers(rows);
        setError(null);
      })
      .catch((err: unknown) => {
        if (cancelled) return;
        setError(err instanceof Error ? err.message : 'Failed to load users.');
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
        setActionError(err instanceof Error ? err.message : 'Action failed.');
      } finally {
        setActionPendingId(null);
      }
    },
    [actionPendingId, refetch],
  );

  const handleChangeRole = useCallback(
    (id: string, role: PlatformRole) => {
      void runAction(id, () => usersAdminApi.changeRole(id, role));
    },
    [runAction],
  );
  const handleRemove = useCallback(
    (id: string) => {
      if (!window.confirm("Remove this user's platform access? They will lose all platform roles.")) {
        return;
      }
      void runAction(id, () => usersAdminApi.remove(id));
    },
    [runAction],
  );

  // Memoized so its identity is stable across unrelated re-renders — it's a dep of
  // AddUserModal's debounced search effect. Declared before the early return to keep
  // hook order unconditional (Rules of Hooks).
  const existingIds = useMemo(() => new Set(users.map((u) => u.principal_id)), [users]);

  // The non-admin path returns null — the AdminConsole already renders the
  // not-authorized panel, so there is nothing to show here.
  if (!isAdmin) return null;

  return (
    <div className="space-y-3">
      {/* Header row: description + Add user. */}
      <div className="flex items-start justify-between gap-3">
        <p className="text-xs text-slate-500">
          Manage who can access the platform and at what role. Roles are granted as Entra app-role
          assignments on the platform.
        </p>
        <button
          type="button"
          onClick={() => setShowAdd(true)}
          className="shrink-0 px-3.5 py-1.5 rounded-lg bg-blue-600 text-white text-sm font-medium hover:bg-blue-700 transition-colors"
        >
          Add user
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
          <h3 className="text-sm font-semibold text-red-700">Couldn’t load users</h3>
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
                <th className="text-left font-medium px-4 py-2">User</th>
                <th className="text-left font-medium px-4 py-2">Type</th>
                <th className="text-left font-medium px-4 py-2">Role</th>
                <th className="text-right font-medium px-4 py-2">Actions</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-slate-100">
              {loading && (
                <tr>
                  <td colSpan={4} className="px-4 py-8 text-center text-slate-400 text-sm">
                    Loading users…
                  </td>
                </tr>
              )}

              {!loading &&
                users.map((u) => {
                  const rowPending = actionPendingId === u.principal_id;
                  // While ANY row's action is in flight, disable every row's controls:
                  // runAction is single-flight (it drops a second concurrent call), so an
                  // enabled select on another row would silently discard the change and
                  // show an unpersisted value until the next refetch snaps it back.
                  const anyPending = actionPendingId !== null;
                  return (
                    <tr key={u.principal_id} className="hover:bg-blue-50/40 transition-colors">
                      <td className="px-4 py-2">
                        <div className="flex items-center gap-2.5">
                          <AgentAvatar name={u.display_name} size="sm" />
                          <span className="text-slate-700 truncate" title={u.display_name}>
                            {u.display_name}
                          </span>
                        </div>
                      </td>
                      <td className="px-4 py-2">
                        <span className="inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium bg-slate-100 text-slate-600">
                          {u.principal_type}
                        </span>
                      </td>
                      <td className="px-4 py-2">
                        <select
                          aria-label={`Role for ${u.display_name}`}
                          className="px-3 py-1.5 text-sm rounded-lg border border-slate-300 bg-white disabled:opacity-40"
                          value={u.role}
                          disabled={rowPending || anyPending}
                          onChange={(e) =>
                            handleChangeRole(u.principal_id, e.target.value as PlatformRole)
                          }
                        >
                          {PLATFORM_ROLES.map((r) => (
                            <option key={r} value={r}>
                              {ROLE_BADGE[r].label}
                            </option>
                          ))}
                        </select>
                      </td>
                      <td className="px-4 py-2">
                        <div className="flex items-center justify-end gap-1.5">
                          <button
                            type="button"
                            onClick={() => handleRemove(u.principal_id)}
                            disabled={rowPending || anyPending}
                            className="px-2.5 py-1 rounded-md bg-white border border-rose-300 text-rose-600 text-xs font-medium hover:bg-rose-50 transition-colors disabled:opacity-40"
                          >
                            {rowPending ? '…' : 'Remove'}
                          </button>
                        </div>
                      </td>
                    </tr>
                  );
                })}

              {!loading && users.length === 0 && (
                <tr>
                  <td colSpan={4} className="px-4 py-8 text-center text-slate-400 text-sm">
                    No platform users yet.
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
      )}

      {showAdd && (
        <AddUserModal
          existingIds={existingIds}
          onClose={() => setShowAdd(false)}
          onSubmit={async ({ principalId, role }) => {
            await usersAdminApi.add({ principal_id: principalId, role });
            // Add doesn't go through runAction, so clear any stale banner from a
            // prior failed Remove/changeRole before closing + refetching.
            setActionError(null);
            setShowAdd(false);
            refetch();
          }}
        />
      )}
    </div>
  );
}
