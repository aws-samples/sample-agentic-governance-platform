import { useState } from 'react';
import { useParams, Link } from 'react-router-dom';
import client, { agentOpsApi, agentsApi, projectsApi } from '../../api/client';
import { effectiveRole, mayMaintainProject } from '../operations/projectRoles';
import type { Agent, AgentRepositoryView, DataClassification, AgentAuthType } from '../../api/client';
import { useLoad } from '../../api/useLoad';
import { opsProvenance } from './opsSource';
import { useUser } from '../../contexts/useUser';
import { visibleTabs } from './agentDetailTabs';
import {
  platformLabel,
  runtimeHandleField,
  inventoryOnlyBadge,
  invokeBadge,
  publishRefusal,
  isTabVisibleForPlatform,
  agentObservability,
  tenantWorkspaceUrl,
  isInventoryOnly,
  identityStatusNote,
} from './platformLabels';
import type { ObservabilityPointers } from './platformLabels';
import { AgentAvatar } from './agentUi';
import { lifecycleBadge, originBadge, emailAlias, tenantBadge } from './agentBadges';
import PublishPanel from './marketplace/PublishPanel';
import type { PublishPanelCopy } from './marketplace/PublishPanel';
import { formatDate } from '../operations/opsUi';
import { resolveTenantName, findTenantAccount } from './tenantUi';
import { maskArn } from '../shared/arnDisplay';
import { useTenantDirectory } from './useTenantDirectory';
import AccessTab from './AccessTab';
import AgentMcpServersTab from './AgentMcpServersTab';
import MissingMcpGrantsHint from './MissingMcpGrantsHint';
import InvokePanel from './InvokePanel';
import ObservabilityPanel from './ObservabilityPanel';
import TracesTab from './agent-tabs/TracesTab';
import CostTab from './agent-tabs/CostTab';
import EvaluationsTab from './agent-tabs/EvaluationsTab';
import ContainmentTab from './agent-tabs/ContainmentTab';

function dataClassLabel(d?: DataClassification | null): string {
  return d ?? '—';
}

const AUTH_TYPE_META: Record<AgentAuthType, { label: string; cls: string }> = {
  none: { label: 'None', cls: 'bg-slate-100 text-slate-500' },
  entra: { label: 'Microsoft Entra', cls: 'bg-blue-50 text-blue-700' },
  api_key: { label: 'API key', cls: 'bg-violet-50 text-violet-700' },
};

const CARD = 'bg-white/70 backdrop-blur rounded-xl border border-slate-200/60 shadow-sm';

const PUBLISH_PANEL_COPY: PublishPanelCopy = {
  intro:
    'Offer this agent as a marketplace product. An admin reviews the declared datasheet before the card goes live.',
  never: 'This agent has never been offered in the marketplace.',
  pitchPlaceholder: 'What consumers get from this agent…',
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

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div>
      <dt className="text-[11px] uppercase tracking-wide text-slate-400 font-medium">{label}</dt>
      <dd className="mt-0.5 text-sm text-slate-700">{children}</dd>
    </div>
  );
}

export default function AgentDetail() {
  const { id = '' } = useParams();
  const { user } = useUser();

  const [mcpGrantsVersion, setMcpGrantsVersion] = useState(0);

  const [active, setActive] = useState<string>('overview');

  const [actionPending, setActionPending] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);
  const [reasonFor, setReasonFor] = useState<'approve' | 'reject' | 'deprecate' | null>(null);
  const [reason, setReason] = useState('');

  const roleLevel = user?.role_level ?? 0;
  const isOperatorOrHigher = roleLevel >= 1;
  const isAdmin = roleLevel >= 2;

  const tenantDirectory = useTenantDirectory(isAdmin);

  const agentLoad = useLoad(() => agentsApi.get(id), [id], 'Failed to load agent.');
  const agent = agentLoad.state.data;

  const opsRepoLoad = useLoad(() => agentOpsApi.repository(id), [id], 'Failed to load the repository');
  const opsRepo = opsRepoLoad.state.kind === 'ready' ? opsRepoLoad.state.data : undefined;
  const opsRepoError = opsRepoLoad.state.kind === 'error' ? opsRepoLoad.state.message : '';

  const opsProjectId = opsRepo?.project_id;
  const mayMaintainLoad = useLoad(
    () =>
      opsProjectId
        ? projectsApi
            .get(opsProjectId)
            .then((detail) => mayMaintainProject(effectiveRole(detail), roleLevel, detail.ungoverned))
        : Promise.resolve(false),
    [opsProjectId, roleLevel],
  );
  const mayMaintainOpsProject = mayMaintainLoad.state.kind === 'ready' && mayMaintainLoad.state.data;

  const tabs = visibleTabs(agent?.origin ?? 'Registered').filter((t) =>
    isTabVisibleForPlatform(t.id, agent?.platform),
  );

  const refresh = agentLoad.reload;

  const runSubmit = async () => {
    setActionPending(true);
    setActionError(null);
    try {
      await agentsApi.submit(id);
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
      await agentsApi.transition(id, reasonFor, trimmed);
      setReasonFor(null);
      setReason('');
      refresh();
    } catch (err: unknown) {
      setActionError(err instanceof Error ? err.message : 'Action failed.');
    } finally {
      setActionPending(false);
    }
  };

  const handleReprovision = async () => {
    setActionPending(true);
    setActionError(null);
    try {
      await client.post(`/api/v1/agents/${id}/reprovision`);
      refresh();
    } catch (err: unknown) {
      setActionError(err instanceof Error ? err.message : 'Re-provision failed.');
    } finally {
      setActionPending(false);
    }
  };

  if (agentLoad.state.kind === 'loading') {
    return (
      <div className="max-w-7xl mx-auto px-6 py-8">
        <p className="text-slate-400 text-sm">Loading agent…</p>
      </div>
    );
  }

  const loadMessage = agentLoad.state.kind === 'error' ? agentLoad.state.message : null;
  // Decision: docs/adr/frontend-refusals-classified-by-code.md
  const notFound = agentLoad.state.kind === 'error' && agentLoad.state.failure?.code === 'agent.not_found';
  if (notFound || !agent) {
    return (
      <div className="max-w-7xl mx-auto px-6 py-8">
        <p className="text-slate-500 text-sm">
          Agent <span className="font-mono">{id}</span> not found.{' '}
          <Link to="/agents/all" className="text-blue-700 hover:underline">Back to all agents</Link>.
        </p>
      </div>
    );
  }

  if (loadMessage !== null) {
    return (
      <div className="max-w-7xl mx-auto px-6 py-8">
        <Link to="/agents/all" className="text-xs text-slate-400 hover:text-slate-600">← All agents</Link>
        <div className={`${CARD} mt-4 border-red-200/70 p-6`}>
          <h3 className="text-sm font-semibold text-red-700">Couldn’t load agent</h3>
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

  const lifecycle = lifecycleBadge(agent.lifecycle_state);
  const provenance = opsProvenance(agent.origin, opsRepo, originBadge);
  const origin = provenance.badge;
  const binding = invokeBadge(agent);
  const inventoryOnly = inventoryOnlyBadge(agent.platform);

  const canSubmit = agent.lifecycle_state === 'proposed' && isOperatorOrHigher;
  const canDecide = agent.lifecycle_state === 'pending_approval' && isAdmin;
  const canDeprecate = agent.lifecycle_state === 'approved' && isAdmin;
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
        <Link to="/agents/all" className="text-xs text-slate-400 hover:text-slate-600">← All agents</Link>

        <div className="mt-2 mb-5">
          <div className="flex items-start justify-between gap-4 flex-wrap">
            <div className="flex items-center gap-3 min-w-0">
              <AgentAvatar name={agent.name} size="lg" />
              <div className="min-w-0">
                <h1 className="text-xl font-semibold text-slate-900 truncate">{agent.name}</h1>
                <div className="flex flex-wrap items-center gap-1.5 mt-1">
                  <span className={`inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium ${lifecycle.cls}`}>
                    {lifecycle.label}
                  </span>
                  <span className={`inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium ${origin.cls}`}>
                    {origin.label}
                  </span>
                  {binding && (
                    <span
                      className={`inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium ${binding.tint}`}
                      title={binding.hint}
                    >
                      {binding.label}
                      <span className="sr-only"> — {binding.hint}</span>
                    </span>
                  )}
                  {inventoryOnly && (
                    <span
                      className={`inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium ${inventoryOnly.tint}`}
                      title={inventoryOnly.hint}
                    >
                      {inventoryOnly.label}
                      <span className="sr-only"> — {inventoryOnly.hint}</span>
                    </span>
                  )}
                  <span className="text-xs text-slate-400">{platformLabel(agent.platform)}</span>
                  {provenance.source && (
                    <Link
                      to={provenance.source.to}
                      className="inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-xs font-medium bg-white border border-emerald-200 text-emerald-700 hover:bg-emerald-50 transition-colors"
                    >
                      Source: {provenance.source.name} → Open in Ops
                    </Link>
                  )}
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

        <MissingMcpGrantsHint
          agentId={agent.id}
          refreshKey={mcpGrantsVersion}
          canManage={isOperatorOrHigher}
          onGrant={() => setActive('mcp-servers')}
        />

        <div
          role="tablist"
          aria-label="Agent sections"
          className="flex items-center gap-1 mb-6 p-1 bg-slate-100/80 rounded-xl w-fit overflow-x-auto"
        >
          {tabs.map((tab) => (
            <button
              key={tab.id}
              role="tab"
              aria-selected={activeTab.id === tab.id}
              onClick={() => setActive(tab.id)}
              className={`px-3.5 py-1.5 rounded-lg text-sm font-medium transition-all whitespace-nowrap ${activeTab.id === tab.id ? 'bg-white text-slate-900 shadow-sm' : 'text-slate-500 hover:text-slate-700'}`}
            >
              {tab.label}{tab.deployedOnly ? ' ★' : ''}
            </button>
          ))}
        </div>

        {activeTab.id === 'overview' ? (
          <OverviewTab
            agent={agent}
            tenantName={resolveTenantName(agent.tenant_id, user?.tenants, tenantDirectory)}
            workspaceUrl={tenantWorkspaceUrl(
              findTenantAccount(agent.tenant_id, user?.tenants, tenantDirectory)?.stages,
            )}
            roleLevel={roleLevel}
            datasetRepo={isOperatorOrHigher && mayMaintainOpsProject ? opsRepo ?? null : null}
          />
        ) : activeTab.id === 'deployment' ? (
          <DeploymentTab agent={agent} />
        ) : activeTab.id === 'access' ? (
          <AccessTab
            agent={agent}
            canManage={isOperatorOrHigher}
            currentOid={user?.oid ?? null}
            onReprovision={handleReprovision}
          />
        ) : activeTab.id === 'mcp-servers' ? (
          <AgentMcpServersTab
            agent={agent}
            canManage={isOperatorOrHigher}
            onGrantsChanged={() => setMcpGrantsVersion((v) => v + 1)}
          />
        ) : activeTab.id === 'traces' ? (
          <TracesTab agent={agent} repo={opsRepo} />
        ) : activeTab.id === 'cost' ? (
          <CostTab agentId={agent.id} />
        ) : activeTab.id === 'evaluations' ? (
          <EvaluationsTab agentId={agent.id} repo={opsRepo} repoError={opsRepoError} />
        ) : activeTab.id === 'containment' ? (
          <ContainmentTab agent={agent} canOperate={isOperatorOrHigher} canAdmin={isAdmin} onOpenTraces={() => setActive('traces')} />
        ) : null}
      </div>
    </div>
  );
}

function InvocationRow({
  label,
  value,
  copyLabel,
}: {
  label: string;
  value?: string | null;
  copyLabel: string;
}) {
  return (
    <div>
      <dt className="text-[11px] uppercase tracking-wide text-slate-400 font-medium">{label}</dt>
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

function ObservabilityCard({ pointers }: { pointers: ObservabilityPointers }) {
  return (
    <div className={`${CARD} p-5`}>
      <h2 className="text-sm font-semibold text-slate-800 mb-1">Observability</h2>
      <p className="text-xs text-slate-400 mb-3">
        Traces for this agent are collected in two places — one of them is not AGP.
      </p>

      <div className="flex items-start gap-2">
        <span
          aria-hidden="true"
          className={`mt-1.5 h-1.5 w-1.5 shrink-0 rounded-full ${
            pointers.langfuse.provisioned ? 'bg-emerald-500' : 'bg-slate-300'
          }`}
        />
        <p className="text-sm text-slate-700">{pointers.langfuse.note}</p>
      </div>

      {pointers.workspace && (
        <div className="mt-4 pt-4 border-t border-slate-200/70 space-y-2">
          <div>
            <a
              href={pointers.workspace.auditUrl}
              target="_blank"
              rel="noopener noreferrer"
              className="text-sm text-blue-700 hover:underline"
            >
              {pointers.workspace.auditLabel} ↗
            </a>
            <span className="block text-[11px] font-mono text-slate-400 mt-0.5 break-all">
              {pointers.workspace.workspaceUrl}
            </span>
          </div>
          <p className="text-sm text-slate-600">{pointers.workspace.inferenceNote}</p>
        </div>
      )}
    </div>
  );
}

function OverviewTab({
  agent,
  tenantName,
  workspaceUrl,
  roleLevel,
  datasetRepo,
}: {
  agent: Agent;
  tenantName: string | null;
  workspaceUrl: string | null;
  roleLevel: number;
  datasetRepo: AgentRepositoryView | null;
}) {
  const auth = AUTH_TYPE_META[agent.auth_type] ?? AUTH_TYPE_META.none;
  const tenant = tenantBadge(tenantName ?? agent.tenant_id);
  const handle = runtimeHandleField(agent);
  const observability = agentObservability(agent, workspaceUrl);
  const identityReady =
    agent.identity_status === 'provisioned' && !!agent.entra_sp_id && !!agent.invoker_role_id;
  const refusal = publishRefusal(agent.platform);
  const blocker = refusal
    ? refusal.note
    : !identityReady
      ? `Publishing needs a provisioned Entra identity, because subscriptions are granted as app-role assignments on it. This agent’s identity is ${agent.identity_status === 'failed' ? 'in a failed state' : 'not provisioned yet'}.`
      : null;

  return (
    <div className="space-y-5">
      <div className={`${CARD} p-5`}>
        <h2 className="text-sm font-semibold text-slate-800 mb-1">Purpose</h2>
        <p className="text-sm text-slate-700 whitespace-pre-wrap">
          {agent.purpose ? agent.purpose : <span className="text-slate-400">No purpose described.</span>}
        </p>
      </div>

      <div className={`${CARD} p-5`}>
        <h2 className="text-sm font-semibold text-slate-800 mb-3">Invocation</h2>
        <dl className="grid grid-cols-1 lg:grid-cols-2 gap-x-8 gap-y-4">
          <div className="lg:col-span-2">
            <InvocationRow label="Endpoint URL" value={agent.endpoint_url} copyLabel="endpoint URL" />
          </div>
          <div>
            <dt className="text-[11px] uppercase tracking-wide text-slate-400 font-medium">Authentication</dt>
            <dd className="mt-0.5">
              <span className={`inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium ${auth.cls}`}>
                {auth.label}
              </span>
            </dd>
          </div>
          {handle && (
            <div className="lg:col-span-2">
              <InvocationRow label={handle.label} value={handle.value} copyLabel={handle.copyLabel} />
            </div>
          )}
        </dl>
      </div>

      <InvokePanel agent={agent} datasetRepo={datasetRepo} />

      {observability && <ObservabilityCard pointers={observability} />}

      {agent.platform && !isInventoryOnly(agent.platform) && <ObservabilityPanel agent={agent} />}

      <div className={`${CARD} p-5`}>
        <h2 className="text-sm font-semibold text-slate-800 mb-3">Governance</h2>
        <dl className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-x-8 gap-y-4">
          <Field label="Tenant">
            <span
              className={`inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium ${tenant.cls}`}
              title={agent.tenant_id ?? undefined}
            >
              {tenant.label}
            </span>
          </Field>
          <Field label="Line of Business">{agent.business_unit ?? '—'}</Field>
          <Field label="Owner">
            {agent.sponsor_email ? (
              <span title={agent.sponsor_email}>{emailAlias(agent.sponsor_email)}</span>
            ) : (
              <span className="text-slate-400">Unassigned</span>
            )}
          </Field>
          <Field label="Region">{agent.region ?? '—'}</Field>
          <Field label="Data classification">{dataClassLabel(agent.data_classification)}</Field>
          <Field label="Platform">{platformLabel(agent.platform)}</Field>
          <Field label="Framework">{agent.framework ?? '—'}</Field>
          <Field label="Identity">
            {agent.entra_app_id ? (
              <span className="font-mono text-xs break-all">{agent.entra_app_id}</span>
            ) : (
              <span className="text-amber-700">{identityStatusNote(agent.identity_status)}</span>
            )}
          </Field>
        </dl>
      </div>

      <PublishPanel
        productType="agent"
        productId={agent.id}
        lifecycle={agent.lifecycle_state}
        marketplace={agent.marketplace}
        blocker={blocker}
        copy={PUBLISH_PANEL_COPY}
        roleLevel={roleLevel}
      />

      <div className={`${CARD} p-5`}>
        <h2 className="text-sm font-semibold text-slate-800 mb-3">Record</h2>
        <dl className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-x-8 gap-y-4">
          <Field label="Registry ID"><span className="font-mono text-xs break-all">{agent.id}</span></Field>
          <Field label="Created by">{agent.created_by ?? '—'}</Field>
          <Field label="Created">{formatDate(agent.created_at)}</Field>
          <Field label="Updated">{formatDate(agent.updated_at)}</Field>
        </dl>
      </div>
    </div>
  );
}

function DeploymentTab({ agent }: { agent: Agent }) {
  return (
    <div className="space-y-5">
      <div className={`${CARD} p-5`}>
        <h2 className="text-sm font-semibold text-slate-800 mb-1">Deployment</h2>
        <p className="text-sm text-slate-700">Deployed via platform pipeline.</p>
        <dl className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-x-8 gap-y-4 mt-4">
          <Field label="Platform">{platformLabel(agent.platform)}</Field>
          <Field label="Region">{agent.region ?? '—'}</Field>
          <Field label="Framework">{agent.framework ?? '—'}</Field>
        </dl>
      </div>

      <div className="bg-white/60 backdrop-blur rounded-xl border border-slate-200/60 p-5 text-sm text-slate-400">
        <span className="font-medium text-slate-600">Deploy details.</span> Build, runtime endpoint,
        and observability links.
      </div>
    </div>
  );
}
