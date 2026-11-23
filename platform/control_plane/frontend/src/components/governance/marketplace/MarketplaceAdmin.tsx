import { Fragment, useCallback, useMemo, useState, type ReactNode } from 'react';
import { marketplaceApi } from '../../../api/client';
import { useLoad } from '../../../api/useLoad';
import type {
  MarketplaceMetrics,
  PublishRequest,
  SubscriptionStatus,
  Subscription,
} from '../../../api/client';
import { useUser } from '../../../contexts/useUser';
import { AgentAvatar } from '../agentUi';
import { emailAlias } from '../agentBadges';
import { formatDate } from '../../operations/opsUi';
import {
  datasheetRows,
  productKey,
  productTypeBadge,
  publishBadge,
  publishQueueCounts,
  subscriptionStatusBadge,
  unpublishStateFor,
} from './publishAdmin';

const CARD = 'bg-white/70 backdrop-blur rounded-xl border border-slate-200/60 shadow-sm';

const STATUS_FILTERS: { value: 'all' | SubscriptionStatus; label: string }[] = [
  { value: 'all', label: 'All statuses' },
  { value: 'pending', label: 'Pending' },
  { value: 'approved', label: 'Approved' },
  { value: 'rejected', label: 'Rejected' },
  { value: 'failed', label: 'Failed' },
  { value: 'revoked', label: 'Revoked' },
];

function MetricTile({ label, value, tint }: { label: string; value: string; tint?: string }) {
  return (
    <div className={`${CARD} px-4 py-3`}>
      <div className="text-[11px] uppercase tracking-wide text-slate-400 font-medium">{label}</div>
      <div className={`mt-1 text-2xl font-semibold ${tint ?? 'text-slate-900'}`}>{value}</div>
    </div>
  );
}

const EMPTY_SUBS: Subscription[] = [];
const EMPTY_PUBLISH_REQS: PublishRequest[] = [];
const EMPTY_KEYS: Set<string> = new Set();

export default function MarketplaceAdmin() {
  const { user } = useUser();
  const isAdmin = (user?.role_level ?? 0) >= 2;

  const [statusFilter, setStatusFilter] = useState<'all' | SubscriptionStatus>('all');

  const [expandedId, setExpandedId] = useState<string | null>(null);

  const [actionPendingId, setActionPendingId] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);

  const subsLoad = useLoad(
    () =>
      isAdmin
        ? Promise.all([
            marketplaceApi.adminSubscriptions(statusFilter === 'all' ? undefined : { status: statusFilter }),
            marketplaceApi.metrics(),
          ]).then(([subs, metrics]) => ({ subs, metrics }))
        : Promise.resolve<{ subs: Subscription[]; metrics: MarketplaceMetrics | null } | null>(null),
    [isAdmin, statusFilter],
    'Failed to load subscriptions.',
  );
  const subs = subsLoad.state.data?.subs ?? EMPTY_SUBS;
  const metrics = subsLoad.state.data?.metrics ?? null;
  const loading = subsLoad.state.kind === 'loading';
  const error = subsLoad.state.kind === 'error' ? subsLoad.state.message : null;

  const publishLoad = useLoad(
    () =>
      isAdmin
        ? Promise.all([
            marketplaceApi.publishRequests(),
            marketplaceApi.listAgentProducts(),
            marketplaceApi.listMcpProducts(),
          ]).then(([reqs, agentProducts, mcpProducts]) => ({
            reqs,
            publishedKeys: new Set(
              [...agentProducts, ...mcpProducts].map((p) => productKey(p.product_type, p.product_id)),
            ),
          }))
        : Promise.resolve<{ reqs: PublishRequest[]; publishedKeys: Set<string> } | null>(null),
    [isAdmin],
    'Failed to load publish requests.',
  );
  const publishReqs =
    publishLoad.state.kind === 'error' ? EMPTY_PUBLISH_REQS : publishLoad.state.data?.reqs ?? EMPTY_PUBLISH_REQS;
  const publishedKeys = publishLoad.state.kind === 'error' ? EMPTY_KEYS : publishLoad.state.data?.publishedKeys ?? EMPTY_KEYS;
  const publishLoading = publishLoad.state.kind === 'loading';
  const publishError = publishLoad.state.kind === 'error' ? publishLoad.state.message : null;

  const reloadSubs = subsLoad.reload;
  const reloadPublish = publishLoad.reload;
  const refetch = useCallback(() => {
    reloadSubs();
    reloadPublish();
  }, [reloadSubs, reloadPublish]);

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
      const reason = window.prompt('Reason for rejecting this request (optional):') ?? undefined;
      void runAction(id, () => marketplaceApi.reject(id, reason || undefined));
    },
    [runAction],
  );
  const handleRevoke = useCallback(
    (id: string) => {
      if (!window.confirm('Revoke this subscription? This removes the live agent access.')) return;
      const reason = window.prompt('Reason for revoking this subscription (optional):') ?? undefined;
      void runAction(id, () => marketplaceApi.revoke(id, reason || undefined));
    },
    [runAction],
  );

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
    (req: PublishRequest) => {
      if (!window.confirm(`Unpublish “${req.product_name}”? It stops appearing in the marketplace catalog.`)) {
        return;
      }
      void runAction(req.id, () =>
        marketplaceApi.unpublishProduct(req.product_type, req.product_id),
      );
    },
    [runAction],
  );

  const publishCounts = useMemo(() => publishQueueCounts(publishReqs), [publishReqs]);

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

        <div className="mb-4">
            <div className="flex items-center gap-2 mb-1.5">
              <h3 className="text-sm font-semibold text-slate-700">Publish requests</h3>
              {publishReqs.length > 0 && (
                <span className="inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium bg-slate-100 text-slate-600">
                  {publishCounts.total}
                </span>
              )}
              {publishCounts.pending > 0 && (
                <span className="inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium bg-amber-50 text-amber-700">
                  {publishCounts.pending} pending
                </span>
              )}
              {publishCounts.failed > 0 && (
                <span className="inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium bg-red-50 text-red-700">
                  {publishCounts.failed} failed
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

        <h3 className="text-sm font-semibold text-slate-700 mb-1.5">Subscription requests</h3>

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


        <p className="text-xs text-slate-500 mb-2">
          Approving an agent subscription assigns the requester to the agent&apos;s Entra app (Invoker role);
          MCP approvals grant the chosen agent instead. A user&apos;s direct Invoker grant on the Access tab
          and their subscription share one assignment — revoking either removes both. Admin-role and group
          grants are unaffected.
        </p>

        <div className="bg-white/70 backdrop-blur rounded-xl border border-slate-200/60 shadow-sm p-2.5 mb-3">
          <div className="grid grid-cols-1 md:grid-cols-12 gap-2">
            <div className="md:col-span-3">
              <select
                aria-label="Filter by status"
                className="w-full px-3 py-1.5 text-sm rounded-lg border border-slate-300 bg-white"
                value={statusFilter}
                onChange={(e) => setStatusFilter(e.target.value as 'all' | SubscriptionStatus)}
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
                    const status = subscriptionStatusBadge(s.status);
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

function DetailField({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div>
      <dt className="text-[11px] uppercase tracking-wide text-slate-400 font-medium">{label}</dt>
      <dd className="mt-0.5 text-sm text-slate-700 break-words">{children}</dd>
    </div>
  );
}
