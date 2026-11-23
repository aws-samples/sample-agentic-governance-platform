// Rationale: frontend/README.md "Tests are node-environment only, by design"

import type { MetricTotals, ScopeMetrics } from '../../../api/client';

export const PROJECT_SCOPE_NOTE =
  'These totals cover the whole project — every agent in it, not only this repository. This repository’s own agent is broken out below.';

export const AGENT_ZERO_NOTE =
  'A zero here means the project read reported zero for this agent. It is not evidence that the agent has been unused: an agent whose observability project was never provisioned reports zero the same way, so a zero cannot tell the two apart.';

export type MetricsState = 'loading' | 'not-instrumented' | 'unread' | 'data';

export interface MetricsReadSource {
  loading: boolean;
  configured: boolean | null | undefined;
  error: boolean;
  metrics: ScopeMetrics | null;
}

export function metricsState(source: MetricsReadSource): MetricsState {
  if (source.loading) return 'loading';
  if (source.configured === false) return 'not-instrumented';
  if (source.configured !== true) return 'unread';
  if (source.error || source.metrics === null) return 'unread';
  return 'data';
}

export const METRICS_STATE_COPY: Record<
  MetricsState,
  { headline: string; detail: string } | null
> = {
  loading: {
    headline: 'Loading metrics…',
    detail: 'Reading this project’s observability data.',
  },
  'not-instrumented': {
    headline: 'Not instrumented',
    detail:
      'Observability is not wired into this environment, so nothing here is being measured. This is NOT a report that the agent is idle — until Langfuse is configured, the platform cannot tell how much this repository is used or what it costs.',
  },
  unread: {
    headline: 'Metrics could not be read',
    detail:
      'The observability read failed, so these figures are unknown — not zero. Retry in a moment; nothing about the agent’s usage or cost can be concluded from this.',
  },
  data: null,
};

export type AgentTotals = MetricTotals;

export interface AgentSlice {
  kind: 'present' | 'absent';
  totals: AgentTotals | null;
}

export function agentSlice(
  metrics: ScopeMetrics | null | undefined,
  agentId: string,
): AgentSlice {
  const row = (metrics?.by_agent ?? []).find((a) => a.agent_id === agentId);
  return row === undefined
    ? { kind: 'absent', totals: null }
    : { kind: 'present', totals: row.totals };
}

export function metricsWindow(
  now: Date = new Date(),
  days = 30,
): { dateFrom: string; dateTo: string } {
  const fmt = (d: Date): string => d.toISOString().slice(0, 10);
  const from = new Date(now.getTime());
  from.setUTCDate(from.getUTCDate() - (days - 1));
  return { dateFrom: fmt(from), dateTo: fmt(now) };
}
