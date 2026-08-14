import { useEffect, useMemo, useState } from 'react';
import { Link, useNavigate } from 'react-router-dom';
import { mcpServersApi } from '../../api/client';
import type { McpServer, LifecycleState, McpServerKind } from '../../api/client';
import { useUser } from '../../contexts/UserContext';
import { AgentAvatar, lifecycleBadge, emailAlias, kindBadge, tenantBadge } from './agentUi';
import { resolveTenantName } from './tenantUi';
import { useTenantDirectory } from './useTenantDirectory';

const LIFECYCLE_OPTIONS: { value: LifecycleState; label: string }[] = [
  { value: 'proposed', label: 'Proposed' },
  { value: 'pending_approval', label: 'Pending approval' },
  { value: 'approved', label: 'Approved' },
  { value: 'rejected', label: 'Rejected' },
  { value: 'deprecated', label: 'Deprecated' },
];

const KIND_OPTIONS: { value: McpServerKind; label: string }[] = [
  { value: 'gateway', label: 'Gateway' },
  { value: 'standard', label: 'Standard' },
];

export default function McpServerCatalog() {
  const navigate = useNavigate();

  // Tenant name resolution (E24): rows carry tenant_id; names come from the
  // caller's memberships, widened by the admin directory when available.
  const { user } = useUser();
  const tenantDirectory = useTenantDirectory((user?.role_level ?? 0) >= 2);

  const [mcpServers, setMcpServers] = useState<McpServer[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  // server-side filter (re-fetches)
  const [filterLifecycle, setFilterLifecycle] = useState<'all' | LifecycleState>('all');
  // client-side filters
  const [search, setSearch] = useState('');
  const [filterKind, setFilterKind] = useState<'all' | McpServerKind>('all');
  const [filterBusinessUnit, setFilterBusinessUnit] = useState<string>('all');

  // Bumped to force a re-fetch (e.g. Retry) even when filters are unchanged.
  const [reloadNonce, setReloadNonce] = useState(0);

  // Load (re-fetches when the server-side lifecycle filter changes).
  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    mcpServersApi
      .list(filterLifecycle === 'all' ? undefined : { lifecycle_state: filterLifecycle })
      .then((res) => {
        if (cancelled) return;
        setMcpServers(res);
        setError(null);
      })
      .catch((err: unknown) => {
        if (cancelled) return;
        setError(err instanceof Error ? err.message : 'Failed to load MCP servers.');
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
    for (const m of mcpServers) if (m.business_unit) set.add(m.business_unit);
    return Array.from(set).sort((x, y) => x.localeCompare(y));
  }, [mcpServers]);

  // Client-side filters: text search (name + owner_email), kind, business_unit.
  const rows = useMemo(() => {
    const q = search.trim().toLowerCase();
    return mcpServers.filter((m) => {
      if (q) {
        const haystack = `${m.name} ${m.owner_email ?? ''}`.toLowerCase();
        if (!haystack.includes(q)) return false;
      }
      if (filterKind !== 'all' && m.kind !== filterKind) return false;
      if (filterBusinessUnit !== 'all' && m.business_unit !== filterBusinessUnit) return false;
      return true;
    });
  }, [mcpServers, search, filterKind, filterBusinessUnit]);

  const hasActiveClientFilter =
    search.trim() !== '' || filterKind !== 'all' || filterBusinessUnit !== 'all';

  return (
    <div>
      <div className="flex items-center justify-between mb-3">
        <p className="text-xs text-slate-500">
          Every governed MCP server. Gateway-managed and standard read as one catalog.
        </p>
        <button
          onClick={() => navigate('/mcp-servers/new')}
          className="px-3 py-1.5 rounded-lg bg-white border border-slate-300 text-slate-700 text-sm font-medium hover:bg-slate-50 transition-colors"
        >
          + Register
        </button>
      </div>

        {/* Compact filter row (M365-style: a single tight band above the table). */}
        <div className="bg-white/70 backdrop-blur rounded-xl border border-slate-200/60 shadow-sm p-2.5 mb-3">
          <div className="grid grid-cols-1 md:grid-cols-12 gap-2">
            <div className="md:col-span-5">
              <input
                value={search}
                onChange={(e) => setSearch(e.target.value)}
                placeholder="Search by name or owner…"
                aria-label="Search MCP servers by name or owner"
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
                aria-label="Filter by kind"
                className="w-full px-3 py-1.5 text-sm rounded-lg border border-slate-300 bg-white"
                value={filterKind}
                onChange={(e) => setFilterKind(e.target.value as 'all' | McpServerKind)}
              >
                <option value="all">All kinds</option>
                {KIND_OPTIONS.map((o) => (
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
            <h3 className="text-sm font-semibold text-red-700">Couldn’t load MCP servers</h3>
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
                  <th className="text-left font-medium px-4 py-2">MCP server</th>
                  <th className="text-left font-medium px-4 py-2">Kind</th>
                  <th className="text-left font-medium px-4 py-2">Tenant</th>
                  <th className="text-left font-medium px-4 py-2">Owner</th>
                  <th className="text-left font-medium px-4 py-2">Line of Business</th>
                  <th className="text-left font-medium px-4 py-2">Lifecycle</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-slate-100">
                {loading && (
                  <tr>
                    <td colSpan={6} className="px-4 py-8 text-center text-slate-400 text-sm">
                      Loading MCP servers…
                    </td>
                  </tr>
                )}

                {!loading && rows.map((m) => {
                  const kind = kindBadge(m.kind);
                  const lifecycle = lifecycleBadge(m.lifecycle_state);
                  const tenant = tenantBadge(
                    resolveTenantName(m.tenant_id, user?.tenants, tenantDirectory),
                  );
                  return (
                    <tr key={m.id} className="hover:bg-blue-50/40 transition-colors">
                      {/* MCP server: avatar + name link + tiny endpoint sublabel. */}
                      <td className="px-4 py-2">
                        <div className="flex items-center gap-2.5">
                          <AgentAvatar name={m.name} size="sm" />
                          <div className="min-w-0 leading-tight">
                            <Link
                              to={`/mcp-servers/${m.id}`}
                              className="block font-medium text-blue-700 hover:underline truncate"
                            >
                              {m.name}
                            </Link>
                            <span className="block text-xs text-slate-500 truncate">
                              {m.endpoint_url ? m.endpoint_url : 'No endpoint set'}
                            </span>
                          </div>
                        </div>
                      </td>
                      <td className="px-4 py-2">
                        <span
                          className={`inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium ${kind.cls}`}
                        >
                          {kind.label}
                        </span>
                      </td>
                      <td className="px-4 py-2">
                        <span
                          className={`inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium ${tenant.cls}`}
                          title={m.tenant_id ?? undefined}
                        >
                          {tenant.label}
                        </span>
                      </td>
                      <td
                        className="px-4 py-2 text-slate-600"
                        title={m.owner_email ?? undefined}
                      >
                        {emailAlias(m.owner_email)}
                      </td>
                      <td className="px-4 py-2 text-slate-600">{m.business_unit ?? '—'}</td>
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
                      {mcpServers.length === 0
                        ? 'No MCP servers registered yet. Use “+ Register” to add one.'
                        : hasActiveClientFilter
                          ? 'No MCP servers match the current filters.'
                          : 'No MCP servers to show.'}
                    </td>
                  </tr>
                )}
              </tbody>
            </table>
          </div>
        )}
    </div>
  );
}
