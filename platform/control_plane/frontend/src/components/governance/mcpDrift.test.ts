import { describe, it, expect } from 'vitest';
import { computeDrift, declaredServerStatus } from './mcpDrift';

describe('computeDrift: declared is what the agent\'s code will call, granted is the Entra assignments; granted rows first, declared-only after', () => {
  it('declared and granted → connected', () => {
    expect(computeDrift(['m1'], ['m1'])).toEqual([{ id: 'm1', state: 'connected' }]);
  });

  it('declared only → not_granted', () => {
    expect(computeDrift(['m1'], [])).toEqual([{ id: 'm1', state: 'not_granted' }]);
  });

  it('granted only → unused_grant', () => {
    expect(computeDrift(['m1'], ['m1', 'm2'])).toEqual([
      { id: 'm1', state: 'connected' },
      { id: 'm2', state: 'unused_grant' },
    ]);
  });

  it('undefined declared (an agent that never declared) → every granted row is connected, since flagging its grants unused would be a false alarm', () => {
    expect(computeDrift(undefined, ['m1', 'm2'])).toEqual([
      { id: 'm1', state: 'connected' },
      { id: 'm2', state: 'connected' },
    ]);
  });

  it('empty declared (the backend default for agents that never declared) → no unused flags', () => {
    expect(computeDrift([], ['m1'])).toEqual([{ id: 'm1', state: 'connected' }]);
  });

  it('mixes all three states, granted first, then declared-only, without duplicates', () => {
    expect(computeDrift(['m3', 'm1', 'm3'], ['m1', 'm2', 'm1'])).toEqual([
      { id: 'm1', state: 'connected' },
      { id: 'm2', state: 'unused_grant' },
      { id: 'm3', state: 'not_granted' },
    ]);
  });
});

describe('declaredServerStatus', () => {
  const reg = [
    { id: 'm1', name: 'claims-api', identity_status: 'provisioned' as const, entra_sp_id: 'sp1' },
    { id: 'm2', name: 'policy-api', identity_status: 'pending' as const, entra_sp_id: null },
  ];

  it('provisioned server with an SP → grantable, with its name', () => {
    expect(declaredServerStatus('m1', reg)).toEqual({ name: 'claims-api', status: 'grantable' });
  });

  it('server not provisioned yet → unprovisioned', () => {
    expect(declaredServerStatus('m2', reg)).toEqual({ name: 'policy-api', status: 'unprovisioned' });
  });

  it('id not in the registry (deleted/unknown) → missing, named by its id', () => {
    expect(declaredServerStatus('gone', reg)).toEqual({ name: 'gone', status: 'missing' });
  });

  it('registry not loaded (or failed) → unresolved, never a false "missing"', () => {
    expect(declaredServerStatus('m1', null)).toEqual({ name: 'm1', status: 'unresolved' });
  });
});
