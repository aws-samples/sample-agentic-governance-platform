/**
 * Pure helpers for the tabbed Marketplace shell (Epic 15). The active tab is
 * derived from the router pathname so deep-links + browser back/forward work.
 */
export type MarketplaceTab = 'agents' | 'mcps';

export const MARKETPLACE_TABS: readonly { key: MarketplaceTab; label: string; to: string }[] = [
  { key: 'agents', label: 'Agents', to: '/marketplace/agents' },
  { key: 'mcps', label: 'MCP Servers', to: '/marketplace/mcps' },
] as const;

/** Map a router pathname to the active Marketplace tab. Anything that isn't the
 *  explicit MCP path (incl. the bare `/marketplace` entry) resolves to 'agents'. */
export function marketplaceTabFromPath(pathname: string): MarketplaceTab {
  return pathname.startsWith('/marketplace/mcps') ? 'mcps' : 'agents';
}
