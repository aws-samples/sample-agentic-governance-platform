import { describe, it, expect } from 'vitest';
import { visibleMcpTabs, ALL_MCP_TABS } from './mcpServerDetailTabs';

describe('visibleMcpTabs', () => {
  it('includes Policies for gateway MCP servers', () => {
    const ids = visibleMcpTabs('gateway').map((t) => t.id);
    expect(ids).toContain('policies');
  });

  it('omits Policies for standard MCP servers', () => {
    const ids = visibleMcpTabs('standard').map((t) => t.id);
    expect(ids).not.toContain('policies');
  });

  it('always includes overview and tools for both kinds', () => {
    for (const kind of ['gateway', 'standard'] as const) {
      const ids = visibleMcpTabs(kind).map((t) => t.id);
      expect(ids).toContain('overview');
      expect(ids).toContain('tools');
    }
  });

  it('gateway shows all tabs in order', () => {
    const ids = visibleMcpTabs('gateway').map((t) => t.id);
    expect(ids).toEqual(['overview', 'tools', 'policies', 'connected-agents', 'audit']);
  });

  it('standard drops only policies, preserving order', () => {
    const ids = visibleMcpTabs('standard').map((t) => t.id);
    expect(ids).toEqual(['overview', 'tools', 'connected-agents', 'audit']);
  });

  it('ALL_MCP_TABS marks policies as gateway-only and others as not', () => {
    const policies = ALL_MCP_TABS.find((t) => t.id === 'policies')!;
    expect(policies.gatewayOnly).toBe(true);
    expect(ALL_MCP_TABS.filter((t) => t.gatewayOnly).length).toBe(1);
  });
});
