import type { Kind } from '../../api/client';

export interface McpServerTab {
  id: string;
  label: string;
  gatewayOnly?: boolean;
}

export const ALL_MCP_TABS: McpServerTab[] = [
  { id: 'overview',         label: 'Overview' },
  { id: 'tools',            label: 'Tools' },
  { id: 'policies',         label: 'Policies', gatewayOnly: true },
  { id: 'connected-agents', label: 'Connected Agents' },
];

export function visibleMcpTabs(kind: Kind): McpServerTab[] {
  return ALL_MCP_TABS.filter((t) => !t.gatewayOnly || kind === 'gateway');
}
