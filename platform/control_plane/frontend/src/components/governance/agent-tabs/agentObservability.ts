import type { AgentMetrics, AgentRepositoryView, TraceRow } from '../../../api/client';
import { objectPath } from '../../operations/objectRoutes';
import { OPS_OBSERVABILITY_TAB } from '../../operations/repositoryDetailTabs';
import { buildDashboardSeries, type DashboardSeries, type DateRange } from '../observability/observabilityMetrics';

export { OPS_OBSERVABILITY_TAB };

export const EM_DASH = '—';

export interface TraceDisplayRow {
  id: string;
  timestamp: string;
  name: string;
  latency: string;
  cost: string;
  user: string;
  raw: TraceRow;
}

export const fmtUsd = (n: number): string => {
  const digits = n === 0 ? 2 : Math.abs(n) < 0.01 ? 6 : Math.abs(n) < 1 ? 4 : 2;
  return `$${n.toLocaleString(undefined, {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  })}`;
};

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

export function deriveTraceUrl(host: string | null | undefined, traceId: string): string | null {
  if (!host) return null;
  return `${host.replace(/\/+$/, '')}/trace/${traceId}`;
}

export function totalPages(total: number, pageSize: number): number {
  if (pageSize <= 0) return 1;
  return Math.max(1, Math.ceil(total / pageSize));
}

export function agentMetricsToSeries(
  metrics: AgentMetrics | null | undefined,
  range?: DateRange,
): DashboardSeries {
  return buildDashboardSeries(metrics ? { ...metrics, by_agent: [] } : metrics, range);
}

export interface HarnessSource {
  agent_arn?: string | null;
  agent_arns?: Record<string, string> | null;
}

function isHarnessArn(arn: string | null | undefined): boolean {
  if (!arn) return false;
  const parts = arn.split(':');
  return parts.length >= 6 && parts.slice(5).join(':').startsWith('harness/');
}

export function isHarnessAgent(agent: HarnessSource): boolean {
  if (isHarnessArn(agent.agent_arn)) return true;
  return Object.values(agent.agent_arns ?? {}).some(isHarnessArn);
}

export interface TracesEmptyState {
  variant: 'langfuse' | 'harness';
  title: string;
  body: string;
  opsLink: { name: string; to: string } | null;
}

const HARNESS_LEAD =
  'This is an AgentCore harness agent: its runs are traced to CloudWatch, not Langfuse, so this list stays empty.';

export function tracesEmptyState(
  agent: HarnessSource,
  repo: Pick<AgentRepositoryView, 'repo_id' | 'name'> | null | undefined,
): TracesEmptyState {
  if (!isHarnessAgent(agent)) {
    return {
      variant: 'langfuse',
      title: 'No traces recorded yet',
      body: "This agent has no Langfuse traces. Once it's provisioned and invoked, each run will appear here with its latency, cost and calling user — newest first.",
      opsLink: null,
    };
  }
  if (repo) {
    return {
      variant: 'harness',
      title: 'Traces for this agent live in CloudWatch',
      body: `${HARNESS_LEAD} Open the Observability tab of its Ops repository to see recent traces.`,
      opsLink: {
        name: repo.name,
        to: `${objectPath({ id: repo.repo_id, artifact_kind: 'agent' })}?tab=${OPS_OBSERVABILITY_TAB}`,
      },
    };
  }
  return {
    variant: 'harness',
    title: 'Traces for this agent live in CloudWatch',
    body: `${HARNESS_LEAD} Its traces are on the Observability tab of its Ops repository, or in CloudWatch directly.`,
    opsLink: null,
  };
}
