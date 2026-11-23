import { useCallback, useEffect, useRef, useState } from 'react';
import { principalsApi } from '../../../api/client';
import type { RoleChange, PrincipalHit } from '../../../api/client';
import { AgentAvatar } from '../agentUi';
import { PLATFORM_ROLES, ROLE_BADGE, filterNewPrincipals } from './usersAdminForm';

const MIN_QUERY = 2;
const DEBOUNCE_MS = 300;

export default function AddUserModal({
  existingIds,
  onSubmit,
  onClose,
}: {
  existingIds: Set<string>;
  onSubmit: (opts: { principalId: string; role: RoleChange['role'] }) => Promise<void>;
  onClose: () => void;
}) {
  const [query, setQuery] = useState('');
  const [settled, setSettled] = useState<{ query: string; hits: PrincipalHit[]; error: string | null } | null>(null);

  const [selected, setSelected] = useState<PrincipalHit | null>(null);
  const [role, setRole] = useState<RoleChange['role']>('viewer');

  const [actionPending, setActionPending] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);

  const inputRef = useRef<HTMLInputElement>(null);

  const mountedRef = useRef(true);
  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
    };
  }, []);

  useEffect(() => {
    inputRef.current?.focus();
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

  // Decision: docs/adr/frontend-loading-state.md
  useEffect(() => {
    const q = query.trim();
    if (q.length < MIN_QUERY) return;
    let cancelled = false;
    const t = setTimeout(() => {
      principalsApi
        .search(q)
        .then((res) => {
          if (!cancelled) setSettled({ query: q, hits: filterNewPrincipals(res, existingIds), error: null });
        })
        .catch((err: unknown) => {
          if (!cancelled) {
            setSettled({ query: q, hits: [], error: err instanceof Error ? err.message : 'Search failed.' });
          }
        });
    }, DEBOUNCE_MS);
    return () => {
      cancelled = true;
      clearTimeout(t);
    };
  }, [query, existingIds]);

  const trimmedQuery = query.trim();
  const searchable = trimmedQuery.length >= MIN_QUERY;
  const current = searchable && settled?.query === trimmedQuery ? settled : null;
  const hits = current?.hits ?? [];
  const searched = current !== null;
  const searching = searchable && current === null;
  const searchError = current?.error ?? null;

  const canSubmit = selected !== null;

  const handleSubmit = useCallback(async () => {
    if (actionPending || !selected) return;
    setActionPending(true);
    setActionError(null);
    try {
      await onSubmit({ principalId: selected.id, role });
    } catch (err: unknown) {
      if (mountedRef.current) {
        setActionError(err instanceof Error ? err.message : 'Failed to add user.');
      }
    } finally {
      if (mountedRef.current) setActionPending(false);
    }
  }, [actionPending, selected, onSubmit, role]);

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-slate-900/30 backdrop-blur-sm p-4"
      role="dialog"
      aria-modal="true"
      aria-label="Add platform user"
      onMouseDown={(e) => {
        if (e.target === e.currentTarget && !actionPending) onClose();
      }}
    >
      <div className="w-full max-w-lg bg-white rounded-2xl border border-slate-200 shadow-xl">
        <div className="flex items-start justify-between gap-3 px-5 py-4 border-b border-slate-200/60">
          <div className="min-w-0">
            <h2 className="text-sm font-semibold text-slate-900 leading-tight">Add platform user</h2>
            <p className="text-xs text-slate-500 mt-0.5">
              Search the directory and assign a platform role. The role is granted as an Entra
              app-role assignment on the platform.
            </p>
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
          <div>
            <label
              htmlFor="add-user-search"
              className="block text-[11px] uppercase tracking-wide text-slate-400 font-medium mb-1"
            >
              Search users and groups
            </label>
            <input
              id="add-user-search"
              ref={inputRef}
              type="text"
              value={query}
              onChange={(e) => {
                setQuery(e.target.value);
                setSelected(null);
              }}
              placeholder="Search the directory by name…"
              className="w-full px-3 py-2 text-sm rounded-lg border border-slate-300 focus:outline-none focus:ring-2 focus:ring-blue-500/40"
              autoComplete="off"
            />

            <div className="mt-3">
              {searchError && (
                <p className="text-sm text-red-600" role="alert">
                  {searchError}
                </p>
              )}

              {!searchError && searching && <p className="text-sm text-slate-400">Searching…</p>}

              {!searchError && !searching && query.trim().length > 0 && query.trim().length < MIN_QUERY && (
                <p className="text-sm text-slate-400">Type at least {MIN_QUERY} characters to search.</p>
              )}

              {!searchError && !searching && searched && hits.length === 0 && query.trim().length >= MIN_QUERY && (
                <p className="text-sm text-slate-400">No new users or groups match “{query.trim()}”.</p>
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
                            isSelected ? 'bg-blue-50/70 ring-1 ring-inset ring-blue-200' : 'hover:bg-slate-50'
                          }`}
                        >
                          <AgentAvatar name={hit.display_name} size="sm" />
                          <span className="min-w-0 flex-1">
                            <span className="block text-sm font-medium text-slate-800 truncate">
                              {hit.display_name}
                            </span>
                            {hit.mail && (
                              <span className="block text-xs text-slate-400 truncate">{hit.mail}</span>
                            )}
                          </span>
                        </button>
                      </li>
                    );
                  })}
                </ul>
              )}
            </div>
          </div>

          <div>
            <label
              htmlFor="add-user-role"
              className="block text-[11px] uppercase tracking-wide text-slate-400 font-medium mb-1"
            >
              Role
            </label>
            <select
              id="add-user-role"
              value={role}
              onChange={(e) => setRole(e.target.value as RoleChange['role'])}
              className="w-full px-3 py-1.5 text-sm rounded-lg border border-slate-300 bg-white focus:outline-none focus:ring-2 focus:ring-blue-500/40"
            >
              {PLATFORM_ROLES.map((r) => (
                <option key={r} value={r}>
                  {ROLE_BADGE[r].label}
                </option>
              ))}
            </select>
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
            {actionPending ? 'Adding…' : 'Confirm'}
          </button>
        </div>
      </div>
    </div>
  );
}
