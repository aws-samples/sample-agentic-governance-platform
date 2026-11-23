import type { Origin } from '../../api/client';

export interface AgentTab {
  id: string;
  label: string;
  deployedOnly?: boolean;
}

export const ALL_TABS: AgentTab[] = [
  { id: 'overview',    label: 'Overview' },
  { id: 'access',      label: 'Access' },
  { id: 'mcp-servers', label: 'MCP Servers' },
  { id: 'deployment',  label: 'Deployment', deployedOnly: true },
  { id: 'traces',      label: 'Traces' },
  { id: 'cost',        label: 'Cost' },
  { id: 'evaluations', label: 'Evaluations' },
  { id: 'containment', label: 'Containment' },
];

export function visibleTabs(origin: Origin): AgentTab[] {
  return ALL_TABS.filter((t) => !t.deployedOnly || origin === 'Deployed');
}
