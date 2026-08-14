import { useEffect, useMemo, useState } from 'react';
import { Link, useNavigate } from 'react-router-dom';
import { agentsApi } from '../../api/client';
import type { Agent, LifecycleState, Platform } from '../../api/client';
import { useUser } from '../../contexts/UserContext';
import { AgentAvatar, lifecycleBadge, originBadge, emailAlias, tenantBadge } from './agentUi';
import { resolveTenantName } from './tenantUi';
import { useTenantDirectory } from './useTenantDirectory';
import { PLATFORM_OPTIONS, platformLabelOr } from './platformLabels';

const LIFECYCLE_OPTIONS: { value: LifecycleState; label: string }[] = [
  { value: 'proposed', label: 'Proposed' },
  { value: 'pending_approval', label: 'Pending approval' },
  { value: 'approved', label: 'Approved' },
  { value: 'rejected', label: 'Rejected' },
  { value: 'deprecated', label: 'Deprecated' },
];

// The filter menu's options AND the per-row sublabel under the agent name (M365 "Foundry"-style)
// both come from the ONE shared platform map (E29/T9). This file previously declared the options
// inline and derived a local label map from them — the same two-vocabularies-by-hand arrangement
// that had to be kept in sync across five files.

export default function AgentsList() {
  const navigate = useNavigate();

  // Tenant name resolution (E24): rows carry tenant_id; names come from the
  // caller's memberships, widened by the admin directory when available.
  const { user } = useUser();
  const tenantDirectory = useTenantDirectory((user?.role_level ?? 0) >= 2);

  const [agents, setAgents] = useState<Agent[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  // server-side filter (re-fetches)
  const [filterLifecycle, setFilterLifecycle] = useState<'all' | LifecycleState>('all');
  // client-side filters
  const [search, setSearch] = useState('');
  const [filterPlatform, setFilterPlatform] = useState<'all' | Platform>('all');
  const [filterBusinessUnit, setFilterBusinessUnit] = useState<string>('all');

  // Bumped to force a re-fetch (e.g. Retry) even when filters are unchanged.
  const [reloadNonce, setReloadNonce] = useState(0);

  // Load (re-fetches when the server-side lifecycle filter changes).
  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    agentsApi
      .list(filterLifecycle === 'all' ? undefined : { lifecycle_state: filterLifecycle })
      .then((res) => {
        if (cancelled) return;
        setAgents(res);
        setError(null);
      })
      .catch((err: unknown) => {
        if (cancelled) return;
        setError(err instanceof Error ? err.message : 'Failed to load agents.');
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [filterLifecycle, reloadNonce]);

  // Distinct Lines of Business from the loaded rows (cleaner than a static list).
  const businessUnits = useMemo(() => {
    const set = new Set<string>();
    for (const a of agents) if (a.business_unit) set.add(a.business_unit);
    return Array.from(set).sort((x, y) => x.localeCompare(y));
  }, [agents]);

  // Client-side filters: text search (name + sponsor_email), platform, business_unit.
  // Real registry records only — Epic 31F removed the synthetic "just deployed"
  // row this list used to prepend from the Operations demo store, because a
  // fabricated row in the registry is indistinguishable from a governed agent.
  const rows = useMemo(() => {
    const q = search.trim().toLowerCase();
    return agents.filter((a) => {
      if (q) {
        const haystack = `${a.name} ${a.sponsor_email ?? ''}`.toLowerCase();
        if (!haystack.includes(q)) return false;
      }
      if (filterPlatform !== 'all' && a.platform !== filterPlatform) return false;
      if (filterBusinessUnit !== 'all' && a.business_unit !== filterBusinessUnit) return false;
      return true;
    });
  }, [agents, search, filterPlatform, filterBusinessUnit]);

  const hasActiveClientFilter =
    search.trim() !== '' || filterPlatform !== 'all' || filterBusinessUnit !== 'all';

  return (
    <div className="min-h-[calc(100vh-4rem)] relative">
      <div className="relative max-w-7xl mx-auto px-6 py-6">
        <div className="flex items-start justify-between mb-4">
          <div>
            <h1 className="text-xl font-semibold text-slate-900">All Agents</h1>
            <p className="text-xs text-slate-500 mt-0.5">
              Every governed agent. “How it got here” is a column, not a section.
            </p>
          </div>
          <div className="flex gap-2">
            <button
              onClick={() => navigate('/agents/new')}
              className="px-3 py-1.5 rounded-lg bg-white border border-slate-300 text-slate-700 text-sm font-medium hover:bg-slate-50 transition-colors"
            >
              + Register
            </button>
          </div>
        </div>

        {/* Compact filter row (M365-style: a single tight band above the table). */}
        <div className="bg-white/70 backdrop-blur rounded-xl border border-slate-200/60 shadow-sm p-2.5 mb-3">
          <div className="grid grid-cols-1 md:grid-cols-12 gap-2">
            <div className="md:col-span-5">
              <input
                value={search}
                onChange={(e) => setSearch(e.target.value)}
                placeholder="Search by name or owner…"
                aria-label="Search agents by name or owner"
                className="w-full px-3 py-1.5 text-sm rounded-lg border border-slate-300 focus:outline-none focus:ring-2 focus:ring-blue-500/40"
              />
            </div>
            <div className="md:col-span-3">
              <select
                aria-label="Filter by lifecycle state"
                className="w-full px-3 py-1.5 text-sm rounded-lg border border-slate-300 bg-white"
                value={filterLifecycle}
                onChange={(e) => setFilterLifecycle(e.target.value as 'all' | LifecycleState)}
              >
                <option value="all">All lifecycle states</option>
                {LIFECYCLE_OPTIONS.map((o) => (
                  <option key={o.value} value={o.value}>{o.label}</option>
                ))}
              </select>
            </div>
            <div className="md:col-span-2">
              <select
                aria-label="Filter by platform"
                className="w-full px-3 py-1.5 text-sm rounded-lg border border-slate-300 bg-white"
                value={filterPlatform}
                onChange={(e) => setFilterPlatform(e.target.value as 'all' | Platform)}
              >
                <option value="all">All platforms</option>
                {PLATFORM_OPTIONS.map((o) => (
                  <option key={o.value} value={o.value}>{o.label}</option>
                ))}
              </select>
            </div>
            <div className="md:col-span-2">
              <select
                aria-label="Filter by Line of Business"
                className="w-full px-3 py-1.5 text-sm rounded-lg border border-slate-300 bg-white"
                value={filterBusinessUnit}
                onChange={(e) => setFilterBusinessUnit(e.target.value)}
              >
                <option value="all">All Lines of Business</option>
                {businessUnits.map((bu) => (
                  <option key={bu} value={bu}>{bu}</option>
                ))}
              </select>
            </div>
          </div>
        </div>

        {error ? (
          <div className="bg-white/70 backdrop-blur rounded-xl border border-red-200/70 shadow-sm p-6">
            <h3 className="text-sm font-semibold text-red-700">Couldn’t load agents</h3>
            <p className="text-sm text-slate-600 mt-1">{error}</p>
            <button
              onClick={() => setReloadNonce((n) => n + 1)}
              className="mt-3 px-3 py-1.5 rounded-lg bg-white border border-slate-300 text-slate-700 text-xs font-medium hover:bg-slate-50 transition-colors"
            >
              Retry
            </button>
          </div>
        ) : (
          <div className="bg-white/70 backdrop-blur rounded-xl border border-slate-200/60 overflow-hidden shadow-sm">
            <table className="w-full text-sm">
              <thead className="bg-slate-50/80 text-slate-500 text-xs uppercase tracking-wide">
                <tr>
                  <th className="text-left font-medium px-4 py-2">Agent</th>
                  <th className="text-left font-medium px-4 py-2">Tenant</th>
                  <th className="text-left font-medium px-4 py-2">Line of Business</th>
                  <th className="text-left font-medium px-4 py-2">Owner</th>
                  <th className="text-left font-medium px-4 py-2">How it got here</th>
                  <th className="text-left font-medium px-4 py-2">Lifecycle</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-slate-100">
                {loading && (
                  <tr>
                    <td colSpan={6} className="px-4 py-8 text-center text-slate-400 text-sm">
                      Loading agents…
                    </td>
                  </tr>
                )}

                {!loading && rows.map((a) => {
                  const origin = originBadge(a.origin);
                  const lifecycle = lifecycleBadge(a.lifecycle_state);
                  const tenant = tenantBadge(
                    resolveTenantName(a.tenant_id, user?.tenants, tenantDirectory),
                  );
                  return (
                    <tr key={a.id} className="hover:bg-blue-50/40 transition-colors">
                      {/* Agent: avatar + name link + tiny platform sublabel (M365 idiom). */}
                      <td className="px-4 py-2">
                        <div className="flex items-center gap-2.5">
                          <AgentAvatar name={a.name} size="sm" />
                          <div className="min-w-0 leading-tight">
                            <div className="flex items-center gap-1.5 min-w-0">
                              <Link
                                to={`/agents/${a.id}`}
                                className="font-medium text-blue-700 hover:underline truncate"
                              >
                                {a.name}
                              </Link>
                            </div>
                            <span className="block text-xs text-slate-500 truncate">
                              {platformLabelOr(a.platform, 'Platform not set')}
                            </span>
                          </div>
                        </div>
                      </td>
                      <td className="px-4 py-2">
                        <span
                          className={`inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium ${tenant.cls}`}
                          title={a.tenant_id ?? undefined}
                        >
                          {tenant.label}
                        </span>
                      </td>
                      <td className="px-4 py-2 text-slate-600">{a.business_unit ?? '—'}</td>
                      <td
                        className="px-4 py-2 text-slate-600"
                        title={a.sponsor_email ?? undefined}
                      >
                        {emailAlias(a.sponsor_email)}
                      </td>
                      <td className="px-4 py-2">
                        <span
                          className={`inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium ${origin.cls}`}
                        >
                          {origin.label}
                        </span>
                      </td>
                      <td className="px-4 py-2">
                        <span
                          className={`inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium ${lifecycle.cls}`}
                        >
                          {lifecycle.label}
                        </span>
                      </td>
                    </tr>
                  );
                })}

                {!loading && rows.length === 0 && (
                  <tr>
                    <td colSpan={6} className="px-4 py-8 text-center text-slate-400 text-sm">
                      {agents.length === 0
                        ? 'No agents registered yet. Use “+ Register” to add one.'
                        : hasActiveClientFilter
                          ? 'No agents match the current filters.'
                          : 'No agents to show.'}
                    </td>
                  </tr>
                )}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </div>
  );
}
