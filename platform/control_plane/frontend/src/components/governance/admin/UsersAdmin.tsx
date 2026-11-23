import { useCallback, useMemo, useState } from 'react';
import { usersAdminApi } from '../../../api/client';
import type { RoleChange, PlatformUser } from '../../../api/client';
import { useLoad } from '../../../api/useLoad';
import { useUser } from '../../../contexts/useUser';
import { AgentAvatar } from '../agentUi';
import { PLATFORM_ROLES, ROLE_BADGE } from './usersAdminForm';
import AddUserModal from './AddUserModal';

const NO_USERS: PlatformUser[] = [];
const CARD = 'bg-white/70 backdrop-blur rounded-xl border border-slate-200/60 shadow-sm';

export default function UsersAdmin() {
  const { user } = useUser();
  const isAdmin = (user?.role_level ?? 0) >= 2;

  const { state, reload: refetch } = useLoad(
    () => (isAdmin ? usersAdminApi.list() : Promise.resolve(NO_USERS)),
    [isAdmin],
    'Failed to load users.',
  );
  const users = state.data ?? NO_USERS;
  const loading = state.kind === 'loading';

  const [actionPendingId, setActionPendingId] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);

  const [showAdd, setShowAdd] = useState(false);

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
    (id: string, role: RoleChange['role']) => {
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

  const existingIds = useMemo(() => new Set(users.map((u) => u.principal_id)), [users]);

  if (!isAdmin) return null;

  return (
    <div className="space-y-3">
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

      {state.kind === 'error' ? (
        <div className="bg-white/70 backdrop-blur rounded-xl border border-red-200/70 shadow-sm p-6">
          <h3 className="text-sm font-semibold text-red-700">Couldn’t load users</h3>
          <p className="text-sm text-slate-600 mt-1">{state.message}</p>
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
                            handleChangeRole(u.principal_id, e.target.value as RoleChange['role'])
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
            setActionError(null);
            setShowAdd(false);
            refetch();
          }}
        />
      )}
    </div>
  );
}
