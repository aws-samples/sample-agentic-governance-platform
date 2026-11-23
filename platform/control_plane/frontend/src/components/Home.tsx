import { useMemo } from 'react';
import { Link, useNavigate } from 'react-router-dom';

import { agentsApi, mcpServersApi } from '../api/client';
import { useLoad } from '../api/useLoad';
import { lifecycleBadge } from './governance/agentBadges';
import { ICON } from './governance/agentsNav';

const GRADIENT_TEXT = {
  backgroundImage: 'linear-gradient(135deg, #1e40af 0%, #3b82f6 40%, #818cf8 100%)',
  WebkitBackgroundClip: 'text',
  WebkitTextFillColor: 'transparent',
  backgroundClip: 'text',
  color: 'transparent',
} as const;

const RECENT_AGENT_COUNT = 5;

interface FleetStat {
  label: string;
  value: string;
  to: string;
  attention?: boolean;
}

interface NavCard {
  title: string;
  blurb: string;
  to: string;
  icon: string;
}

const NAV_CARDS: NavCard[] = [
  {
    title: 'Agents',
    blurb: 'The single registry of every governed agent.',
    to: '/agents',
    icon: ICON.agents,
  },
  {
    title: 'MCPs',
    blurb: 'What agents consume — governed once, independently.',
    to: '/tools-mcp',
    icon: ICON.tools,
  },
  {
    title: 'Secure',
    blurb: 'Guardrails, policy, audit & incidents.',
    to: '/secure/guardrails',
    icon: ICON.shield,
  },
  {
    title: 'Observability',
    blurb: 'Fleet-wide traces and Langfuse.',
    to: '/observability',
    icon: ICON.eye,
  },
];

function GovernanceHome() {
  const navigate = useNavigate();

  const fleet = useLoad(
    () =>
      Promise.all([agentsApi.list(), mcpServersApi.list()]).then(([agents, mcpServers]) => ({
        agents,
        mcpCount: mcpServers.length,
      })),
    [],
    'Failed to load fleet counts.',
  );
  const data = fleet.state.data;
  const loading = fleet.state.kind === 'loading';
  const error = fleet.state.kind === 'error' ? fleet.state.message : null;

  const fleetStats = useMemo<FleetStat[]>(() => {
    const agents = data?.agents ?? [];
    const mcpCount = data?.mcpCount ?? 0;
    const pending = agents.filter((a) => a.lifecycle_state === 'pending_approval').length;
    const ownerless = agents.filter((a) => !a.sponsor_email || !a.sponsor_email.trim()).length;
    return [
      { label: 'Total agents', value: String(agents.length), to: '/agents/all' },
      {
        label: 'Pending approvals',
        value: String(pending),
        to: '/agents/all',
        attention: pending > 0,
      },
      { label: 'MCP servers', value: String(mcpCount), to: '/tools-mcp' },
      {
        label: 'Ownerless',
        value: String(ownerless),
        to: '/agents/all',
        attention: ownerless > 0,
      },
    ];
  }, [data]);

  const recentAgents = useMemo(
    () =>
      [...(data?.agents ?? [])]
        .sort((a, b) => b.created_at.localeCompare(a.created_at))
        .slice(0, RECENT_AGENT_COUNT),
    [data],
  );

  const errorCard = (
    <div className="bg-white/70 backdrop-blur rounded-xl border border-red-200/70 shadow-sm p-6">
      <h3 className="text-sm font-semibold text-red-700">Couldn’t load fleet counts</h3>
      <p className="text-sm text-slate-600 mt-1">{error}</p>
      <button
        type="button"
        onClick={() => fleet.reload()}
        className="mt-3 px-3 py-1.5 rounded-lg bg-white border border-slate-300 text-slate-700 text-xs font-medium hover:bg-slate-50 transition-colors"
      >
        Retry
      </button>
    </div>
  );

  return (
    <div className="min-h-[calc(100vh-4rem)] relative">
      <div className="relative max-w-7xl mx-auto px-6 py-10 animate-fade-in">
        <div className="mb-8">
          <h1 className="text-4xl font-bold tracking-tight" style={{ ...GRADIENT_TEXT, lineHeight: '1.15' }}>
            Agentic Governance Platform
          </h1>
          <p className="text-sm md:text-base text-slate-500 mt-2 max-w-2xl">
            The single control plane to govern every agent and MCP server — prove it's safe, and keep it accountable.
          </p>
        </div>

        <div className="flex items-center justify-between mb-3">
          <h2 className="text-sm font-semibold text-slate-700 uppercase tracking-wide">Fleet at a glance</h2>
        </div>
        {loading ? (
          <p className="text-slate-400 text-sm mb-10">Loading fleet counts…</p>
        ) : error ? (
          <div className="mb-10">{errorCard}</div>
        ) : (
          <div className="grid grid-cols-2 md:grid-cols-4 gap-4 mb-10">
            {fleetStats.map((s) => (
              <button
                key={s.label}
                type="button"
                onClick={() => navigate(s.to)}
                className={`text-left bg-white/70 backdrop-blur rounded-xl border p-5 shadow-sm transition-all hover:shadow-md hover:-translate-y-0.5 cursor-pointer ${s.attention ? 'border-amber-200/80' : 'border-slate-200/60'}`}
              >
                <div className={`text-3xl font-semibold ${s.attention ? 'text-amber-600' : 'text-slate-900'}`}>{s.value}</div>
                <div className="text-sm font-medium text-slate-700 mt-1 flex items-center gap-1.5 flex-wrap">
                  {s.attention && <span className="w-1.5 h-1.5 rounded-full bg-amber-400 inline-block" />}
                  {s.label}
                </div>
              </button>
            ))}
          </div>
        )}

        <div className="grid grid-cols-1 lg:grid-cols-3 gap-6 mb-10">
          <div className="lg:col-span-1 bg-white/70 backdrop-blur rounded-xl border border-slate-200/60 shadow-sm p-5 flex flex-col">
            <h2 className="text-base font-semibold text-slate-900 mb-3">Recently registered</h2>
            {loading ? (
              <p className="text-slate-400 text-sm">Loading agents…</p>
            ) : error ? (
              <p className="text-slate-500 text-sm">Agents couldn’t be loaded.</p>
            ) : recentAgents.length === 0 ? (
              <div className="flex-1">
                <p className="text-sm text-slate-500">No agents registered yet.</p>
                <Link
                  to="/agents/new"
                  className="inline-block mt-3 px-3.5 py-1.5 rounded-lg bg-blue-600 text-white text-sm font-medium hover:bg-blue-700 transition-colors"
                >
                  Register an agent
                </Link>
              </div>
            ) : (
              <ul className="space-y-2.5 flex-1">
                {recentAgents.map((a) => {
                  const badge = lifecycleBadge(a.lifecycle_state);
                  return (
                    <li key={a.id}>
                      <Link
                        to={`/agents/${a.id}`}
                        className="group flex items-center justify-between gap-2.5 text-sm text-slate-700"
                      >
                        <span className="truncate group-hover:text-blue-900 transition-colors" title={a.name}>
                          {a.name}
                        </span>
                        <span className={`px-2 py-0.5 rounded-full text-[11px] font-medium shrink-0 ${badge.cls}`}>
                          {badge.label}
                        </span>
                      </Link>
                    </li>
                  );
                })}
              </ul>
            )}
          </div>

          <div className="lg:col-span-2">
            <h2 className="text-base font-semibold text-slate-900 mb-3">Jump to</h2>
            <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
              {NAV_CARDS.map((c) => (
                <button
                  type="button"
                  key={c.title}
                  onClick={() => navigate(c.to)}
                  className="group w-full text-left bg-white/70 backdrop-blur rounded-xl border border-slate-200/60 shadow-sm home-card cursor-pointer p-5 flex items-start gap-4"
                >
                  <div className="w-10 h-10 rounded-xl bg-blue-50 flex items-center justify-center flex-shrink-0 group-hover:bg-blue-100 transition-colors">
                    <svg className="w-5 h-5 text-blue-600" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={1.5}>
                      <path strokeLinecap="round" strokeLinejoin="round" d={c.icon} />
                    </svg>
                  </div>
                  <div className="flex-1 min-w-0">
                    <div className="flex items-center justify-between">
                      <h3 className="text-base font-semibold text-slate-900 group-hover:text-blue-900 transition-colors">{c.title}</h3>
                      <svg className="w-4 h-4 text-slate-300 group-hover:text-blue-400 transition-colors flex-shrink-0" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={1.5}>
                        <path strokeLinecap="round" strokeLinejoin="round" d="M4.5 19.5l15-15m0 0H8.25m11.25 0v11.25" />
                      </svg>
                    </div>
                    <p className="text-sm text-slate-500 leading-relaxed mt-1">{c.blurb}</p>
                  </div>
                </button>
              ))}
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}

export default function Home() {
  return <GovernanceHome />;
}
