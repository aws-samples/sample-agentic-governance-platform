import type { Origin } from '../../api/client';

export interface AgentTab {
  id: string;
  label: string;
  /** Epic that fills this tab's real content; shown in the stub. */
  comingIn: string;
  /** When true, the tab renders only for origin === 'Deployed'. */
  deployedOnly?: boolean;
}

export const ALL_TABS: AgentTab[] = [
  { id: 'overview',    label: 'Overview',    comingIn: 'Epic 4' },
  { id: 'access',      label: 'Access',      comingIn: 'Epic 6' },
  // The agent-direction half of the E7 two-direction grant story (the mirror of
  // the MCP detail "Connected Agents" tab): which MCP servers this agent reaches.
  { id: 'mcp-servers', label: 'MCP Servers', comingIn: 'Epic 7' },
  // NOTE: "MCPs / Tools" and "Policies" used to sit here. Both only ever rendered an
  // empty stub on the AGENT detail page — the agent's tool surface is already covered by
  // MCP Servers, and Cedar per-tool policies belong to the gateway MCP (they live on the
  // MCP-server detail page, which is where they are authored and enforced).
  { id: 'deployment',  label: 'Deployment',  comingIn: 'Epic 4', deployedOnly: true },
  // Live as of Epic 26 (Langfuse base observability) — real per-agent Traces +
  // Cost tabs, no longer stubs (comingIn now records the delivering epic, as with
  // the other live tabs; it does not gate any "coming soon" affordance).
  { id: 'traces',      label: 'Traces',      comingIn: 'Epic 26' },
  { id: 'cost',        label: 'Cost',        comingIn: 'Epic 26' },
];

/** Tabs visible for an agent of the given origin (drops Deployment for Registered). */
export function visibleTabs(origin: Origin): AgentTab[] {
  return ALL_TABS.filter((t) => !t.deployedOnly || origin === 'Deployed');
}
