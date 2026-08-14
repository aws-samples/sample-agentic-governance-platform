// Pure shaping/display helpers for the per-agent Traces + Cost tabs (Epic 26 /
// Task 9). Framework-free so vitest (src/**/*.test.ts only) can unit-test it —
// same idiom as the T8 dashboard's observabilityMetrics.ts, which keeps the JSX
// thin and delegates the snake_case→series + null-tolerant display logic here.
//
// The Cost tab reuses T8's buildDashboardSeries verbatim: a per-agent
// AgentMetrics is structurally the aggregate half of a ScopeMetrics (just no
// by_agent[]), so the same FinOps chart recipe (timeseries + by-model palette +
// zeroed-totals empty-state) applies with zero duplication.
import type { AgentMetrics, TraceRow } from '../../../types';
import { buildDashboardSeries, type DashboardSeries } from '../observability/observabilityMetrics';

// The em dash the Traces table renders for any field Langfuse omitted (the
// backend TraceRow emits timestamp/name/latency_ms/user_id as Optional).
export const EM_DASH = '—';

// A trace row shaped for the table: every cell is a display-ready string, with
// `raw` kept for the drill-down affordance (deep link / detail).
export interface TraceDisplayRow {
  id: string;
  timestamp: string;
  name: string;
  latency: string;
  cost: string;
  user: string;
  raw: TraceRow;
}

// A single LLM trace costs fractions of a cent (e.g. $0.004008), so a fixed 2-decimal
// format renders every per-trace cost as "$0.00". Scale the precision to the magnitude:
// sub-cent values keep enough digits to stay legible, larger sums stay at 2.
export const fmtUsd = (n: number): string => {
  const digits = n === 0 ? 2 : Math.abs(n) < 0.01 ? 6 : Math.abs(n) < 1 ? 4 : 2;
  return `$${n.toLocaleString(undefined, {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  })}`;
};

// Absolute local timestamp; falls back to the raw string if unparseable so a
// weird value is still visible rather than swallowed.
function fmtTimestamp(iso: string | null): string {
  if (!iso) return EM_DASH;
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return d.toLocaleString(undefined, {
    year: 'numeric',
    month: 'short',
    day: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
  });
}

function fmtLatency(ms: number | null): string {
  if (ms === null || ms === undefined || Number.isNaN(ms)) return EM_DASH;
  return `${Math.round(ms).toLocaleString()} ms`;
}

// Map raw Langfuse trace rows into null-safe display rows. Every Optional field
// (timestamp/name/latency_ms/user_id) degrades to "—"; cost_usd is non-null
// server-side (defaults 0.0) so it always renders a dollar figure.
export function mapTraceRows(rows: TraceRow[]): TraceDisplayRow[] {
  return rows.map((r) => ({
    id: r.id,
    timestamp: fmtTimestamp(r.timestamp),
    name: r.name ?? EM_DASH,
    latency: fmtLatency(r.latency_ms),
    cost: fmtUsd(r.cost_usd ?? 0),
    user: r.user_id ?? EM_DASH,
    raw: r,
  }));
}

// Deep link to a single trace in the deployed Langfuse UI, or null when the host
// is unknown (Langfuse not wired in) — then the row shows a detail-only
// affordance instead of an outbound link. Trailing slash on the host is trimmed.
export function deriveTraceUrl(host: string | null | undefined, traceId: string): string | null {
  if (!host) return null;
  return `${host.replace(/\/+$/, '')}/trace/${traceId}`;
}

// Page count for the trace pager — at least 1 so the footer never reads "of 0".
export function totalPages(total: number, pageSize: number): number {
  if (pageSize <= 0) return 1;
  return Math.max(1, Math.ceil(total / pageSize));
}

// Shape a per-agent AgentMetrics into the same DashboardSeries the FinOps/T8
// charts consume. by_agent is empty for a single agent (no per-agent breakdown).
export function agentMetricsToSeries(metrics: AgentMetrics | null | undefined): DashboardSeries {
  return buildDashboardSeries(metrics ? { ...metrics, by_agent: [] } : metrics);
}
