// Decision: docs/adr/frontend-refusals-classified-by-code.md
import type { EvalEvidence, LifecycleState, Repository } from '../../api/client';
import { isApiFailure } from '../../api/failure';
import { lifecycleBadge } from '../governance/agentBadges';
import { classifyPromotionFailure } from './projectRoles';
import { checksText, judgesText } from './repo-tabs/evaluationsLogic';
import { warnOnce } from './warnOnce';

/** The backend's cap on an override reason (`promote_repo`), verbatim. */
export const OVERRIDE_REASON_MAX = 500;

export type PromoteGateTone = 'ok' | 'warn' | 'muted';

export interface PromoteGateView {
  tone: PromoteGateTone;
  label: string;
  note: string;
  needsOverride: boolean;
}

type EvalGate = NonNullable<Repository['eval_gate']>;

type EvalGateReading = EvalGate | null | undefined | { kind: 'unknown'; raw: string };

const KNOWN_EVAL_GATES = ['passed', 'failed', 'running', 'none'] as const satisfies readonly EvalGate[];

function isKnownEvalGate(gate: string): gate is (typeof KNOWN_EVAL_GATES)[number] {
  return (KNOWN_EVAL_GATES as readonly string[]).includes(gate);
}

function readEvalGate(gate: Repository['eval_gate'] | undefined): EvalGateReading {
  if (gate == null || isKnownEvalGate(gate)) return gate;
  const raw: string = gate satisfies never;
  warnOnce('[promoteGate] eval_gate value this frontend does not know; the gate stays closed:', raw);
  return { kind: 'unknown', raw };
}

export function promoteGateView(gate: Repository['eval_gate'] | undefined, runStatus?: string | null): PromoteGateView {
  if (gate === 'failed' && runStatus === 'error') {
    return { tone: 'warn', label: 'Evaluation inconclusive', note: 'The latest run for this build errored before it could score.', needsOverride: true };
  }
  const read = readEvalGate(gate);
  if (read !== null && typeof read === 'object') {
    return {
      tone: 'muted',
      label: `Evaluation status "${read.raw}" not recognised`,
      note: `This build reports "${read.raw}", which this frontend does not know yet; reload once the deploy finishes, or promote with an override.`,
      needsOverride: true,
    };
  }
  switch (read) {
    case 'passed':
      return { tone: 'ok', label: 'Evaluation passed', note: 'The latest run for this build passed the gate.', needsOverride: false };
    case 'failed':
      return { tone: 'warn', label: 'Evaluation failed', note: 'The latest run for this build did not pass the gate.', needsOverride: true };
    case 'running':
      return { tone: 'muted', label: 'Evaluation running', note: 'Wait for the run to finish, or promote with an override.', needsOverride: true };
    case 'none':
      return { tone: 'warn', label: 'Not evaluated', note: 'No evaluation has run for this build.', needsOverride: true };
    case null:
    case undefined:
      return { tone: 'muted', label: 'Evaluation status unknown', note: 'No passing run can be shown for this build.', needsOverride: true };
  }
}

export function canSubmitPromote(view: PromoteGateView, reason: string): boolean {
  if (!view.needsOverride) return true;
  const r = reason.trim();
  return r.length > 0 && r.length <= OVERRIDE_REASON_MAX;
}

export function overrideReasonToSend(view: PromoteGateView, reason: string): string | undefined {
  return view.needsOverride ? reason.trim() || undefined : undefined;
}

export function isEvalGateRefusal(err: unknown): boolean {
  return isApiFailure(err) && classifyPromotionFailure(err) === 'eval_gate';
}

export function regressionLine(regressions: string[] | null | undefined): string | null {
  return regressions && regressions.length > 0 ? `Regression vs prod: ${regressions.join('; ')}` : null;
}

export function evidenceLine(ev: EvalEvidence | null | undefined): string | null {
  if (!ev || ev.status === 'running') return null;
  const summary =
    ev.checks_passed != null && ev.checks_total != null
      ? { items: 0, checks_passed: ev.checks_passed, checks_failed: ev.checks_total - ev.checks_passed, judge_means: ev.judge_means ?? {} }
      : null;
  const parts = [checksText(summary), ev.judge_min_mean != null ? judgesText(summary, ev.judge_min_mean)?.text : null];
  const text = parts.filter(Boolean).join(' · ');
  return text || (ev.status === 'error' ? 'The run errored before scoring' : null);
}

export function governanceLine(state: LifecycleState | null | undefined): { text: string; ok: boolean } | null {
  return state ? { text: `Governance: ${lifecycleBadge(state).label}`, ok: state === 'approved' } : null;
}

export const GATE_TONE_CLS: Record<PromoteGateTone, { dot: string; text: string }> = {
  ok: { dot: 'bg-emerald-500', text: 'text-emerald-700' },
  warn: { dot: 'bg-amber-500', text: 'text-amber-700' },
  muted: { dot: 'bg-slate-400', text: 'text-slate-600' },
};
