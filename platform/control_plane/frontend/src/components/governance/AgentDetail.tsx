import { useEffect, useState } from 'react';
import { useParams, Link } from 'react-router-dom';
import client, { agentsApi, marketplaceApi } from '../../api/client';
import type { Agent, DataClassification, AuthType, MarketplacePublishRequest } from '../../api/client';
import { useUser } from '../../contexts/UserContext';
import { visibleTabs } from './agentDetailTabs';
import {
  platformLabel,
  runtimeHandleField,
  bindingModeBadge,
  isTabVisibleForPlatform,
  agentObservability,
  tenantWorkspaceUrl,
} from './platformLabels';
import type { ObservabilityPointers } from './platformLabels';
import { AgentAvatar, lifecycleBadge, originBadge, emailAlias, tenantBadge } from './agentUi';
import {
  EMPTY_DATASHEET_DRAFT,
  MARKETPLACE_STATE_UI,
  buildDatasheet,
  draftFromDatasheet,
  isMissingPublishRequest,
  listingStaysLive,
  publishStateLabel,
  validateDatasheet,
} from './publishForm';
import type { DatasheetDraft } from './publishForm';
import { derivePublishState, resolveTenantName, findTenantAccount } from './tenantUi';
import { useTenantDirectory } from './useTenantDirectory';
import AccessTab from './AccessTab';
import AgentMcpServersTab from './AgentMcpServersTab';
import InvokePanel from './InvokePanel';
import TracesTab from './agent-tabs/TracesTab';
import CostTab from './agent-tabs/CostTab';

// ---------------------------------------------------------------------------
// Local label helpers (badge maps live in agentUi — Decision 9; platform labels and every
// platform-conditional DECISION on this page live in platformLabels — E29/T9).
// ---------------------------------------------------------------------------

function dataClassLabel(d?: DataClassification | null): string {
  return d ?? '—';
}

// Auth-type display: human label + a tasteful badge class (Decision 1).
const AUTH_TYPE_META: Record<AuthType, { label: string; cls: string }> = {
  none: { label: 'None', cls: 'bg-slate-100 text-slate-500' },
  entra: { label: 'Microsoft Entra', cls: 'bg-blue-50 text-blue-700' },
  api_key: { label: 'API key', cls: 'bg-violet-50 text-violet-700' },
};

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

// Tighter card chrome than E4 (density pass — matches the new list idiom).
const CARD = 'bg-white/70 backdrop-blur rounded-xl border border-slate-200/60 shadow-sm';

// ---------------------------------------------------------------------------
// CopyButton — inline-SVG copy affordance (no icon library, Decision 7).
// ---------------------------------------------------------------------------
function CopyButton({ value, label }: { value: string; label: string }) {
  const [copied, setCopied] = useState(false);
  const onCopy = async () => {
    try {
      await navigator.clipboard.writeText(value);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1500);
    } catch {
      // Clipboard unavailable (e.g. insecure context) — fail silently.
    }
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

// A dense labeled definition row (denser than the E4 grid Field).
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

  const [agent, setAgent] = useState<Agent | null>(null);
  const [loading, setLoading] = useState(true);
  const [notFound, setNotFound] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Forces a re-fetch after an action (submit/approve/reject/deprecate) so the
  // badge + available actions update.
  const [reloadNonce, setReloadNonce] = useState(0);

  const [active, setActive] = useState<string>('overview');

  // Lifecycle-action UI state.
  const [actionPending, setActionPending] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);
  // Which transition is awaiting a reason ('approve' | 'reject' | 'deprecate' | null).
  const [reasonFor, setReasonFor] = useState<'approve' | 'reject' | 'deprecate' | null>(null);
  const [reason, setReason] = useState('');

  // Role gating (role_level: VIEWER=0, OPERATOR=1, ADMIN=2). Default 0 when no user.
  const roleLevel = user?.role_level ?? 0;
  const isOperatorOrHigher = roleLevel >= 1;
  const isAdmin = roleLevel >= 2;

  // Tenant name resolution (E24): memberships first, widened by the admin directory.
  const tenantDirectory = useTenantDirectory(isAdmin);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError(null);
    setNotFound(false);
    agentsApi
      .get(id)
      .then((res) => {
        if (cancelled) return;
        setAgent(res);
      })
      .catch((err: unknown) => {
        if (cancelled) return;
        const msg = err instanceof Error ? err.message : 'Failed to load agent.';
        // The backend 404s a missing record; surface the not-found fallback.
        if (/not found/i.test(msg) || /404/.test(msg)) {
          setNotFound(true);
        } else {
          setError(msg);
        }
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [id, reloadNonce]);

  // Keep the active tab valid for the loaded agent's visible tabs.
  //
  // Two independent gates, composed. `visibleTabs` drops origin-conditional tabs (Deployment is
  // Deployed-only); `isTabVisibleForPlatform` drops tabs whose CAPABILITY does not exist on this
  // agent's platform — HIDDEN, never greyed (E29 design §4). A greyed tab promises a feature that
  // is coming; Bedrock Guardrails on a Databricks agent is not coming, because it would be a
  // different product built on Databricks' own primitives.
  //
  // Applied here rather than inside `visibleTabs` because the two gates read different fields
  // (origin vs platform) and `visibleTabs`'s origin-only signature is asserted by its own tests.
  const tabs = visibleTabs(agent?.origin ?? 'Registered').filter((t) =>
    isTabVisibleForPlatform(t.id, agent?.platform),
  );
  useEffect(() => {
    if (!tabs.some((t) => t.id === active)) {
      setActive(tabs[0].id);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [agent?.origin, agent?.platform]);

  // -- actions --------------------------------------------------------------

  const refresh = () => setReloadNonce((n) => n + 1);

  const runSubmit = async () => {
    setActionPending(true);
    setActionError(null);
    try {
      await agentsApi.submit(id);
      refresh();
    } catch (err: unknown) {
      // Surfaces a clean 409 IllegalTransition message if the edge is illegal.
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
      // 409 IllegalTransitionError surfaces here via err.message; page stays usable.
      setActionError(err instanceof Error ? err.message : 'Action failed.');
    } finally {
      setActionPending(false);
    }
  };

  // Re-provision the agent's Entra identity (Epic 6). The backend route
  // (POST /agents/{id}/reprovision) is owned by T-ROUTES; we call it directly
  // via the shared axios instance rather than widening the already-reviewed
  // agentsApi. The response interceptor unwraps the backend `detail` into
  // err.message (same as every other api method here), so we surface that.
  // Refetch on success so the Access banner + identity_status reflect the
  // now-pending re-config.
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

  // Cross-tenant publish flip (E24/T5). The response carries the updated Agent,
  // so lift it straight into state (no refetch). Errors surface in the header
  // actionError slot like every other mutation on this page.
  const handleTogglePublish = async (next: boolean) => {
    setActionPending(true);
    setActionError(null);
    try {
      setAgent(await agentsApi.publish(id, next));
    } catch (err: unknown) {
      setActionError(err instanceof Error ? err.message : 'Publish update failed.');
    } finally {
      setActionPending(false);
    }
  };

  // -- render ---------------------------------------------------------------

  if (loading) {
    return (
      <div className="max-w-7xl mx-auto px-6 py-8">
        <p className="text-slate-400 text-sm">Loading agent…</p>
      </div>
    );
  }

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

  if (error) {
    return (
      <div className="max-w-7xl mx-auto px-6 py-8">
        <Link to="/agents/all" className="text-xs text-slate-400 hover:text-slate-600">← All agents</Link>
        <div className={`${CARD} mt-4 border-red-200/70 p-6`}>
          <h3 className="text-sm font-semibold text-red-700">Couldn’t load agent</h3>
          <p className="text-sm text-slate-600 mt-1">{error}</p>
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
  const origin = originBadge(agent.origin);
  // The binding-mode badge (C-6). null for an AWS agent, for a record provisioned before its mode
  // was resolved, and for any mode this build does not know — an unprobed agent has no mode to
  // claim. Lives in the header beside lifecycle/origin because HOW a call is attributed is a
  // property of the agent, not of one tab.
  const binding = bindingModeBadge(agent.binding_mode);

  // Two-step lifecycle gating (Decision 2 — fixes the DRAFT→APPROVED bug):
  //  proposed         → Submit for approval (Operator+); NEVER Approve here.
  //  pending_approval → Approve / Reject    (Admin).
  //  approved         → Deprecate           (Admin, legal terminal edge).
  //  rejected / deprecated → no actions (deprecated is terminal).
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

        {/* ----- Page header: avatar + name + badges + single action bar ----- */}
        {/* The lifecycle actions live HERE — rendered ONCE regardless of the    */}
        {/* active tab (fixes the actions-on-every-tab bug, Decision 2/§Bug 5).  */}
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
                  {/* Binding mode (C-6) — the badge carries its consequence in BOTH `title`
                      (pointer hover) and an sr-only span, so the consequence is never colour-only
                      or hover-only. A keyboard/screen-reader user gets the same sentence a mouse
                      user gets. That holds for all three modes without a change here (E29/T14):
                      the mode-specific copy lives entirely in `bindingModeBadge`, so
                      `invoke_unavailable` gets its "what federation needs" line by the same route
                      the sp_secret audit-attribution cost gets its own. */}
                  {binding && (
                    <span
                      className={`inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium ${binding.tint}`}
                      title={binding.hint}
                    >
                      {binding.label}
                      <span className="sr-only"> — {binding.hint}</span>
                    </span>
                  )}
                  <span className="text-xs text-slate-400">{platformLabel(agent.platform)}</span>
                </div>
              </div>
            </div>

            {/* Action bar — buttons only when there's a legal action for this  */}
            {/* lifecycle_state + role. The reason input renders inline below.    */}
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

          {/* Inline reason capture (required) for approve / reject / deprecate. */}
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

          {/* Action error (e.g. a 409 illegal-transition) — page stays usable.  */}
          {actionError && (
            <p className="text-sm text-red-600 mt-3" role="alert">{actionError}</p>
          )}
        </div>

        {/* ----- Tab bar ----- */}
        <div className="flex items-center gap-1 mb-6 p-1 bg-slate-100/80 rounded-xl w-fit overflow-x-auto">
          {tabs.map((tab) => (
            <button
              key={tab.id}
              onClick={() => setActive(tab.id)}
              className={`px-3.5 py-1.5 rounded-lg text-sm font-medium transition-all whitespace-nowrap ${active === tab.id ? 'bg-white text-slate-900 shadow-sm' : 'text-slate-500 hover:text-slate-700'}`}
            >
              {tab.label}{tab.deployedOnly ? ' ★' : ''}
            </button>
          ))}
        </div>

        {/* ----- Tab body (no actions here — actions live in the header) ----- */}
        {activeTab.id === 'overview' ? (
          <OverviewTab
            agent={agent}
            tenantName={resolveTenantName(agent.tenant_id, user?.tenants, tenantDirectory)}
            // The agent tenant's Databricks workspace origin, for the Observability card's
            // pointers. Memberships FIRST, then the admin directory — the same precedence and the
            // same fail-soft as `resolveTenantName` above, and it matters here for a specific
            // reason: OB-9 made `/users/me` project `workspace_url` onto a Databricks membership,
            // so a plain OPERATOR gets the links without the admin-only directory ever loading.
            // An unresolvable tenant yields null, which drops the workspace half of the card and
            // keeps the Langfuse half (knowable from the record alone).
            workspaceUrl={tenantWorkspaceUrl(
              findTenantAccount(agent.tenant_id, user?.tenants, tenantDirectory)?.stages,
            )}
            roleLevel={roleLevel}
            publishPending={actionPending}
            onTogglePublish={handleTogglePublish}
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
          <AgentMcpServersTab agent={agent} canManage={isOperatorOrHigher} />
        ) : activeTab.id === 'traces' ? (
          <TracesTab agentId={agent.id} />
        ) : activeTab.id === 'cost' ? (
          <CostTab agentId={agent.id} />
        ) : null}
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Overview tab
// ---------------------------------------------------------------------------

// One mono value row in the Invocation card, with a "Not set" fallback + copy.
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
            <code className="text-xs font-mono text-slate-700 break-all">{value}</code>
            <CopyButton value={value} label={copyLabel} />
          </div>
        ) : (
          <span className="text-sm text-slate-400">Not set</span>
        )}
      </dd>
    </div>
  );
}

// ---------------------------------------------------------------------------
// ObservabilityCard — two systems, and only one of them is AGP's (E29/T11)
// ---------------------------------------------------------------------------
//
// READ-ONLY AND ZERO-FETCH, by construction: everything it renders comes from the agent record
// already in state plus the tenant directory already loaded for the header. No Databricks call,
// no MLflow bridge (design §4 forbids both), and no probe of whether a trace has actually landed
// — that question is the Traces tab's, asked against the real data.
//
// WHY IT EXISTS ONLY FOR DATABRICKS. An AgentCore agent's observability is already first-class
// (Traces, Cost, the platform Observability page), so a card above them saying "a project exists"
// would be noise. A Databricks agent's telemetry is SPLIT: AGP holds the Langfuse project, and
// per-request prompts/responses plus the who-called-what audit trail live inside the customer's
// own workspace, in tables AGP is not authorised to query and would not want to be. That split is
// a fact the surface has to state, and before this card it was stated nowhere — leaving an
// operator to assume the Traces tab was the whole story.
//
// The card renders NOTHING it decided itself: `agentObservability` returns null for every other
// platform (so the whole card disappears), owns the Langfuse copy, and refuses to build an href
// from a workspace URL that does not pass the tenant form's own validator.
function ObservabilityCard({ pointers }: { pointers: ObservabilityPointers }) {
  return (
    <div className={`${CARD} p-5`}>
      <h2 className="text-sm font-semibold text-slate-800 mb-1">Observability</h2>
      <p className="text-xs text-slate-400 mb-3">
        Traces for this agent are collected in two places — one of them is not AGP.
      </p>

      {/* AGP's half. A dot, because "does a project exist" is a binary record fact — emerald for
          provisioned, slate for not. Never amber/red: no project is a configuration state, not a
          fault, and Langfuse provisioning is deliberately best-effort so that a Langfuse outage
          cannot fail an agent registration. */}
      <div className="flex items-start gap-2">
        <span
          aria-hidden="true"
          className={`mt-1.5 h-1.5 w-1.5 shrink-0 rounded-full ${
            pointers.langfuse.provisioned ? 'bg-emerald-500' : 'bg-slate-300'
          }`}
        />
        <p className="text-sm text-slate-700">{pointers.langfuse.note}</p>
      </div>

      {/* The workspace's half — POINTERS ONLY. Absent entirely when no single workspace URL could
          be attributed to this agent (an unresolvable tenant, or stages on different workspaces —
          see `tenantWorkspaceUrl` for why a guess is worse than a gap). The Langfuse half above
          still renders, because it is knowable from the record alone. */}
      {pointers.workspace && (
        <div className="mt-4 pt-4 border-t border-slate-200/70 space-y-2">
          <div>
            <a
              href={pointers.workspace.auditUrl}
              target="_blank"
              // `noopener` is the security half (a cross-origin tab must not get `window.opener`
              // and thus a handle on this document); `noreferrer` covers the same in older
              // engines. Same idiom as the Traces tab's Langfuse deep links.
              rel="noopener noreferrer"
              className="text-sm text-blue-700 hover:underline"
            >
              {pointers.workspace.auditLabel} ↗
            </a>
            <span className="block text-[11px] font-mono text-slate-400 mt-0.5 break-all">
              {pointers.workspace.workspaceUrl}
            </span>
          </div>
          {/* Stated, not linked: the inference tables sit in the customer's Unity Catalog under
              names AGP does not know, so naming the FEATURE is the honest precision available. A
              guessed table path would be a fabricated one. */}
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
  publishPending,
  onTogglePublish,
}: {
  agent: Agent;
  // Resolved display name for agent.tenant_id (null when unresolvable).
  tenantName: string | null;
  // The agent tenant's Databricks workspace origin, or null when it cannot be attributed to ONE
  // workspace. Resolved by the parent (which owns the tenant directory) and decided by
  // `tenantWorkspaceUrl` — this component neither picks a stage nor validates a URL.
  workspaceUrl: string | null;
  roleLevel: number;
  publishPending: boolean;
  onTogglePublish: (next: boolean) => void;
}) {
  const auth = AUTH_TYPE_META[agent.auth_type] ?? AUTH_TYPE_META.none;
  const tenant = tenantBadge(tenantName ?? agent.tenant_id);
  const publish = derivePublishState(agent.published, roleLevel);
  // `null` ⇒ this agent has no runtime handle of any kind AND no platform that implies one, so
  // the Invocation card renders no handle row at all (see the JSX below).
  const handle = runtimeHandleField(agent);
  // `null` ⇒ render no Observability card (every non-Databricks platform — see the card's note).
  const observability = agentObservability(agent, workspaceUrl);

  return (
    <div className="space-y-5">
      {/* Purpose */}
      <div className={`${CARD} p-5`}>
        <h2 className="text-sm font-semibold text-slate-800 mb-1">Purpose</h2>
        <p className="text-sm text-slate-700 whitespace-pre-wrap">
          {agent.purpose ? agent.purpose : <span className="text-slate-400">No purpose described.</span>}
        </p>
      </div>

      {/* Invocation — "how to invoke this agent" (Decision 1). */}
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
          {/* The runtime handle, LABELLED BY PLATFORM — `App URL` for a Databricks app,
              `Agent ARN` only for an agent that actually has one, and NO ROW AT ALL for a
              metadata-only record.

              This used to be an unconditional `Agent ARN` row, which meant every non-AgentCore
              agent — every Databricks app, every Azure agent, all ~18 metadata-only seed records —
              displayed "Agent ARN — Not set". That is not a missing value: the field does not
              apply. It read as a pending AWS deployment for an agent that will never have an ARN.
              `runtimeHandleField` owns the whole decision (see its contract for why a
              handle-less Databricks record still shows an empty App URL row while an ARN-less
              AgentCore record shows nothing). */}
          {handle && (
            <div className="lg:col-span-2">
              <InvocationRow label={handle.label} value={handle.value} copyLabel={handle.copyLabel} />
            </div>
          )}
        </dl>
      </div>

      {/* Test invoke (self-gates: renders only for provisioned Entra agents) */}
      <InvokePanel agent={agent} />

      {/* Observability — Databricks only, and it renders nothing it decided itself (E29/T11).
          Placed after Invoke because it answers "where did that call's trace go", which is the
          question invoking the agent raises. */}
      {observability && <ObservabilityCard pointers={observability} />}

      {/* Governance */}
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
          <Field label="Published">
            <span className="inline-flex items-center gap-2">
              <span
                className={`inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium ${publish.badge.cls}`}
              >
                {publish.badge.label}
              </span>
              {publish.canToggle && (
                <button
                  type="button"
                  onClick={() => onTogglePublish(publish.next)}
                  disabled={publishPending}
                  className="px-2 py-0.5 rounded-lg bg-white border border-slate-300 text-slate-700 text-xs font-medium hover:bg-slate-50 transition-colors disabled:opacity-50"
                >
                  {publishPending ? 'Working…' : publish.actionLabel}
                </button>
              )}
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
              <span className="text-amber-700">Identity provisioning pending</span>
            )}
          </Field>
        </dl>
      </div>

      {/* Marketplace publication (E33) — self-gates to Operator+ on an approved agent.
          Distinct from the E24 "Published" tenant flag in the Governance card above. */}
      <MarketplacePanel agent={agent} roleLevel={roleLevel} />

      {/* Record */}
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

// ---------------------------------------------------------------------------
// Marketplace publication panel (E33/T7)
//
// This is the MARKETPLACE listing flow: a publisher declares a datasheet, an admin
// approves it, and the agent becomes a marketplace product card. It is a DIFFERENT
// feature from the E24 tenant `published` flag rendered in the Governance card, and
// deliberately shares no copy with it ("Listed"/"Delisted" here vs "Published"/"Private"
// there; "Publish to marketplace" here vs "Publish"/"Unpublish" there).
//
// The inline datasheet form clones the header's openReason gating idiom (a `formOpen`
// selector + a CARD block with Confirm/Cancel and a pending-disabled row) rather than
// extracting it — the two flows have nothing else in common.
//
// The badge/CTA copy (MARKETPLACE_STATE_UI) lives in publishForm.ts because McpServerDetail
// renders the same five states over the same module (Amendment 1 / C11).
// ---------------------------------------------------------------------------

// One labeled single-line datasheet input (9 of the 10 draft fields; pitch is a textarea).
function DraftField({
  label,
  field,
  draft,
  onChange,
  placeholder,
  required,
}: {
  label: string;
  field: keyof DatasheetDraft;
  draft: DatasheetDraft;
  onChange: (field: keyof DatasheetDraft, value: string) => void;
  placeholder: string;
  required?: boolean;
}) {
  const id = `datasheet-${field}`;
  return (
    <div>
      <label htmlFor={id} className="text-xs font-medium text-slate-600">
        {label}{required ? ' *' : ''}
      </label>
      <input
        id={id}
        type="text"
        value={draft[field]}
        onChange={(e) => onChange(field, e.target.value)}
        placeholder={placeholder}
        className="mt-1 w-full px-3 py-2 text-sm rounded-lg border border-slate-300 focus:outline-none focus:ring-2 focus:ring-blue-500/40"
      />
    </div>
  );
}

function MarketplacePanel({ agent, roleLevel }: { agent: Agent; roleLevel: number }) {
  // Publishing is OPERATOR+ (mirrors POST /marketplace/publish-requests) and only legal
  // once the agent is approved (the backend answers 409 agent_not_approved otherwise).
  const eligible = roleLevel >= 1 && agent.lifecycle_state === 'approved';
  // The backend's C10 gate mirrored: a subscription is granted as an Entra app-role
  // assignment, so publish 409s until the identity is provisioned with an SP + Invoker
  // role. Stated instead of offering a CTA that can only fail (the AccessTab rule) — an
  // invoke-unavailable Databricks agent sits at identity_status='failed' permanently.
  const identityReady =
    agent.identity_status === 'provisioned' && !!agent.entra_sp_id && !!agent.invoker_role_id;

  const [request, setRequest] = useState<MarketplacePublishRequest | null>(null);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [reloadNonce, setReloadNonce] = useState(0);

  const [formOpen, setFormOpen] = useState(false);
  const [draft, setDraft] = useState<DatasheetDraft>(EMPTY_DATASHEET_DRAFT);
  const [formErrors, setFormErrors] = useState<string[]>([]);
  const [submitPending, setSubmitPending] = useState(false);
  const [submitError, setSubmitError] = useState<string | null>(null);

  useEffect(() => {
    if (!eligible) return;
    let cancelled = false;
    setLoading(true);
    setLoadError(null);
    marketplaceApi
      .publishRequestForProduct('agent', agent.id)
      .then((res) => {
        if (!cancelled) setRequest(res);
      })
      .catch((err: unknown) => {
        if (cancelled) return;
        const msg = err instanceof Error ? err.message : '';
        // The route 404s "never requested" and "not your tenant" with one byte-identical
        // literal; either way there is no request to show. Anything else is a real failure.
        if (isMissingPublishRequest(msg)) {
          setRequest(null);
        } else {
          setLoadError(msg || 'Failed to load the marketplace publication.');
        }
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [agent.id, eligible, reloadNonce]);

  // The backend's is_databricks_governed gate mirrored (runtime handle + Entra + platform):
  // publish is refused for these agents (databricks_publish_unsupported) because the
  // subscription grant path has no Databricks ACL mirror yet.
  const databricksGoverned =
    !!agent.runtime_handle && agent.auth_type === 'entra' && agent.platform === 'databricks';

  if (!eligible) return null;
  if (databricksGoverned) {
    return (
      <div className={`${CARD} p-5`}>
        <h2 className="text-sm font-semibold text-slate-800">Marketplace</h2>
        <p className="text-xs text-slate-500 mt-1.5">
          Databricks-governed agents cannot be published to the marketplace yet — a
          subscription would grant the Entra role without the workspace ACL, access the
          app itself would refuse.
        </p>
      </div>
    );
  }
  if (!identityReady) {
    return (
      <div className={`${CARD} p-5`}>
        <h2 className="text-sm font-semibold text-slate-800">Marketplace</h2>
        <p className="text-xs text-amber-700 mt-1.5">
          Publishing needs a provisioned Entra identity — subscriptions are granted as
          app-role assignments on it. This agent&apos;s identity is{' '}
          {agent.identity_status === 'failed' ? 'in a failed state' : 'not provisioned yet'}.
        </p>
      </div>
    );
  }

  const state = publishStateLabel(request, agent.marketplace);
  const ui = MARKETPLACE_STATE_UI[state];
  // An in-review / declined *update* on an already-published agent: the card consumers see is
  // still live, so the state copy must say so (pending/rejected outrank the block).
  const staysLive = listingStaysLive(state, agent.marketplace);

  const openForm = () => {
    // Seed from the datasheet already on record (a re-publish overwrites it wholesale).
    setDraft(draftFromDatasheet(request?.datasheet ?? agent.marketplace?.datasheet));
    setFormErrors([]);
    setSubmitError(null);
    setFormOpen(true);
  };

  const cancelForm = () => {
    setFormOpen(false);
    setDraft(EMPTY_DATASHEET_DRAFT);
    setFormErrors([]);
    setSubmitError(null);
  };

  const onField = (field: keyof DatasheetDraft, value: string) =>
    setDraft((d) => ({ ...d, [field]: value }));

  const submitDatasheet = async () => {
    const errors = validateDatasheet(draft);
    setFormErrors(errors);
    if (errors.length > 0) return;
    setSubmitPending(true);
    setSubmitError(null);
    try {
      await marketplaceApi.createPublishRequest({
        product_type: 'agent',
        product_id: agent.id,
        datasheet: buildDatasheet(draft),
      });
      setFormOpen(false);
      setDraft(EMPTY_DATASHEET_DRAFT);
      // Refetch the record so the state line reflects the new pending request.
      setReloadNonce((n) => n + 1);
    } catch (err: unknown) {
      // The 409s surface here verbatim via err.message: lifecycle not approved, "identity is
      // not provisioned" (C10 — publish needs an Entra identity), or a duplicate pending request.
      setSubmitError(err instanceof Error ? err.message : 'Publish request failed.');
    } finally {
      setSubmitPending(false);
    }
  };

  return (
    <div className={`${CARD} p-5`}>
      <div className="flex items-start justify-between gap-3 flex-wrap">
        <div className="min-w-0">
          <h2 className="text-sm font-semibold text-slate-800">Marketplace</h2>
          <p className="text-xs text-slate-500 mt-0.5">
            Offer this agent as a marketplace product. An admin reviews the declared datasheet
            before the card goes live.
          </p>
        </div>
        <span className={`inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium ${ui.cls}`}>
          {ui.label}
        </span>
      </div>

      <div className="mt-3 text-sm text-slate-700">
        {loading ? (
          <span className="text-slate-400">Loading marketplace publication…</span>
        ) : loadError ? (
          <span className="text-red-600" role="alert">{loadError}</span>
        ) : state === 'pending' ? (
          <span>
            {staysLive
              ? 'Datasheet update in review — awaiting admin review. The current listing remains live.'
              : 'Datasheet submitted — awaiting admin review.'}
          </span>
        ) : state === 'rejected' ? (
          <span>
            {staysLive
              ? 'An admin declined this datasheet update. The current listing remains live.'
              : 'An admin declined this datasheet.'}{' '}
            {request?.decision_reason ? (
              <span className="text-slate-600">Reason: {request.decision_reason}</span>
            ) : (
              <span className="text-slate-400">No reason was recorded.</span>
            )}
          </span>
        ) : state === 'published' ? (
          <span>Live in the marketplace since {formatDate(agent.marketplace?.declared_at)}.</span>
        ) : state === 'unpublished' ? (
          <span>An admin removed this card from the marketplace. The declared datasheet is kept.</span>
        ) : (
          <span className="text-slate-500">This agent has never been offered in the marketplace.</span>
        )}
      </div>

      {/* CTA — hidden while a request is pending (a second one is a 409 publish_conflict). */}
      {!loading && !loadError && ui.cta && !formOpen && (
        <button
          type="button"
          onClick={openForm}
          className="mt-3 px-3.5 py-1.5 rounded-lg bg-white border border-slate-300 text-slate-700 text-sm font-medium hover:bg-slate-50 transition-colors"
        >
          {ui.cta}
        </button>
      )}

      {/* Inline datasheet capture (the gated-form idiom from the lifecycle reason block). */}
      {formOpen && (
        <div className={`${CARD} mt-3 p-4`}>
          <p className="text-xs font-medium text-slate-600">
            Declared datasheet — publisher-asserted, shown on the marketplace card.
          </p>
          <div className="grid grid-cols-1 sm:grid-cols-2 gap-x-6 gap-y-3 mt-3">
            <DraftField label="Owner team" field="owner_team" draft={draft} onChange={onField} placeholder="Claims Platform" required />
            <DraftField label="Support contact" field="support_contact" draft={draft} onChange={onField} placeholder="claims-support@example.com" required />
            <DraftField label="Data classification" field="data_classification" draft={draft} onChange={onField} placeholder="internal" required />
            <DraftField label="SLA tier" field="sla_tier" draft={draft} onChange={onField} placeholder="gold" />
            <DraftField label="Support hours" field="support_hours" draft={draft} onChange={onField} placeholder="09:00–17:00 CET" />
            <DraftField label="Version" field="version" draft={draft} onChange={onField} placeholder="1.2.0" />
            <DraftField label="Region" field="region" draft={draft} onChange={onField} placeholder="eu-central-1" />
            <DraftField label="Compliance (comma-separated)" field="compliance" draft={draft} onChange={onField} placeholder="GDPR, SOC2" />
            <DraftField label="Guardrails (comma-separated)" field="guardrails" draft={draft} onChange={onField} placeholder="pii-redaction, no-pricing" />
          </div>

          <div className="mt-3">
            <label htmlFor="datasheet-pitch" className="text-xs font-medium text-slate-600">Pitch</label>
            <textarea
              id="datasheet-pitch"
              value={draft.pitch}
              onChange={(e) => onField('pitch', e.target.value)}
              rows={2}
              className="mt-1 w-full px-3 py-2 text-sm rounded-lg border border-slate-300 focus:outline-none focus:ring-2 focus:ring-blue-500/40"
              placeholder="What consumers get from this agent…"
            />
          </div>

          {formErrors.length > 0 && (
            <ul className="mt-3 text-sm text-red-600 list-disc list-inside" role="alert">
              {formErrors.map((msg) => <li key={msg}>{msg}</li>)}
            </ul>
          )}

          <div className="flex items-center gap-2 mt-3">
            <button
              type="button"
              onClick={submitDatasheet}
              disabled={submitPending}
              className="px-3.5 py-1.5 rounded-lg bg-blue-600 text-white text-sm font-medium hover:bg-blue-700 transition-colors disabled:opacity-50"
            >
              {submitPending ? 'Working…' : 'Send for admin review'}
            </button>
            <button
              type="button"
              onClick={cancelForm}
              disabled={submitPending}
              className="px-3.5 py-1.5 rounded-lg bg-white border border-slate-300 text-slate-700 text-sm font-medium hover:bg-slate-50 transition-colors disabled:opacity-50"
            >
              Cancel
            </button>
          </div>

          {submitError && <p className="text-sm text-red-600 mt-3" role="alert">{submitError}</p>}
        </div>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Deployment tab (rendered only when origin === 'Deployed' — gated by visibleTabs)
// ---------------------------------------------------------------------------

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
