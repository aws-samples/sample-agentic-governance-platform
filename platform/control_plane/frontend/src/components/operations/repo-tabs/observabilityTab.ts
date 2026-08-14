// observabilityTab.ts — the pure companion behind the repository's Observability tab
// (E28/T12, design D13).
//
// Pure and framework-free so vitest reaches it (`src/**/*.test.ts` only); `ObservabilityTab.tsx` is
// wiring. Same split as `repositoryDetailTabs.ts` / `repoRowModel.ts`.
//
// ---------------------------------------------------------------------------
// D13: "NOT INSTRUMENTED" IS A DISTINCT THIRD STATE, NEVER CONFLATED WITH ZERO
//
// This is the whole reason the module exists, and the distinction cannot be made further down the
// stack. `langfuse_metrics_service` degrades EVERY unhappy path to a ZEROED payload — an
// unprovisioned agent, a missing secret, a failed HTTP read and a genuinely quiet agent all return
// `totals: {traces: 0, cost_usd: 0, tokens: 0}` — and that behaviour is deliberately NOT changed
// (the design says so explicitly: it is handled in the UI, not by editing the service, because
// degrade-and-continue is right for an aggregate fan-out that must not fail wholesale on one bad
// agent).
//
// The consequence is that a rendered `0` is ambiguous by construction, and the ambiguity runs in the
// reassuring direction: "this agent cost nothing and served nothing" reads as calm, while the truth
// may be that the platform cannot measure it at all. On a cost-and-usage surface that is the
// expensive misreading — an agent nobody is watching looks identical to an agent nobody is using.
//
// So THREE states, and one more for the read that has not landed:
//
//   • loading          — nothing is claimed yet. Outranks everything: a page mid-read must not
//                        flash "not instrumented" and then contradict itself.
//   • not-instrumented — Langfuse is not wired into this environment. Established ONLY by a
//                        SUCCESSFUL settings probe answering `configured: false`. Draws no figures
//                        at all, because there is nothing to draw and a row of zeroes IS the
//                        conflation.
//   • unread           — we could not ask, or we asked and got nothing back. Distinct from both:
//                        `configured` unestablished (the probe itself failed), a metrics read that
//                        errored, or a `null` payload. NOT "not instrumented", because a failed
//                        probe has not established that Langfuse is absent — the platform
//                        Observability page degrades its own failed settings read to
//                        `configured: false`, which asserts exactly that, and this module refuses
//                        to inherit it.
//   • data             — a successful read. Its numbers are rendered AS THEY CAME, including
//                        genuine zeroes: the distinction has to cut both ways, or the fix has only
//                        moved the dishonesty and a quiet agent can no longer say it is quiet.
//
// Same family as the rest of this surface: absent runtime ⇒ `unknown`, never `ready`; a failed
// count ⇒ em dash, never `0`; a failed history ⇒ unknown, never "never deployed".

import type { ScopeMetrics } from '../../../types';

// ---------------------------------------------------------------------------
// PROJECT_SCOPE_NOTE — the figures are the PROJECT'S, not this repository's.
//
// There is no repo-scoped metrics route and this task does not build one. `scope=project`
// aggregates every agent materialized into the project (the backend resolves them through the
// project's repositories), so labelling the headline totals as this repository's would be a
// straightforward false attribution — and a costly one on a cost surface, where an operator may be
// reading it to decide whether THIS agent is expensive.
//
// The honest move is to state the scope rather than relabel the number, and to offer the repo's own
// agent separately via `agentSlice` — which comes from `by_agent[]`, is genuinely per-agent, and is
// the only figure on the panel that may carry this repository's name.
// ---------------------------------------------------------------------------
export const PROJECT_SCOPE_NOTE =
  'These totals cover the whole project — every agent in it, not only this repository. This repository’s own agent is broken out below.';

// ---------------------------------------------------------------------------
// AGENT_ZERO_NOTE — the LAST place D13's conflation survives, and it is on the one card that
// carries this repository's name.
//
// `configured` above is a PLATFORM-wide fact: the settings probe answers whether the environment
// has a Langfuse host at all. Whether THIS agent is instrumented is a separate, per-agent thing —
// the agent record carries its own Langfuse secret, and the metrics service returns a ZEROED row
// with no upstream call whatsoever when that secret is absent. Meanwhile the scope route appends a
// breakdown row for EVERY visible agent unconditionally, so the agent is `present` either way.
//
// The consequence: platform instrumented + this agent never provisioned ⇒ the panel's state is
// `data` ⇒ this card draws 0 traces, $0.00, 0 tokens. Which is precisely the misreading D13 exists
// to prevent — an agent nobody is watching rendered identically to an agent nobody is using — on
// the numbers an operator is most likely to attribute to this repository.
//
// It is NOT fixed by reading the real signal, deliberately: the per-agent instrumentation flag is
// not on the frontend agent type, and widening that type is out of bounds for this epic. Adding a
// second metrics call would not help either — the service degrades that read to zeroes too. So the
// available honest fix is to say what a zero here does and does not establish, and to say it beside
// the figure rather than in a doc. A number qualified truthfully beats a number that reads well.
// ---------------------------------------------------------------------------
export const AGENT_ZERO_NOTE =
  'A zero here means the project read reported zero for this agent. It is not evidence that the agent has been unused: an agent whose observability project was never provisioned reports zero the same way, so a zero cannot tell the two apart.';

// ---------------------------------------------------------------------------
// metricsState
// ---------------------------------------------------------------------------

export type MetricsState = 'loading' | 'not-instrumented' | 'unread' | 'data';

export interface MetricsReadSource {
  /** The read has not landed. Outranks every other signal. */
  loading: boolean;
  /**
   * The SETTINGS probe's answer. `true`/`false` only when the probe actually SUCCEEDED;
   * `null`/`undefined` means it did not, which is `unread` and never `not-instrumented`.
   */
  configured: boolean | null | undefined;
  /** The metrics read itself failed. */
  error: boolean;
  /** The payload, or null when nothing came back. */
  metrics: ScopeMetrics | null;
}

export function metricsState(source: MetricsReadSource): MetricsState {
  if (source.loading) return 'loading';
  // A SUCCESSFUL probe reporting no Langfuse. Only a literal `false` establishes it — see the
  // header note on why an unestablished probe must not become this state.
  if (source.configured === false) return 'not-instrumented';
  if (source.configured !== true) return 'unread';
  if (source.error || source.metrics === null) return 'unread';
  return 'data';
}

/**
 * What the panel SAYS instead of drawing figures, or `null` when it draws them.
 *
 * `Record<MetricsState, …>` with no `default` branch, so a fifth state is a `tsc` error naming this
 * table (the C3 idiom). `data` is `null` precisely because a successful read — zeroes included — is
 * rendered as numbers.
 *
 * Note what `not-instrumented`'s copy must NOT say: anything about an absence of traces, activity or
 * usage. That sentence IS the conflation, written out. It talks about the PLATFORM's ability to
 * measure, never about what the agent did.
 */
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

// ---------------------------------------------------------------------------
// agentSlice — THIS repository's agent, pulled out of the project-wide payload.
//
// `by_agent[]` carries one row per agent the backend enumerated in scope, each with its own totals,
// so this is the one figure on the panel that is honestly per-agent.
//
// AN AGENT MISSING FROM THE BREAKDOWN IS `absent`, NEVER A ZERO ROW. The same rule as everything
// else here, one level down: a read that did not enumerate this agent has not established that the
// agent recorded nothing. It happens for ordinary reasons — the agent is not visible to this caller
// under the tenant filter, or its record is not yet in the project's repositories — and answering
// "0 traces, $0" would be a claim about usage derived from a list that never mentioned it.
//
// An agent that IS enumerated and reports real zeroes is `present` with zero totals, because the
// distinction has to cut both ways.
// ---------------------------------------------------------------------------

export interface AgentTotals {
  traces: number;
  cost_usd: number;
  tokens: number;
}

export interface AgentSlice {
  /** `present` ⇒ the payload enumerated this agent. `absent` ⇒ it did not; nothing is known. */
  kind: 'present' | 'absent';
  /** The agent's own totals, or null when absent — NEVER a zeroed stand-in. */
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

// ---------------------------------------------------------------------------
// metricsWindow — the wire date range the metrics route expects.
//
// Declared here rather than imported from `governance/observability/observabilityMetrics.ts`: that
// module is under `components/governance/**`, which this epic may not touch OR import (Jorge's
// surface). The shape is identical by contract — an inclusive `days`-wide window ending today, as
// `YYYY-MM-DD` in UTC, which is what the backend's `date_from`/`date_to` query params parse.
//
// `now` is injectable so the range is deterministic under test; the UTC accessors are what keep a
// browser west of Greenwich from asking for tomorrow.
// ---------------------------------------------------------------------------
export function metricsWindow(
  now: Date = new Date(),
  days = 30,
): { dateFrom: string; dateTo: string } {
  const fmt = (d: Date): string => d.toISOString().slice(0, 10);
  const from = new Date(now.getTime());
  from.setUTCDate(from.getUTCDate() - (days - 1));
  return { dateFrom: fmt(from), dateTo: fmt(now) };
}
