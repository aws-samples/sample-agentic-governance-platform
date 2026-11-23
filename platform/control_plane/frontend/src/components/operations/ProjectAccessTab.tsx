// Decision: docs/adr/frontend-loading-state.md
import { useCallback, useEffect, useMemo, useRef, useState, type JSX } from 'react';

import {
  principalsApi,
  projectRolesApi,
  type PrincipalHit,
  type ProjectRoleRead,
  type ProjectRoleCreate,
} from '../../api/client';
import { useLoad } from '../../api/useLoad';
import { useUser } from '../../contexts/useUser';
import ConfirmDialog from '../ConfirmDialog';
import {
  EMPTY_ROSTER_COPY,
  PROJECT_ROLE_OPTIONS,
  emptyRosterReason,
  filterNewPrincipals,
  grantRefusal,
  grantedPrincipalIds,
  canManageRoles,
  isProjectRoleName,
  mayOfferGrant,
  projectRoleBadge,
  roleActionMessage,
  roleLabel,
  rosterLoadMessage,
  sortRoleRows,
  type ProjectRoleName,
} from './projectRoles';
import { initialsFor } from '../shared/initials';
import { OPS_CARD, OPS_PRIMARY_BTN } from './opsUi';
import { useReturnFocus } from '../shared/useReturnFocus';

const MIN_QUERY = 2;
const DEBOUNCE_MS = 300;

const FIELD_LABEL = 'block text-[11px] uppercase tracking-wide text-slate-400 font-medium mb-1';

const GROUPS_FIRST_COPY = 'Groups are recommended — they stay in sync with your identity provider.';
const EMPTY_ROSTER: ProjectRoleRead[] = [];

type SearchKind = PrincipalHit['type'];

interface DirectorySearch {
  query: string;
  kind: SearchKind;
  existingIds: Set<string>;
  hits: PrincipalHit[];
  error: string | null;
}

function principalTypeBadge(type: string): { cls: string; label: string } {
  return type === 'group'
    ? { cls: 'bg-violet-50 text-violet-700', label: 'Group' }
    : { cls: 'bg-blue-50 text-blue-700', label: 'User' };
}

function PrincipalAvatar({ type, name }: { type: string; name: string }): JSX.Element {
  const badge = principalTypeBadge(type);
  return (
    <span
      aria-hidden="true"
      className={`h-8 w-8 text-xs font-semibold flex items-center justify-center shrink-0 ${badge.cls} ${
        type === 'group' ? 'rounded-lg' : 'rounded-full'
      }`}
    >
      {initialsFor(name)}
    </span>
  );
}

export default function ProjectAccessTab({
  projectId,
  heldRole,
  ungoverned,
  onRolesChanged,
}: {
  projectId: string;
  heldRole: ProjectRoleName | null;
  ungoverned: boolean | undefined;
  onRolesChanged: () => void;
}): JSX.Element {
  const { user } = useUser();
  const roleLevel = user?.role_level ?? 0;

  const { state: rosterState, reload: refetch } = useLoad(
    () => projectRolesApi.list(projectId),
    [projectId],
    'Failed to load the access list.',
  );
  const rows = rosterState.kind === 'error' ? EMPTY_ROSTER : (rosterState.data ?? EMPTY_ROSTER);
  const loading = rosterState.kind === 'loading';
  const loadError = rosterState.kind === 'error' ? rosterLoadMessage(rosterState.failure ?? rosterState.message) : null;
  const rosterLoaded = rosterState.kind === 'ready' || (loading && rosterState.data !== null);

  const [actionPending, setActionPending] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);

  const [picking, setPicking] = useState(false);
  const [revokeTarget, setRevokeTarget] = useState<ProjectRoleRead | null>(null);

  const addTriggerRef = useRef<HTMLButtonElement>(null);

  const mountedRef = useRef(true);
  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
    };
  }, []);

  const sorted = useMemo(() => sortRoleRows(rows), [rows]);
  const existingIds = useMemo(() => grantedPrincipalIds(rows), [rows]);

  const canManage = canManageRoles(heldRole, roleLevel);

  const canGrant = mayOfferGrant(heldRole, roleLevel, rosterLoaded);

  const returnFocus = useReturnFocus(addTriggerRef);
  const closePicker = useCallback(() => {
    setPicking(false);
    setActionError(null);
    returnFocus();
  }, [returnFocus]);

  if (picking && !canGrant) setPicking(false);

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

  const settled = useCallback(() => {
    refetch();
    onRolesChanged();
  }, [refetch, onRolesChanged]);

  const handleGrant = useCallback(
    async (hit: PrincipalHit, role: ProjectRoleName) => {
      if (actionPending) return;
      if (hit.type !== 'user' && hit.type !== 'group') return;
      const refusal = grantRefusal(hit.id, existingIds);
      if (refusal) {
        setActionError(refusal);
        return;
      }
      setActionPending(true);
      setActionError(null);
      try {
        await projectRolesApi.grant(projectId, {
          principal_id: hit.id,
          principal_type: hit.type,
          principal_display: hit.display_name,
          role,
        });
        if (!mountedRef.current) return;
        closePicker();
        settled();
      } catch (err: unknown) {
        if (mountedRef.current) {
          setActionError(
            roleActionMessage(err, 'Failed to grant the role.')
          );
        }
      } finally {
        if (mountedRef.current) setActionPending(false);
      }
    },
    [actionPending, projectId, existingIds, closePicker, settled]
  );

  const handleChangeRole = useCallback(
    async (row: ProjectRoleRead, role: ProjectRoleName) => {
      if (actionPending || row.role === role) return;
      setActionPending(true);
      setActionError(null);
      try {
        const body: ProjectRoleCreate = {
          principal_id: row.principal_id,
          principal_type: row.principal_type,
          principal_display: row.principal_display,
          role,
        };
        await projectRolesApi.update(projectId, row.principal_id, body);
        if (!mountedRef.current) return;
        settled();
      } catch (err: unknown) {
        if (mountedRef.current) {
          setActionError(
            roleActionMessage(err, 'Failed to change the role.')
          );
          refetch();
        }
      } finally {
        if (mountedRef.current) setActionPending(false);
      }
    },
    [actionPending, projectId, refetch, settled]
  );

  const handleRevoke = useCallback(async () => {
    const row = revokeTarget;
    if (!row || actionPending) return;
    setActionPending(true);
    setActionError(null);
    try {
      await projectRolesApi.revoke(projectId, row.principal_id);
      if (!mountedRef.current) return;
      setRevokeTarget(null);
      settled();
    } catch (err: unknown) {
      if (mountedRef.current) {
        setActionError(
          roleActionMessage(err, 'Failed to remove the role.')
        );
        setRevokeTarget(null);
      }
    } finally {
      if (mountedRef.current) setActionPending(false);
    }
  }, [revokeTarget, actionPending, projectId, settled]);

  const ownerCount = rows.filter((r) => r.role === 'owner').length;

  const emptyReason = emptyRosterReason(ungoverned, roleLevel >= 2);
  const emptyCopy = EMPTY_ROSTER_COPY[emptyReason];

  const revokingSelf = !!user?.oid && revokeTarget?.principal_id === user.oid;

  return (
    <div className="space-y-4">
      <div className={`${OPS_CARD} overflow-hidden`}>
        <div className="flex items-start justify-between gap-3 px-4 py-3.5 border-b border-emerald-200/50">
          <div className="min-w-0">
            <h2 className="text-sm font-semibold text-slate-800">Who has access to this project</h2>
            <p className="text-xs text-slate-400 mt-0.5">
              Project roles are checked in addition to tenant access, never instead of it.
            </p>
          </div>
          {canGrant && !picking && (
            <button
              ref={addTriggerRef}
              type="button"
              onClick={() => {
                setPicking(true);
                setActionError(null);
              }}
              className={`${OPS_PRIMARY_BTN} shrink-0`}
            >
              Grant access
            </button>
          )}
        </div>

        {picking && canGrant && (
          <div className="px-4 py-4 border-b border-emerald-200/50 bg-emerald-50/30">
            <GrantPicker
              existingIds={existingIds}
              pending={actionPending}
              onGrant={handleGrant}
              onClose={closePicker}
            />
          </div>
        )}

        {actionError && (
          <p className="px-4 pt-3 text-sm text-rose-700" role="alert">
            {actionError}
          </p>
        )}

        {loading ? (
          <div className="px-4 py-8 text-center text-sm text-slate-400">Loading access…</div>
        ) : loadError ? (
          <div className="px-4 py-8 text-center">
            <p className="text-sm text-rose-700" role="alert">
              {loadError}
            </p>
            <button
              type="button"
              onClick={refetch}
              className="mt-3 px-3 py-1.5 rounded-lg bg-white border border-slate-300 text-slate-700 text-xs font-medium hover:bg-slate-50 transition-colors"
            >
              Retry
            </button>
          </div>
        ) : sorted.length === 0 ? (
          <div className="px-4 py-10 text-center">
            <p className="text-sm font-medium text-slate-600">{emptyCopy.headline}</p>
            <p className="text-xs text-slate-400 mt-1 max-w-md mx-auto">
              {emptyCopy.detail}
              {canManage && emptyReason === 'ungoverned' ? ` ${GROUPS_FIRST_COPY}` : ''}
            </p>
            {emptyReason === 'unknown' && (
              <button
                type="button"
                onClick={refetch}
                className="mt-3 px-3 py-1.5 rounded-lg bg-white border border-slate-300 text-slate-700 text-xs font-medium hover:bg-slate-50 transition-colors"
              >
                Retry
              </button>
            )}
          </div>
        ) : (
          <ul className="divide-y divide-emerald-100/70">
            {sorted.map((row) => {
              const badge = principalTypeBadge(row.principal_type);
              const isYou = !!user?.oid && row.principal_id === user.oid;
              const rowRole = isProjectRoleName(row.role) ? row.role : null;
              const roleBadge = projectRoleBadge(row.role);
              return (
                <li
                  key={row.principal_id}
                  className={`flex items-center gap-3 px-4 py-3 ${isYou ? 'bg-emerald-50/40' : ''}`}
                >
                  <PrincipalAvatar type={row.principal_type} name={row.principal_display || row.principal_id} />

                  <div className="min-w-0 flex-1">
                    <div className="flex items-center gap-2 min-w-0">
                      <span className="text-sm font-medium text-slate-800 truncate">
                        {row.principal_display || row.principal_id}
                      </span>
                      {isYou && (
                        <span className="shrink-0 inline-flex items-center px-1.5 py-0.5 rounded-full text-[10px] font-medium uppercase tracking-wide bg-emerald-100 text-emerald-700">
                          You
                        </span>
                      )}
                    </div>
                    <span className="block text-xs text-slate-400 truncate">
                      {row.granted_by ? `Granted by ${row.granted_by}` : 'Granted'}
                      {row.granted_at ? ` · ${row.granted_at.slice(0, 10)}` : ''}
                    </span>
                  </div>

                  <span
                    className={`shrink-0 inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium ${badge.cls}`}
                  >
                    {badge.label}
                  </span>

                  {canManage && rowRole ? (
                    <>
                      <label htmlFor={`role-${row.principal_id}`} className="sr-only">
                        Role for {row.principal_display || row.principal_id}
                      </label>
                      <select
                        id={`role-${row.principal_id}`}
                        value={rowRole}
                        onChange={(e) => {
                          const next = e.target.value;
                          if (isProjectRoleName(next)) void handleChangeRole(row, next);
                        }}
                        disabled={actionPending}
                        className="shrink-0 px-2.5 py-1 text-xs font-medium rounded-lg border border-slate-300 bg-white focus:outline-none focus:ring-2 focus:ring-emerald-500/40 disabled:opacity-40"
                      >
                        {PROJECT_ROLE_OPTIONS.map((o) => (
                          <option key={o.value} value={o.value}>
                            {o.label}
                          </option>
                        ))}
                      </select>
                    </>
                  ) : (
                    <span
                      className={`shrink-0 inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium ${roleBadge.cls}`}
                    >
                      {roleBadge.label}
                    </span>
                  )}

                  {canManage && (
                    <button
                      type="button"
                      onClick={() => setRevokeTarget(row)}
                      disabled={actionPending}
                      aria-label={`Remove access for ${row.principal_display || row.principal_id}`}
                      className="shrink-0 inline-flex items-center justify-center h-7 w-7 rounded-md text-slate-400 hover:text-rose-600 hover:bg-rose-50 transition-colors disabled:opacity-40"
                    >
                      <span aria-hidden="true" className="text-base leading-none">
                        ×
                      </span>
                    </button>
                  )}
                </li>
              );
            })}
          </ul>
        )}
      </div>

      {!loading && !loadError && sorted.length > 0 && (
        <div className="px-1 space-y-1">
          {ownerCount === 1 && (
            <p className="text-xs text-slate-400">
              This project has one owner. Grant Owner to someone else before changing or removing
              them — a project must keep at least one.
            </p>
          )}
          <p className="text-xs text-slate-400">{GROUPS_FIRST_COPY}</p>
        </div>
      )}

      <ConfirmDialog
        open={revokeTarget !== null}
        title={revokingSelf ? 'Remove your own access' : 'Remove project access'}
        message={
          revokeTarget
            ? `Remove ${revokeTarget.principal_display || revokeTarget.principal_id}’s ${
                isProjectRoleName(revokeTarget.role) ? roleLabel(revokeTarget.role) : revokeTarget.role
              } role on this project?${
                revokingSelf
                  ? ' This removes YOUR OWN access — you will lose these controls immediately and will need another owner to grant the role back.'
                  : ''
              }`
            : ''
        }
        confirmText={actionPending ? 'Removing…' : 'Remove'}
        variant="danger"
        onConfirm={() => void handleRevoke()}
        onCancel={() => {
          if (!actionPending) setRevokeTarget(null);
        }}
      />
    </div>
  );
}

function GrantPicker({
  existingIds,
  pending,
  onGrant,
  onClose,
}: {
  existingIds: Set<string>;
  pending: boolean;
  onGrant: (hit: PrincipalHit, role: ProjectRoleName) => void;
  onClose: () => void;
}): JSX.Element {
  const [kind, setKind] = useState<SearchKind>('group');
  const [query, setQuery] = useState('');
  const [settled, setSettled] = useState<DirectorySearch | null>(null);

  const [selected, setSelected] = useState<PrincipalHit | null>(null);
  const [role, setRole] = useState<ProjectRoleName>('maintainer');

  const inputRef = useRef<HTMLInputElement>(null);
  useEffect(() => {
    inputRef.current?.focus();
  }, []);

  useEffect(() => {
    const q = query.trim();
    if (q.length < MIN_QUERY) return;
    let cancelled = false;
    const t = setTimeout(() => {
      principalsApi
        .search(q)
        .then((res) => {
          if (cancelled) return;
          setSettled({
            query: q,
            kind,
            existingIds,
            hits: filterNewPrincipals(res.filter((h) => h.type === kind), existingIds),
            error: null,
          });
        })
        .catch((err: unknown) => {
          if (cancelled) return;
          setSettled({
            query: q,
            kind,
            existingIds,
            hits: [],
            error: err instanceof Error ? err.message : 'Search failed.',
          });
        });
    }, DEBOUNCE_MS);
    return () => {
      cancelled = true;
      clearTimeout(t);
    };
  }, [query, kind, existingIds]);

  const trimmedQuery = query.trim();
  const current =
    trimmedQuery.length >= MIN_QUERY &&
    settled !== null &&
    settled.query === trimmedQuery &&
    settled.kind === kind &&
    settled.existingIds === existingIds
      ? settled
      : null;
  const searching = trimmedQuery.length >= MIN_QUERY && current === null;
  const searched = current !== null;
  const hits = current?.hits ?? [];
  const searchError = current?.error ?? null;

  const groupMode = kind === 'group';
  const selectedBadge = selected ? principalTypeBadge(selected.type) : null;
  const roleHint = PROJECT_ROLE_OPTIONS.find((o) => o.value === role)?.hint ?? '';

  return (
    <div
      className="bg-white/80 backdrop-blur rounded-xl border border-emerald-200/50 shadow-sm p-4"
      role="dialog"
      aria-label="Grant project access"
    >
      <div className="flex items-start justify-between gap-3 mb-3">
        <div className="min-w-0">
          <h3 className="text-sm font-semibold text-slate-800">
            {groupMode ? 'Grant access to a group' : 'Grant access to a person'}
          </h3>
          <p className="text-xs text-slate-400 mt-0.5">
            {groupMode
              ? GROUPS_FIRST_COPY
              : 'An individual grant is tied to one person and must be re-entered if your directory changes.'}
          </p>
        </div>
        <button
          type="button"
          onClick={onClose}
          aria-label="Close picker"
          className="shrink-0 inline-flex items-center justify-center h-6 w-6 rounded-md text-slate-400 hover:text-slate-700 hover:bg-slate-100 transition-colors"
        >
          <span aria-hidden="true" className="text-base leading-none">
            ×
          </span>
        </button>
      </div>

      <label htmlFor="project-role-search" className="sr-only">
        {groupMode ? 'Search groups' : 'Search people'}
      </label>
      <input
        id="project-role-search"
        ref={inputRef}
        type="text"
        value={query}
        onChange={(e) => {
          setQuery(e.target.value);
          setSelected(null);
        }}
        placeholder={groupMode ? 'Search groups by name…' : 'Search people by name…'}
        className="w-full px-3 py-2 text-sm rounded-lg border border-slate-300 focus:outline-none focus:ring-2 focus:ring-emerald-500/40"
        autoComplete="off"
      />

      <button
        type="button"
        onClick={() => {
          setKind(groupMode ? 'user' : 'group');
          setSelected(null);
        }}
        className="mt-2 text-xs font-medium text-emerald-700 hover:text-emerald-800 hover:underline transition-colors"
      >
        {groupMode ? 'Search people instead' : '← Back to searching groups'}
      </button>

      <div className="mt-3">
        {searchError && (
          <p className="text-sm text-rose-700" role="alert">
            {searchError}
          </p>
        )}

        {!searchError && searching && <p className="text-sm text-slate-400">Searching…</p>}

        {!searchError && !searching && query.trim().length === 0 && (
          <p className="text-sm text-slate-400">
            Type at least {MIN_QUERY} characters to search your directory.
          </p>
        )}

        {!searchError && !searching && query.trim().length > 0 && query.trim().length < MIN_QUERY && (
          <p className="text-sm text-slate-400">Type at least {MIN_QUERY} characters to search.</p>
        )}

        {!searchError && !searching && searched && hits.length === 0 && query.trim().length >= MIN_QUERY && (
          <p className="text-sm text-slate-400">
            No {groupMode ? 'groups' : 'people'} match “{query.trim()}” that don’t already have a role.
          </p>
        )}

        {!searchError && !searching && hits.length > 0 && (
          <ul className="max-h-64 overflow-y-auto -mx-1 divide-y divide-slate-100">
            {hits.map((hit) => {
              const isSelected = selected?.id === hit.id;
              return (
                <li key={hit.id}>
                  <button
                    type="button"
                    onClick={() => setSelected(hit)}
                    aria-pressed={isSelected}
                    className={`w-full flex items-center gap-3 px-2 py-2 rounded-lg text-left transition-colors ${
                      isSelected ? 'bg-emerald-50/70 ring-1 ring-inset ring-emerald-200' : 'hover:bg-slate-50'
                    }`}
                  >
                    <PrincipalAvatar type={hit.type} name={hit.display_name} />
                    <span className="min-w-0 flex-1">
                      <span className="block text-sm font-medium text-slate-800 truncate">
                        {hit.display_name}
                      </span>
                      {hit.mail && <span className="block text-xs text-slate-400 truncate">{hit.mail}</span>}
                    </span>
                  </button>
                </li>
              );
            })}
          </ul>
        )}
      </div>

      {selected && selectedBadge && (
        <div className="mt-3 pt-3 border-t border-emerald-200/50">
          <div className="flex items-center gap-2 mb-3 text-sm text-slate-600">
            <span className="text-slate-400">Granting</span>
            <span className="font-medium text-slate-800 truncate">{selected.display_name}</span>
            <span
              className={`shrink-0 inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium ${selectedBadge.cls}`}
            >
              {selectedBadge.label}
            </span>
          </div>

          {selected.type === 'group' && (
            <p className="text-xs text-slate-400 mb-3">
              A group grant covers the group’s members. Nested groups are resolved by your
              directory, so confirm membership there if a role doesn’t appear to apply.
            </p>
          )}

          <div className="flex items-end gap-2 flex-wrap">
            <div>
              <label htmlFor="project-grant-role" className={FIELD_LABEL}>
                Role
              </label>
              <select
                id="project-grant-role"
                value={role}
                onChange={(e) => {
                  const next = e.target.value;
                  if (isProjectRoleName(next)) setRole(next);
                }}
                className="px-3 py-1.5 text-sm rounded-lg border border-slate-300 bg-white focus:outline-none focus:ring-2 focus:ring-emerald-500/40"
              >
                {PROJECT_ROLE_OPTIONS.map((o) => (
                  <option key={o.value} value={o.value}>
                    {o.label}
                  </option>
                ))}
              </select>
            </div>
            <button
              type="button"
              onClick={() => onGrant(selected, role)}
              disabled={pending}
              className={`${OPS_PRIMARY_BTN} disabled:opacity-40`}
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

          {roleHint && <p className="text-xs text-slate-400 mt-2">{roleHint}.</p>}
        </div>
      )}
    </div>
  );
}
