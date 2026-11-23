import type { AuditRow, SignalView, StepResult, ContainmentView } from '../../../api/client';
import type { ApiFailure, ErrorCode } from '../../../api/failure';
import { refusalKind } from '../../../api/refusalKind';

export const STEP_ORDER = ['refuse-invokes', 'cedar-forbid', 'runtime-deny'] as const satisfies readonly StepResult['id'][];

const STEP_COPY: Record<StepResult['id'], { label: string; control: string; system: string }> = {
  'refuse-invokes': { label: 'Refuse invokes through AGP (409)', control: 'AGP invoke gate', system: 'AGP' },
  'cedar-forbid': { label: 'Forbid every tool call from this agent', control: 'Gateway policy (Cedar)', system: 'AgentCore Gateway' },
  'runtime-deny': { label: 'Deny direct invokes of the runtime', control: 'Runtime resource policy', system: 'AgentCore Runtime' },
};

const RESTORE_LABEL: Record<StepResult['id'], string> = {
  'refuse-invokes': 'AGP accepts invokes again',
  'cedar-forbid': 'Gateway forbid removed',
  'runtime-deny': 'Runtime accepts direct invokes again',
};

export function stepLabel(id: string, restoring = false): string {
  return (restoring ? RESTORE_LABEL[id as StepResult['id']] : STEP_COPY[id as StepResult['id']]?.label) ?? id;
}
export function stepControl(id: string): string {
  return STEP_COPY[id as StepResult['id']]?.control ?? id;
}
export function stepSystem(id: string): string {
  return STEP_COPY[id as StepResult['id']]?.system ?? 'AGP';
}

export const MIN_REASON = 8;
export function canSubmitReason(reason: string): boolean {
  return reason.trim().length >= MIN_REASON;
}

const ms = (iso: string) => new Date(iso).getTime();

export function detectedInSeconds(signal: Pick<SignalView, 'span_end' | 'detected_at'>): number {
  return Math.max(0, Math.round((ms(signal.detected_at) - ms(signal.span_end)) / 1000));
}

export function containedInSeconds(view: Pick<ContainmentView, 'since' | 'steps'>): number | null {
  if (!view.since) return null;
  const done = view.steps.filter((s) => s.status === 'ok' && s.at).map((s) => ms(s.at!));
  if (done.length === 0) return null;
  return Math.max(0, Math.round((Math.max(...done) - ms(view.since)) / 1000));
}

function restoreRequestedAt(view: Pick<ContainmentView, 'since' | 'audit'>): string | null {
  const ats = view.audit
    .filter((r) => r.action === 'restore' && r.result === 'requested' && (!view.since || ms(r.at) >= ms(view.since)))
    .map((r) => r.at);
  return ats.length ? ats.reduce((a, b) => (ms(b) > ms(a) ? b : a)) : null;
}

export function isRestoreJob(view: Pick<ContainmentView, 'job' | 'since' | 'audit'>): boolean {
  if (view.job === 'restoring' || view.job === 'restored') return true;
  return view.job === 'failed' && restoreRequestedAt(view) !== null;
}

export function restoredInSeconds(view: Pick<ContainmentView, 'since' | 'steps' | 'audit'>): number | null {
  const at = restoreRequestedAt(view);
  return at ? containedInSeconds({ since: at, steps: view.steps }) : null;
}

export function maskArns(text: string): string {
  return text.replace(/(arn:aws[a-z-]*:[^:\s]*:[^:\s]*:)\d{12}(?=:)/g, '$1••••');
}

export function formatSeconds(msValue: number): string {
  const s = Math.max(0, msValue) / 1000;
  return Number.isInteger(s) ? `${s} s` : `${s.toFixed(1)} s`;
}

export function pollIntervalMs(view: Pick<ContainmentView, 'job'> | null): number {
  return view?.job === 'running' || view?.job === 'restoring' ? 3000 : 10000;
}

export interface Actor {
  oid: string | null;
  email: string;
}

function isContainer(view: Pick<ContainmentView, 'by_oid'>, user: Actor): boolean {
  return !!view.by_oid && (user.oid === view.by_oid || user.email === view.by_oid);
}

const jobRunning = (view: Pick<ContainmentView, 'job'>) => view.job === 'running' || view.job === 'restoring';

export function restoreBlockedReason(view: ContainmentView, user: Actor | null, canAdmin: boolean): string | null {
  if (view.state !== 'CONTAINED') return 'This agent is not contained.';
  if (jobRunning(view)) return 'Wait for the running job to finish.';
  if (!user) return 'Sign in to restore.';
  if (isContainer(view, user)) return 'Restore requires a different person than the one who contained the agent.';
  if (!canAdmin) return 'Restore needs the Admin role.';
  return null;
}

export function canRestore(view: ContainmentView, user: Actor | null, canAdmin: boolean): boolean {
  return restoreBlockedReason(view, user, canAdmin) === null;
}

export function containBlockedReason(view: ContainmentView, reason: string, canOperate: boolean): string | null {
  if (view.state === 'CONTAINED') return 'This agent is already contained.';
  if (jobRunning(view)) return 'Wait for the running job to finish.';
  if (!canOperate) return 'Contain needs the Operator role.';
  if (!canSubmitReason(reason)) return `Give a reason (at least ${MIN_REASON} characters).`;
  return null;
}

export function createRequestGate() {
  let latest = 0;
  return {
    begin: (): number => ++latest,
    isCurrent: (ticket: number): boolean => ticket === latest,
  };
}

export type InvokeRefusalKind = 'contained' | 'unassigned';

export const INVOKE_REFUSAL_CODES = {
  contained: ['invoke.contained'],
  unassigned: ['obo.not_assigned', 'eval.not_assigned'],
} as const satisfies Record<InvokeRefusalKind, readonly ErrorCode[]>;

export function classifyInvokeRefusal(failure: ApiFailure): InvokeRefusalKind | null {
  return refusalKind(INVOKE_REFUSAL_CODES, failure);
}

const ACTION_COPY: Record<string, string> = {
  contain: 'Contain',
  restore: 'Restore',
  'monitor-on': 'Monitor on',
  'monitor-off': 'Monitor off',
  signal: 'Signal',
};

export function auditWhat(row: Pick<AuditRow, 'action' | 'step'>): string {
  const action = ACTION_COPY[row.action] ?? row.action;
  return row.step ? `${action} · ${stepControl(row.step)}` : action;
}

export function clock(iso: string): string {
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? iso : d.toTimeString().slice(0, 8);
}

const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];

export function auditStamp(iso: string, now: Date = new Date()): string {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  if (d.toDateString() === now.toDateString()) return clock(iso);
  return `${d.getDate()} ${MONTHS[d.getMonth()]} ${d.toTimeString().slice(0, 5)}`;
}

export function auditNewestFirst<T extends Pick<AuditRow, 'at'>>(rows: T[]): T[] {
  return [...rows].reverse().sort((a, b) => ms(b.at) - ms(a.at));
}
