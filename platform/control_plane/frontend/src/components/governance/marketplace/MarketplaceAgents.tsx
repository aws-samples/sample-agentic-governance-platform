// MarketplaceAgents — the consumer page for published agent products (Epic 9, T8;
// E33/T6).
//
// A card grid (not a table) of every agent whose publisher submitted a datasheet an
// admin approved — there is no curated blueprint list behind this page any more, so a
// fresh environment shows an empty catalog rather than three fixtures. Mirrors the
// AgentsList idiom: a glass search band, a client useMemo filter over
// `${name} ${pitch}`, loading/empty/error states, and a reloadNonce retry.
// Subscribe opens SubscribeModal (the agent variant — message only); on success
// we optimistically flip the card to my_status='pending' and fire a reconciling
// refetch (the CedarPoliciesTab optimistic-then-reconcile idiom) so an
// auto-approved product settles to its real 'approved' state.

import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { marketplaceApi } from '../../../api/client';
import type { ProductCard } from '../../../api/client';
import MarketplaceCard from './MarketplaceCard';
import SubscribeModal from './SubscribeModal';
import AgentDetailDrawer from './AgentDetailDrawer';
import { buildSubscribeBody } from './marketplaceForm';

// The catalog settles after a subscribe (an auto-approving listing resolves to
// 'approved'); refetch shortly after to reconcile the optimistic 'pending'.
const RECONCILE_MS = 600;

export default function MarketplaceAgents() {
  const [products, setProducts] = useState<ProductCard[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [search, setSearch] = useState('');
  const [reloadNonce, setReloadNonce] = useState(0);

  // The product the user is subscribing to (drives the subscribe modal).
  const [subscribing, setSubscribing] = useState<ProductCard | null>(null);
  // The product whose full datasheet drawer is open (whole-card click-through).
  const [viewing, setViewing] = useState<ProductCard | null>(null);

  // Unmount guard for the reconcile timer.
  const mountedRef = useRef(true);
  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
    };
  }, []);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    marketplaceApi
      .listAgentProducts()
      .then((res) => {
        if (cancelled) return;
        setProducts(res);
        setError(null);
      })
      .catch((err: unknown) => {
        if (cancelled) return;
        setError(err instanceof Error ? err.message : 'Failed to load published agents.');
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [reloadNonce]);

  // Silent reconcile after a subscribe (absorb the auto-approve transition).
  const reconcile = useCallback(async () => {
    try {
      const fresh = await marketplaceApi.listAgentProducts();
      if (mountedRef.current) setProducts(fresh);
    } catch {
      // swallow — a background refetch error must not clobber the UI.
    }
  }, []);

  const rows = useMemo(() => {
    const q = search.trim().toLowerCase();
    if (!q) return products;
    return products.filter((p) => `${p.name} ${p.pitch ?? ''}`.toLowerCase().includes(q));
  }, [products, search]);

  const handleSubscribe = useCallback(
    async (opts: { message?: string }) => {
      if (!subscribing) return;
      const card = subscribing;
      const created = await marketplaceApi.subscribe(
        buildSubscribeBody(card, { message: opts.message }),
      );
      if (!mountedRef.current) return;
      // Optimistic: reflect the server's status (pending, or approved when the
      // product's listing auto-approves) onto the card immediately.
      setProducts((prev) =>
        prev.map((p) =>
          p.product_id === card.product_id
            ? { ...p, my_status: created.status, my_subscription_id: created.id }
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
          <h1 className="text-xl font-semibold text-slate-900">Agent Marketplace</h1>
          <p className="text-xs text-slate-500 mt-0.5">
            Discover agents published by other teams. Subscribe to request access for your line of
            business.
          </p>
        </div>

        {/* Search band — cloned from AgentsList's glass filter row. */}
        <div className="bg-white/70 backdrop-blur rounded-xl border border-slate-200/60 shadow-sm p-2.5 mb-3">
          <input
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            placeholder="Search published agents by name or description…"
            aria-label="Search published agents"
            className="w-full px-3 py-1.5 text-sm rounded-lg border border-slate-300 focus:outline-none focus:ring-2 focus:ring-blue-500/40"
          />
        </div>

        {error ? (
          <div className="bg-white/70 backdrop-blur rounded-xl border border-red-200/70 shadow-sm p-6">
            <h3 className="text-sm font-semibold text-red-700">Couldn’t load published agents</h3>
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
            Loading published agents…
          </div>
        ) : rows.length === 0 ? (
          // Honest empty state (E33/T6): the catalog is built from real publications, so
          // a fresh environment legitimately has none. Say that, and say what fills it,
          // instead of implying the load failed.
          <div className="bg-white/70 backdrop-blur rounded-xl border border-slate-200/60 shadow-sm p-10 text-center">
            {products.length === 0 ? (
              <>
                <p className="text-sm font-medium text-slate-600">No published agents yet</p>
                <p className="text-xs text-slate-500 mt-1">
                  An agent appears here once its team submits a datasheet and a platform
                  administrator approves the publication.
                </p>
              </>
            ) : (
              <p className="text-sm font-medium text-slate-600">No agents match your search.</p>
            )}
          </div>
        ) : (
          <div className="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-3 gap-4">
            {rows.map((card) => (
              <MarketplaceCard
                key={card.product_id}
                card={card}
                onSubscribe={() => setSubscribing(card)}
                onView={() => setViewing(card)}
              />
            ))}
          </div>
        )}
      </div>

      {subscribing && (
        <SubscribeModal
          card={subscribing}
          eligibleAgents={[]}
          onSubmit={handleSubscribe}
          onClose={() => setSubscribing(null)}
        />
      )}

      {viewing && (
        <AgentDetailDrawer
          card={viewing}
          onClose={() => setViewing(null)}
          // The drawer's primary CTA hands off to the SAME subscribe flow the card
          // uses — close the drawer, then open the modal. (There is no deploy hand-off:
          // the Deploy affordance is disabled in both places and E33/T6 deleted the
          // form it used to open.)
          onSubscribe={() => {
            const card = viewing;
            setViewing(null);
            setSubscribing(card);
          }}
        />
      )}
    </div>
  );
}
