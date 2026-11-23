import { describe, expect, expectTypeOf, it } from 'vitest';
import { ApiFailure, type ErrorCode } from '../../../api/failure';
import type { AuditRow, ContainmentView } from '../../../api/client';
import { containmentView } from '../../../test/wire';
import {
  INVOKE_REFUSAL_CODES,
  STEP_ORDER,
  auditNewestFirst,
  auditStamp,
  auditWhat,
  canRestore,
  canSubmitReason,
  classifyInvokeRefusal,
  clock,
  containBlockedReason,
  createRequestGate,
  containedInSeconds,
  detectedInSeconds,
  formatSeconds,
  isRestoreJob,
  maskArns,
  pollIntervalMs,
  restoreBlockedReason,
  restoredInSeconds,
  stepControl,
  stepLabel,
  stepSystem,
} from './containmentLogic';

const SINCE = '2026-10-16T10:44:05Z';

const CONTAINED_FIELDS: Partial<ContainmentView> = {
  state: 'CONTAINED',
  job: 'contained',
  since: SINCE,
  by_name: 'Lars Svensson',
  by_oid: 'oid-lars',
  reason: 'suspected prompt injection',
  steps: [
    { id: 'refuse-invokes', status: 'ok', detail: '', native_ref: null, at: '2026-10-16T10:44:05.400Z' },
    { id: 'cedar-forbid', status: 'ok', detail: '', native_ref: 'pol-1', at: '2026-10-16T10:44:09Z' },
    { id: 'runtime-deny', status: 'skipped', detail: 'no runtime', native_ref: null, at: '2026-10-16T10:44:30Z' },
  ],
};

describe('timings the demo narrates', () => {
  it('detected in = signal written minus the judged span end, never negative', () => {
    expect(detectedInSeconds({ span_end: '2026-10-16T10:42:00Z', detected_at: '2026-10-16T10:42:14.400Z' })).toBe(14);
    expect(detectedInSeconds({ span_end: '2026-10-16T10:42:05Z', detected_at: '2026-10-16T10:42:00Z' })).toBe(0);
  });

  it('contained in = the last OK step minus the click (skipped steps do not count)', () => {
    expect(containedInSeconds(containmentView(CONTAINED_FIELDS))).toBe(4);
    expect(containedInSeconds(containmentView({ ...CONTAINED_FIELDS, steps: [] }))).toBeNull();
    expect(containedInSeconds(containmentView({ ...CONTAINED_FIELDS, since: null }))).toBeNull();
  });

  it('formats seconds', () => {
    expect(formatSeconds(41000)).toBe('41 s');
    expect(formatSeconds(7400)).toBe('7.4 s');
    expect(formatSeconds(-5)).toBe('0 s');
  });
});

describe('step copy', () => {
  it('runs in the pinned order', () => {
    expect(STEP_ORDER).toEqual(['refuse-invokes', 'cedar-forbid', 'runtime-deny']);
  });

  it('names the native control under each step', () => {
    expect(stepControl('refuse-invokes')).toBe('AGP invoke gate');
    expect(stepControl('cedar-forbid')).toBe('Gateway policy (Cedar)');
    expect(stepControl('runtime-deny')).toBe('Runtime resource policy');
    for (const id of STEP_ORDER) expect(stepLabel(id)).not.toBe('');
  });

  it('an unknown step id (a newer backend) reads as the id itself, never blank', () => {
    expect(stepLabel('quarantine-session')).toBe('quarantine-session');
    expect(stepLabel('quarantine-session', true)).toBe('quarantine-session');
    expect(stepControl('quarantine-session')).toBe('quarantine-session');
    expect(stepSystem('quarantine-session')).toBe('AGP');
  });
});

describe('who may restore', () => {
  const other = { oid: 'oid-jannis', email: 'jkambach@example.com' };

  it('a second person may restore a contained agent', () => {
    expect(canRestore(containmentView(CONTAINED_FIELDS), other, true)).toBe(true);
    expect(restoreBlockedReason(containmentView(CONTAINED_FIELDS), other, true)).toBeNull();
  });

  it('the person who contained may not, matched on oid or email', () => {
    expect(canRestore(containmentView(CONTAINED_FIELDS), { oid: 'oid-lars', email: 'lars@example.com' }, true)).toBe(false);
    expect(canRestore(containmentView({ ...CONTAINED_FIELDS, by_oid: 'lars@example.com' }), { oid: null, email: 'lars@example.com' }, true)).toBe(false);
    expect(restoreBlockedReason(containmentView(CONTAINED_FIELDS), { oid: 'oid-lars', email: 'x' }, true)).toMatch(/different person/);
  });

  it('nothing to restore unless contained and settled', () => {
    expect(canRestore(containmentView({ ...CONTAINED_FIELDS, state: 'RESTORED', job: 'restored' }), other, true)).toBe(false);
    expect(canRestore(containmentView({ ...CONTAINED_FIELDS, state: null, job: null }), other, true)).toBe(false);
    expect(canRestore(containmentView({ ...CONTAINED_FIELDS, job: 'running' }), other, true)).toBe(false);
    expect(canRestore(containmentView({ ...CONTAINED_FIELDS, job: 'failed' }), other, true)).toBe(true);
    expect(canRestore(containmentView(CONTAINED_FIELDS), null, true)).toBe(false);
  });
});

describe('form and polling', () => {
  it('a reason needs at least 8 non-blank characters', () => {
    expect(canSubmitReason('   short ')).toBe(false);
    expect(canSubmitReason('  misuse!  ')).toBe(false);
    expect(canSubmitReason('tool misuse')).toBe(true);
  });

  it('polls fast while a job runs, slow otherwise', () => {
    expect(pollIntervalMs(containmentView({ ...CONTAINED_FIELDS, job: 'running' }))).toBe(3000);
    expect(pollIntervalMs(containmentView({ ...CONTAINED_FIELDS, job: 'restoring' }))).toBe(3000);
    expect(pollIntervalMs(containmentView(CONTAINED_FIELDS))).toBe(10000);
    expect(pollIntervalMs(null)).toBe(10000);
  });

});

describe('classifyInvokeRefusal: the invoke panel reads the refusal\'s code, never its status or sentence', () => {
  function failure(status: number, code: ErrorCode | null = null, detail?: string): ApiFailure {
    return new ApiFailure(status, code, detail, detail ?? `Request failed with status code ${status}`);
  }

  it('the gate\'s 409 code is contained; the containment tab\'s own 409s, "already contained" and "not contained", are not', () => {
    expect(classifyInvokeRefusal(failure(409, 'invoke.contained', 'agent is contained'))).toBe('contained');
    expect(classifyInvokeRefusal(failure(409, 'containment.already_contained', 'agent is already contained'))).toBeNull();
    expect(classifyInvokeRefusal(failure(409, 'containment.not_contained', 'agent is not contained'))).toBeNull();
  });

  it('a 403 carrying the Entra code or the eval runner\'s is unassigned; a codeless 403 is not', () => {
    expect(classifyInvokeRefusal(failure(403, 'obo.not_assigned', 'x is not assigned to this agent'))).toBe('unassigned');
    expect(classifyInvokeRefusal(failure(403, 'eval.not_assigned', 'x is not assigned to this agent'))).toBe('unassigned');
    expect(classifyInvokeRefusal(failure(403))).toBeNull();
    expect(classifyInvokeRefusal(failure(403, 'auth.role_required', 'Requires operator role or higher'))).toBeNull();
  });

  it('the sentence alone classifies nothing', () => {
    expect(classifyInvokeRefusal(failure(500, null, 'agent is contained'))).toBeNull();
    expect(classifyInvokeRefusal(failure(502, 'invoke.upstream_error', 'x is not assigned to this agent'))).toBeNull();
    expect(classifyInvokeRefusal(failure(0))).toBeNull();
  });

  it('the table is typed on the generated codes, so a renamed kind fails tsc where the table names it', () => {
    expectTypeOf(INVOKE_REFUSAL_CODES).toExtend<Record<'contained' | 'unassigned', readonly ErrorCode[]>>();
  });
});

describe('audit lines', () => {
  it('names the step control, else the action', () => {
    expect(auditWhat({ action: 'contain', step: 'cedar-forbid' })).toBe('Contain · Gateway policy (Cedar)');
    expect(auditWhat({ action: 'contain', step: null })).toBe('Contain');
    expect(auditWhat({ action: 'monitor-on', step: null })).toBe('Monitor on');
    expect(auditWhat({ action: 'signal', step: null })).toBe('Signal');
  });

  it('stamps today with the time only, other days with a short date', () => {
    const now = new Date(2026, 9, 14, 12, 0, 0);
    expect(auditStamp(new Date(2026, 9, 14, 8, 41, 7).toISOString(), now)).toBe('08:41:07');
    expect(auditStamp(new Date(2026, 9, 13, 22, 51, 52).toISOString(), now)).toBe('13 Oct 22:51');
    expect(auditStamp('not-a-date', now)).toBe('not-a-date');
  });

  it('clock is HH:MM:SS local time, or the raw value when it does not parse', () => {
    expect(clock(new Date(2026, 9, 14, 8, 41, 7).toISOString())).toBe('08:41:07');
    expect(clock('not-a-date')).toBe('not-a-date');
  });

  it('lists newest first; equal stamps keep the later-appended row on top', () => {
    const rows = [
      { at: '2026-10-13T10:00:00Z', id: 'old' },
      { at: '2026-10-14T08:00:00Z', id: 'a' },
      { at: '2026-10-14T08:00:00Z', id: 'b' },
      { at: '2026-10-14T09:00:00Z', id: 'new' },
    ];
    expect(auditNewestFirst(rows).map((r) => r.id)).toEqual(['new', 'b', 'a', 'old']);
    expect(rows[0].id).toBe('old');
  });
});

describe('role gates', () => {
  const other = { oid: 'oid-jannis', email: 'jkambach@example.com' };

  it('Restore needs Admin', () => {
    expect(canRestore(containmentView(CONTAINED_FIELDS), other, false)).toBe(false);
    expect(restoreBlockedReason(containmentView(CONTAINED_FIELDS), other, false)).toBe('Restore needs the Admin role.');
  });

  it('the container sees the four-eyes reason even without Admin', () => {
    expect(restoreBlockedReason(containmentView(CONTAINED_FIELDS), { oid: 'oid-lars', email: 'x' }, false)).toBe(
      'Restore requires a different person than the one who contained the agent.',
    );
  });

  it('Contain needs Operator, a reason, and an agent that is not contained', () => {
    const fresh = containmentView({ ...CONTAINED_FIELDS, state: null, job: null, steps: [] });
    expect(containBlockedReason(fresh, 'tool misuse', true)).toBeNull();
    expect(containBlockedReason(fresh, 'tool misuse', false)).toBe('Contain needs the Operator role.');
    expect(containBlockedReason(fresh, 'short', true)).toBe('Give a reason (at least 8 characters).');
    expect(containBlockedReason(containmentView(CONTAINED_FIELDS), 'tool misuse', true)).toBe('This agent is already contained.');
    expect(containBlockedReason(containmentView({ ...CONTAINED_FIELDS, state: 'RESTORED', job: 'restoring' }), 'tool misuse', true)).toMatch(/running job/);
  });
});

describe('request gate', () => {
  it('only the latest ticket applies: an action supersedes an in-flight poll', () => {
    const gate = createRequestGate();
    const poll = gate.begin();
    expect(gate.isCurrent(poll)).toBe(true);
    gate.begin();
    expect(gate.isCurrent(poll)).toBe(false);
    const next = gate.begin();
    expect(gate.isCurrent(next)).toBe(true);
  });
});

describe('restore wording and timing', () => {
  const RESTORE_AT = '2026-10-16T11:00:00Z';
  const requested: AuditRow = { at: RESTORE_AT, actor_name: 'jkambach', action: 'restore', step: null, result: 'requested', detail: 'false positive', native_ref: null };
  const restoreSteps = [
    { id: 'refuse-invokes' as const, status: 'ok' as const, detail: 'AGP accepts invokes again', native_ref: null, at: '2026-10-16T11:00:05Z' },
    { id: 'cedar-forbid' as const, status: 'ok' as const, detail: '', native_ref: null, at: '2026-10-16T11:00:03Z' },
  ];

  it('a restore job is the restoring/restored job, or a failed one after a restore request made since the current contain', () => {
    expect(isRestoreJob(containmentView({ ...CONTAINED_FIELDS, job: 'restoring' }))).toBe(true);
    expect(isRestoreJob(containmentView({ ...CONTAINED_FIELDS, job: 'restored' }))).toBe(true);
    expect(isRestoreJob(containmentView({ ...CONTAINED_FIELDS, job: 'contained' }))).toBe(false);
    expect(isRestoreJob(containmentView({ ...CONTAINED_FIELDS, job: 'failed' }))).toBe(false);
    expect(isRestoreJob(containmentView({ ...CONTAINED_FIELDS, job: 'failed', audit: [requested] }))).toBe(true);
    expect(isRestoreJob(containmentView({ ...CONTAINED_FIELDS, job: 'failed', audit: [{ ...requested, at: '2026-10-16T10:00:00Z' }] }))).toBe(false);
  });

  it('restored in = the last OK restore step minus the restore request', () => {
    expect(restoredInSeconds(containmentView({ ...CONTAINED_FIELDS, job: 'restored', steps: restoreSteps, audit: [requested] }))).toBe(5);
    expect(restoredInSeconds(containmentView({ ...CONTAINED_FIELDS, job: 'restored', steps: restoreSteps, audit: [] }))).toBeNull();
  });

  it('restore labels say what is lifted', () => {
    expect(stepLabel('refuse-invokes', true)).toBe('AGP accepts invokes again');
    expect(stepLabel('cedar-forbid', true)).toBe('Gateway forbid removed');
    expect(stepLabel('runtime-deny', true)).toBe('Runtime accepts direct invokes again');
    expect(stepLabel('refuse-invokes')).toBe('Refuse invokes through AGP (409)');
  });
});

describe('maskArns (display only)', () => {
  it('masks the account id in every ARN and leaves the rest; the full value is for the title attribute only', () => {
    expect(maskArns('arn:aws:bedrock-agentcore:us-east-1:123456789012:runtime/claims_rogue_dev-O6o1UGFR4c: ok'))
      .toBe('arn:aws:bedrock-agentcore:us-east-1:••••:runtime/claims_rogue_dev-O6o1UGFR4c: ok');
    expect(maskArns('a,arn:aws:iam::123456789012:role/x,arn:aws-us-gov:sts::210987654321:assumed-role/y'))
      .toBe('a,arn:aws:iam::••••:role/x,arn:aws-us-gov:sts::••••:assumed-role/y');
    expect(maskArns('YJrWa2Ga1ZGH: deleted (agppolea636f)')).toBe('YJrWa2Ga1ZGH: deleted (agppolea636f)');
  });
});
