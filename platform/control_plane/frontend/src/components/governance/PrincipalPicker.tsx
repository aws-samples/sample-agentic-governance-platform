import { useEffect, useMemo, useRef, useState } from 'react';
import { agentsApi, principalsApi } from '../../api/client';
import type { PrincipalHit } from '../../api/client';
import { useLoad } from '../../api/useLoad';
import { initialsFor } from '../shared/initials';

const CARD = 'bg-white/70 backdrop-blur rounded-xl border border-slate-200/60 shadow-sm';

const MIN_QUERY = 2;
const DEBOUNCE_MS = 300;

export type PickerHit = Omit<PrincipalHit, 'type'> & { type: PrincipalHit['type'] | 'agent' };

interface DirectorySearch {
  query: string;
  hits: PrincipalHit[];
  error: string | null;
}

function principalTypeBadge(type: string): { cls: string; label: string } {
  if (type === 'group') return { cls: 'bg-violet-50 text-violet-700', label: 'Group' };
  if (type === 'agent') return { cls: 'bg-indigo-50 text-indigo-700', label: 'Agent' };
  return { cls: 'bg-blue-50 text-blue-700', label: 'User' };
}

export default function PrincipalPicker({
  onPick,
  onClose,
  pending = false,
  agentMode = false,
}: {
  onPick: (p: PickerHit, role: 'Invoker' | 'Admin') => void;
  onClose: () => void;
  pending?: boolean;
  agentMode?: boolean;
}) {
  const [query, setQuery] = useState('');
  const [settled, setSettled] = useState<DirectorySearch | null>(null);

  const [selected, setSelected] = useState<PickerHit | null>(null);
  const [role, setRole] = useState<'Invoker' | 'Admin'>('Invoker');

  const inputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    inputRef.current?.focus();
  }, []);

  const agentHitsLoad = useLoad(
    () =>
      agentMode
        ? agentsApi.list().then((agents): PickerHit[] =>
            agents
              .filter((a) => a.identity_status === 'provisioned' && !!a.entra_sp_id)
              .map((a) => ({ id: a.entra_sp_id as string, display_name: a.name, type: 'agent', mail: null })),
          )
        : Promise.resolve<PickerHit[]>([]),
    [agentMode],
    'Failed to load agents.',
  );
  const agentHits = agentHitsLoad.state.data;
  const agentHitCount = agentHits?.length ?? 0;

  const filteredAgentHits = useMemo(() => {
    if (!agentMode || agentHits === null) return [];
    const q = query.trim().toLowerCase();
    if (!q) return agentHits;
    return agentHits.filter((h) => h.display_name.toLowerCase().includes(q));
  }, [agentMode, agentHits, query]);

  // Decision: docs/adr/frontend-loading-state.md
  useEffect(() => {
    if (agentMode) return;
    const q = query.trim();
    if (q.length < MIN_QUERY) return;

    let cancelled = false;
    const handle = window.setTimeout(async () => {
      try {
        const hits = await principalsApi.search(q);
        if (cancelled) return;
        setSettled({ query: q, hits, error: null });
      } catch (err: unknown) {
        if (cancelled) return;
        setSettled({ query: q, hits: [], error: err instanceof Error ? err.message : 'Search failed.' });
      }
    }, DEBOUNCE_MS);

    return () => {
      cancelled = true;
      window.clearTimeout(handle);
    };
  }, [query, agentMode]);

  const trimmedQuery = query.trim();
  const searchable = !agentMode && trimmedQuery.length >= MIN_QUERY;
  const current = searchable && settled?.query === trimmedQuery ? settled : null;
  const results = current?.hits ?? [];
  const searched = current !== null;
  const loading = agentMode ? agentHitsLoad.state.kind === 'loading' : searchable && current === null;
  const error = agentMode
    ? agentHitsLoad.state.kind === 'error'
      ? agentHitsLoad.state.message
      : null
    : current?.error ?? null;

  const selectedBadge = selected ? principalTypeBadge(selected.type) : null;

  const displayResults = agentMode ? filteredAgentHits : results;

  return (
    <div className={`${CARD} p-4`} role="dialog" aria-label={agentMode ? 'Grant agent access' : 'Add principal'}>
      <div className="flex items-center justify-between gap-3 mb-3">
        <h3 className="text-sm font-semibold text-slate-800">{agentMode ? 'Grant agent access' : 'Add principal'}</h3>
        <button
          type="button"
          onClick={onClose}
          aria-label="Close picker"
          className="shrink-0 inline-flex items-center justify-center h-6 w-6 rounded-md text-slate-400 hover:text-slate-700 hover:bg-slate-100 transition-colors"
        >
          <span aria-hidden="true" className="text-base leading-none">×</span>
        </button>
      </div>

      <label htmlFor="principal-search" className="sr-only">
        {agentMode ? 'Filter agents' : 'Search users and groups'}
      </label>
      <input
        id="principal-search"
        ref={inputRef}
        type="text"
        value={query}
        onChange={(e) => {
          setQuery(e.target.value);
          setSelected(null);
        }}
        placeholder={agentMode ? 'Filter agents by name…' : 'Search users and groups by name…'}
        className="w-full px-3 py-2 text-sm rounded-lg border border-slate-300 focus:outline-none focus:ring-2 focus:ring-blue-500/40"
        autoComplete="off"
      />

      <div className="mt-3">
        {error && (
          <p className="text-sm text-red-600" role="alert">{error}</p>
        )}

        {!error && loading && (
          <p className="text-sm text-slate-400">{agentMode ? 'Loading agents…' : 'Searching…'}</p>
        )}

        {agentMode && !error && !loading && agentHitCount === 0 && (
          <p className="text-sm text-slate-400">No provisioned agents are available to grant.</p>
        )}
        {agentMode && !error && !loading && agentHitCount > 0 && filteredAgentHits.length === 0 && (
          <p className="text-sm text-slate-400">No agents match “{query.trim()}”.</p>
        )}

        {!agentMode && !error && !loading && query.trim().length > 0 && query.trim().length < MIN_QUERY && (
          <p className="text-sm text-slate-400">Type at least {MIN_QUERY} characters to search.</p>
        )}

        {!agentMode && !error && !loading && searched && results.length === 0 && query.trim().length >= MIN_QUERY && (
          <p className="text-sm text-slate-400">No users or groups match “{query.trim()}”.</p>
        )}

        {!error && !loading && displayResults.length > 0 && (
          <ul className="max-h-64 overflow-y-auto -mx-1 divide-y divide-slate-100">
            {displayResults.map((hit) => {
              const badge = principalTypeBadge(hit.type);
              const isSelected = selected?.id === hit.id;
              return (
                <li key={hit.id}>
                  <button
                    type="button"
                    onClick={() => {
                      setSelected(hit);
                      setRole('Invoker');
                    }}
                    aria-pressed={isSelected}
                    className={`w-full flex items-center gap-3 px-2 py-2 rounded-lg text-left transition-colors ${
                      isSelected ? 'bg-blue-50/70 ring-1 ring-inset ring-blue-200' : 'hover:bg-slate-50'
                    }`}
                  >
                    <span
                      aria-hidden="true"
                      className={`h-8 w-8 rounded-lg text-xs font-semibold flex items-center justify-center shrink-0 ${badge.cls}`}
                    >
                      {initialsFor(hit.display_name)}
                    </span>
                    <span className="min-w-0 flex-1">
                      <span className="block text-sm font-medium text-slate-800 truncate">
                        {hit.display_name}
                      </span>
                      {hit.mail && (
                        <span className="block text-xs text-slate-400 truncate">{hit.mail}</span>
                      )}
                    </span>
                    <span className={`shrink-0 inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium ${badge.cls}`}>
                      {badge.label}
                    </span>
                  </button>
                </li>
              );
            })}
          </ul>
        )}
      </div>

      {selected && selectedBadge && (
        <div className="mt-3 pt-3 border-t border-slate-200/70">
          <div className="flex items-center gap-2 mb-3 text-sm text-slate-600">
            <span className="text-slate-400">Granting</span>
            <span className="font-medium text-slate-800 truncate">{selected.display_name}</span>
            <span className={`shrink-0 inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium ${selectedBadge.cls}`}>
              {selectedBadge.label}
            </span>
          </div>

          {selected.type === 'group' && (
            <p className="text-xs text-slate-400 mb-3">
              Group grants apply to direct members only — members via nested groups are not
              granted access (Entra app-role assignment limitation).
            </p>
          )}

          <div className="flex items-end gap-2 flex-wrap">
            <div>
              <label htmlFor="grant-role" className="block text-[11px] uppercase tracking-wide text-slate-400 font-medium mb-1">
                Role
              </label>
              <select
                id="grant-role"
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
              data-testid="access-grant"
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
