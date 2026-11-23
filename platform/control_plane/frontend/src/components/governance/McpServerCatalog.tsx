import { useMemo, useState } from 'react';
import { Link, useNavigate } from 'react-router-dom';
import { mcpServersApi } from '../../api/client';
import type { LifecycleState, McpServer, Kind } from '../../api/client';
import { useLoad } from '../../api/useLoad';
import { useUser } from '../../contexts/useUser';
import { AgentAvatar } from './agentUi';
import { lifecycleBadge, emailAlias, kindBadge, tenantBadge } from './agentBadges';
import { mcpEndpoint } from './mcpRepoOwnership';
import { resolveTenantName } from './tenantUi';
import { useTenantDirectory } from './useTenantDirectory';

const NO_SERVERS: McpServer[] = [];

const LIFECYCLE_OPTIONS: { value: LifecycleState; label: string }[] = [
  { value: 'proposed', label: 'Proposed' },
  { value: 'pending_approval', label: 'Pending approval' },
  { value: 'approved', label: 'Approved' },
  { value: 'rejected', label: 'Rejected' },
  { value: 'deprecated', label: 'Deprecated' },
];

const KIND_OPTIONS: { value: Kind; label: string }[] = [
  { value: 'gateway', label: 'Gateway' },
  { value: 'standard', label: 'Standard' },
];

export default function McpServerCatalog() {
  const navigate = useNavigate();

  const { user } = useUser();
  const tenantDirectory = useTenantDirectory((user?.role_level ?? 0) >= 2);

  const [filterLifecycle, setFilterLifecycle] = useState<'all' | LifecycleState>('all');
  const [search, setSearch] = useState('');
  const [filterKind, setFilterKind] = useState<'all' | Kind>('all');
  const [filterBusinessUnit, setFilterBusinessUnit] = useState<string>('all');

  const { state, reload } = useLoad(
    () =>
      mcpServersApi.list(filterLifecycle === 'all' ? undefined : { lifecycle_state: filterLifecycle }),
    [filterLifecycle],
    'Failed to load MCP servers.',
  );
  const mcpServers = state.data ?? NO_SERVERS;
  const loading = state.kind === 'loading';

  const businessUnits = useMemo(() => {
    const set = new Set<string>();
    for (const m of mcpServers) if (m.business_unit) set.add(m.business_unit);
    return Array.from(set).sort((x, y) => x.localeCompare(y));
  }, [mcpServers]);

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
                onChange={(e) => setFilterKind(e.target.value as 'all' | Kind)}
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

        {state.kind === 'error' ? (
          <div className="bg-white/70 backdrop-blur rounded-xl border border-red-200/70 shadow-sm p-6">
            <h3 className="text-sm font-semibold text-red-700">Couldn’t load MCP servers</h3>
            <p className="text-sm text-slate-600 mt-1">{state.message}</p>
            <button
              onClick={reload}
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
                    <tr
                      key={m.id}
                      onClick={(e) => {
                        if ((e.target as HTMLElement).closest('a')) return;
                        navigate(`/mcp-servers/${m.id}`);
                      }}
                      role="button"
                      tabIndex={0}
                      aria-label={`Open ${m.name}`}
                      onKeyDown={(e) => {
                        if ((e.target as HTMLElement).closest('a')) return;
                        if (e.key === 'Enter' || e.key === ' ') {
                          e.preventDefault();
                          navigate(`/mcp-servers/${m.id}`);
                        }
                      }}
                      className="cursor-pointer hover:bg-blue-50/40 transition-colors"
                    >
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
                              {mcpEndpoint(m)?.url ?? 'No endpoint set'}
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
