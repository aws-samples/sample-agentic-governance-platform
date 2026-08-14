import type { McpServerKind } from '../../api/client';

export interface McpServerTab {
  id: string;
  label: string;
  comingIn: string;
  /** When true, the tab renders only for kind === 'gateway'. */
  gatewayOnly?: boolean;
}

export const ALL_MCP_TABS: McpServerTab[] = [
  { id: 'overview',         label: 'Overview',         comingIn: 'Epic 5' },
  { id: 'tools',            label: 'Tools',            comingIn: 'Epic 5' },
  { id: 'policies',         label: 'Policies',         comingIn: 'Live', gatewayOnly: true },
  { id: 'connected-agents', label: 'Connected Agents', comingIn: 'Epic 7' },
  { id: 'audit',            label: 'Audit',            comingIn: 'Epic 10' },
];

/** Tabs visible for an MCP server of the given kind (drops Policies for standard). */
export function visibleMcpTabs(kind: McpServerKind): McpServerTab[] {
  return ALL_MCP_TABS.filter((t) => !t.gatewayOnly || kind === 'gateway');
}
