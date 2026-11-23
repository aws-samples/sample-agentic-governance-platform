import { describe, it, expect } from 'vitest';
import { visibleMcpTabs, ALL_MCP_TABS } from './mcpServerDetailTabs';
import { stripComments } from '../../rawSource';
import connectedAgentsRaw from './McpConnectedAgentsTab.tsx?raw';

const connectedAgentsSrc = stripComments(connectedAgentsRaw);

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
    expect(ids).toEqual(['overview', 'tools', 'policies', 'connected-agents']);
  });

  it('standard drops only policies, preserving order', () => {
    const ids = visibleMcpTabs('standard').map((t) => t.id);
    expect(ids).toEqual(['overview', 'tools', 'connected-agents']);
  });

  it('has no Audit tab: it rendered only its own label', () => {
    expect(ALL_MCP_TABS.map((t) => t.id)).not.toContain('audit');
  });

  it('ALL_MCP_TABS marks policies as gateway-only and others as not', () => {
    const policies = ALL_MCP_TABS.find((t) => t.id === 'policies')!;
    expect(policies.gatewayOnly).toBe(true);
    expect(ALL_MCP_TABS.filter((t) => t.gatewayOnly).length).toBe(1);
  });
});

describe('McpConnectedAgentsTab copy', () => {
  it('offers “Grant agent access” on the button and names it in the empty-state hint: the words a user reads, which a type rename must never rewrite', () => {
    expect(connectedAgentsSrc).toContain('+ Grant agent access');
    expect(connectedAgentsSrc).toContain('Use “Grant agent access” to let an agent call this MCP server.');
  });
});
