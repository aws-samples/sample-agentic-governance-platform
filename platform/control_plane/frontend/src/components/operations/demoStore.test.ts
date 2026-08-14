import { describe, it, expect } from 'vitest';
import { demoReducer, INITIAL_DEMO_STATE } from './demoStore';
import { TENANTS } from './demoData';

describe('demoReducer', () => {
  it('ADD_EXPERIMENT appends a sandbox tenant', () => {
    const t = { ...TENANTS[0], id: 'exp-new', kind: 'sandbox' as const };
    const s = demoReducer(INITIAL_DEMO_STATE, { type: 'ADD_EXPERIMENT', tenant: t });
    expect(s.experiments.at(-1)!.id).toBe('exp-new');
    expect(INITIAL_DEMO_STATE.experiments).not.toContain(t); // immutable
  });
  it('PROMOTE_AGENT records the built agent', () => {
    const agent = { name: 'Claims Triage Agent', useCase: 'triage claims', kind: 'agent' as const, model: 'Claude Opus 4.8', account: 'aws://9001-0002-0003', costPerRun: 0.012 };
    const s = demoReducer(INITIAL_DEMO_STATE, { type: 'PROMOTE_AGENT', agent });
    expect(s.builtAgent!.name).toBe('Claims Triage Agent');
  });
});
