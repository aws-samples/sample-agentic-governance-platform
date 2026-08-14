import { useEffect, useMemo, useState } from 'react';
import { Link } from 'react-router-dom';
import { agentsApi } from '../../api/client';
import type { Agent, LifecycleState, Platform, Origin } from '../../api/client';
import { lifecycleBadge } from './agentUi';
// Platform labels come from the ONE shared map (E29/T9) — this file used to keep its own copy,
// one of five that had to agree by hand.
import { platformLabelOr } from './platformLabels';
import { ComingSoonBadge } from '../shared/comingSoon';

// Tighter card chrome — matches the redesigned list/detail density (Decision 7).
const CARD = 'bg-white/70 backdrop-blur rounded-xl border border-slate-200/60 shadow-sm';

// Order used when rendering the lifecycle distribution (and to colour its bars
// with the shared lifecycleBadge palette).
const LIFECYCLE_ORDER: LifecycleState[] = [
  'proposed',
  'pending_approval',
  'approved',
  'rejected',
  'deprecated',
];

// Map a lifecycleBadge text class -> a solid bar fill in the same hue family,
// so the Lifecycle card's bars echo the badges used everywhere else.
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
  /** Optional explicit bar colour; falls back to the neutral slate bar. */
  bar?: string;
}

// A single titled distribution card: label + count + a proportional bar.
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

// Sort a count map into rows, descending by count then label.
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
  const [agents, setAgents] = useState<Agent[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [reloadNonce, setReloadNonce] = useState(0);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    agentsApi
      .list()
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
  }, [reloadNonce]);

  // -- computed metrics (all client-side) -----------------------------------

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
        // The raw key is the fallback, not 'Not set': this bucket key comes from a stored record,
        // so a value written by a build that knew a platform this one does not is possible, and
        // showing the raw key beats mislabelling a real count as unset. (`platformLabelOr`
        // handles both the absent and the unrecognised case — it does not index blindly.)
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

  const originRows = useMemo(
    () => toRows(tally(agents, (a) => a.origin as Origin), { label: (k) => k }),
    [agents],
  );

  // -- render ---------------------------------------------------------------

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

  if (loading) {
    return (
      <div className="min-h-[calc(100vh-4rem)] relative">
        <div className="relative max-w-7xl mx-auto px-6 py-8">
          {header}
          <p className="text-slate-400 text-sm">Loading fleet analytics…</p>
        </div>
      </div>
    );
  }

  if (error) {
    return (
      <div className="min-h-[calc(100vh-4rem)] relative">
        <div className="relative max-w-7xl mx-auto px-6 py-8">
          {header}
          <div className={`${CARD} border-red-200/70 p-6`}>
            <h3 className="text-sm font-semibold text-red-700">Couldn’t load fleet analytics</h3>
            <p className="text-sm text-slate-600 mt-1">{error}</p>
            <button
              onClick={() => setReloadNonce((n) => n + 1)}
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

        {/* ----- Headline counts (real, computed) ----- */}
        <div className="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-6 gap-3 mb-8">
          <StatCard label="Total agents" value={counts.total} hint="in the registry" />
          {/* Pending approval is the actionable one — link it to the list. */}
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

        {/* ----- Real distribution cards ----- */}
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
          <DistributionCard title="By origin" rows={originRows} empty="No origin data." />
        </div>

        {/* ----- Sample usage (Decision 3 — CLEARLY labeled, not real) ----- */}
        <h2 className="text-xs font-semibold uppercase tracking-wide text-slate-400 mb-3">
          Usage
        </h2>
        <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
          <SampleActiveUsersCard />
          <SampleSessionsCard />
        </div>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Headline stat card
// ---------------------------------------------------------------------------
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

// ---------------------------------------------------------------------------
// Sample-usage cards — OBVIOUSLY placeholder data, each carries the shared
// <ComingSoonBadge /> pill so it can't be mistaken for real telemetry. (We have
// no real usage data yet; honesty is a hard requirement.) The badge used to be a
// local twin labelled "30-day trend", then "Sample data" — user-requested copy
// settled on "Coming soon" since the whole dashboard section is roadmap.
// ---------------------------------------------------------------------------

function SampleCardShell({
  title,
  children,
}: {
  title: string;
  children: React.ReactNode;
}) {
  return (
    // Dashed border + reduced opacity reinforce that this is illustrative only.
    <div className="bg-white/50 backdrop-blur rounded-xl border border-dashed border-slate-300/80 shadow-sm p-4">
      <div className="flex items-center justify-between mb-3">
        <h3 className="text-sm font-semibold text-slate-500">{title}</h3>
        <ComingSoonBadge />
      </div>
      {children}
    </div>
  );
}

// A few obviously-round sample numbers so nothing reads as measured data.
const SAMPLE_SERIES = [12, 18, 15, 24, 22, 30, 28, 34, 31, 40, 38, 46];

function SampleActiveUsersCard() {
  const max = Math.max(...SAMPLE_SERIES);
  const w = 320;
  const h = 72;
  const step = w / (SAMPLE_SERIES.length - 1);
  const points = SAMPLE_SERIES.map((v, i) => {
    const x = i * step;
    const y = h - (v / max) * (h - 8) - 4;
    return `${x.toFixed(1)},${y.toFixed(1)}`;
  }).join(' ');
  const areaPoints = `0,${h} ${points} ${w},${h}`;

  return (
    <SampleCardShell title="Active users (30 days)">
      <div className="flex items-end gap-4">
        <div className="text-2xl font-semibold text-slate-400">~46</div>
        <span className="text-xs text-slate-400 pb-1">peak daily</span>
      </div>
      <svg
        viewBox={`0 0 ${w} ${h}`}
        className="mt-2 w-full h-16"
        preserveAspectRatio="none"
        role="img"
        aria-label="Active-users trend over the last 30 days"
      >
        <polygon points={areaPoints} className="fill-slate-200/50" />
        <polyline
          points={points}
          fill="none"
          className="stroke-slate-300"
          strokeWidth="2"
          strokeLinecap="round"
          strokeLinejoin="round"
        />
      </svg>
    </SampleCardShell>
  );
}

const SAMPLE_SESSIONS = [
  { label: 'Mon', v: 120 },
  { label: 'Tue', v: 180 },
  { label: 'Wed', v: 150 },
  { label: 'Thu', v: 220 },
  { label: 'Fri', v: 260 },
  { label: 'Sat', v: 90 },
  { label: 'Sun', v: 70 },
];

function SampleSessionsCard() {
  const max = Math.max(...SAMPLE_SESSIONS.map((d) => d.v));
  return (
    <SampleCardShell title="Sessions over time">
      <div className="flex items-end gap-4 mb-3">
        <div className="text-2xl font-semibold text-slate-400">~1.1k</div>
        <span className="text-xs text-slate-400 pb-1">this week</span>
      </div>
      <div className="flex items-end gap-2 h-20">
        {SAMPLE_SESSIONS.map((d) => (
          <div key={d.label} className="flex-1 flex flex-col items-center justify-end h-full">
            <div
              className="w-full rounded-t bg-slate-300/70"
              style={{ height: `${(d.v / max) * 100}%` }}
              aria-hidden="true"
            />
            <span className="text-[10px] text-slate-400 mt-1">{d.label}</span>
          </div>
        ))}
      </div>
    </SampleCardShell>
  );
}
