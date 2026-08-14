// Marketplace — the tabbed shell collapsing the two catalog pages behind one
// /marketplace destination (Epic 15). A slim header band carries the
// [ Agents | MCP Servers ] pill tab bar; the active tab body is the existing
// MarketplaceAgents / MarketplaceMcps page, reused unchanged. The active tab is
// derived from the router pathname (pure marketplaceTabFromPath) so deep-links
// to /marketplace/agents · /marketplace/mcps and browser back/forward work;
// clicking a tab navigates to its canonical child path.
//
// Each body owns its own page wrapper + <h1>, so the tab bar sits in its own
// slim band above the body rather than competing with the body heading.

import { useLocation, useNavigate } from 'react-router-dom';
import { MARKETPLACE_TABS, marketplaceTabFromPath } from './marketplaceTabs';
import MarketplaceAgents from './MarketplaceAgents';
import MarketplaceMcps from './MarketplaceMcps';

export default function Marketplace() {
  const { pathname } = useLocation();
  const navigate = useNavigate();
  const active = marketplaceTabFromPath(pathname);

  return (
    <div className="min-h-[calc(100vh-4rem)] relative">
      {/* Slim header band — the pill tab bar (AgentDetail idiom: active tint +
          shadow-sm pill, transition-colors only, no scale/translate motion). */}
      <div className="relative max-w-7xl mx-auto px-6 pt-6">
        <div className="flex items-center gap-1 p-1 bg-slate-100/80 rounded-xl w-fit overflow-x-auto">
          {MARKETPLACE_TABS.map((tab) => (
            <button
              key={tab.key}
              onClick={() => navigate(tab.to)}
              className={`px-3.5 py-1.5 rounded-lg text-sm font-medium transition-colors whitespace-nowrap ${active === tab.key ? 'bg-white text-slate-900 shadow-sm' : 'text-slate-500 hover:text-slate-700'}`}
            >
              {tab.label}
            </button>
          ))}
        </div>
      </div>

      {/* Active tab body — the existing catalog page, reused unchanged. */}
      {active === 'agents' ? <MarketplaceAgents /> : <MarketplaceMcps />}
    </div>
  );
}
