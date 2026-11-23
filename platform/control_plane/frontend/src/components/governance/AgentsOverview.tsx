import { useMemo } from 'react';
import { Link } from 'react-router-dom';
import { agentsApi } from '../../api/client';
import type { Agent, LifecycleState, Platform } from '../../api/client';
import { useLoad } from '../../api/useLoad';
import { lifecycleBadge } from './agentBadges';
import { platformLabelOr } from './platformLabels';

const NO_AGENTS: Agent[] = [];

const CARD = 'bg-white/70 backdrop-blur rounded-xl border border-slate-200/60 shadow-sm';

const LIFECYCLE_ORDER: LifecycleState[] = [
  'proposed',
  'pending_approval',
  'approved',
  'rejected',
  'deprecated',
];

const LIFECYCLE_BAR: Record<LifecycleState, string> = {
  approved: 'bg-emerald-400',
  proposed: 'bg-slate-300',
  pending_approval: 'bg-amber-400',
  rejected: 'bg-red-400',
  deprecated: 'bg-slate-300',
};

interface DistRow {
  key: string;
  label: string;
  count: number;
  bar?: string;
}

function DistributionCard({
  title,
  rows,
  empty,
}: {
  title: string;
  rows: DistRow[];
  empty: string;
}) {
  const max = rows.reduce((m, r) => Math.max(m, r.count), 0) || 1;
  return (
    <div className={`${CARD} p-4`}>
      <h2 className="text-sm font-semibold text-slate-800 mb-3">{title}</h2>
      {rows.length === 0 ? (
        <p className="text-xs text-slate-400">{empty}</p>
      ) : (
        <ul className="space-y-2.5">
          {rows.map((r) => (
            <li key={r.key}>
              <div className="flex items-center justify-between text-xs mb-1">
                <span className="text-slate-600 truncate pr-2" title={r.label}>
                  {r.label}
                </span>
                <span className="text-slate-400 tabular-nums shrink-0">{r.count}</span>
              </div>
              <div className="h-1.5 w-full rounded-full bg-slate-100 overflow-hidden">
                <div
                  className={`h-full rounded-full ${r.bar ?? 'bg-blue-400'}`}
                  style={{ width: `${Math.max((r.count / max) * 100, 4)}%` }}
                />
              </div>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

function toRows(
  counts: Map<string, number>,
  opts?: { bar?: (key: string) => string; label?: (key: string) => string },
): DistRow[] {
  return Array.from(counts.entries())
    .map(([key, count]) => ({
      key,
      count,
      label: opts?.label ? opts.label(key) : key,
      bar: opts?.bar ? opts.bar(key) : undefined,
    }))
    .sort((a, b) => b.count - a.count || a.label.localeCompare(b.label));
}

function tally<T>(items: T[], pick: (t: T) => string): Map<string, number> {
  const m = new Map<string, number>();
  for (const it of items) {
    const k = pick(it);
    m.set(k, (m.get(k) ?? 0) + 1);
  }
  return m;
}

export default function AgentsOverview() {
  const { state, reload } = useLoad(() => agentsApi.list(), [], 'Failed to load agents.');
  const agents = state.data ?? NO_AGENTS;

  const counts = useMemo(() => {
    const by = (s: LifecycleState) => agents.filter((a) => a.lifecycle_state === s).length;
    return {
      total: agents.length,
      proposed: by('proposed'),
      pending: by('pending_approval'),
      approved: by('approved'),
      deprecated: by('deprecated'),
      ownerless: agents.filter((a) => !a.sponsor_email || !a.sponsor_email.trim()).length,
    };
  }, [agents]);

  const platformRows = useMemo(
    () =>
      toRows(tally(agents, (a) => a.platform ?? '__unset'), {
        label: (k) => (k === '__unset' ? 'Not set' : platformLabelOr(k as Platform, k)),
      }),
    [agents],
  );

  const lobRows = useMemo(
    () =>
      toRows(tally(agents, (a) => (a.business_unit?.trim() ? a.business_unit : '__unset')), {
        label: (k) => (k === '__unset' ? 'Unassigned' : k),
      }),
    [agents],
  );

  const regionRows = useMemo(
    () =>
      toRows(tally(agents, (a) => (a.region?.trim() ? a.region : '__unset')), {
        label: (k) => (k === '__unset' ? 'Unassigned' : k),
      }),
    [agents],
  );

  const lifecycleRows = useMemo(() => {
    const t = tally(agents, (a) => a.lifecycle_state);
    return LIFECYCLE_ORDER.filter((s) => (t.get(s) ?? 0) > 0).map((s) => ({
      key: s,
      count: t.get(s) ?? 0,
      label: lifecycleBadge(s).label,
      bar: LIFECYCLE_BAR[s],
    }));
  }, [agents]);

  const header = (
    <div className="flex items-start justify-between mb-6 animate-fade-in">
      <div>
        <h1 className="text-2xl font-semibold text-slate-900 tracking-tight">Agents</h1>
        <p className="text-sm text-slate-500 mt-0.5">
          Fleet analytics — distributions across the single registry of every governed agent.
        </p>
      </div>
      <Link
        to="/agents/all"
        className="px-3.5 py-1.5 rounded-lg bg-blue-600 text-white text-sm font-medium hover:bg-blue-700 transition-colors shrink-0"
      >
        View all agents
      </Link>
    </div>
  );

  if (state.kind === 'loading') {
    return (
      <div className="min-h-[calc(100vh-4rem)] relative">
        <div className="relative max-w-7xl mx-auto px-6 py-8">
          {header}
          <p className="text-slate-400 text-sm">Loading fleet analytics…</p>
        </div>
      </div>
    );
  }

  if (state.kind === 'error') {
    return (
      <div className="min-h-[calc(100vh-4rem)] relative">
        <div className="relative max-w-7xl mx-auto px-6 py-8">
          {header}
          <div className={`${CARD} border-red-200/70 p-6`}>
            <h3 className="text-sm font-semibold text-red-700">Couldn’t load fleet analytics</h3>
            <p className="text-sm text-slate-600 mt-1">{state.message}</p>
            <button
              onClick={reload}
              className="mt-3 px-3 py-1.5 rounded-lg bg-white border border-slate-300 text-slate-700 text-xs font-medium hover:bg-slate-50 transition-colors"
            >
              Retry
            </button>
          </div>
        </div>
      </div>
    );
  }

  if (counts.total === 0) {
    return (
      <div className="min-h-[calc(100vh-4rem)] relative">
        <div className="relative max-w-7xl mx-auto px-6 py-8">
          {header}
          <div className={`${CARD} p-10 text-center`}>
            <h3 className="text-sm font-semibold text-slate-700">No agents registered yet</h3>
            <p className="text-sm text-slate-500 mt-1">
              Analytics appear once the registry has agents.
            </p>
            <Link
              to="/agents/new"
              className="inline-block mt-4 px-3.5 py-1.5 rounded-lg bg-blue-600 text-white text-sm font-medium hover:bg-blue-700 transition-colors"
            >
              Register an agent
            </Link>
          </div>
        </div>
      </div>
    );
  }

  return (
    <div className="min-h-[calc(100vh-4rem)] relative">
      <div className="relative max-w-7xl mx-auto px-6 py-8">
        {header}

        <div className="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-6 gap-3 mb-8">
          <StatCard label="Total agents" value={counts.total} hint="in the registry" />
          <Link to="/agents/all" className="block group">
            <div className={`${CARD} p-4 h-full transition-colors group-hover:border-amber-300/80`}>
              <div className="text-2xl font-semibold text-amber-700">{counts.pending}</div>
              <div className="text-xs font-medium text-slate-700 mt-1">Pending approval</div>
              <div className="text-[11px] text-amber-600/80 mt-0.5 group-hover:underline">
                Review now →
              </div>
            </div>
          </Link>
          <StatCard label="Proposed" value={counts.proposed} hint="drafts" />
          <StatCard label="Approved" value={counts.approved} hint="in good standing" />
          <StatCard label="Deprecated" value={counts.deprecated} hint="retired" />
          <StatCard
            label="Ownerless"
            value={counts.ownerless}
            hint="no sponsor"
            accent={counts.ownerless > 0 ? 'text-rose-700' : undefined}
          />
        </div>

        <h2 className="text-xs font-semibold uppercase tracking-wide text-slate-400 mb-3">
          Distributions
        </h2>
        <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-4 mb-8">
          <DistributionCard
            title="By platform"
            rows={platformRows}
            empty="No platform data."
          />
          <DistributionCard
            title="By Line of Business"
            rows={lobRows}
            empty="No Line of Business data."
          />
          <DistributionCard title="By region" rows={regionRows} empty="No region data." />
          <DistributionCard
            title="By lifecycle"
            rows={lifecycleRows}
            empty="No lifecycle data."
          />
        </div>
      </div>
    </div>
  );
}

function StatCard({
  label,
  value,
  hint,
  accent,
}: {
  label: string;
  value: number;
  hint: string;
  accent?: string;
}) {
  return (
    <div className={`${CARD} p-4`}>
      <div className={`text-2xl font-semibold ${accent ?? 'text-slate-900'}`}>{value}</div>
      <div className="text-xs font-medium text-slate-700 mt-1">{label}</div>
      <div className="text-[11px] text-slate-400 mt-0.5">{hint}</div>
    </div>
  );
}
