// ProjectAccessTab — the "Access" tab of the project detail page (E27/T11):
// who holds Viewer / Maintainer / Owner on THIS project.
//
// Structure mirrors the governance AccessTab.tsx (card header + count + inline
// picker above a roster list) re-tinted into the emerald-on-glass Ops idiom
// (opsUi tokens), and the picker itself mirrors PrincipalPicker.tsx: debounced
// principalsApi.search at MIN_QUERY=2 / DEBOUNCE_MS=300 with the
// `cancelled` + clearTimeout cleanup and the `searched` / `searchError` states,
// then pick-principal → reveal-role-<select> → confirm. `existingIds` +
// filterNewPrincipals (the AddUserModal idiom) hide principals who already hold
// a role, so the picker can only ever ADD — changing an existing grant is the
// row's own <select>. That invariant depends on the roster being KNOWN: the backend
// POST is an UPSERT, so an empty `existingIds` would let a "grant Viewer" silently
// DOWNGRADE an existing owner. Hence `rosterLoaded` (`mayOfferGrant`) — a FAILED read
// is never treated as an empty roster — plus `grantRefusal` at the write itself.
//
// GROUPS LEAD, INDIVIDUALS ARE THE EXCEPTION (design §9). The picker searches
// GROUPS by default with a secondary "Search people instead" toggle. This is not
// cosmetic: a group grant is expressed in the currency a team-synced customer
// already administers and survives an identity-provider integration untouched,
// whereas individual-oid grants are exactly what they would have to re-enter.
//
// Gating: every mutating affordance is CONDITIONALLY RENDERED, never `disabled`
// — `disabled` is reserved for in-flight requests. All of the role logic lives in
// the pure `projectRoles.ts` (only `src/**/*.test.ts` is collected by vitest, so
// this file is wiring only). The caller's own role arrives as the `heldRole` prop,
// read off the SERVER's `ProjectDetail.effective_role` — the roster cannot answer
// it, because a role may be granted to an Entra GROUP.
//
// The backend is the real authority (`may()` server-side); this tab surfaces its
// FIXED error literals through `roleActionMessage` so the 409 last-owner refusal
// and the 503 unverifiable-ownership case both read as sentences.

import { useCallback, useEffect, useMemo, useRef, useState, type JSX } from 'react';

import {
  principalsApi,
  projectRolesApi,
  type PrincipalHit,
  type ProjectRoleRecord,
} from '../../api/client';
import { useUser } from '../../contexts/UserContext';
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
  sortRoleRows,
  type ProjectRoleName,
} from './projectRoles';
import { OPS_CARD, OPS_PRIMARY_BTN } from './opsUi';

// PrincipalPicker's search constants, verbatim — the same debounce budget the
// rest of the app's Graph pickers use.
const MIN_QUERY = 2;
const DEBOUNCE_MS = 300;

const FIELD_LABEL = 'block text-[11px] uppercase tracking-wide text-slate-400 font-medium mb-1';

// The ONE line of groups-first copy (design §9) — the reason, not just the rule.
const GROUPS_FIRST_COPY = 'Groups are recommended — they stay in sync with your identity provider.';

// Which principal kind the picker is searching. Groups lead; `user` is the
// deliberate exception, reached through the secondary toggle.
type SearchKind = 'group' | 'user';

// user=blue, group=violet — the SAME tints PrincipalPicker/AccessTab use, so a
// Group reads identically here and on the agent access surface.
function principalTypeBadge(type: string): { cls: string; label: string } {
  return type === 'group'
    ? { cls: 'bg-violet-50 text-violet-700', label: 'Group' }
    : { cls: 'bg-blue-50 text-blue-700', label: 'User' };
}

// Initials for the result/roster avatar — mirrors PrincipalPicker.principalInitials.
function principalInitials(name: string): string {
  const trimmed = name.trim();
  if (!trimmed) return '?';
  const segments = trimmed.split(/\s+/).filter(Boolean);
  if (segments.length >= 2) return (segments[0][0] + segments[1][0]).toUpperCase();
  return trimmed.slice(0, 2).toUpperCase();
}

// A group avatar reads as a rounded-square stack, a user as a circle — so the two
// kinds are distinguishable without relying on the pill alone (or on color).
function PrincipalAvatar({ type, name }: { type: string; name: string }): JSX.Element {
  const badge = principalTypeBadge(type);
  return (
    <span
      aria-hidden="true"
      className={`h-8 w-8 text-xs font-semibold flex items-center justify-center shrink-0 ${badge.cls} ${
        type === 'group' ? 'rounded-lg' : 'rounded-full'
      }`}
    >
      {principalInitials(name)}
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
  // The caller's EFFECTIVE role on this project, as the SERVER reported it on the
  // detail read (`ProjectDetail.effective_role`, narrowed by `effectiveRole`). Passed
  // down rather than derived here: the roster cannot answer it, because a role may be
  // granted to an Entra GROUP and the browser cannot evaluate group membership.
  heldRole: ProjectRoleName | null;
  // The SERVER's `ProjectDetail.ungoverned` bit — does this project hold NO role rows,
  // so the design-§3 fallback applies? The only trustworthy source for the "anyone in
  // the tenant can maintain it" claim: an empty roster is NOT evidence — it is per-project
  // role ROWS, and only the server's derivation accounts for the §3 fallback.
  // `undefined` = not reported.
  ungoverned: boolean | undefined;
  // Refetch the PARENT's project detail. `heldRole` lives on that read, and a role
  // mutation can change the caller's OWN standing — with two owners present, demoting
  // your own row succeeds server-side. Without this the roster would show you as Viewer
  // while Grant / the row <select> / the ✕ stayed rendered and 403'd until a reload.
  onRolesChanged: () => void;
}): JSX.Element {
  const { user } = useUser();
  const roleLevel = user?.role_level ?? 0;

  const [rows, setRows] = useState<ProjectRoleRecord[]>([]);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState<string | null>(null);
  // Did the roster read SUCCEED? Distinct from `rows.length === 0`, which cannot tell
  // "loaded and empty" from "load failed" — and the difference decides whether the
  // upsert-backed Grant affordance is safe to offer at all (see `mayOfferGrant`).
  const [rosterLoaded, setRosterLoaded] = useState(false);
  const [reloadNonce, setReloadNonce] = useState(0);

  // Mutation state. `actionPending` is the ONLY thing that drives `disabled`.
  const [actionPending, setActionPending] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);

  const [picking, setPicking] = useState(false);
  // The row awaiting a revoke confirmation (null = no dialog).
  const [revokeTarget, setRevokeTarget] = useState<ProjectRoleRecord | null>(null);

  // Focus returns to the trigger when the picker closes (the AccessTab idiom) so
  // focus never drops to document start.
  const addTriggerRef = useRef<HTMLButtonElement>(null);

  const mountedRef = useRef(true);
  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
    };
  }, []);

  const refetch = useCallback(() => setReloadNonce((n) => n + 1), []);

  // Load the roster. A 403 here means the caller cannot even READ the roster
  // (the route is VIEWER-gated); say so plainly instead of showing an empty table,
  // which would read as "nobody has access".
  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    projectRolesApi
      .list(projectId)
      .then((data) => {
        if (cancelled) return;
        setRows(data);
        setLoadError(null);
        setRosterLoaded(true);
      })
      .catch((err: unknown) => {
        if (cancelled) return;
        const message = err instanceof Error ? err.message : '';
        setLoadError(
          /insufficient project role/i.test(message)
            ? 'You don’t have a role on this project, so its access list is hidden. Ask an owner to grant you one.'
            : roleActionMessage(message, 'Failed to load the access list.')
        );
        setRows([]);
        // The roster is UNKNOWN, not empty. Clearing this is what keeps `existingIds`
        // from being mistaken for "nobody holds a role", which would turn the picker's
        // ADD-only grant into a silent role downgrade through the backend's upsert.
        setRosterLoaded(false);
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [projectId, reloadNonce]);

  const sorted = useMemo(() => sortRoleRows(rows), [rows]);
  const existingIds = useMemo(() => grantedPrincipalIds(rows), [rows]);

  // Whether to render the Grant / Change / Revoke affordances. Now driven by the
  // SERVER's answer (`heldRole`) instead of a roster guess, so a group-derived owner
  // gets them and a maintainer on a group-granted project does NOT see a Grant button
  // that 403s. Still cosmetic: `may()` server-side remains the gate, and
  // `roleActionMessage` maps a refusal to a sentence if one ever slips through
  // (e.g. a role revoked between this page load and the click).
  const canManage = canManageRoles(heldRole, roleLevel);

  // Grant needs MORE than authority: the trigger sits in the card header, OUTSIDE the
  // loading / error / empty branches, so without this it stayed live over a roster we
  // never read. The backend POST is an upsert and `existingIds` would be empty, so
  // granting Viewer to a principal already holding Owner would silently downgrade them.
  // Withhold the affordance until the roster is a fact.
  const canGrant = mayOfferGrant(heldRole, roleLevel, rosterLoaded);

  const closePicker = useCallback(() => {
    setPicking(false);
    setActionError(null);
    window.setTimeout(() => addTriggerRef.current?.focus(), 0);
  }, []);

  // If the grant affordance goes away while the picker is open — a refetch failed, so the
  // roster is no longer known — drop `picking` too, so the picker does not silently
  // reappear when the next read succeeds.
  useEffect(() => {
    if (!canGrant) setPicking(false);
  }, [canGrant]);

  // Close the picker on Escape while it is open (the AccessTab idiom).
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

  // -- mutations ------------------------------------------------------------
  // Each refetches rather than reconciling optimistically: unlike the agent grants
  // surface (live Entra app-role assignments, eventually consistent), a project role
  // is a DDB row the route writes synchronously — the read is already authoritative.
  //
  // Each ALSO calls `onRolesChanged()`, which re-reads the parent's project detail. That
  // is what refreshes the caller's OWN `heldRole`: these writes can change it (a
  // self-demotion succeeds whenever a second owner remains), and `_invalidate_project_roles()`
  // drops the resolver cache on write, so the follow-up read returns a fresh
  // `effective_role`. Two refreshes off ONE signal beats a second source of truth.
  const settled = useCallback(() => {
    refetch();
    onRolesChanged();
  }, [refetch, onRolesChanged]);

  const handleGrant = useCallback(
    async (hit: PrincipalHit, role: ProjectRoleName) => {
      if (actionPending) return;
      // 'agent' belongs to the E7 MCP-grant flow and is not a project-role subject.
      if (hit.type !== 'user' && hit.type !== 'group') return;
      // Belt-and-braces on the upsert: the picker filters already-granted principals out,
      // and `canGrant` keeps it closed while the roster is unknown — but if one reaches
      // here anyway (the roster moved between the search and the click), REFUSE. A "grant"
      // must never overwrite a role someone already holds.
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
            roleActionMessage(err instanceof Error ? err.message : '', 'Failed to grant the role.')
          );
        }
      } finally {
        if (mountedRef.current) setActionPending(false);
      }
    },
    [actionPending, projectId, existingIds, closePicker, settled]
  );

  // Change one row's role. The whole row is re-sent (the backend's PUT is the
  // store's upsert, so a partial body would blank principal_type/display) with only
  // `role` changed.
  const handleChangeRole = useCallback(
    async (row: ProjectRoleRecord, role: ProjectRoleName) => {
      if (actionPending || row.role === role) return;
      setActionPending(true);
      setActionError(null);
      try {
        await projectRolesApi.update(projectId, row.principal_id, {
          principal_id: row.principal_id,
          principal_type: row.principal_type,
          principal_display: row.principal_display,
          role,
        });
        if (!mountedRef.current) return;
        settled();
      } catch (err: unknown) {
        if (mountedRef.current) {
          setActionError(
            roleActionMessage(err instanceof Error ? err.message : '', 'Failed to change the role.')
          );
          // The <select> is controlled by `rows`, so a refetch snaps it back to the
          // value the server actually holds after a refused change.
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
          roleActionMessage(err instanceof Error ? err.message : '', 'Failed to remove the role.')
        );
        setRevokeTarget(null);
      }
    } finally {
      if (mountedRef.current) setActionPending(false);
    }
  }, [revokeTarget, actionPending, projectId, settled]);

  const ownerCount = rows.filter((r) => r.role === 'owner').length;

  // Which empty-roster story to tell — see `emptyRosterReason`. `roleLevel >= 2` is the
  // platform-admin test the backend's `is_global` short-circuit corresponds to.
  const emptyReason = emptyRosterReason(ungoverned, roleLevel >= 2);
  const emptyCopy = EMPTY_ROSTER_COPY[emptyReason];

  // Is the pending revoke the caller's OWN row? Only true for a DIRECT user grant — a
  // group-derived role has no row of the caller's to remove.
  const revokingSelf = !!user?.oid && revokeTarget?.principal_id === user.oid;

  // -- render ---------------------------------------------------------------

  return (
    <div className="space-y-4">
      <div className={`${OPS_CARD} overflow-hidden`}>
        {/* Card header — title, what a role means, and the Grant trigger. */}
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

        {/* Inline picker, above the roster (the AccessTab placement). Also gated on
            `canGrant`, not just `picking`: a roster refetch can FAIL while the picker is
            open, and an open picker over an unknown roster is the same upsert hazard as
            an open trigger. */}
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

        {/* Mutation error — a mapped sentence, never a raw backend literal. */}
        {actionError && (
          <p className="px-4 pt-3 text-sm text-rose-700" role="alert">
            {actionError}
          </p>
        )}

        {/* Body: loading / error / empty / roster. */}
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
          /* Empty roster. The "anyone in the tenant can maintain it" claim is a statement
             about BLAST RADIUS, so it is made only from the server's `ungoverned` bit —
             never inferred from list length, which answers a different question (role ROWS,
             with no account of the §3 fallback). The admin case is explicit: an admin ALWAYS
             reads `ungoverned: false`, so that `false` is not evidence of governance either.
             See `emptyRosterReason`. */
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
              // The pill's tint AND label in one pinned derivation (E28/T10). `rowRole`
              // stays because the role SELECT below needs the narrowed value; the badge no
              // longer re-derives it.
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
                    {/* Granted-by / when — the audit line an FSI reviewer looks for. */}
                    <span className="block text-xs text-slate-400 truncate">
                      {row.granted_by ? `Granted by ${row.granted_by}` : 'Granted'}
                      {row.granted_at ? ` · ${row.granted_at.slice(0, 10)}` : ''}
                    </span>
                  </div>

                  {/* Type pill (User / Group). */}
                  <span
                    className={`shrink-0 inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium ${badge.cls}`}
                  >
                    {badge.label}
                  </span>

                  {/* Role: an owner gets a <select>; everyone else a static pill. */}
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
                    // Tint AND label from the one pinned helper (E28/T10). This was an
                    // inline narrow-then-branch pair, which vitest cannot even collect from
                    // a `.tsx` — and the governance surface's three copies of the
                    // equivalent helper show where that leads: two of them answer
                    // `Invoker` for a role they did not recognize, manufacturing a
                    // privilege name. The behaviour here is unchanged; it is now pinned.
                    <span
                      className={`shrink-0 inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium ${roleBadge.cls}`}
                    >
                      {roleBadge.label}
                    </span>
                  )}

                  {/* Revoke ✕ — conditionally rendered, disabled only in flight. */}
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

      {/* Footnotes: the last-owner rule (so the 409 is predictable rather than a
          surprise) and the groups-first rationale, once the roster is non-empty. */}
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
                // Self-removal is permitted (the backend only refuses stripping the LAST
                // owner), so say what it costs rather than letting a self-lockout read as a
                // routine row edit.
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

// ---------------------------------------------------------------------------
// GrantPicker — the groups-first principal picker.
//
// PrincipalPicker.tsx's search machinery verbatim (MIN_QUERY / DEBOUNCE_MS, the
// `cancelled` + clearTimeout cleanup, `searched` / `searchError`), with two
// changes: the role <select> offers the three PROJECT roles, and the results are
// narrowed to ONE principal kind — groups by default.
//
// `principalsApi.search` is reused UNCHANGED (its route takes no kind parameter
// and always returns users + groups), so the narrowing is client-side.
// ---------------------------------------------------------------------------
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
  const [hits, setHits] = useState<PrincipalHit[]>([]);
  const [searching, setSearching] = useState(false);
  // Whether a search has completed for the current query — gates the "no matches"
  // empty state so it doesn't flash before the first fetch.
  const [searched, setSearched] = useState(false);
  // A directory failure (Graph 502 / throttle) must not read as "no results".
  const [searchError, setSearchError] = useState<string | null>(null);

  const [selected, setSelected] = useState<PrincipalHit | null>(null);
  const [role, setRole] = useState<ProjectRoleName>('maintainer');

  const inputRef = useRef<HTMLInputElement>(null);
  useEffect(() => {
    inputRef.current?.focus();
  }, []);

  // Debounced directory search. A short query clears results without hitting
  // Graph; each keystroke cancels the prior in-flight debounce. Results drop
  // principals who already hold a role (filterNewPrincipals) and any principal of
  // the kind we are not currently searching.
  useEffect(() => {
    const q = query.trim();
    if (q.length < MIN_QUERY) {
      setHits([]);
      setSearched(false);
      setSearching(false);
      setSearchError(null);
      return;
    }
    let cancelled = false;
    setSearching(true);
    setSearchError(null);
    const t = setTimeout(() => {
      principalsApi
        .search(q)
        .then((res) => {
          if (cancelled) return;
          setHits(filterNewPrincipals(res.filter((h) => h.type === kind), existingIds));
          setSearchError(null);
        })
        .catch((err: unknown) => {
          if (cancelled) return;
          setHits([]);
          // The axios interceptor surfaces the backend `detail` as err.message.
          setSearchError(err instanceof Error ? err.message : 'Search failed.');
        })
        .finally(() => {
          if (!cancelled) {
            setSearching(false);
            setSearched(true);
          }
        });
    }, DEBOUNCE_MS);
    return () => {
      cancelled = true;
      clearTimeout(t);
    };
  }, [query, kind, existingIds]);

  const groupMode = kind === 'group';
  const selectedBadge = selected ? principalTypeBadge(selected.type) : null;
  const roleHint = PROJECT_ROLE_OPTIONS.find((o) => o.value === role)?.hint ?? '';

  return (
    <div
      className="bg-white/80 backdrop-blur rounded-xl border border-emerald-200/50 shadow-sm p-4"
      role="dialog"
      aria-label="Grant project access"
    >
      {/* Header: title + close. */}
      <div className="flex items-start justify-between gap-3 mb-3">
        <div className="min-w-0">
          <h3 className="text-sm font-semibold text-slate-800">
            {groupMode ? 'Grant access to a group' : 'Grant access to a person'}
          </h3>
          {/* The groups-first rationale, stated once, where the choice is made. */}
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

      {/* Search box. */}
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
          // Re-opening the search clears any pending selection.
          setSelected(null);
        }}
        placeholder={groupMode ? 'Search groups by name…' : 'Search people by name…'}
        className="w-full px-3 py-2 text-sm rounded-lg border border-slate-300 focus:outline-none focus:ring-2 focus:ring-emerald-500/40"
        autoComplete="off"
      />

      {/* The secondary kind toggle — a text button, deliberately quieter than the
          primary path. Switching kinds clears the pending selection so a chosen
          group can't be confirmed while the list shows people. */}
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

      {/* Results region. */}
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

      {/* Role select + confirm — revealed once a principal is chosen. */}
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

          {/* The nested-group caveat, stated where a group is actually chosen —
              the same limitation PrincipalPicker surfaces for Entra grants. */}
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

          {/* What the chosen role actually permits — read off the ONE options list
              so the copy can never drift from the role set. */}
          {roleHint && <p className="text-xs text-slate-400 mt-2">{roleHint}.</p>}
        </div>
      )}
    </div>
  );
}
