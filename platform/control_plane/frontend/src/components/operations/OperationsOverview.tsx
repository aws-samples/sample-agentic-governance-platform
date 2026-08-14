// Operations Overview — the /ops section root (Epic 18, Task 12; rewired in
// Epic 20, Task 18). The landing surface for the Operations side: four KPI stat
// cards, a small provisioning trend chart, and the recent provisioning activity
// feed for the shared Claims-Triage scenario.
//
// The four KPI tiles now read LIVE counts on mount — projects, repositories,
// templates, and org connections — each fetched best-effort (one failure shows
// a dash for that tile, never blanks the page), mirroring the Projects.tsx /
// Repositories.tsx (T16/T17) fetch idiom. No agents count is surfaced here:
// agents live in the separate governance registry, not the ops APIs, so we show
// only numbers this side genuinely owns — honest tiles, no fabricated KPIs.
//
// The trend chart + the activity feed below remain illustrative placeholders for
// domains not yet backed by live data; their series/rows are local to this file.
//
// House style: the shared OpsPage frame + emerald-on-glass tokens (opsUi.ts),
// and a recharts area chart mirroring the FinOps "Spend Velocity" idiom
// (ResponsiveContainer → defs gradient → CartesianGrid → XAxis/YAxis → Tooltip
// → Area) but recolored emerald to carry the Ops identity.

import { useEffect, useState, type JSX } from 'react';
import { AreaChart, Area, XAxis, YAxis, CartesianGrid, Tooltip, ResponsiveContainer } from 'recharts';

import {
  connectionsApi,
  projectsApi,
  repositoriesApi,
} from '../../api/client';
import OpsPage from './OpsPage';
import { OPS_CARD, OPS_BADGE } from './opsUi';
import { SampleBadge } from '../shared/comingSoon';
import { tooltipStyle } from '../govern/mockData';

// ───────────────────────────── chart data ─────────────────────────────

// The two illustrative sections below (trend + activity feed) carry the shared
// <SampleBadge />, which replaced this file's own local slate pill in Epic 31F so
// every "not live" disclosure on the platform reads the same. Light tone (the
// default): both badges sit on white OPS_CARD surfaces, not the dark chrome.

// Agents provisioned per day this week — a small local illustrative series for
// the trend chart (not backed by a live agents-per-day API yet).
const AGENTS_THIS_WEEK: { day: string; agents: number }[] = [
  { day: 'Mon', agents: 14 },
  { day: 'Tue', agents: 15 },
  { day: 'Wed', agents: 17 },
  { day: 'Thu', agents: 18 },
  { day: 'Fri', agents: 20 },
  { day: 'Sat', agents: 20 },
  { day: 'Sun', agents: 21 },
];

// Recent provisioning activity for the Claims-Triage scenario. Agent/repo names
// match the scenario (acme/claims-triage et al.) and the statuses key into
// OPS_BADGE.
const ACTIVITY: { agent: string; action: string; status: keyof typeof OPS_BADGE; time: string }[] = [
  { agent: 'acme/claims-triage', action: 'Repo created from Python Agent template', status: 'ready', time: '2h ago' },
  { agent: 'acme/policy-lookup-svc', action: 'Entra app registered', status: 'ready', time: '5h ago' },
  { agent: 'acme/fnol-intake', action: 'Provisioning started', status: 'provisioning', time: '8h ago' },
  { agent: 'acme/contact-center', action: 'CI/CD pipeline connected', status: 'failed', time: '1d ago' },
];

// ───────────────────────────── the page ─────────────────────────────

// A tile's live count is `null` while loading and after a failed fetch — both
// render as an em-dash so a best-effort miss reads as "unknown", never a fake 0.
type Counts = {
  projects: number | null;
  repositories: number | null;
  templates: number | null;
  connections: number | null;
};

export default function OperationsOverview(): JSX.Element {
  const [counts, setCounts] = useState<Counts>({
    projects: null,
    repositories: null,
    templates: null,
    connections: null,
  });
  const [loading, setLoading] = useState(true);

  // Fetch the four live tile counts on mount. Each is independent + best-effort:
  // a single failure (e.g. a 403 for a true OPERATOR) leaves that tile at `null`
  // (an em-dash) without crashing the page or blanking the others. Mirrors the
  // Projects.tsx / Repositories.tsx (T16/T17) best-effort idiom.
  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    const set = (key: keyof Counts, value: number | null) => {
      if (!cancelled) setCounts((prev) => ({ ...prev, [key]: value }));
    };
    // Templates are connection-scoped (githubTemplatesApi requires a connectionId),
    // so there is no single org-wide count to fetch here — the Templates tile stays
    // at `null` (an em-dash) rather than fabricating a number.
    const fetches = [
      projectsApi.list().then((r) => set('projects', r.length), () => set('projects', null)),
      repositoriesApi.list().then((r) => set('repositories', r.length), () => set('repositories', null)),
      connectionsApi.list().then((r) => set('connections', r.length), () => set('connections', null)),
    ];
    Promise.allSettled(fetches).finally(() => {
      if (!cancelled) setLoading(false);
    });
    return () => {
      cancelled = true;
    };
  }, []);

  // A null tile (still loading, or a failed fetch) renders an em-dash; the
  // loading vs. failed distinction is carried by the animate-pulse className.
  const show = (value: number | null): string => (value === null ? '—' : String(value));

  const kpis: { label: string; value: string; sub: string }[] = [
    { label: 'Projects', value: show(counts.projects), sub: 'Provisioning containers' },
    { label: 'Repositories', value: show(counts.repositories), sub: 'Across all projects' },
    { label: 'Templates', value: show(counts.templates), sub: 'Agent blueprints' },
    { label: 'Org Connections', value: show(counts.connections), sub: 'GitHub · GitLab' },
  ];

  return (
    <OpsPage
      title="Operations"
      subtitle="Provision and operate agents — repositories, CI/CD, and deployments in one place."
    >
      {/* KPI stat cards — live counts fetched on mount, best-effort per tile */}
      <div className="grid grid-cols-2 md:grid-cols-4 gap-3 mb-4">
        {kpis.map((k) => (
          <div key={k.label} className={`${OPS_CARD} p-4`}>
            <div className="text-[11px] font-medium text-slate-500 uppercase tracking-wide">{k.label}</div>
            <div
              className={`text-3xl font-semibold text-slate-900 mt-1 tabular-nums ${
                k.value === '—' && loading ? 'animate-pulse text-slate-300' : ''
              }`}
            >
              {k.value}
            </div>
            <div className="text-[11px] text-slate-400 mt-0.5">{k.sub}</div>
          </div>
        ))}
      </div>

      {/* Agents-provisioned trend — mirrors the FinOps area-chart idiom, emerald */}
      <div className={`${OPS_CARD} p-5 mb-4`}>
        <div className="flex items-center gap-2 mb-3">
          <div className="text-sm font-semibold text-slate-900">Agents provisioned this week</div>
          <SampleBadge />
        </div>
        <ResponsiveContainer width="100%" height={180}>
          <AreaChart data={AGENTS_THIS_WEEK}>
            <defs>
              <linearGradient id="agentsGrad" x1="0" y1="0" x2="0" y2="1">
                <stop offset="0%" stopColor="#10b981" stopOpacity={0.3} />
                <stop offset="100%" stopColor="#10b981" stopOpacity={0} />
              </linearGradient>
            </defs>
            <CartesianGrid strokeDasharray="3 3" stroke="#d1fae5" />
            <XAxis dataKey="day" tick={{ fill: '#94a3b8', fontSize: 10 }} />
            <YAxis tick={{ fill: '#94a3b8', fontSize: 10 }} allowDecimals={false} />
            <Tooltip contentStyle={tooltipStyle} formatter={(v) => `${Number(v)} agents`} />
            <Area type="monotone" dataKey="agents" stroke="#10b981" fill="url(#agentsGrad)" strokeWidth={2} />
          </AreaChart>
        </ResponsiveContainer>
      </div>

      {/* Recent provisioning activity */}
      <div className={`${OPS_CARD} overflow-hidden`}>
        <div className="px-5 py-4 border-b border-emerald-100/70">
          <div className="flex items-center gap-2">
            <div className="text-sm font-semibold text-slate-900">Recent provisioning activity</div>
            <SampleBadge />
          </div>
          <div className="text-[11px] text-slate-400 mt-0.5">Latest agent provisioning events across all projects</div>
        </div>
        <div className="divide-y divide-emerald-100/70">
          {ACTIVITY.map((a) => (
            <div key={`${a.agent}-${a.action}`} className="flex items-center gap-3 px-5 py-3 hover:bg-emerald-50/40 transition-colors">
              <span className="text-sm font-mono text-[12px] font-medium text-slate-900 w-52 flex-shrink-0 truncate">{a.agent}</span>
              <span className="text-sm text-slate-600 flex-1 truncate">{a.action}</span>
              <span className={`inline-flex items-center gap-1.5 text-[11px] font-semibold px-2 py-0.5 rounded-full uppercase tracking-wide ${OPS_BADGE[a.status]}`}>
                <span aria-hidden="true">●</span>
                {a.status}
              </span>
              <span className="text-[11px] text-slate-400 w-16 text-right flex-shrink-0">{a.time}</span>
            </div>
          ))}
        </div>
      </div>
    </OpsPage>
  );
}
