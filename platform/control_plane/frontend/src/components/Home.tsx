import { useEffect, useMemo, useState } from 'react';
import { Link, useNavigate } from 'react-router-dom';

import { agentsApi, mcpServersApi } from '../api/client';
import type { Agent } from '../api/client';
import { lifecycleBadge } from './governance/agentUi';
import { SampleBadge } from './shared/comingSoon';

const GRADIENT_TEXT = {
  backgroundImage: 'linear-gradient(135deg, #1e40af 0%, #3b82f6 40%, #818cf8 100%)',
  WebkitBackgroundClip: 'text',
  WebkitTextFillColor: 'transparent',
  backgroundClip: 'text',
  color: 'transparent',
} as const;

// How many recently-registered agents the attention panel lists.
const RECENT_AGENT_COUNT = 5;

interface FleetStat {
  label: string;
  value: string;
  to: string;
  attention?: boolean;
  /** true = illustrative, not measured; renders a <SampleBadge /> beside the label. */
  sample?: boolean;
}

interface NavCard {
  title: string;
  blurb: string;
  to: string;
  icon: string;
}

// Inline SVG <path d="..."> strings (Heroicons style); copied from governance/agentsNav.ts.
const NAV_CARDS: NavCard[] = [
  {
    title: 'Agents',
    blurb: 'The single registry of every governed agent.',
    to: '/agents',
    icon: 'M9.813 15.904L9 18.75l-.813-2.846a4.5 4.5 0 00-3.09-3.09L2.25 12l2.846-.813a4.5 4.5 0 003.09-3.09L9 5.25l.813 2.846a4.5 4.5 0 003.09 3.09L15.75 12l-2.846.813a4.5 4.5 0 00-3.09 3.09zM18.259 8.715L18 9.75l-.259-1.035a3.375 3.375 0 00-2.455-2.456L14.25 6l1.036-.259a3.375 3.375 0 002.455-2.456L18 2.25l.259 1.035a3.375 3.375 0 002.456 2.456L21.75 6l-1.035.259a3.375 3.375 0 00-2.456 2.456z',
  },
  {
    title: 'MCPs',
    blurb: 'What agents consume — governed once, independently.',
    to: '/tools-mcp',
    icon: 'M11.42 15.17L17.25 21A2.652 2.652 0 0021 17.25l-5.877-5.877M11.42 15.17l2.496-3.03c.317-.384.74-.626 1.208-.766M11.42 15.17l-4.655 5.653a2.548 2.548 0 11-3.586-3.586l6.837-5.63m5.108-.233c.55-.164 1.163-.188 1.743-.14a4.5 4.5 0 004.486-6.336l-3.276 3.277a3.004 3.004 0 01-2.25-2.25l3.276-3.276a4.5 4.5 0 00-6.336 4.486c.091 1.076-.071 2.264-.904 2.95l-.102.085',
  },
  {
    title: 'Secure',
    blurb: 'Guardrails, policy, audit & incidents.',
    to: '/secure/guardrails',
    icon: 'M12 9v3.75m0-10.036A11.959 11.959 0 013.598 6 11.99 11.99 0 003 9.75c0 5.592 3.824 10.29 9 11.622 5.176-1.332 9-6.03 9-11.622 0-1.31-.21-2.571-.598-3.75h-.152c-3.196 0-6.1-1.248-8.25-3.285z',
  },
  {
    title: 'Govern',
    blurb: 'Cost, LLM Gateway, and fleet observability.',
    to: '/govern/finops',
    icon: 'M2.25 18.75a60.07 60.07 0 0115.797 2.101c.727.198 1.453-.342 1.453-1.096V18.75M3.75 4.5v.75A.75.75 0 013 6h-.75m0 0v-.375c0-.621.504-1.125 1.125-1.125H20.25M2.25 6v9m18-10.5v.75c0 .414.336.75.75.75h.75m-1.5-1.5h.375c.621 0 1.125.504 1.125 1.125v9.75c0 .621-.504 1.125-1.125 1.125h-.375m1.5-1.5H21a.75.75 0 00-.75.75v.75m0 0H3.75m0 0h-.375a1.125 1.125 0 01-1.125-1.125V15m1.5 1.5v-.75A.75.75 0 003 15h-.75M15 10.5a3 3 0 11-6 0 3 3 0 016 0zm3 0h.008v.008H18V10.5zm-12 0h.008v.008H6V10.5z',
  },
  {
    title: 'Observability',
    blurb: 'Fleet-wide traces and Langfuse.',
    to: '/observability',
    icon: 'M2.036 12.322a1.012 1.012 0 010-.639C3.423 7.51 7.36 4.5 12 4.5c4.638 0 8.573 3.007 9.963 7.178.07.207.07.431 0 .639C20.577 16.49 16.64 19.5 12 19.5c-4.638 0-8.573-3.007-9.963-7.178z M15 12a3 3 0 11-6 0 3 3 0 016 0z',
  },
];

// The one number on this page we cannot measure: there is no FinOps backend, so
// no client.ts namespace can produce a spend figure. It stays deliberately round
// (nothing here should read as measured) and carries a <SampleBadge /> — see the
// same reasoning behind the sample cards in governance/AgentsOverview.tsx.
const SAMPLE_MONTHLY_COST = '~$10k';

function GovernanceHome() {
  const navigate = useNavigate();

  // Fleet counts come from the two registry list endpoints and are tallied
  // client-side; loading/error/retry idiom follows governance/AgentsOverview.tsx.
  const [agents, setAgents] = useState<Agent[]>([]);
  const [mcpCount, setMcpCount] = useState(0);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [reloadNonce, setReloadNonce] = useState(0);

  // `loading` is seeded true and re-armed by the Retry handler rather than at the
  // top of this effect: an effect-body setState trips react-hooks/set-state-in-effect,
  // and the retry click is the only thing that ever restarts the read.
  useEffect(() => {
    let cancelled = false;
    Promise.all([agentsApi.list(), mcpServersApi.list()])
      .then(([agentList, mcpList]) => {
        if (cancelled) return;
        setAgents(agentList);
        setMcpCount(mcpList.length);
        setError(null);
      })
      .catch((err: unknown) => {
        if (cancelled) return;
        setError(err instanceof Error ? err.message : 'Failed to load fleet counts.');
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [reloadNonce]);

  const fleetStats = useMemo<FleetStat[]>(() => {
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
      { label: 'Monthly cost', value: SAMPLE_MONTHLY_COST, to: '/govern/finops', sample: true },
    ];
  }, [agents, mcpCount]);

  // Most recently registered agents — replaces the panel's former hardcoded rows.
  const recentAgents = useMemo(
    () =>
      [...agents]
        .sort((a, b) => b.created_at.localeCompare(a.created_at))
        .slice(0, RECENT_AGENT_COUNT),
    [agents],
  );

  const errorCard = (
    <div className="bg-white/70 backdrop-blur rounded-xl border border-red-200/70 shadow-sm p-6">
      <h3 className="text-sm font-semibold text-red-700">Couldn’t load fleet counts</h3>
      <p className="text-sm text-slate-600 mt-1">{error}</p>
      <button
        type="button"
        onClick={() => {
          setLoading(true);
          setError(null);
          setReloadNonce((n) => n + 1);
        }}
        className="mt-3 px-3 py-1.5 rounded-lg bg-white border border-slate-300 text-slate-700 text-xs font-medium hover:bg-slate-50 transition-colors"
      >
        Retry
      </button>
    </div>
  );

  return (
    <div className="min-h-[calc(100vh-4rem)] relative">
      <div className="relative max-w-7xl mx-auto px-6 py-10 animate-fade-in">
        {/* Hero */}
        <div className="mb-8">
          <h1 className="text-4xl font-bold tracking-tight" style={{ ...GRADIENT_TEXT, lineHeight: '1.15' }}>
            Agentic Governance Platform
          </h1>
          <p className="text-sm md:text-base text-slate-500 mt-2 max-w-2xl">
            The single control plane to govern every agent and MCP server — prove it's safe, and keep it accountable.
          </p>
        </div>

        {/* (a) Fleet-at-a-glance metric cards */}
        <div className="flex items-center justify-between mb-3">
          <h2 className="text-sm font-semibold text-slate-700 uppercase tracking-wide">Fleet at a glance</h2>
        </div>
        {loading ? (
          <p className="text-slate-400 text-sm mb-10">Loading fleet counts…</p>
        ) : error ? (
          <div className="mb-10">{errorCard}</div>
        ) : (
          <div className="grid grid-cols-2 md:grid-cols-5 gap-4 mb-10">
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
                  {s.sample && <SampleBadge />}
                </div>
              </button>
            ))}
          </div>
        )}

        <div className="grid grid-cols-1 lg:grid-cols-3 gap-6 mb-10">
          {/* (b) Recently registered panel — the newest real registry entries */}
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

          {/* (c) Quick navigation cards */}
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
