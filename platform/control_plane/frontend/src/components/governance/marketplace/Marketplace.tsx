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

      {active === 'agents' ? <MarketplaceAgents /> : <MarketplaceMcps />}
    </div>
  );
}
