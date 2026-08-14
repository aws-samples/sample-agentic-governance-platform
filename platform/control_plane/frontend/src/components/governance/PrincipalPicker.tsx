// PrincipalPicker — Graph-backed user/group search picker (Epic 6, T-FE-ACCESS).
//
// A debounced search box (→ principalsApi.search) that lists Entra users/groups,
// then reveals an Invoker/Admin role select on selection before confirming the
// grant. Re-skinned from the Microsoft Agent-365 "add people" flyout into the
// governance slate idiom: glass card chrome, slate neutrals, a single blue primary
// action, tinted User/Group pills, and a unicode ✕ (no icon library, no new deps).
//
// Agent mode (Epic 7, T-FE-MCP-ACCESS) — additive: when `agentMode` is set the
// picker sources AGENTS from the registry (agentsApi.list(), filtered to
// identity_status==='provisioned') instead of Graph users/groups, and on pick
// returns { id: agent.entra_sp_id, display_name: agent.name, type: 'agent' }.
// The E6 user/group path (no agentMode) is unchanged.

import { useEffect, useMemo, useRef, useState } from 'react';
import { agentsApi, principalsApi } from '../../api/client';
import type { PrincipalHit } from '../../api/client';

// Card chrome — copied verbatim from AgentDetail's CARD constant so the picker
// reads as part of the same surface family.
const CARD = 'bg-white/70 backdrop-blur rounded-xl border border-slate-200/60 shadow-sm';

const MIN_QUERY = 2;
const DEBOUNCE_MS = 300;

// User=blue, Group=violet — distinct tints so the two principal kinds read
// apart at a glance (mirrors the badge idiom in agentUi).
function principalTypeBadge(type: 'user' | 'group' | 'agent'): { cls: string; label: string } {
  if (type === 'group') return { cls: 'bg-violet-50 text-violet-700', label: 'Group' };
  if (type === 'agent') return { cls: 'bg-indigo-50 text-indigo-700', label: 'Agent' };
  return { cls: 'bg-blue-50 text-blue-700', label: 'User' };
}

// Initials for the search-result avatar — first letter of the first 1-2
// whitespace segments of the display name (mirrors agentUi's initialsFor).
function principalInitials(name: string): string {
  const trimmed = name.trim();
  if (!trimmed) return '?';
  const segments = trimmed.split(/\s+/).filter(Boolean);
  if (segments.length >= 2) return (segments[0][0] + segments[1][0]).toUpperCase();
  return trimmed.slice(0, 2).toUpperCase();
}

export default function PrincipalPicker({
  onPick,
  onClose,
  pending = false,
  agentMode = false,
}: {
  onPick: (p: PrincipalHit, role: 'Invoker' | 'Admin') => void;
  onClose: () => void;
  /** When true, the parent's add call is in-flight — disable the Grant button. */
  pending?: boolean;
  /**
   * Agent mode (Epic 7) — additive. When true, the picker sources provisioned
   * agents from the registry instead of Graph users/groups. The E6 user/group
   * path (default false) is unchanged.
   */
  agentMode?: boolean;
}) {
  const [query, setQuery] = useState('');
  const [results, setResults] = useState<PrincipalHit[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // Whether a search has run at least once for the current query — gates the
  // "No matches" empty state so it doesn't flash before the first fetch.
  // (User/group mode only — agent mode shows the full provisioned list eagerly.)
  const [searched, setSearched] = useState(false);

  const [selected, setSelected] = useState<PrincipalHit | null>(null);
  const [role, setRole] = useState<'Invoker' | 'Admin'>('Invoker');

  const inputRef = useRef<HTMLInputElement>(null);

  // Focus the search box on open.
  useEffect(() => {
    inputRef.current?.focus();
  }, []);

  // -- agent mode: list provisioned agents once on open, filter client-side ---
  // Agents are a small, bounded set (the registry), so unlike Graph search we
  // fetch the whole provisioned list once and filter locally — no debounce, no
  // per-keystroke fetch.
  const [agentHits, setAgentHits] = useState<PrincipalHit[]>([]);
  useEffect(() => {
    if (!agentMode) return;
    let cancelled = false;
    setLoading(true);
    setError(null);
    agentsApi
      .list()
      .then((agents) => {
        if (cancelled) return;
        const hits: PrincipalHit[] = agents
          .filter((a) => a.identity_status === 'provisioned' && !!a.entra_sp_id)
          .map((a) => ({ id: a.entra_sp_id as string, display_name: a.name, type: 'agent' as const }));
        setAgentHits(hits);
      })
      .catch((err: unknown) => {
        if (cancelled) return;
        setError(err instanceof Error ? err.message : 'Failed to load agents.');
        setAgentHits([]);
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [agentMode]);

  // Client-side filter of the provisioned-agent list by the query (agent mode).
  const filteredAgentHits = useMemo(() => {
    if (!agentMode) return [];
    const q = query.trim().toLowerCase();
    if (!q) return agentHits;
    return agentHits.filter((h) => h.display_name.toLowerCase().includes(q));
  }, [agentMode, agentHits, query]);

  // Debounced Graph search (user/group mode ONLY — agent mode never hits Graph).
  // A short query (< MIN_QUERY) clears results without hitting Graph; each
  // keystroke cancels the prior in-flight debounce.
  useEffect(() => {
    if (agentMode) return;
    const q = query.trim();
    if (q.length < MIN_QUERY) {
      setResults([]);
      setSearched(false);
      setError(null);
      setLoading(false);
      return;
    }

    let cancelled = false;
    setLoading(true);
    setError(null);
    const handle = window.setTimeout(async () => {
      try {
        const hits = await principalsApi.search(q);
        if (cancelled) return;
        setResults(hits);
      } catch (err: unknown) {
        if (cancelled) return;
        // The axios interceptor surfaces the backend `detail` as err.message.
        setError(err instanceof Error ? err.message : 'Search failed.');
        setResults([]);
      } finally {
        if (!cancelled) {
          setLoading(false);
          setSearched(true);
        }
      }
    }, DEBOUNCE_MS);

    return () => {
      cancelled = true;
      window.clearTimeout(handle);
    };
  }, [query, agentMode]);

  const selectedBadge = selected ? principalTypeBadge(selected.type) : null;

  // The list rendered below: provisioned agents (filtered client-side) in agent
  // mode, else the Graph user/group search hits.
  const displayResults = agentMode ? filteredAgentHits : results;

  return (
    <div className={`${CARD} p-4`} role="dialog" aria-label={agentMode ? 'Grant agent access' : 'Add principal'}>
      {/* Header: title + close. */}
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

      {/* Search box. */}
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
          // Re-opening the search clears any prior pending selection.
          setSelected(null);
        }}
        placeholder={agentMode ? 'Filter agents by name…' : 'Search users and groups by name…'}
        className="w-full px-3 py-2 text-sm rounded-lg border border-slate-300 focus:outline-none focus:ring-2 focus:ring-blue-500/40"
        autoComplete="off"
      />

      {/* Results region. */}
      <div className="mt-3">
        {error && (
          <p className="text-sm text-red-600" role="alert">{error}</p>
        )}

        {!error && loading && (
          <p className="text-sm text-slate-400">{agentMode ? 'Loading agents…' : 'Searching…'}</p>
        )}

        {/* Agent mode empty states (no Graph search; the full list is loaded). */}
        {agentMode && !error && !loading && agentHits.length === 0 && (
          <p className="text-sm text-slate-400">No provisioned agents are available to grant.</p>
        )}
        {agentMode && !error && !loading && agentHits.length > 0 && filteredAgentHits.length === 0 && (
          <p className="text-sm text-slate-400">No agents match “{query.trim()}”.</p>
        )}

        {/* User/group mode empty + min-query states (Graph search). */}
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
                      {principalInitials(hit.display_name)}
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

      {/* Role select + confirm — revealed once a principal is chosen. */}
      {selected && selectedBadge && (
        <div className="mt-3 pt-3 border-t border-slate-200/70">
          <div className="flex items-center gap-2 mb-3 text-sm text-slate-600">
            <span className="text-slate-400">Granting</span>
            <span className="font-medium text-slate-800 truncate">{selected.display_name}</span>
            <span className={`shrink-0 inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium ${selectedBadge.cls}`}>
              {selectedBadge.label}
            </span>
          </div>

          {/* Nested-group caveat surfaces the moment a Group is chosen (Entra
              app-role assignments don't transit nested groups). */}
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
