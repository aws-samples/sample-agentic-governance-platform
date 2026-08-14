// ObservabilityTab — the project's Langfuse-backed metrics, honestly scoped (E28/T12, design D13).
//
// ---------------------------------------------------------------------------
// WHAT IT READS, AND WHY IT OWNS ITS OWN RENDERING
//
// `observabilityApi.getMetrics('project', …)` directly. The client signature already accepts the
// project scope and the backend already implements it (it resolves the project's agents through its
// repositories and tenant-filters them), so nothing anywhere needs widening for this tab to exist.
//
// It imports NOTHING from the governance component tree — that surface belongs to another owner and
// this epic may not touch or depend on one line of it. The consequence is a deliberately small panel:
// the figures are rendered as tiles rather than reusing that tree's Recharts composition, because
// reaching for those components would mean importing from it. A guard asserts no such import path
// appears in this file, and it reads raw source without skipping comments — which is why this one
// describes the tree instead of writing its path.
//
// ---------------------------------------------------------------------------
// THE SCOPE MISMATCH IS STATED, NOT PAPERED OVER
//
// There is no repo-scoped metrics route and this task does not build one. `scope=project` aggregates
// EVERY agent in the project, so labelling those totals as this repository's would be a false
// attribution — and a costly one here, where an operator may be reading the number to decide whether
// THIS agent is expensive. So the project totals are labelled as the project's (`PROJECT_SCOPE_NOTE`)
// and this repository's own agent is broken out separately from `by_agent[]`, which is genuinely
// per-agent and is the only figure on the panel that may carry this repository's name.
//
// ---------------------------------------------------------------------------
// D13: NOT INSTRUMENTED IS A THIRD STATE, NEVER A ZERO
//
// The metrics service degrades every unhappy path to a ZEROED payload and that behaviour is
// deliberately left alone, so a rendered `0` is ambiguous by construction — and ambiguous in the
// reassuring direction, because "this agent cost nothing" reads calm while the truth may be that the
// platform cannot measure it at all. `metricsState` splits the four cases (loading / not-instrumented
// / unread / data) in the `.ts` where tests reach them, and an agent MISSING from the breakdown is
// reported absent rather than as zero. This file only renders whichever state it is handed.
//
// House style: emerald-on-glass Ops tokens, Tailwind v4 utility strings, 2-space indent.

import { useEffect, useMemo, useState, type JSX } from 'react';

import { observabilityApi, type Repository } from '../../../api/client';
import type { ScopeMetrics } from '../../../types';
import { NO_VALUE } from '../opsLabels';
import { OPS_CARD } from '../opsUi';
import {
  AGENT_ZERO_NOTE,
  METRICS_STATE_COPY,
  PROJECT_SCOPE_NOTE,
  agentSlice,
  metricsState,
  metricsWindow,
} from './observabilityTab';

const FIELD_LABEL = 'block text-[11px] uppercase tracking-wide text-slate-400 font-medium mb-1';

const fmtInt = (n: number): string => n.toLocaleString();
const fmtUsd = (n: number): string => `$${n.toFixed(2)}`;

export default function ObservabilityTab({ repo }: { repo: Repository }): JSX.Element {
  const range = useMemo(() => metricsWindow(), []);
  const [metrics, setMetrics] = useState<ScopeMetrics | null>(null);
  // `null` until the settings probe SUCCEEDS. A failed probe stays null, which `metricsState` reads
  // as unread rather than as "not instrumented" — the platform Observability page degrades its own
  // failed read to `configured: false`, asserting an absence it never established, and this panel
  // deliberately does not inherit that.
  const [configured, setConfigured] = useState<boolean | null>(null);
  const [error, setError] = useState(false);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError(false);
    (async () => {
      let isConfigured: boolean | null = null;
      try {
        const settings = await observabilityApi.getSettings();
        isConfigured = settings.configured;
      } catch {
        isConfigured = null;
      }
      if (cancelled) return;
      setConfigured(isConfigured);
      // Not instrumented ⇒ no metrics read at all. Asking for figures the platform cannot produce
      // would only turn a clean third state into a degraded row of zeroes.
      if (isConfigured !== true) {
        setMetrics(null);
        setLoading(false);
        return;
      }
      try {
        const m = await observabilityApi.getMetrics('project', {
          projectId: repo.project_id,
          dateFrom: range.dateFrom,
          dateTo: range.dateTo,
        });
        if (!cancelled) setMetrics(m);
      } catch {
        if (!cancelled) {
          setMetrics(null);
          setError(true);
        }
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [repo.project_id, range.dateFrom, range.dateTo]);

  const state = metricsState({ loading, configured, error, metrics });
  const copy = METRICS_STATE_COPY[state];
  const slice = agentSlice(metrics, repo.agent_id);

  // Loading / not-instrumented / unread each STATE their case and draw no figures. A row of zeroes
  // under any of them IS the D13 conflation.
  if (copy !== null) {
    return (
      <div className={`${OPS_CARD} p-6`}>
        <h3 className="text-sm font-semibold text-slate-800">{copy.headline}</h3>
        <p className="text-sm text-slate-500 mt-1 max-w-2xl">{copy.detail}</p>
        <p className="text-[11px] text-slate-400 mt-3">
          Window {range.dateFrom} to {range.dateTo}
        </p>
      </div>
    );
  }

  return (
    <div className="space-y-6">
      {/* THIS REPOSITORY'S AGENT — the only per-agent figures available, and the only ones that may
          carry this repository's name. An agent the project payload did not enumerate is reported
          ABSENT, never as zero: a read that never mentioned it has not established that it recorded
          nothing. */}
      <div className={`${OPS_CARD} p-5`}>
        <div className="flex flex-wrap items-baseline justify-between gap-2 mb-4">
          <h3 className="text-sm font-semibold text-slate-800">This repository’s agent</h3>
          <span className="text-[11px] text-slate-400">
            {range.dateFrom} to {range.dateTo}
          </span>
        </div>
        {slice.kind === 'present' && slice.totals !== null ? (
          <>
            <dl className="grid grid-cols-1 sm:grid-cols-3 gap-4">
              <Tile label="Traces" value={fmtInt(slice.totals.traces)} />
              <Tile label="Cost" value={fmtUsd(slice.totals.cost_usd)} />
              <Tile label="Tokens" value={fmtInt(slice.totals.tokens)} />
            </dl>
            {/* The tiles are qualified WHERE THEY ARE READ. `configured` above is a platform-wide
                fact, while instrumentation is per-agent — and the metrics service returns a zeroed
                row with no upstream call at all for an agent whose own observability project was
                never provisioned, while the scope route still enumerates it. So a zero on this card
                has two possible causes and the figure alone cannot separate them. Stating that is
                the D13 rule applied one level down; the alternative would be tiles that read
                reassuringly and may be measuring nothing. */}
            <p className="text-[11px] text-slate-400 mt-3 max-w-2xl">{AGENT_ZERO_NOTE}</p>
          </>
        ) : (
          <p className="text-sm text-slate-500 max-w-2xl">
            This agent isn’t in the project’s metrics breakdown, so its own traces and cost are
            unknown — not zero. That happens when the agent isn’t visible to you or isn’t yet
            attributed to this project.
          </p>
        )}
      </div>

      {/* THE PROJECT'S totals, labelled as the project's. Never presented as this repository's. */}
      <div className={`${OPS_CARD} p-5`}>
        <h3 className="text-sm font-semibold text-slate-800">Project totals</h3>
        <p className="text-sm text-slate-500 mt-1 max-w-2xl">{PROJECT_SCOPE_NOTE}</p>
        {/* `null` rather than a zero fallback (E28 final review). These tiles are only reached in
            the `data` state, where the payload is non-null — so the nullish-coalescing-to-zero that
            stood here was unreachable, and an unreachable zero-for-absent-data fallback is still the
            wrong thing to have written in the one file whose whole thesis is that a zero must come
            from a read that returned zero. `Tile` already renders `null` as the em dash, so if the
            state machine ever changed, this degrades honestly instead of reassuringly.

            A guard asserts that operator-plus-zero form appears NOWHERE in this file, and it reads
            raw source without skipping comments — which is why this comment names the shape in prose
            rather than writing it out. */}
        <dl className="grid grid-cols-1 sm:grid-cols-3 gap-4 mt-4">
          <Tile
            label="Traces"
            value={metrics === null ? null : fmtInt(metrics.totals.traces)}
          />
          <Tile
            label="Cost"
            value={metrics === null ? null : fmtUsd(metrics.totals.cost_usd)}
          />
          <Tile
            label="Tokens"
            value={metrics === null ? null : fmtInt(metrics.totals.tokens)}
          />
        </dl>
      </div>

      {/* The per-agent breakdown, so the project total above is decomposable rather than a number an
          operator has to take on trust. Read-only: no drill-down into the governance surface, which
          this epic does not link into from here. */}
      <div className={`${OPS_CARD} p-5`}>
        <h3 className="text-sm font-semibold text-slate-800 mb-4">Agents in this project</h3>
        {(metrics?.by_agent ?? []).length === 0 ? (
          <p className="text-sm text-slate-500">
            The read returned no per-agent breakdown for this project.
          </p>
        ) : (
          <ul className={`divide-y divide-emerald-100/70`}>
            {(metrics?.by_agent ?? []).map((a) => (
              <li key={a.agent_id} className="flex items-baseline justify-between gap-4 py-2">
                <span className="text-sm text-slate-700 truncate">
                  {a.agent_name || a.agent_id}
                  {a.agent_id === repo.agent_id && (
                    <span className="ml-2 text-[11px] text-emerald-700 font-medium">
                      this repository
                    </span>
                  )}
                </span>
                <span className="shrink-0 text-xs text-slate-500 tabular-nums">
                  {fmtInt(a.totals.traces)} traces · {fmtUsd(a.totals.cost_usd)}
                </span>
              </li>
            ))}
          </ul>
        )}
      </div>
    </div>
  );
}

function Tile({ label, value }: { label: string; value: string | null }): JSX.Element {
  return (
    <div className="rounded-lg border border-emerald-100/70 px-4 py-3">
      <dt className={FIELD_LABEL}>{label}</dt>
      <dd className="text-2xl font-semibold text-slate-900 tabular-nums">{value ?? NO_VALUE}</dd>
    </div>
  );
}
