import { useEffect, useMemo, useRef, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { marketplaceApi } from '../../../api/client';
import { useLoad } from '../../../api/useLoad';
import type { AgentLite, ProductCard } from '../../../api/client';
import MarketplaceCard from './MarketplaceCard';
import SubscribeModal from './SubscribeModal';
import { buildSubscribeBody } from './marketplaceForm';

const RECONCILE_MS = 600;
const EMPTY_CATALOG: { products: ProductCard[]; agents: AgentLite[] } = { products: [], agents: [] };

export default function MarketplaceMcps() {
  const navigate = useNavigate();
  const { state, reload, patch } = useLoad(
    () =>
      Promise.all([marketplaceApi.listMcpProducts(), marketplaceApi.listEligibleAgents()]).then(
        ([products, agents]) => ({ products, agents }),
      ),
    [],
    'Failed to load MCP servers.',
  );
  const { products, agents: pickerAgents } = state.data ?? EMPTY_CATALOG;
  const loading = state.kind === 'loading';
  const error = state.kind === 'error' ? state.message : null;
  const [search, setSearch] = useState('');

  const [subscribing, setSubscribing] = useState<ProductCard | null>(null);

  const mountedRef = useRef(true);
  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
    };
  }, []);

  const reconcile = async () => {
    const fresh = await marketplaceApi.listMcpProducts().catch(() => null);
    if (fresh !== null && mountedRef.current) patch((catalog) => ({ ...catalog, products: fresh }));
  };

  const rows = useMemo(() => {
    const q = search.trim().toLowerCase();
    if (!q) return products;
    return products.filter((p) =>
      `${p.name} ${p.pitch ?? ''} ${p.support_contact ?? ''}`.toLowerCase().includes(q),
    );
  }, [products, search]);

  const handleSubscribe = async (opts: { agentId?: string; message?: string }) => {
    if (!subscribing) return;
    const card = subscribing;
    const created = await marketplaceApi.subscribe(
      buildSubscribeBody(card, { agentId: opts.agentId, message: opts.message }),
    );
    if (!mountedRef.current) return;
    patch((catalog) => ({
      ...catalog,
      products: catalog.products.map((p) =>
        p.product_id === card.product_id
          ? {
              ...p,
              my_status: created.status,
              my_subscription_id: created.id,
              my_agent_id: created.agent_id ?? null,
            }
          : p,
      ),
    }));
    setSubscribing(null);
    window.setTimeout(() => {
      void reconcile();
    }, RECONCILE_MS);
  };

  return (
    <div className="min-h-[calc(100vh-4rem)] relative">
      <div className="relative max-w-7xl mx-auto px-6 py-6">
        <div className="mb-4">
          <h1 className="text-xl font-semibold text-slate-900">MCP Server Marketplace</h1>
          <p className="text-xs text-slate-500 mt-0.5">
            Browse published MCP servers. Subscribe on behalf of one of your agents to request
            live access.
          </p>
        </div>

        <div className="bg-white/70 backdrop-blur rounded-xl border border-slate-200/60 shadow-sm p-2.5 mb-3">
          <input
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            placeholder="Search MCP servers by name or pitch…"
            aria-label="Search MCP servers"
            className="w-full px-3 py-1.5 text-sm rounded-lg border border-slate-300 focus:outline-none focus:ring-2 focus:ring-blue-500/40"
          />
        </div>

        {error ? (
          <div className="bg-white/70 backdrop-blur rounded-xl border border-red-200/70 shadow-sm p-6">
            <h3 className="text-sm font-semibold text-red-700">Couldn’t load MCP servers</h3>
            <p className="text-sm text-slate-600 mt-1">{error}</p>
            <button
              onClick={reload}
              className="mt-3 px-3 py-1.5 rounded-lg bg-white border border-slate-300 text-slate-700 text-xs font-medium hover:bg-slate-50 transition-colors"
            >
              Retry
            </button>
          </div>
        ) : loading ? (
          <div className="bg-white/70 backdrop-blur rounded-xl border border-slate-200/60 shadow-sm p-8 text-center text-slate-400 text-sm">
            Loading MCP servers…
          </div>
        ) : rows.length === 0 ? (
          <div className="bg-white/70 backdrop-blur rounded-xl border border-slate-200/60 shadow-sm p-10 text-center">
            {products.length === 0 ? (
              <>
                <p className="text-sm font-medium text-slate-600">No published MCP servers yet</p>
                <p className="text-xs text-slate-500 mt-1">
                  An MCP server appears here once its team submits a datasheet and a platform
                  administrator approves the publication.
                </p>
              </>
            ) : (
              <p className="text-sm font-medium text-slate-600">No MCP servers match your search.</p>
            )}
          </div>
        ) : (
          <div className="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-3 gap-4">
            {rows.map((card) => (
              <MarketplaceCard
                key={card.product_id}
                card={card}
                onSubscribe={() => setSubscribing(card)}
                onView={() => navigate(`/mcp-servers/${card.product_id}`)}
              />
            ))}
          </div>
        )}
      </div>

      {subscribing && (
        <SubscribeModal
          card={subscribing}
          eligibleAgents={pickerAgents}
          onSubmit={handleSubscribe}
          onClose={() => setSubscribing(null)}
        />
      )}
    </div>
  );
}
