// MarketplaceAdmin — the admin approval panel (Epic 9, T8).
//
// Admin-gated (role_level >= 2). Non-admins get a cosmetic "not authorized"
// panel (the route is the real guard — the backend admin routes are ADMIN-only).
// Layout:
//   • a metrics strip — summary cards from marketplaceApi.metrics (total /
//     pending / approved / rejected + approval rate + a small by-type / top
//     products view);
//   • a status filter <select>;
//   • an AgentsList-style table of marketplaceApi.adminSubscriptions (the
//     service returns pending-first). Each ROW is click-to-expand, showing the
//     requester (avatar + email alias), the product, the for-agent (MCP), the
//     message, created/decided timestamps, and the decision reason / error.
//   • Approve / Reject (with an optional reason prompt) / Retry (on failed) act
//     on eligible rows → call the api → refetch.
//   • a SECOND queue above it (E33): marketplace publish requests. Same table /
//     badge / runAction idiom; the expanded row shows the FULL declared datasheet
//     (publishAdmin.datasheetRows), and Approve / Reject / Unpublish act on it.
// Avatars + email aliases are the shared agentUi helpers; the status badge is a
// small local map (the agentUi lifecycle badge is keyed on a different enum).

import { Fragment, useCallback, useEffect, useMemo, useState, type ReactNode } from 'react';
import { marketplaceApi } from '../../../api/client';
import type {
  MarketplaceMetrics,
  MarketplacePublishRequest,
  MarketplaceStatus,
  MarketplaceSubscription,
} from '../../../api/client';
import { useUser } from '../../../contexts/UserContext';
import { AgentAvatar, emailAlias } from '../agentUi';
import {
  datasheetRows,
  productKey,
  productTypeBadge,
  publishBadge,
  unpublishStateFor,
} from './publishAdmin';

const CARD = 'bg-white/70 backdrop-blur rounded-xl border border-slate-200/60 shadow-sm';

// Subscription-status badge — same semantic tints as the rest of the governance UI
// (emerald=approved, amber=pending, red=rejected/failed). Local because the
// shared agentUi lifecycleBadge is keyed on the agent LifecycleState enum, not
// the marketplace SubscriptionStatus.
const STATUS_BADGE: Record<MarketplaceStatus, { cls: string; label: string }> = {
  pending: { cls: 'bg-amber-50 text-amber-700', label: 'Pending' },
  approved: { cls: 'bg-emerald-50 text-emerald-700', label: 'Approved' },
  rejected: { cls: 'bg-red-50 text-red-700', label: 'Rejected' },
  failed: { cls: 'bg-red-50 text-red-700', label: 'Failed' },
  // Revoked is a muted slate — semantically "wound down", distinct from the red
  // of a denied/failed request (the access existed and was deliberately torn down).
  revoked: { cls: 'bg-slate-100 text-slate-600', label: 'Revoked' },
};

const STATUS_FILTERS: { value: 'all' | MarketplaceStatus; label: string }[] = [
  { value: 'all', label: 'All statuses' },
  { value: 'pending', label: 'Pending' },
  { value: 'approved', label: 'Approved' },
  { value: 'rejected', label: 'Rejected' },
  { value: 'failed', label: 'Failed' },
  { value: 'revoked', label: 'Revoked' },
];

function formatDate(iso?: string | null): string {
  if (!iso) return '—';
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return d.toLocaleString(undefined, {
    year: 'numeric',
    month: 'short',
    day: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
  });
}

// A single summary stat tile in the metrics strip.
function MetricTile({ label, value, tint }: { label: string; value: string; tint?: string }) {
  return (
    <div className={`${CARD} px-4 py-3`}>
      <div className="text-[11px] uppercase tracking-wide text-slate-400 font-medium">{label}</div>
      <div className={`mt-1 text-2xl font-semibold ${tint ?? 'text-slate-900'}`}>{value}</div>
    </div>
  );
}

export default function MarketplaceAdmin() {
  const { user } = useUser();
  const isAdmin = (user?.role_level ?? 0) >= 2;

  const [subs, setSubs] = useState<MarketplaceSubscription[]>([]);
  const [metrics, setMetrics] = useState<MarketplaceMetrics | null>(null);
  // Publish queue (E33; both product types since Amendment 1). `publishedKeys` is the set of
  // (product_type, product_id) keys currently listed as products — the two product listings only
  // ever return published products, so membership IS "published right now". Used to show
  // Unpublish only where it can actually succeed (an approved request whose product was already
  // unpublished would 404). The key is the PAIR, never the bare id: the agent and MCP registries
  // have independent id spaces, so one id can name a product in each.
  const [publishReqs, setPublishReqs] = useState<MarketplacePublishRequest[]>([]);
  const [publishedKeys, setPublishedKeys] = useState<Set<string>>(new Set());
  // The publish queue has its OWN loading + error slots: the two sections are independent
  // features on one page, so neither may blank or misreport the other (a shared error slot
  // let a publish failure hide the subscriptions table, and a shared success path let a
  // subscriptions failure discard an already-loaded publish queue).
  const [publishLoading, setPublishLoading] = useState(true);
  const [publishError, setPublishError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [statusFilter, setStatusFilter] = useState<'all' | MarketplaceStatus>('all');
  const [reloadNonce, setReloadNonce] = useState(0);

  const [expandedId, setExpandedId] = useState<string | null>(null);

  // Per-row action state.
  const [actionPendingId, setActionPendingId] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);

  // Load subscriptions (status-filtered server-side) + metrics, and the publish queue, as
  // TWO fully independent chains. Skipped entirely for non-admins (they only see the
  // not-authorized panel). The publish queue is unfiltered: the status <select> above
  // belongs to the subscription statuses, which are a different enum.
  //
  // Independence is the point, in both directions. One shared chain cannot give it: a
  // rejection anywhere skips the whole .then, so a subscriptions failure would DISCARD an
  // already-resolved publish load and leave the approval queue asserting "No publish
  // requests yet." with no error — the worst outcome for a governance queue — while a
  // publish failure would blank the subscriptions table behind "Couldn't load
  // subscriptions". Each chain therefore owns its own then/catch/finally, its own loading
  // flag and its own error slot; each settles exactly once per run.
  useEffect(() => {
    if (!isAdmin) {
      setLoading(false);
      setPublishLoading(false);
      return;
    }
    let cancelled = false;

    setLoading(true);
    Promise.all([
      marketplaceApi.adminSubscriptions(
        statusFilter === 'all' ? undefined : { status: statusFilter },
      ),
      marketplaceApi.metrics(),
    ])
      .then(([rows, m]) => {
        if (cancelled) return;
        setSubs(rows);
        setMetrics(m);
        setError(null);
      })
      .catch((err: unknown) => {
        if (cancelled) return;
        setError(err instanceof Error ? err.message : 'Failed to load subscriptions.');
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });

    setPublishLoading(true);
    // BOTH product listings feed the live-listing set: the queue mixes agents and MCP servers,
    // and an Unpublish button may only appear for a product that is actually listed. They ride
    // the publish chain (not the subscriptions one) because they exist to answer that question.
    Promise.all([
      marketplaceApi.publishRequests(),
      marketplaceApi.listAgentProducts(),
      marketplaceApi.listMcpProducts(),
    ])
      .then(([reqs, agentProducts, mcpProducts]) => {
        if (cancelled) return;
        setPublishReqs(reqs);
        setPublishedKeys(
          new Set(
            [...agentProducts, ...mcpProducts].map((p) => productKey(p.product_type, p.product_id)),
          ),
        );
        setPublishError(null);
      })
      .catch((err: unknown) => {
        if (cancelled) return;
        // Clear both, so a stale queue is never shown beside a load error.
        setPublishReqs([]);
        setPublishedKeys(new Set());
        setPublishError(err instanceof Error ? err.message : 'Failed to load publish requests.');
      })
      .finally(() => {
        if (!cancelled) setPublishLoading(false);
      });

    return () => {
      cancelled = true;
    };
  }, [isAdmin, statusFilter, reloadNonce]);

  const refetch = useCallback(() => setReloadNonce((n) => n + 1), []);

  const runAction = useCallback(
    async (id: string, fn: () => Promise<unknown>) => {
      if (actionPendingId) return;
      setActionPendingId(id);
      setActionError(null);
      try {
        await fn();
        refetch();
      } catch (err: unknown) {
        setActionError(err instanceof Error ? err.message : 'Action failed.');
      } finally {
        setActionPendingId(null);
      }
    },
    [actionPendingId, refetch],
  );

  const handleApprove = useCallback(
    (id: string) => runAction(id, () => marketplaceApi.approve(id)),
    [runAction],
  );
  const handleRetry = useCallback(
    (id: string) => runAction(id, () => marketplaceApi.retry(id)),
    [runAction],
  );
  const handleReject = useCallback(
    (id: string) => {
      // A lightweight reason prompt — matches the AgentDetail reject idiom.
      const reason = window.prompt('Reason for rejecting this request (optional):') ?? undefined;
      void runAction(id, () => marketplaceApi.reject(id, reason || undefined));
    },
    [runAction],
  );
  const handleRevoke = useCallback(
    (id: string) => {
      // Revoke tears down a LIVE grant (app-role assignment + OBO consent) — confirm
      // first, then collect an optional reason, cloning the reject idiom. On a Graph
      // failure (502) runAction surfaces the error banner and the row stays approved
      // so the admin can simply click Revoke again.
      if (!window.confirm('Revoke this subscription? This removes the live agent access.')) return;
      const reason = window.prompt('Reason for revoking this subscription (optional):') ?? undefined;
      void runAction(id, () => marketplaceApi.revoke(id, reason || undefined));
    },
    [runAction],
  );

  // --- Publish-request actions (E33) ---------------------------------------
  // Approve writes the declared datasheet onto the product's registry envelope (agent or MCP
  // server — the backend dispatches on the request's product_type). If that write fails
  // the backend keeps the request PENDING with a safe `error` literal, so clicking
  // Approve again is the retry — no separate Retry button is needed here.
  const handleApprovePublish = useCallback(
    (id: string) => runAction(id, () => marketplaceApi.approvePublish(id)),
    [runAction],
  );
  const handleRejectPublish = useCallback(
    (id: string) => {
      const reason = window.prompt('Reason for rejecting this publish request (optional):') ?? undefined;
      void runAction(id, () => marketplaceApi.rejectPublish(id, reason || undefined));
    },
    [runAction],
  );
  const handleUnpublish = useCallback(
    (req: MarketplacePublishRequest) => {
      // Delisting only — the product keeps its declared datasheet with published=false, so
      // an admin can re-publish it. Confirm first, cloning the revoke idiom.
      if (!window.confirm(`Unpublish “${req.product_name}”? It stops appearing in the marketplace catalog.`)) {
        return;
      }
      void runAction(req.id, () =>
        marketplaceApi.unpublishProduct(req.product_type, req.product_id),
      );
    },
    [runAction],
  );

  const approvalRatePct = useMemo(() => {
    if (!metrics) return '—';
    return `${Math.round(metrics.approval_rate * 100)}%`;
  }, [metrics]);

  return (
    <div className="space-y-3">
      <p className="text-xs text-slate-500">
        Review publish and subscription requests. Approving a publish request lists that agent or MCP
        server in the marketplace with its declared datasheet; approving an MCP subscription request
        applies the live agent→MCP grant.
      </p>

      {/* Metrics strip. */}
        {metrics && (
          <>
            <div className="grid grid-cols-2 md:grid-cols-4 xl:grid-cols-7 gap-3 mb-3">
              <MetricTile label="Total" value={String(metrics.total)} />
              <MetricTile label="Pending" value={String(metrics.pending)} tint="text-amber-600" />
              <MetricTile label="Approved" value={String(metrics.approved)} tint="text-emerald-600" />
              <MetricTile label="Rejected" value={String(metrics.rejected)} tint="text-red-600" />
              <MetricTile label="Failed" value={String(metrics.failed)} tint="text-red-600" />
              <MetricTile label="Revoked" value={String(metrics.revoked)} tint="text-slate-500" />
              <MetricTile label="Approval rate" value={approvalRatePct} />
            </div>

            {/* By-type + top products — a compact secondary view. */}
            <div className="grid grid-cols-1 md:grid-cols-2 gap-3 mb-3">
              <div className={`${CARD} px-4 py-3`}>
                <div className="text-[11px] uppercase tracking-wide text-slate-400 font-medium mb-2">
                  By product type
                </div>
                <div className="flex flex-wrap gap-2">
                  {Object.keys(metrics.by_type).length === 0 ? (
                    <span className="text-sm text-slate-400">No subscriptions yet.</span>
                  ) : (
                    Object.entries(metrics.by_type).map(([type, count]) => (
                      <span
                        key={type}
                        className="inline-flex items-center gap-1.5 px-2.5 py-1 rounded-full text-xs font-medium bg-slate-100 text-slate-600"
                      >
                        <span className="capitalize">{type}</span>
                        <span className="text-slate-400">·</span>
                        <span className="font-semibold text-slate-700">{count}</span>
                      </span>
                    ))
                  )}
                </div>
              </div>
              <div className={`${CARD} px-4 py-3`}>
                <div className="text-[11px] uppercase tracking-wide text-slate-400 font-medium mb-2">
                  Top products
                </div>
                {metrics.top_products.length === 0 ? (
                  <span className="text-sm text-slate-400">No subscriptions yet.</span>
                ) : (
                  <ul className="space-y-1">
                    {metrics.top_products.map((p) => (
                      <li key={p.product_id} className="flex items-center justify-between text-sm">
                        <span className="text-slate-700 truncate pr-2">{p.product_name}</span>
                        <span className="shrink-0 text-slate-400">{p.count}</span>
                      </li>
                    ))}
                  </ul>
                )}
              </div>
            </div>
          </>
        )}

        {/* Publish requests queue (E33) — declared datasheets awaiting a listing decision.
            Rendered independently of the subscriptions `error`: neither section may take the
            other down. */}
        <div className="mb-4">
            <div className="flex items-center gap-2 mb-1.5">
              <h3 className="text-sm font-semibold text-slate-700">Publish requests</h3>
              {publishReqs.length > 0 && (
                <span className="inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium bg-slate-100 text-slate-600">
                  {publishReqs.length}
                </span>
              )}
            </div>
            {publishError && (
              <p className="mb-1.5 text-sm text-red-600" role="alert">
                Couldn’t load publish requests: {publishError}{' '}
                <button
                  type="button"
                  onClick={refetch}
                  className="underline underline-offset-2 hover:no-underline"
                >
                  Retry
                </button>
              </p>
            )}
            <div className="bg-white/70 backdrop-blur rounded-xl border border-slate-200/60 overflow-hidden shadow-sm">
              <table className="w-full text-sm">
                <thead className="bg-slate-50/80 text-slate-500 text-xs uppercase tracking-wide">
                  <tr>
                    <th className="text-left font-medium px-4 py-2">Product</th>
                    <th className="text-left font-medium px-4 py-2">Type</th>
                    <th className="text-left font-medium px-4 py-2">Publisher</th>
                    <th className="text-left font-medium px-4 py-2">Requested</th>
                    <th className="text-left font-medium px-4 py-2">Status</th>
                    <th className="text-right font-medium px-4 py-2">Actions</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-slate-100">
                  {publishLoading && (
                    <tr>
                      <td colSpan={6} className="px-4 py-8 text-center text-slate-400 text-sm">
                        Loading publish requests…
                      </td>
                    </tr>
                  )}

                  {!publishLoading &&
                    publishReqs.map((r) => {
                      const expanded = expandedId === r.id;
                      const badge = publishBadge(r);
                      const typeBadge = productTypeBadge(r);
                      const rowPending = actionPendingId === r.id;
                      // Unpublish follows the LIVE listing, not the request status — a
                      // re-publish overwrites this one record back to pending/rejected while
                      // the product stays listed (see unpublishStateFor).
                      const unpublishState = unpublishStateFor(r, publishedKeys);
                      return (
                        <Fragment key={r.id}>
                          <tr
                            className="hover:bg-blue-50/40 transition-colors cursor-pointer"
                            onClick={() => setExpandedId(expanded ? null : r.id)}
                            aria-expanded={expanded}
                          >
                            <td className="px-4 py-2">
                              <div className="flex items-center gap-2.5">
                                <span
                                  aria-hidden="true"
                                  className={`inline-block transition-transform text-slate-300 ${
                                    expanded ? 'rotate-90' : ''
                                  }`}
                                >
                                  ›
                                </span>
                                <AgentAvatar name={r.product_name} size="sm" />
                                <span className="text-slate-700 truncate" title={r.product_id}>
                                  {r.product_name}
                                </span>
                              </div>
                            </td>
                            <td className="px-4 py-2">
                              {/* Which registry this product lives in — the queue mixes agents
                                  and MCP servers, and the two id spaces are independent, so the
                                  name alone does not say which one a row is. */}
                              <span
                                className={`inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium ${typeBadge.cls}`}
                              >
                                {typeBadge.label}
                              </span>
                            </td>
                            <td
                              className="px-4 py-2 text-slate-600 truncate"
                              title={r.requested_by_email ?? r.requested_by}
                            >
                              {emailAlias(r.requested_by_email)}
                            </td>
                            <td className="px-4 py-2 text-slate-500 whitespace-nowrap">
                              {formatDate(r.created_at)}
                            </td>
                            <td className="px-4 py-2">
                              <span
                                className={`inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium ${badge.cls}`}
                              >
                                {badge.label}
                              </span>
                            </td>
                            <td className="px-4 py-2">
                              <div
                                className="flex items-center justify-end gap-1.5"
                                onClick={(e) => e.stopPropagation()}
                              >
                                {r.status === 'pending' && (
                                  <>
                                    <button
                                      type="button"
                                      onClick={() => handleApprovePublish(r.id)}
                                      disabled={rowPending}
                                      className="px-2.5 py-1 rounded-md bg-emerald-600 text-white text-xs font-medium hover:bg-emerald-700 transition-colors disabled:opacity-40"
                                    >
                                      {rowPending ? '…' : r.error ? 'Retry approve' : 'Approve'}
                                    </button>
                                    <button
                                      type="button"
                                      onClick={() => handleRejectPublish(r.id)}
                                      disabled={rowPending}
                                      className="px-2.5 py-1 rounded-md bg-white border border-slate-300 text-slate-700 text-xs font-medium hover:bg-slate-50 transition-colors disabled:opacity-40"
                                    >
                                      Reject
                                    </button>
                                  </>
                                )}
                                {unpublishState === 'unpublish' && (
                                  <button
                                    type="button"
                                    onClick={() => handleUnpublish(r)}
                                    disabled={rowPending}
                                    className="px-2.5 py-1 rounded-md bg-white border border-rose-300 text-rose-600 text-xs font-medium hover:bg-rose-50 transition-colors disabled:opacity-40"
                                  >
                                    {rowPending ? '…' : 'Unpublish'}
                                  </button>
                                )}
                                {unpublishState === 'unpublished' && (
                                  <span className="text-xs text-slate-400">Unpublished</span>
                                )}
                                {r.status === 'rejected' && unpublishState === 'none' && (
                                  <span className="text-xs text-slate-300">—</span>
                                )}
                              </div>
                            </td>
                          </tr>

                          {expanded && (
                            <tr className="bg-slate-50/60">
                              <td colSpan={6} className="px-4 py-4">
                                <dl className="grid grid-cols-1 md:grid-cols-2 gap-x-8 gap-y-3">
                                  <DetailField label={typeBadge.label}>
                                    {r.product_name}{' '}
                                    <span className="text-slate-400">({r.product_id})</span>
                                  </DetailField>
                                  <DetailField label="Requested by">
                                    {r.requested_by_email ?? r.requested_by}
                                  </DetailField>
                                  {/* The declared datasheet, in full. Fields the publisher left
                                      blank are omitted entirely — declared, never measured. */}
                                  {datasheetRows(r).map((row) => (
                                    <DetailField key={row.label} label={row.label}>
                                      {row.value}
                                    </DetailField>
                                  ))}
                                  <DetailField label="Requested at">{formatDate(r.created_at)}</DetailField>
                                  <DetailField label="Decided at">{formatDate(r.decided_at)}</DetailField>
                                  {r.decided_by && <DetailField label="Decided by">{r.decided_by}</DetailField>}
                                  {r.decision_reason && (
                                    <DetailField label="Decision reason">{r.decision_reason}</DetailField>
                                  )}
                                  {r.error && (
                                    <DetailField label="Error">
                                      <span className="text-red-600">{r.error}</span>
                                    </DetailField>
                                  )}
                                </dl>
                              </td>
                            </tr>
                          )}
                        </Fragment>
                      );
                    })}

                  {!publishLoading && publishReqs.length === 0 && (
                    <tr>
                      <td colSpan={6} className="px-4 py-8 text-center text-slate-400 text-sm">
                        {publishError ? 'Publish requests unavailable.' : 'No publish requests yet.'}
                      </td>
                    </tr>
                  )}
                </tbody>
              </table>
            </div>
        </div>

        {/* Shared-assignment advisory (E33 A2/C13) — an approved agent subscription and a direct
            USER grant in the INVOKER role are the SAME single Entra (user, agent, Invoker)
            assignment, so revoking either side removes the other's access. Admin-role grants and
            group grants are distinct assignments and never collide. Static copy, no logic. */}
        <p className="text-xs text-slate-500 mb-2">
          Approving an agent subscription assigns the requester to the agent&apos;s Entra app (Invoker role);
          MCP approvals grant the chosen agent instead. A user&apos;s direct Invoker grant on the Access tab
          and their subscription share one assignment — revoking either removes both. Admin-role and group
          grants are unaffected.
        </p>

        {/* Status filter band — subscriptions only (the publish queue is unfiltered). */}
        <div className="bg-white/70 backdrop-blur rounded-xl border border-slate-200/60 shadow-sm p-2.5 mb-3">
          <div className="grid grid-cols-1 md:grid-cols-12 gap-2">
            <div className="md:col-span-3">
              <select
                aria-label="Filter by status"
                className="w-full px-3 py-1.5 text-sm rounded-lg border border-slate-300 bg-white"
                value={statusFilter}
                onChange={(e) => setStatusFilter(e.target.value as 'all' | MarketplaceStatus)}
              >
                {STATUS_FILTERS.map((o) => (
                  <option key={o.value} value={o.value}>
                    {o.label}
                  </option>
                ))}
              </select>
            </div>
          </div>
        </div>

        {actionError && (
          <p className="mb-3 text-sm text-red-600" role="alert">
            {actionError}
          </p>
        )}

        {/* Table. */}
        {error ? (
          <div className="bg-white/70 backdrop-blur rounded-xl border border-red-200/70 shadow-sm p-6">
            <h3 className="text-sm font-semibold text-red-700">Couldn’t load subscriptions</h3>
            <p className="text-sm text-slate-600 mt-1">{error}</p>
            <button
              onClick={refetch}
              className="mt-3 px-3 py-1.5 rounded-lg bg-white border border-slate-300 text-slate-700 text-xs font-medium hover:bg-slate-50 transition-colors"
            >
              Retry
            </button>
          </div>
        ) : (
          <div className="bg-white/70 backdrop-blur rounded-xl border border-slate-200/60 overflow-hidden shadow-sm">
            <table className="w-full text-sm">
              <thead className="bg-slate-50/80 text-slate-500 text-xs uppercase tracking-wide">
                <tr>
                  <th className="text-left font-medium px-4 py-2">Requester</th>
                  <th className="text-left font-medium px-4 py-2">Product</th>
                  <th className="text-left font-medium px-4 py-2">Type</th>
                  <th className="text-left font-medium px-4 py-2">Requested</th>
                  <th className="text-left font-medium px-4 py-2">Status</th>
                  <th className="text-right font-medium px-4 py-2">Actions</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-slate-100">
                {loading && (
                  <tr>
                    <td colSpan={6} className="px-4 py-8 text-center text-slate-400 text-sm">
                      Loading subscriptions…
                    </td>
                  </tr>
                )}

                {!loading &&
                  subs.map((s) => {
                    const expanded = expandedId === s.id;
                    const status = STATUS_BADGE[s.status];
                    const rowPending = actionPendingId === s.id;
                    return (
                      <Fragment key={s.id}>
                        <tr
                          className="hover:bg-blue-50/40 transition-colors cursor-pointer"
                          onClick={() => setExpandedId(expanded ? null : s.id)}
                          aria-expanded={expanded}
                        >
                          <td className="px-4 py-2">
                            <div className="flex items-center gap-2.5">
                              <span
                                aria-hidden="true"
                                className={`inline-block transition-transform text-slate-300 ${
                                  expanded ? 'rotate-90' : ''
                                }`}
                              >
                                ›
                              </span>
                              <AgentAvatar name={s.requester_email ?? s.requester_oid} size="sm" />
                              <span
                                className="text-slate-700 truncate"
                                title={s.requester_email ?? s.requester_oid}
                              >
                                {emailAlias(s.requester_email)}
                              </span>
                            </div>
                          </td>
                          <td className="px-4 py-2 text-slate-600 truncate" title={s.product_name}>
                            {s.product_name}
                          </td>
                          <td className="px-4 py-2">
                            <span className="inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium bg-slate-100 text-slate-600 capitalize">
                              {s.product_type}
                            </span>
                          </td>
                          <td className="px-4 py-2 text-slate-500 whitespace-nowrap">
                            {formatDate(s.created_at)}
                          </td>
                          <td className="px-4 py-2">
                            <span
                              className={`inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium ${status.cls}`}
                            >
                              {status.label}
                            </span>
                          </td>
                          <td className="px-4 py-2">
                            <div
                              className="flex items-center justify-end gap-1.5"
                              onClick={(e) => e.stopPropagation()}
                            >
                              {s.status === 'pending' && (
                                <>
                                  <button
                                    type="button"
                                    onClick={() => handleApprove(s.id)}
                                    disabled={rowPending}
                                    className="px-2.5 py-1 rounded-md bg-emerald-600 text-white text-xs font-medium hover:bg-emerald-700 transition-colors disabled:opacity-40"
                                  >
                                    {rowPending ? '…' : 'Approve'}
                                  </button>
                                  <button
                                    type="button"
                                    onClick={() => handleReject(s.id)}
                                    disabled={rowPending}
                                    className="px-2.5 py-1 rounded-md bg-white border border-slate-300 text-slate-700 text-xs font-medium hover:bg-slate-50 transition-colors disabled:opacity-40"
                                  >
                                    Reject
                                  </button>
                                </>
                              )}
                              {s.status === 'failed' && (
                                <button
                                  type="button"
                                  onClick={() => handleRetry(s.id)}
                                  disabled={rowPending}
                                  className="px-2.5 py-1 rounded-md bg-blue-600 text-white text-xs font-medium hover:bg-blue-700 transition-colors disabled:opacity-40"
                                >
                                  {rowPending ? '…' : 'Retry'}
                                </button>
                              )}
                              {s.status === 'approved' && (
                                <button
                                  type="button"
                                  onClick={() => handleRevoke(s.id)}
                                  disabled={rowPending}
                                  className="px-2.5 py-1 rounded-md bg-white border border-rose-300 text-rose-600 text-xs font-medium hover:bg-rose-50 transition-colors disabled:opacity-40"
                                >
                                  {rowPending ? '…' : 'Revoke'}
                                </button>
                              )}
                              {(s.status === 'rejected' || s.status === 'revoked') && (
                                <span className="text-xs text-slate-300">—</span>
                              )}
                            </div>
                          </td>
                        </tr>

                        {expanded && (
                          <tr className="bg-slate-50/60">
                            <td colSpan={6} className="px-4 py-4">
                              <dl className="grid grid-cols-1 md:grid-cols-2 gap-x-8 gap-y-3">
                                <DetailField label="Requester">
                                  {s.requester_email ?? s.requester_oid}
                                </DetailField>
                                <DetailField label="Product">
                                  {s.product_name}{' '}
                                  <span className="text-slate-400">({s.product_id})</span>
                                </DetailField>
                                {s.product_type === 'mcp' && (
                                  <DetailField label="For agent">
                                    {s.agent_name ?? s.agent_id ?? '—'}
                                  </DetailField>
                                )}
                                <DetailField label="Message">
                                  {s.message ? s.message : <span className="text-slate-400">—</span>}
                                </DetailField>
                                <DetailField label="Requested at">{formatDate(s.created_at)}</DetailField>
                                <DetailField label="Decided at">{formatDate(s.decided_at)}</DetailField>
                                {s.decided_by && (
                                  <DetailField label="Decided by">{s.decided_by}</DetailField>
                                )}
                                {s.decision_reason && (
                                  <DetailField label="Decision reason">{s.decision_reason}</DetailField>
                                )}
                                {s.revoked_by && (
                                  <DetailField label="Revoked by">{s.revoked_by}</DetailField>
                                )}
                                {s.revoked_at && (
                                  <DetailField label="Revoked at">{formatDate(s.revoked_at)}</DetailField>
                                )}
                                {s.revoke_reason && (
                                  <DetailField label="Revoke reason">{s.revoke_reason}</DetailField>
                                )}
                                {s.error && (
                                  <DetailField label="Error">
                                    <span className="text-red-600">{s.error}</span>
                                  </DetailField>
                                )}
                              </dl>
                            </td>
                          </tr>
                        )}
                      </Fragment>
                    );
                  })}

                {!loading && subs.length === 0 && (
                  <tr>
                    <td colSpan={6} className="px-4 py-8 text-center text-slate-400 text-sm">
                      {statusFilter === 'all'
                        ? 'No subscription requests yet.'
                        : `No ${statusFilter} requests.`}
                    </td>
                  </tr>
                )}
              </tbody>
            </table>
          </div>
        )}
    </div>
  );
}

// A labeled definition row inside the expanded detail (mirrors AgentDetail Field).
function DetailField({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div>
      <dt className="text-[11px] uppercase tracking-wide text-slate-400 font-medium">{label}</dt>
      <dd className="mt-0.5 text-sm text-slate-700 break-words">{children}</dd>
    </div>
  );
}
