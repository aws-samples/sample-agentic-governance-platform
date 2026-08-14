// MarketplaceMcps — the consumer page for published MCP servers (Epic 9, T8).
//
// A card grid of PUBLISHED MCP servers — of any registry kind since Amendment 1, which retired
// the `kind == "gateway"` auto-listing filter: publication (datasheet submitted + admin
// approved) is now the only door into this catalog, exactly as for agents. Same idiom as
// MarketplaceAgents (glass search band, client filter, loading/empty/error,
// reloadNonce retry, optimistic-then-reconcile) but the subscribe flow is the
// MCP variant: the user picks one of THEIR provisioned agents (the SP the grant
// will land on) inside SubscribeModal. On approval (admin or auto), the backend
// applies the real E7 agent→MCP grant.
//
// Picker list (Epic 9, F1): loaded from GET /marketplace/eligible-agents — the
// backend returns exactly the provisioned agents the caller may subscribe on
// behalf of (sponsor OR granted, direct user OR group). The same rule is
// enforced server-side at subscribe time, so this picker is a real guard, not
// just a hint.

import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { marketplaceApi } from '../../../api/client';
import type { EligibleAgent, ProductCard } from '../../../api/client';
import MarketplaceCard from './MarketplaceCard';
import SubscribeModal from './SubscribeModal';
import { buildSubscribeBody } from './marketplaceForm';

const RECONCILE_MS = 600;

export default function MarketplaceMcps() {
  const navigate = useNavigate();
  const [products, setProducts] = useState<ProductCard[]>([]);
  const [pickerAgents, setPickerAgents] = useState<EligibleAgent[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [search, setSearch] = useState('');
  const [reloadNonce, setReloadNonce] = useState(0);

  const [subscribing, setSubscribing] = useState<ProductCard | null>(null);

  const mountedRef = useRef(true);
  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
    };
  }, []);

  // Load the MCP products + the eligible-agents picker list together. The
  // backend computes eligibility (sponsor OR granted), so the picker shows
  // exactly what a subscribe would be allowed to use.
  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    Promise.all([marketplaceApi.listMcpProducts(), marketplaceApi.listEligibleAgents()])
      .then(([prods, ags]) => {
        if (cancelled) return;
        setProducts(prods);
        setPickerAgents(ags);
        setError(null);
      })
      .catch((err: unknown) => {
        if (cancelled) return;
        setError(err instanceof Error ? err.message : 'Failed to load MCP servers.');
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [reloadNonce]);

  const reconcile = useCallback(async () => {
    try {
      const fresh = await marketplaceApi.listMcpProducts();
      if (mountedRef.current) setProducts(fresh);
    } catch {
      // swallow — background refetch must not clobber the UI.
    }
  }, []);

  // Search haystack: name + pitch + the DECLARED support contact. `owner_email` is gone —
  // published cards deliberately no longer carry it (it is cross-tenant PII, dropped from the
  // card projection), so searching it matched nothing while promising it would. The datasheet's
  // support_contact is the honest replacement: it is a declared, publishable team mailbox.
  const rows = useMemo(() => {
    const q = search.trim().toLowerCase();
    if (!q) return products;
    return products.filter((p) =>
      `${p.name} ${p.pitch ?? ''} ${p.support_contact ?? ''}`.toLowerCase().includes(q),
    );
  }, [products, search]);

  const handleSubscribe = useCallback(
    async (opts: { agentId?: string; message?: string }) => {
      if (!subscribing) return;
      const card = subscribing;
      const created = await marketplaceApi.subscribe(
        buildSubscribeBody(card, { agentId: opts.agentId, message: opts.message }),
      );
      if (!mountedRef.current) return;
      setProducts((prev) =>
        prev.map((p) =>
          p.product_id === card.product_id
            ? {
                ...p,
                my_status: created.status,
                my_subscription_id: created.id,
                my_agent_id: created.agent_id ?? null,
              }
            : p,
        ),
      );
      setSubscribing(null);
      window.setTimeout(() => {
        void reconcile();
      }, RECONCILE_MS);
    },
    [subscribing, reconcile],
  );

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
              onClick={() => setReloadNonce((n) => n + 1)}
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
          // Honest empty state (Amendment 1): publish is the only door for MCP servers too, so
          // the catalog is built from real publications and a fresh environment legitimately has
          // none. Say that, and say what fills it — mirrors the agents tab.
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
