import { describe, it, expect } from 'vitest';
import { visibleTabs, ALL_TABS } from './agentDetailTabs';

describe('visibleTabs', () => {
  it('includes Deployment for Deployed agents', () => {
    const ids = visibleTabs('Deployed').map((t) => t.id);
    expect(ids).toContain('deployment');
  });

  it('omits Deployment for Registered agents', () => {
    const ids = visibleTabs('Registered').map((t) => t.id);
    expect(ids).not.toContain('deployment');
  });

  it('always includes the non-conditional tabs in order for Deployed', () => {
    const ids = visibleTabs('Deployed').map((t) => t.id);
    expect(ids).toEqual(['overview', 'access', 'mcp-servers', 'deployment', 'traces', 'cost', 'evaluations', 'containment']);
  });

  it('Registered drops only deployment, keeping evaluations and order: agents built in Ops are Registered (Deployed is seed-only), so Evaluations never keys on origin', () => {
    const ids = visibleTabs('Registered').map((t) => t.id);
    expect(ids).toEqual(['overview', 'access', 'mcp-servers', 'traces', 'cost', 'evaluations', 'containment']);
  });

  it('ALL_TABS marks only deployment as conditional', () => {
    const dep = ALL_TABS.find((t) => t.id === 'deployment')!;
    expect(dep.deployedOnly).toBe(true);
    expect(ALL_TABS.filter((t) => t.deployedOnly).map((t) => t.id)).toEqual(['deployment']);
  });
});
