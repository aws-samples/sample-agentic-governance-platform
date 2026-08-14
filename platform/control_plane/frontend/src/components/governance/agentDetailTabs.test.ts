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
    expect(ids).toEqual(['overview', 'access', 'mcp-servers', 'deployment', 'traces', 'cost']);
  });

  it('Registered drops only deployment, preserving order', () => {
    const ids = visibleTabs('Registered').map((t) => t.id);
    expect(ids).toEqual(['overview', 'access', 'mcp-servers', 'traces', 'cost']);
  });

  it('ALL_TABS marks deployment as conditional and others as not', () => {
    const dep = ALL_TABS.find((t) => t.id === 'deployment')!;
    expect(dep.deployedOnly).toBe(true);
    expect(ALL_TABS.filter((t) => t.deployedOnly).length).toBe(1);
  });
});
