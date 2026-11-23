export type MarketplaceTab = 'agents' | 'mcps';

export const MARKETPLACE_TABS: readonly { key: MarketplaceTab; label: string; to: string }[] = [
  { key: 'agents', label: 'Agents', to: '/marketplace/agents' },
  { key: 'mcps', label: 'MCP Servers', to: '/marketplace/mcps' },
] as const;

export function marketplaceTabFromPath(pathname: string): MarketplaceTab {
  return pathname.startsWith('/marketplace/mcps') ? 'mcps' : 'agents';
}
