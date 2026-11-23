import { describe, it, expect } from 'vitest';
import { demoReducer, INITIAL_DEMO_STATE } from './demoStore';
import { TENANTS } from './demoData';

describe('demoReducer', () => {
  it('ADD_EXPERIMENT appends a sandbox tenant without mutating the initial state', () => {
    const t = { ...TENANTS[0], id: 'exp-new', kind: 'sandbox' as const };
    const s = demoReducer(INITIAL_DEMO_STATE, { type: 'ADD_EXPERIMENT', tenant: t });
    expect(s.experiments.at(-1)!.id).toBe('exp-new');
    expect(INITIAL_DEMO_STATE.experiments).not.toContain(t);
  });
});
