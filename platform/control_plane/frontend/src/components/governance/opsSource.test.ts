import { describe, it, expect } from 'vitest';

import { BUILT_IN_OPS_LABEL, opsProvenance } from './opsSource';

const originBadge = (o: string) => ({ cls: 'x', label: o === 'Deployed' ? 'Deployed' : 'Registered' });

describe('opsProvenance: derived from the agent → repository edge, never from the stored origin, which reads Registered for an Ops-built agent', () => {
  it('an agent with an Ops repository reads "Built in Ops" and links to its Ops agent page', () => {
    const p = opsProvenance('Registered', { repo_id: 'r-1', name: 'fnol-e40' }, originBadge);
    expect(p.badge.label).toBe(BUILT_IN_OPS_LABEL);
    expect(p.source).toEqual({ name: 'fnol-e40', to: '/ops/agents/r-1' });
  });
  it('no repository (404 → null) keeps the stored origin and shows no chip', () => {
    for (const repo of [null, undefined]) {
      const p = opsProvenance('Registered', repo, originBadge);
      expect(p.badge.label).toBe('Registered');
      expect(p.source).toBeNull();
    }
  });
});
