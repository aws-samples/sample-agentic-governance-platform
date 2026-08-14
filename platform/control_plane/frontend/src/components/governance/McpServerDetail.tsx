import { useEffect, useState } from 'react';
import { useParams, Link } from 'react-router-dom';
import { mcpServersApi, marketplaceApi } from '../../api/client';
import type { McpServer, DataClassification, MarketplacePublishRequest } from '../../api/client';
import { useUser } from '../../contexts/UserContext';
import { visibleMcpTabs } from './mcpServerDetailTabs';
import { AgentAvatar, lifecycleBadge, emailAlias, kindBadge, tenantBadge } from './agentUi';
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
import { canEditShared, derivePublishState, resolveTenantName, sharedBadge } from './tenantUi';
import { useTenantDirectory } from './useTenantDirectory';
import McpConnectedAgentsTab from './McpConnectedAgentsTab';
import CedarPoliciesTab from './CedarPoliciesTab';

// ---------------------------------------------------------------------------
// Local label helpers + dense card chrome (mirrors AgentDetail).
// ---------------------------------------------------------------------------

function dataClassLabel(d?: DataClassification | null): string {
  return d ?? '—';
}

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

const CARD = 'bg-white/70 backdrop-blur rounded-xl border border-slate-200/60 shadow-sm';

// ---------------------------------------------------------------------------
// CopyButton — inline-SVG copy affordance (no icon library).
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

// A dense labeled definition row.
function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div>
      <dt className="text-[11px] uppercase tracking-wide text-slate-400 font-medium">{label}</dt>
      <dd className="mt-0.5 text-sm text-slate-700">{children}</dd>
    </div>
  );
}

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

export default function McpServerDetail() {
  const { id = '' } = useParams();
  const { user } = useUser();

  const [mcp, setMcp] = useState<McpServer | null>(null);
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
    mcpServersApi
      .get(id)
      .then((res) => {
        if (cancelled) return;
        setMcp(res);
      })
      .catch((err: unknown) => {
        if (cancelled) return;
        const msg = err instanceof Error ? err.message : 'Failed to load MCP server.';
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

  // Keep the active tab valid for the loaded server's visible tabs (drops
  // Policies for standard, so reset off it if the kind changes).
  const tabs = visibleMcpTabs(mcp?.kind ?? 'standard');
  useEffect(() => {
    if (!tabs.some((t) => t.id === active)) {
      setActive(tabs[0].id);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [mcp?.kind]);

  // -- actions --------------------------------------------------------------

  const refresh = () => setReloadNonce((n) => n + 1);

  const runSubmit = async () => {
    setActionPending(true);
    setActionError(null);
    try {
      await mcpServersApi.submit(id);
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
      await mcpServersApi.transition(id, reasonFor, trimmed);
      setReasonFor(null);
      setReason('');
      refresh();
    } catch (err: unknown) {
      // 409 IllegalTransitionError / 422 surfaces here via err.message.
      setActionError(err instanceof Error ? err.message : 'Action failed.');
    } finally {
      setActionPending(false);
    }
  };

  // Cross-tenant publish flip (E24/T5). The response carries the updated
  // McpServer, so lift it straight into state (no refetch).
  const handleTogglePublish = async (next: boolean) => {
    setActionPending(true);
    setActionError(null);
    try {
      setMcp(await mcpServersApi.publish(id, next));
    } catch (err: unknown) {
      setActionError(err instanceof Error ? err.message : 'Publish update failed.');
    } finally {
      setActionPending(false);
    }
  };

  // Platform-shared flip (E24) — ADMIN-only (the backend 403s anyone else, and
  // the control below only renders for role_level >= 2). Rides the plain update
  // path: PUT /mcp-servers/{id} { shared }.
  const handleToggleShared = async (next: boolean) => {
    setActionPending(true);
    setActionError(null);
    try {
      setMcp(await mcpServersApi.update(id, { shared: next }));
    } catch (err: unknown) {
      setActionError(err instanceof Error ? err.message : 'Shared update failed.');
    } finally {
      setActionPending(false);
    }
  };

  // -- render ---------------------------------------------------------------

  if (loading) {
    return (
      <div className="max-w-7xl mx-auto px-6 py-8">
        <p className="text-slate-400 text-sm">Loading MCP server…</p>
      </div>
    );
  }

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

  if (error) {
    return (
      <div className="max-w-7xl mx-auto px-6 py-8">
        <Link to="/tools-mcp" className="text-xs text-slate-400 hover:text-slate-600">← Tools &amp; MCP</Link>
        <div className={`${CARD} mt-4 border-red-200/70 p-6`}>
          <h3 className="text-sm font-semibold text-red-700">Couldn’t load MCP server</h3>
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

  const lifecycle = lifecycleBadge(mcp.lifecycle_state);
  const kind = kindBadge(mcp.kind);

  // Two-step lifecycle gating (identical to E4):
  //  proposed         → Submit for approval (Operator+); NEVER Approve here.
  //  pending_approval → Approve / Reject    (Admin).
  //  approved         → Deprecate           (Admin, legal terminal edge).
  //  rejected / deprecated → no actions (deprecated is terminal).
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

        {/* ----- Page header: avatar + name + badges + single action bar ----- */}
        {/* The lifecycle actions live HERE — rendered ONCE regardless of the    */}
        {/* active tab.                                                          */}
        <div className="mt-2 mb-5">
          <div className="flex items-start justify-between gap-4 flex-wrap">
            <div className="flex items-center gap-3 min-w-0">
              <AgentAvatar name={mcp.name} size="lg" />
              <div className="min-w-0">
                <h1 className="text-xl font-semibold text-slate-900 truncate">{mcp.name}</h1>
                <div className="flex flex-wrap items-center gap-1.5 mt-1">
                  <span className={`inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium ${lifecycle.cls}`}>
                    {lifecycle.label}
                  </span>
                  <span className={`inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium ${kind.cls}`}>
                    {kind.label}
                  </span>
                  <span className="text-xs text-slate-400">
                    {mcp.endpoint_url ? mcp.endpoint_url : 'No endpoint set'}
                  </span>
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

          {/* Action error (e.g. a 409 illegal-transition / 422) — page stays usable. */}
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
              {tab.label}{tab.gatewayOnly ? ' ★' : ''}
            </button>
          ))}
        </div>

        {/* ----- Tab body (no actions here — actions live in the header) ----- */}
        {activeTab.id === 'overview' ? (
          <OverviewTab
            mcp={mcp}
            tenantName={resolveTenantName(mcp.tenant_id, user?.tenants, tenantDirectory)}
            roleLevel={roleLevel}
            actionPending={actionPending}
            onTogglePublish={handleTogglePublish}
            onToggleShared={handleToggleShared}
          />
        ) : activeTab.id === 'tools' ? (
          <ToolsTab mcp={mcp} canManage={isOperatorOrHigher} onRefreshed={setMcp} />
        ) : activeTab.id === 'connected-agents' ? (
          <McpConnectedAgentsTab mcp={mcp} canManage={isOperatorOrHigher} />
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

// ---------------------------------------------------------------------------
// Overview tab
// ---------------------------------------------------------------------------

function OverviewTab({
  mcp,
  tenantName,
  roleLevel,
  actionPending,
  onTogglePublish,
  onToggleShared,
}: {
  mcp: McpServer;
  // Resolved display name for mcp.tenant_id (null when unresolvable).
  tenantName: string | null;
  roleLevel: number;
  actionPending: boolean;
  onTogglePublish: (next: boolean) => void;
  onToggleShared: (next: boolean) => void;
}) {
  const kind = kindBadge(mcp.kind);
  const tenant = tenantBadge(tenantName ?? mcp.tenant_id);
  const publish = derivePublishState(mcp.published, roleLevel);
  const isShared = mcp.shared === true;
  const shared = sharedBadge(isShared);

  return (
    <div className="space-y-5">
      {/* Description */}
      <div className={`${CARD} p-5`}>
        <h2 className="text-sm font-semibold text-slate-800 mb-1">Description</h2>
        <p className="text-sm text-slate-700 whitespace-pre-wrap">
          {mcp.description ? mcp.description : <span className="text-slate-400">No description.</span>}
        </p>
      </div>

      {/* Invocation — how callers reach this MCP server. */}
      <div className={`${CARD} p-5`}>
        <h2 className="text-sm font-semibold text-slate-800 mb-3">Invocation</h2>
        <dl className="grid grid-cols-1 lg:grid-cols-2 gap-x-8 gap-y-4">
          <div className="lg:col-span-2">
            <InvocationRow label="Endpoint URL" value={mcp.endpoint_url} copyLabel="endpoint URL" />
          </div>
          <Field label="Version">
            <span className="font-mono text-xs">{mcp.version}</span>
          </Field>
        </dl>
      </div>

      {/* Governance */}
      <div className={`${CARD} p-5`}>
        <h2 className="text-sm font-semibold text-slate-800 mb-3">Governance</h2>
        <dl className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-x-8 gap-y-4">
          <Field label="Kind">
            <span className={`inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium ${kind.cls}`}>
              {kind.label}
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
                  disabled={actionPending}
                  className="px-2 py-0.5 rounded-lg bg-white border border-slate-300 text-slate-700 text-xs font-medium hover:bg-slate-50 transition-colors disabled:opacity-50"
                >
                  {actionPending ? 'Working…' : publish.actionLabel}
                </button>
              )}
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

      {/* Marketplace publication (E33 Amendment 1) — self-gates to Operator+ on an approved
          server. Distinct from the E24 "Published"/"Shared" tenant flags in the Governance
          card above. */}
      <MarketplacePanel mcp={mcp} roleLevel={roleLevel} />

      {/* Record */}
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

// ---------------------------------------------------------------------------
// Marketplace publication panel (E33 Amendment 1 / C11)
//
// The MARKETPLACE listing flow, now identical for both product types: a publisher declares a
// datasheet, an admin approves it, and the server becomes a marketplace product card that
// agents can subscribe to. Publish is the ONLY door into the catalog — the old
// `kind == "gateway"` auto-listing is retired, so every kind goes through here.
//
// It is a DIFFERENT feature from the E24 tenant `published` and platform `shared` flags
// rendered in the Governance card above, and deliberately shares no copy with them
// ("Listed"/"Delisted" here vs "Published"/"Private" and "Shared"/"Unshared" there;
// "Publish to marketplace" here vs "Publish"/"Share" there).
//
// The panel is a clone of AgentDetail's (same states, same form, same gating) over the shared
// publishForm module, which holds every piece of logic and copy the two have in common. Only
// the product noun, the product_type on the wire, and the tenant-flag disambiguation differ.
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

function MarketplacePanel({ mcp, roleLevel }: { mcp: McpServer; roleLevel: number }) {
  // Publishing is OPERATOR+ (mirrors POST /marketplace/publish-requests) and only legal once
  // the server is approved (the backend answers 409 otherwise). The identity precondition
  // (C10) is NOT pre-checked here — the backend owns that truth and answers 409 "identity is
  // not provisioned", which surfaces verbatim in the form below.
  const eligible = roleLevel >= 1 && mcp.lifecycle_state === 'approved';

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
      .publishRequestForProduct('mcp', mcp.id)
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
  }, [mcp.id, eligible, reloadNonce]);

  if (!eligible) return null;

  const state = publishStateLabel(request, mcp.marketplace);
  const ui = MARKETPLACE_STATE_UI[state];
  // An in-review / declined *update* on an already-published server: the card consumers see is
  // still live, so the state copy must say so (pending/rejected outrank the block).
  const staysLive = listingStaysLive(state, mcp.marketplace);

  const openForm = () => {
    // Seed from the datasheet already on record (a re-publish overwrites it wholesale).
    setDraft(draftFromDatasheet(request?.datasheet ?? mcp.marketplace?.datasheet));
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
        product_type: 'mcp',
        product_id: mcp.id,
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
            Offer this MCP server as a marketplace product agents can subscribe to. An admin
            reviews the declared datasheet before the card goes live.
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
          <span>Live in the marketplace since {formatDate(mcp.marketplace?.declared_at)}.</span>
        ) : state === 'unpublished' ? (
          <span>An admin removed this card from the marketplace. The declared datasheet is kept.</span>
        ) : (
          <span className="text-slate-500">This MCP server has never been offered in the marketplace.</span>
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
              placeholder="What consumers get from this MCP server…"
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
// Tools tab — declared MCP tools (one card per tool, with its JSON Schema).
// ---------------------------------------------------------------------------

// CSS-only spinner (no icon library) — inline spinner sized + inverted for the
// blue button surface (white arc on a blue-200 ring). The canonical card spinner
// (slate ring / blue-600 arc) would be near-invisible on blue.
function Spinner() {
  return (
    <span
      aria-hidden="true"
      className="inline-block h-3.5 w-3.5 rounded-full border-2 border-blue-200 border-t-white animate-spin"
    />
  );
}

function ToolsTab({
  mcp,
  canManage,
  onRefreshed,
}: {
  mcp: McpServer;
  canManage: boolean;
  // Lifts the freshly-returned McpServer up to the parent's `mcp` state so the
  // whole page (Overview's Updated timestamp, the tool list here) stays
  // consistent without a refetch. The backend already persisted the read.
  onRefreshed: (next: McpServer) => void;
}) {
  // Refresh is gateway-only (the backend 409s a non-gateway) and a mutation, so
  // it's gated on gateway + Operator+. Runtime/standard servers get no button.
  const canRefresh = mcp.kind === 'gateway' && canManage;

  const [refreshing, setRefreshing] = useState(false);
  const [refreshError, setRefreshError] = useState<string | null>(null);
  // Subtle, tasteful post-refresh feedback ("Updated just now") — set on success
  // so the operator gets confirmation even when the tool set is unchanged.
  const [justUpdated, setJustUpdated] = useState(false);

  const runRefresh = async () => {
    setRefreshing(true);
    setRefreshError(null);
    setJustUpdated(false);
    try {
      // Synchronous: one POST, the response carries the fresh available_tools.
      const next = await mcpServersApi.refreshTools(mcp.id);
      onRefreshed(next);
      setJustUpdated(true);
    } catch (err: unknown) {
      // The api throws Error with .message = the backend `detail`. Non-blocking.
      setRefreshError(err instanceof Error ? err.message : 'Failed to refresh tools.');
    } finally {
      setRefreshing(false);
    }
  };

  // The refresh control + feedback — rendered above both the empty state and the
  // tool list so an empty catalog can be populated by clicking refresh.
  const RefreshBar = canRefresh ? (
    <div className="flex items-center justify-between gap-3 flex-wrap">
      <div className="flex items-center gap-2.5 min-w-0">
        <span className="text-[11px] uppercase tracking-wide text-slate-400 font-medium">
          {mcp.available_tools.length} {mcp.available_tools.length === 1 ? 'tool' : 'tools'}
        </span>
        {justUpdated && !refreshing && !refreshError && (
          <span className="text-xs text-emerald-600">Updated just now</span>
        )}
      </div>
      <button
        type="button"
        onClick={runRefresh}
        disabled={refreshing}
        className="shrink-0 inline-flex items-center gap-1.5 px-3.5 py-1.5 rounded-lg bg-blue-600 text-white text-sm font-medium hover:bg-blue-700 transition-colors disabled:opacity-50"
      >
        {refreshing ? (
          <>
            <Spinner />
            Refreshing…
          </>
        ) : (
          <>
            <span aria-hidden="true">↻</span>
            Refresh tools
          </>
        )}
      </button>
    </div>
  ) : null;

  // Inline, non-blocking error (e.g. a transient gateway-read failure).
  const RefreshError = refreshError ? (
    <p className="text-sm text-red-600" role="alert">{refreshError}</p>
  ) : null;

  if (mcp.available_tools.length === 0) {
    return (
      <div className="space-y-3">
        {RefreshBar}
        {RefreshError}
        <div className="bg-white/60 backdrop-blur rounded-xl border border-slate-200/60 p-8 text-center text-slate-400 text-sm">
          No tools declared.
          {canRefresh && (
            <span className="block mt-1 text-slate-500">
              Refresh tools to re-read this gateway's catalog.
            </span>
          )}
        </div>
      </div>
    );
  }

  return (
    <div className="space-y-3">
      {RefreshBar}
      {RefreshError}
      {mcp.available_tools.map((tool, i) => (
        <div key={`${tool.name}-${i}`} className={`${CARD} p-5`}>
          <div className="flex items-baseline justify-between gap-3 flex-wrap">
            <code className="text-sm font-mono font-semibold text-slate-800 break-all">{tool.name}</code>
          </div>
          {tool.description && (
            <p className="text-sm text-slate-600 mt-1 whitespace-pre-wrap">{tool.description}</p>
          )}
          <div className="mt-3">
            <dt className="text-[11px] uppercase tracking-wide text-slate-400 font-medium mb-1">Input schema</dt>
            <pre className="text-xs font-mono text-slate-700 bg-slate-50 border border-slate-200/60 rounded-lg p-3 overflow-x-auto">
              {JSON.stringify(tool.input_schema, null, 2)}
            </pre>
          </div>
        </div>
      ))}
    </div>
  );
}
