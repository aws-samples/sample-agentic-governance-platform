import { afterEach, describe, it, expect, vi } from 'vitest';
import { ApiFailure } from '../../api/failure';
import {
  GATE_TONE_CLS,
  OVERRIDE_REASON_MAX,
  canSubmitPromote,
  evidenceLine,
  governanceLine,
  isEvalGateRefusal,
  overrideReasonToSend,
  promoteGateView,
  regressionLine,
} from './promoteGate';

afterEach(() => vi.restoreAllMocks());

describe('regressionLine', () => {
  it('names every regression vs prod, or nothing', () => {
    expect(regressionLine(['Judges 0.62, down from 0.86 on prod build b1a0bca', 'Checks 10/12 passed, prod build b1a0bca passed 12/12']))
      .toBe('Regression vs prod: Judges 0.62, down from 0.86 on prod build b1a0bca; Checks 10/12 passed, prod build b1a0bca passed 12/12');
    expect(regressionLine([])).toBeNull();
    expect(regressionLine(null)).toBeNull();
    expect(regressionLine(undefined)).toBeNull();
  });
});

describe('promoteGateView', () => {
  it('passed is green and needs no override', () => {
    const v = promoteGateView('passed');
    expect(v.tone).toBe('ok');
    expect(v.needsOverride).toBe(false);
  });
  it('failed and none are amber and need an override', () => {
    for (const g of ['failed', 'none'] as const) {
      const v = promoteGateView(g);
      expect(v.tone).toBe('warn');
      expect(v.needsOverride).toBe(true);
    }
    expect(promoteGateView('none').label).toMatch(/not evaluated/i);
  });
  it('running is grey and allows an override', () => {
    const v = promoteGateView('running');
    expect(v.tone).toBe('muted');
    expect(v.needsOverride).toBe(true);
    expect(v.note).toMatch(/wait|override/i);
  });
  it('unknown (null/absent) fails closed like the backend: no agent or an unreadable eval index is not a passing run', () => {
    expect(promoteGateView(null).needsOverride).toBe(true);
    expect(promoteGateView(undefined).needsOverride).toBe(true);
  });
  it('renders the raw word with the gate closed for a value outside the union, without throwing', () => {
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => undefined);
    const v = promoteGateView('verified' as never);
    expect(v.label).toContain('"verified"');
    expect(v.note).toContain('"verified"');
    expect(v.needsOverride).toBe(true);
    expect(v.tone).toBe('muted');
    promoteGateView('verified' as never);
    expect(warn).toHaveBeenCalledTimes(1);
  });
  it('every tone has classes', () => {
    for (const g of ['passed', 'failed', 'running', 'none', null] as const) {
      expect(GATE_TONE_CLS[promoteGateView(g).tone]).toBeDefined();
    }
  });
});

describe('canSubmitPromote', () => {
  it('passes without a reason when the gate passed', () => {
    expect(canSubmitPromote(promoteGateView('passed'), '')).toBe(true);
  });
  it('needs a non-blank reason otherwise', () => {
    const v = promoteGateView('failed');
    expect(canSubmitPromote(v, '')).toBe(false);
    expect(canSubmitPromote(v, '   ')).toBe(false);
    expect(canSubmitPromote(v, 'Hotfix for incident 42')).toBe(true);
  });
  it('refuses a reason over the backend cap', () => {
    const v = promoteGateView('none');
    expect(canSubmitPromote(v, 'x'.repeat(OVERRIDE_REASON_MAX))).toBe(true);
    expect(canSubmitPromote(v, 'x'.repeat(OVERRIDE_REASON_MAX + 1))).toBe(false);
  });
});

describe('overrideReasonToSend', () => {
  it('sends nothing when the gate passed, even if text was typed', () => {
    expect(overrideReasonToSend(promoteGateView('passed'), 'leftover')).toBeUndefined();
  });
  it('sends the trimmed reason when an override is needed', () => {
    expect(overrideReasonToSend(promoteGateView('failed'), '  why  ')).toBe('why');
    expect(overrideReasonToSend(promoteGateView('failed'), '  ')).toBeUndefined();
  });
});

describe('isEvalGateRefusal', () => {
  const sentence = 'Prod promotion needs a passing evaluation for this build (or an override with a reason).';
  it('recognises the project.eval_gate code whatever the detail says, and nothing else', () => {
    expect(isEvalGateRefusal(new ApiFailure(409, 'project.eval_gate', sentence, sentence))).toBe(true);
    expect(isEvalGateRefusal(new ApiFailure(409, 'project.eval_gate', 'reworded', 'reworded'))).toBe(true);
    expect(isEvalGateRefusal(new ApiFailure(409, 'project.promote_in_flight', 'a promotion is already in flight', 'a promotion is already in flight'))).toBe(false);
  });
  it('does not read the sentence: the same words under no code, a bare Error or nothing at all are not the gate', () => {
    expect(isEvalGateRefusal(new ApiFailure(409, null, sentence, sentence))).toBe(false);
    expect(isEvalGateRefusal(new Error(sentence))).toBe(false);
    expect(isEvalGateRefusal(null)).toBe(false);
  });
});

describe('evidenceLine', () => {
  const ev = {
    run_id: 'run-1', status: 'failed', checks_passed: 10, checks_total: 12,
    judge_means: { 'Builtin.Correctness': 0.74 }, judge_min_mean: 0.8,
  };
  it('states checks first, then the judge mean against the threshold', () => {
    expect(evidenceLine(ev)).toBe('10/12 checks passed · Correctness 0.74 < 0.80');
  });
  it('quotes a passing judge with ≥', () => {
    expect(evidenceLine({ ...ev, checks_passed: 12, judge_means: { 'Builtin.Correctness': 0.9 } }))
      .toBe('12/12 checks passed · Correctness 0.90 ≥ 0.80');
  });
  it('says an errored run scored nothing, and is silent while running or with no run', () => {
    expect(evidenceLine({ ...ev, status: 'error', checks_passed: null, checks_total: null, judge_means: {} }))
      .toBe('The run errored before scoring');
    expect(evidenceLine({ ...ev, status: 'running', checks_passed: null, checks_total: null, judge_means: {} })).toBeNull();
    expect(evidenceLine(null)).toBeNull();
    expect(evidenceLine(undefined)).toBeNull();
  });
});

describe('promoteGateView inconclusive', () => {
  it('a failed gate whose run errored reads Inconclusive, still needing an override', () => {
    const v = promoteGateView('failed', 'error');
    expect(v.label).toBe('Evaluation inconclusive');
    expect(v.needsOverride).toBe(true);
    expect(promoteGateView('failed', 'failed').label).toBe('Evaluation failed');
  });
});

describe('governanceLine', () => {
  it('names the agent\'s governance status, or nothing when unknown', () => {
    expect(governanceLine('approved')).toEqual({ text: 'Governance: Approved', ok: true });
    expect(governanceLine('proposed')).toEqual({ text: 'Governance: Proposed', ok: false });
    expect(governanceLine(undefined)).toBeNull();
    expect(governanceLine(null)).toBeNull();
  });
});
