import { useState } from 'react';
import { useParams, Link } from 'react-router-dom';
import { mcpServersApi, projectsApi } from '../../api/client';
import type { McpServer, DataClassification } from '../../api/client';
import { useLoad } from '../../api/useLoad';
import { useUser } from '../../contexts/useUser';
import { visibleMcpTabs } from './mcpServerDetailTabs';
import { lockedReason, mcpEndpoint, repoOwnership, repoOwnershipLabel, showGatewayRows } from './mcpRepoOwnership';
import { maskArn } from '../shared/arnDisplay';
import type { RepoOwnership } from './mcpRepoOwnership';
import { AgentAvatar } from './agentUi';
import { lifecycleBadge, emailAlias, kindBadge, tenantBadge } from './agentBadges';
import PublishPanel from './marketplace/PublishPanel';
import type { PublishPanelCopy } from './marketplace/PublishPanel';
import { formatDate } from '../operations/opsUi';
import { canEditShared, resolveTenantName, sharedBadge } from './tenantUi';
import { useTenantDirectory } from './useTenantDirectory';
import McpConnectedAgentsTab from './McpConnectedAgentsTab';
import McpToolsTab from './McpToolsTab';
import CedarPoliciesTab from './CedarPoliciesTab';
import ConfirmDialog from '../ConfirmDialog';

function dataClassLabel(d?: DataClassification | null): string {
  return d ?? '—';
}

const CARD = 'bg-white/70 backdrop-blur rounded-xl border border-slate-200/60 shadow-sm';

const PUBLISH_PANEL_COPY: PublishPanelCopy = {
  intro:
    'Offer this MCP server as a marketplace product agents can subscribe to. An admin reviews the declared datasheet before the card goes live.',
  never: 'This MCP server has never been offered in the marketplace.',
  pitchPlaceholder: 'What consumers get from this MCP server…',
};

function CopyButton({ value, label }: { value: string; label: string }) {
  const [copied, setCopied] = useState(false);
  const onCopy = () => {
    navigator.clipboard
      .writeText(value)
      .then(() => {
        setCopied(true);
        window.setTimeout(() => setCopied(false), 1500);
      })
      .catch(() => {});
  };
  return (
    <button
      type="button"
      onClick={onCopy}
      aria-label={copied ? `Copied ${label}` : `Copy ${label}`}
      className="shrink-0 inline-flex items-center justify-center h-6 w-6 rounded-md text-slate-400 hover:text-slate-700 hover:bg-slate-100 transition-colors"
    >
      {copied ? (
        <svg viewBox="0 0 20 20" fill="none" className="h-3.5 w-3.5" aria-hidden="true">
          <path d="M5 10.5l3.5 3.5L15 6" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" />
        </svg>
      ) : (
        <svg viewBox="0 0 20 20" fill="none" className="h-3.5 w-3.5" aria-hidden="true">
          <rect x="7" y="7" width="9" height="9" rx="1.5" stroke="currentColor" strokeWidth="1.5" />
          <path d="M13 4.5H5.5A1.5 1.5 0 004 6v7.5" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" />
        </svg>
      )}
    </button>
  );
}

function RepoGlyph() {
  return (
    <svg viewBox="0 0 20 20" fill="none" className="h-3 w-3" aria-hidden="true">
      <circle cx="6" cy="5" r="1.75" stroke="currentColor" strokeWidth="1.5" />
      <circle cx="6" cy="15" r="1.75" stroke="currentColor" strokeWidth="1.5" />
      <circle cx="14" cy="7" r="1.75" stroke="currentColor" strokeWidth="1.5" />
      <path d="M6 6.75v6.5M14 8.75c0 3-8 2-8 4.5" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" />
    </svg>
  );
}

function LockMark({ reason }: { reason: string }) {
  return (
    <span title={reason} className="inline-flex text-slate-400">
      <svg viewBox="0 0 20 20" fill="none" className="h-3 w-3" aria-hidden="true">
        <rect x="4.5" y="9" width="11" height="7.5" rx="1.5" stroke="currentColor" strokeWidth="1.5" />
        <path d="M7 9V6.5a3 3 0 016 0V9" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" />
      </svg>
      <span className="sr-only">{reason}</span>
    </span>
  );
}

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div>
      <dt className="text-[11px] uppercase tracking-wide text-slate-400 font-medium">{label}</dt>
      <dd className="mt-0.5 text-sm text-slate-700">{children}</dd>
    </div>
  );
}

function InvocationRow({
  label,
  value,
  copyLabel,
  lock,
}: {
  label: string;
  value?: string | null;
  copyLabel: string;
  lock?: string | null;
}) {
  return (
    <div>
      <dt className="flex items-center gap-1 text-[11px] uppercase tracking-wide text-slate-400 font-medium">
        {label}
        {lock && <LockMark reason={lock} />}
      </dt>
      <dd className="mt-0.5">
        {value ? (
          <div className="flex items-center gap-1.5">
            <code className="text-xs font-mono text-slate-700 break-all" title={value}>{maskArn(value)}</code>
            <CopyButton value={value} label={copyLabel} />
          </div>
        ) : (
          <span className="text-sm text-slate-400">Not set</span>
        )}
      </dd>
    </div>
  );
}

export default function McpServerDetail() {
  const { id = '' } = useParams();
  const { user } = useUser();

  const [active, setActive] = useState<string>('overview');

  const [actionPending, setActionPending] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);
  const [reasonFor, setReasonFor] = useState<'approve' | 'reject' | 'deprecate' | null>(null);
  const [reason, setReason] = useState('');
  const [confirmingShare, setConfirmingShare] = useState(false);

  const roleLevel = user?.role_level ?? 0;
  const isOperatorOrHigher = roleLevel >= 1;
  const isAdmin = roleLevel >= 2;

  const tenantDirectory = useTenantDirectory(isAdmin);

  const mcpLoad = useLoad(() => mcpServersApi.get(id), [id], 'Failed to load MCP server.');
  const mcp = mcpLoad.state.data;

  const ownership = mcp ? repoOwnership(mcp) : null;
  const ownerProjectId = ownership?.projectId ?? null;
  const ownerRepoId = ownership?.repoId ?? null;
  const repoNameLoad = useLoad(
    () =>
      ownerProjectId && ownerRepoId
        ? projectsApi.getRepoStatus(ownerProjectId, ownerRepoId).then((repo) => repo.name)
        : Promise.resolve<string | null>(null),
    [ownerProjectId, ownerRepoId],
  );
  const repoName = repoNameLoad.state.kind === 'ready' ? repoNameLoad.state.data : null;

  const tabs = visibleMcpTabs(mcp?.kind ?? 'standard');

  const refresh = mcpLoad.reload;

  const runSubmit = async () => {
    setActionPending(true);
    setActionError(null);
    try {
      await mcpServersApi.submit(id);
      refresh();
    } catch (err: unknown) {
      setActionError(err instanceof Error ? err.message : 'Submit failed.');
    } finally {
      setActionPending(false);
    }
  };

  const openReason = (action: 'approve' | 'reject' | 'deprecate') => {
    setReasonFor(action);
    setReason('');
    setActionError(null);
  };

  const cancelReason = () => {
    setReasonFor(null);
    setReason('');
    setActionError(null);
  };

  const runTransition = async () => {
    if (!reasonFor) return;
    const trimmed = reason.trim();
    if (!trimmed) {
      setActionError('A reason is required.');
      return;
    }
    setActionPending(true);
    setActionError(null);
    try {
      await mcpServersApi.transition(id, reasonFor, trimmed);
      setReasonFor(null);
      setReason('');
      refresh();
    } catch (err: unknown) {
      setActionError(err instanceof Error ? err.message : 'Action failed.');
    } finally {
      setActionPending(false);
    }
  };

  const handleToggleShared = async (next: boolean) => {
    setActionPending(true);
    setActionError(null);
    try {
      const updated = await mcpServersApi.update(id, { shared: next });
      mcpLoad.patch(() => updated);
    } catch (err: unknown) {
      setActionError(err instanceof Error ? err.message : 'Shared update failed.');
    } finally {
      setActionPending(false);
    }
  };
  const requestToggleShared = (next: boolean) => {
    if (next) setConfirmingShare(true);
    else void handleToggleShared(false);
  };

  if (mcpLoad.state.kind === 'loading') {
    return (
      <div className="max-w-7xl mx-auto px-6 py-8">
        <p className="text-slate-400 text-sm">Loading MCP server…</p>
      </div>
    );
  }

  const loadMessage = mcpLoad.state.kind === 'error' ? mcpLoad.state.message : null;
  // Decision: docs/adr/frontend-refusals-classified-by-code.md
  const notFound = mcpLoad.state.kind === 'error' && mcpLoad.state.failure?.code === 'mcp_server.not_found';
  if (notFound || !mcp) {
    return (
      <div className="max-w-7xl mx-auto px-6 py-8">
        <p className="text-slate-500 text-sm">
          MCP server <span className="font-mono">{id}</span> not found.{' '}
          <Link to="/tools-mcp" className="text-blue-700 hover:underline">Back to Tools &amp; MCP</Link>.
        </p>
      </div>
    );
  }

  if (loadMessage !== null) {
    return (
      <div className="max-w-7xl mx-auto px-6 py-8">
        <Link to="/tools-mcp" className="text-xs text-slate-400 hover:text-slate-600">← Tools &amp; MCP</Link>
        <div className={`${CARD} mt-4 border-red-200/70 p-6`}>
          <h3 className="text-sm font-semibold text-red-700">Couldn’t load MCP server</h3>
          <p className="text-sm text-slate-600 mt-1">{loadMessage}</p>
          <button
            onClick={refresh}
            className="mt-3 px-3 py-1.5 rounded-lg bg-white border border-slate-300 text-slate-700 text-xs font-medium hover:bg-slate-50 transition-colors"
          >
            Retry
          </button>
        </div>
      </div>
    );
  }

  const activeTab = tabs.find((t) => t.id === active) ?? tabs[0];

  const lifecycle = lifecycleBadge(mcp.lifecycle_state);
  const kind = kindBadge(mcp.kind);
  const endpoint = mcpEndpoint(mcp);

  const canSubmit = mcp.lifecycle_state === 'proposed' && isOperatorOrHigher;
  const canDecide = mcp.lifecycle_state === 'pending_approval' && isAdmin;
  const canDeprecate = mcp.lifecycle_state === 'approved' && isAdmin;
  const hasActions = canSubmit || canDecide || canDeprecate;

  const reasonNoun =
    reasonFor === 'approve' ? 'approval' : reasonFor === 'reject' ? 'rejection' : 'deprecation';
  const confirmCls =
    reasonFor === 'approve'
      ? 'bg-emerald-600 hover:bg-emerald-700'
      : reasonFor === 'reject'
        ? 'bg-red-600 hover:bg-red-700'
        : 'bg-slate-700 hover:bg-slate-800';

  return (
    <div className="min-h-[calc(100vh-4rem)] relative">
      <div className="relative max-w-7xl mx-auto px-6 py-6">
        <Link to="/tools-mcp" className="text-xs text-slate-400 hover:text-slate-600">← Tools &amp; MCP</Link>

        <div className="mt-2 mb-5">
          <div className="flex items-start justify-between gap-4 flex-wrap">
            <div className="flex items-center gap-3 min-w-0">
              <AgentAvatar name={mcp.name} size="lg" />
              <div className="min-w-0">
                <h1 className="flex items-center gap-1.5 text-xl font-semibold text-slate-900 min-w-0">
                  <span className="truncate">{mcp.name}</span>
                  {ownership && <LockMark reason={lockedReason('name', true) ?? ''} />}
                </h1>
                <div className="flex flex-wrap items-center gap-1.5 mt-1">
                  <span className={`inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium ${lifecycle.cls}`}>
                    {lifecycle.label}
                  </span>
                  <span className={`inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium ${kind.cls}`}>
                    {kind.label}
                  </span>
                  {ownership && (
                    <Link
                      to={ownership.href}
                      className="inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-xs font-medium bg-white text-blue-700 ring-1 ring-inset ring-blue-200 hover:bg-blue-50 transition-colors"
                    >
                      <RepoGlyph />
                      {repoOwnershipLabel(repoName)}
                    </Link>
                  )}
                  <span className="text-xs text-slate-400">{endpoint ? endpoint.url : 'No endpoint set'}</span>
                </div>
              </div>
            </div>

            {hasActions && !reasonFor && (
              <div className="flex flex-wrap items-center gap-2">
                {canSubmit && (
                  <button
                    onClick={runSubmit}
                    disabled={actionPending}
                    className="px-3.5 py-1.5 rounded-lg bg-blue-600 text-white text-sm font-medium hover:bg-blue-700 transition-colors disabled:opacity-50"
                  >
                    {actionPending ? 'Working…' : 'Submit for approval'}
                  </button>
                )}
                {canDecide && (
                  <>
                    <button
                      onClick={() => openReason('approve')}
                      disabled={actionPending}
                      className="px-3.5 py-1.5 rounded-lg bg-emerald-600 text-white text-sm font-medium hover:bg-emerald-700 transition-colors disabled:opacity-50"
                    >
                      Approve
                    </button>
                    <button
                      onClick={() => openReason('reject')}
                      disabled={actionPending}
                      className="px-3.5 py-1.5 rounded-lg bg-red-600 text-white text-sm font-medium hover:bg-red-700 transition-colors disabled:opacity-50"
                    >
                      Reject
                    </button>
                  </>
                )}
                {canDeprecate && (
                  <button
                    onClick={() => openReason('deprecate')}
                    disabled={actionPending}
                    className="px-3.5 py-1.5 rounded-lg bg-white border border-slate-300 text-slate-700 text-sm font-medium hover:bg-slate-50 transition-colors disabled:opacity-50"
                  >
                    Deprecate
                  </button>
                )}
              </div>
            )}
          </div>

          {reasonFor && (
            <div className={`${CARD} mt-3 p-4`}>
              <label htmlFor="transition-reason" className="text-xs font-medium text-slate-600">
                Reason for {reasonNoun} (required)
              </label>
              <textarea
                id="transition-reason"
                value={reason}
                onChange={(e) => setReason(e.target.value)}
                rows={2}
                aria-label={`Reason for ${reasonFor}`}
                className="mt-1 w-full px-3 py-2 text-sm rounded-lg border border-slate-300 focus:outline-none focus:ring-2 focus:ring-blue-500/40"
                placeholder="Explain the decision…"
              />
              {actionError && (
                <p className="text-sm text-red-600 mt-2" role="alert">{actionError}</p>
              )}
              <div className="flex items-center gap-2 mt-3">
                <button
                  onClick={runTransition}
                  disabled={actionPending}
                  className={`px-3.5 py-1.5 rounded-lg text-sm font-medium text-white transition-colors disabled:opacity-50 ${confirmCls}`}
                >
                  {actionPending ? 'Working…' : `Confirm ${reasonNoun}`}
                </button>
                <button
                  onClick={cancelReason}
                  disabled={actionPending}
                  className="px-3.5 py-1.5 rounded-lg bg-white border border-slate-300 text-slate-700 text-sm font-medium hover:bg-slate-50 transition-colors disabled:opacity-50"
                >
                  Cancel
                </button>
              </div>
            </div>
          )}

          {!reasonFor && actionError && (
            <p className="text-sm text-red-600 mt-3" role="alert">{actionError}</p>
          )}
        </div>

        <ConfirmDialog
          open={confirmingShare}
          title="Share with every tenant?"
          message={`Every tenant on this platform will see “${mcp.name}” in its MCP list, detail page and governance graph, and Operators in any tenant can grant their agents access to it. Only its own tenant and admins can still edit it. Turn sharing off to make it private again.`}
          confirmText="Share"
          variant="warning"
          onConfirm={() => {
            setConfirmingShare(false);
            void handleToggleShared(true);
          }}
          onCancel={() => setConfirmingShare(false)}
        />

        <div className="flex items-center gap-1 mb-6 p-1 bg-slate-100/80 rounded-xl w-fit overflow-x-auto">
          {tabs.map((tab) => (
            <button
              key={tab.id}
              onClick={() => setActive(tab.id)}
              className={`px-3.5 py-1.5 rounded-lg text-sm font-medium transition-all whitespace-nowrap ${activeTab.id === tab.id ? 'bg-white text-slate-900 shadow-sm' : 'text-slate-500 hover:text-slate-700'}`}
            >
              {tab.label}{tab.gatewayOnly ? ' ★' : ''}
            </button>
          ))}
        </div>

        {activeTab.id === 'overview' ? (
          <OverviewTab
            mcp={mcp}
            ownership={ownership}
            repoName={repoName}
            tenantName={resolveTenantName(mcp.tenant_id, user?.tenants, tenantDirectory)}
            roleLevel={roleLevel}
            actionPending={actionPending}
            onToggleShared={requestToggleShared}
          />
        ) : activeTab.id === 'tools' ? (
          <McpToolsTab mcp={mcp} canManage={isOperatorOrHigher} onRefreshed={(fresh) => mcpLoad.patch(() => fresh)} />
        ) : activeTab.id === 'connected-agents' ? (
          <McpConnectedAgentsTab mcp={mcp} canManage={isOperatorOrHigher} repoHref={ownership?.href ?? null} />
        ) : activeTab.id === 'policies' ? (
          <CedarPoliciesTab mcp={mcp} canManage={isOperatorOrHigher} />
        ) : (
          <div className="bg-white/60 backdrop-blur rounded-xl border border-slate-200/60 p-8 text-center text-slate-400 text-sm">
            <span className="font-medium text-slate-600">{activeTab.label}</span>
          </div>
        )}
      </div>
    </div>
  );
}

function OverviewTab({
  mcp,
  ownership,
  repoName,
  tenantName,
  roleLevel,
  actionPending,
  onToggleShared,
}: {
  mcp: McpServer;
  ownership: RepoOwnership | null;
  repoName: string | null;
  tenantName: string | null;
  roleLevel: number;
  actionPending: boolean;
  onToggleShared: (next: boolean) => void;
}) {
  const kind = kindBadge(mcp.kind);
  const tenant = tenantBadge(tenantName ?? mcp.tenant_id);
  const isShared = mcp.shared === true;
  const shared = sharedBadge(isShared);
  const repoOwned = ownership !== null;

  return (
    <div className="space-y-5">
      {ownership && (
        <div className={`${CARD} border-blue-200/70 p-5`}>
          <div className="flex items-start justify-between gap-4 flex-wrap">
            <div className="min-w-0">
              <h2 className="text-sm font-semibold text-slate-800">{repoOwnershipLabel(repoName)}</h2>
              <p className="text-sm text-slate-600 mt-1 max-w-2xl">
                This MCP server was added from Ops, and its gateway is built by the repository’s
                Terraform. Name, kind, gateway ARN, runtime ARN and endpoint URL can’t be edited
                here. Change them in the repository and rebuild.
              </p>
              <p className="text-sm text-slate-600 mt-2">
                <span className="font-medium text-slate-700">Delete the repository instead.</span>{' '}
                Deleting the repository removes this MCP server with it.
              </p>
            </div>
            <Link
              to={ownership.href}
              className="shrink-0 px-3.5 py-1.5 rounded-lg bg-white border border-slate-300 text-slate-700 text-sm font-medium hover:bg-slate-50 transition-colors"
            >
              Open in Ops →
            </Link>
          </div>
        </div>
      )}

      <div className={`${CARD} p-5`}>
        <h2 className="text-sm font-semibold text-slate-800 mb-1">Description</h2>
        <p className="text-sm text-slate-700 whitespace-pre-wrap">
          {mcp.description ? mcp.description : <span className="text-slate-400">No description.</span>}
        </p>
      </div>

      <div className={`${CARD} p-5`}>
        <h2 className="text-sm font-semibold text-slate-800 mb-3">Invocation</h2>
        <dl className="grid grid-cols-1 lg:grid-cols-2 gap-x-8 gap-y-4">
          {!mcpEndpoint(mcp)?.fromGateway && (
            <div className="lg:col-span-2">
              <InvocationRow
                label="Endpoint URL"
                value={mcp.endpoint_url}
                copyLabel="endpoint URL"
                lock={lockedReason('endpoint_url', repoOwned)}
              />
            </div>
          )}
          {showGatewayRows(mcp) && (
            <>
              <div className="lg:col-span-2">
                <InvocationRow
                  label="Gateway ARN"
                  value={mcp.gateway_arn}
                  copyLabel="gateway ARN"
                  lock={lockedReason('gateway_arn', repoOwned)}
                />
              </div>
              <div className="lg:col-span-2">
                <InvocationRow label="Gateway URL" value={mcp.gateway_url} copyLabel="gateway URL" />
              </div>
            </>
          )}
          {mcp.runtime_arn && (
            <div className="lg:col-span-2">
              <InvocationRow
                label="Runtime ARN"
                value={mcp.runtime_arn}
                copyLabel="runtime ARN"
                lock={lockedReason('runtime_arn', repoOwned)}
              />
            </div>
          )}
          <Field label="Version">
            <span className="font-mono text-xs">{mcp.version}</span>
          </Field>
        </dl>
      </div>

      <div className={`${CARD} p-5`}>
        <h2 className="text-sm font-semibold text-slate-800 mb-3">Governance</h2>
        <dl className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-x-8 gap-y-4">
          <Field label="Kind">
            <span className="inline-flex items-center gap-1.5">
              <span className={`inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium ${kind.cls}`}>
                {kind.label}
              </span>
              {repoOwned && <LockMark reason={lockedReason('kind', true) ?? ''} />}
            </span>
          </Field>
          <Field label="Tenant">
            <span
              className={`inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium ${tenant.cls}`}
              title={mcp.tenant_id ?? undefined}
            >
              {tenant.label}
            </span>
          </Field>
          <Field label="Shared">
            <span className="inline-flex items-center gap-2">
              <span
                className={`inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium ${shared.cls}`}
              >
                {shared.label}
              </span>
              {canEditShared(roleLevel) && (
                <button
                  type="button"
                  onClick={() => onToggleShared(!isShared)}
                  disabled={actionPending}
                  className="px-2 py-0.5 rounded-lg bg-white border border-slate-300 text-slate-700 text-xs font-medium hover:bg-slate-50 transition-colors disabled:opacity-50"
                >
                  {actionPending ? 'Working…' : isShared ? 'Unshare' : 'Share'}
                </button>
              )}
            </span>
            <p className="text-xs text-slate-500 mt-1">
              Lets every tenant see this server and grant it to their agents. Separate from the
              marketplace listing below.
            </p>
          </Field>
          <Field label="Owner">
            {mcp.owner_email ? (
              <span title={mcp.owner_email}>{emailAlias(mcp.owner_email)}</span>
            ) : (
              <span className="text-slate-400">Unassigned</span>
            )}
          </Field>
          <Field label="Line of Business">{mcp.business_unit ?? '—'}</Field>
          <Field label="Region">{mcp.region ?? '—'}</Field>
          <Field label="Data classification">{dataClassLabel(mcp.data_classification)}</Field>
          <Field label="Identity">
            {mcp.entra_app_id ? (
              <span className="font-mono text-xs break-all">{mcp.entra_app_id}</span>
            ) : (
              <span className="text-amber-700">Identity provisioning pending</span>
            )}
          </Field>
        </dl>
      </div>

      <PublishPanel
        productType="mcp"
        productId={mcp.id}
        lifecycle={mcp.lifecycle_state}
        marketplace={mcp.marketplace}
        blocker={null}
        copy={PUBLISH_PANEL_COPY}
        roleLevel={roleLevel}
      />

      <div className={`${CARD} p-5`}>
        <h2 className="text-sm font-semibold text-slate-800 mb-3">Record</h2>
        <dl className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-x-8 gap-y-4">
          <Field label="Registry ID"><span className="font-mono text-xs break-all">{mcp.id}</span></Field>
          <Field label="Created by">{mcp.created_by ?? '—'}</Field>
          <Field label="Created">{formatDate(mcp.created_at)}</Field>
          <Field label="Updated">{formatDate(mcp.updated_at)}</Field>
        </dl>
      </div>
    </div>
  );
}
