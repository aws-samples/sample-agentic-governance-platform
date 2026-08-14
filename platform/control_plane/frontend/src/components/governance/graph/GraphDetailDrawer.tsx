// GraphDetailDrawer — the node/edge detail panel for the Governance Graph
// (Epic 11, Task 8). A right-anchored slide-in panel (fixed inset-y-0 right-0,
// left border + shadow) — NOT a full-canvas modal. There is no backdrop blur so the
// graph and its neighborhood-highlight dimming stay fully visible behind the drawer.
// Close via Escape or the close-X button. Pinned avatar+title+close-X header,
// scrollable body, footer CTA. The section-title + ROW/ROW_KEY/ROW_VAL class
// strings are copied verbatim from AgentDetailDrawer so this drawer reads
// identically to the rest of governance.
//
// READ-ONLY (research §5): it READS via governanceGraphApi.principal (lazy Entra
// by-oid for a clicked user/group) and cedarPoliciesApi.list (the real (user,tool)
// permits behind a policy edge). It NEVER calls any mutating api.
//
// What it shows by selection:
//  - Agent node → metadata rows (origin, lifecycle, identity, platform) + a
//    "Open detail page →" Link to /agents/:refId.
//  - MCP node → kindBadge + metadata rows (kind, cedar enforcement, identity) +
//    a Link to /mcp-servers/:refId.
//  - User/Group node → lazy Entra detail (display name, UPN, mail, job title, and
//    a user's group_names as chips). No detail-page link (no registry page exists).
//  - Edge → source→target + role; for a can_call edge with hasPolicy (target is a
//    gateway MCP) it lazily lists the Cedar permits with the enforcement caveat.

import { useEffect, useRef, useState } from 'react';
import { Link } from 'react-router-dom';
import type { FlowNode, FlowEdge } from './layout';
import {
  governanceGraphApi,
  cedarPoliciesApi,
} from '../../../api/client';
import type { PrincipalDetail, CedarPolicySet, McpServerKind, LifecycleState, CedarEnforcementMode } from '../../../api/client';
import { AgentAvatar, kindBadge, lifecycleBadge } from '../agentUi';

// --- Selection shape (the page passes either a clicked node or a clicked edge) --
export type GraphSelection =
  | { kind: 'node'; node: FlowNode; edge?: undefined; sourceLabel?: undefined; targetLabel?: undefined }
  | {
      kind: 'edge';
      edge: FlowEdge;
      node?: undefined;
      // Resolved display labels for the edge endpoints (the page has them from the
      // node list; the edge only carries source/target node ids).
      sourceLabel?: string;
      targetLabel?: string;
      // The MCP's bare ref_id, when the edge targets an MCP — used for the lazy
      // cedar list. (Edge target node id is "mcp:<refId>"; the page passes refId.)
      targetMcpRefId?: string;
    };

// --- Class strings copied verbatim from AgentDetailDrawer (visual parity) -------
const SECTION_TITLE =
  'text-[11px] uppercase tracking-wide text-slate-400 font-semibold mb-2';
const ROW = 'flex items-baseline justify-between gap-3 text-sm';
const ROW_KEY = 'text-slate-500 shrink-0';
const ROW_VAL = 'text-slate-800 font-medium text-right';
const CHIP =
  'inline-flex items-center px-2 py-0.5 rounded-md text-xs font-medium bg-slate-100 text-slate-600';

// Cedar enforcement mode → human label (verbatim from CedarPoliciesTab.tsx ENFORCEMENT_COPY).
const ENFORCEMENT_LABEL: Record<CedarEnforcementMode, string> = {
  none: 'No policy engine attached',
  log_only: 'Logging only',
  enforce: 'Enforcing — default-deny',
};
const LINK_CTA =
  'px-3.5 py-1.5 rounded-lg bg-blue-600 text-white text-sm font-medium hover:bg-blue-700 transition-colors';
const CLOSE_BTN =
  'px-3.5 py-1.5 rounded-lg bg-white border border-slate-300 text-slate-700 text-sm font-medium hover:bg-slate-50 transition-colors';

const MCP_KINDS: ReadonlySet<string> = new Set<McpServerKind>(['gateway', 'runtime', 'standard']);

// Defensive metadata read (backend metadata is Record<string, unknown>).
function meta(node: FlowNode, key: string): string {
  const v = node.data.metadata[key];
  return v == null ? '' : String(v);
}

export default function GraphDetailDrawer({
  selected,
  onClose,
}: {
  selected: GraphSelection | null;
  onClose: () => void;
}) {
  const closeRef = useRef<HTMLButtonElement>(null);
  useEffect(() => {
    closeRef.current?.focus();
  }, [selected]);

  // Escape closes (mirrors AgentDetailDrawer).
  useEffect(() => {
    const onKeyDown = (e: KeyboardEvent) => {
      if (e.key === 'Escape') {
        e.preventDefault();
        onClose();
      }
    };
    document.addEventListener('keydown', onKeyDown);
    return () => document.removeEventListener('keydown', onKeyDown);
  }, [onClose]);

  if (!selected) return null;

  // Header avatar + title vary by selection.
  let headerAvatarName = '';
  let headerTitle = '';
  let headerBadge: { cls: string; label: string } | null = null;

  if (selected.kind === 'node') {
    const { data } = selected.node;
    headerAvatarName = data.label;
    headerTitle = data.label;
    if (data.nodeType === 'mcp') {
      const kindRaw = meta(selected.node, 'kind');
      headerBadge = MCP_KINDS.has(kindRaw) ? kindBadge(kindRaw as McpServerKind) : null;
    }
  } else {
    headerTitle = 'Access relationship';
    headerAvatarName = selected.sourceLabel ?? 'Edge';
  }

  return (
    <div
      className="fixed inset-y-0 right-0 z-50 w-full max-w-md flex flex-col bg-white border-l border-slate-200 shadow-xl"
      role="dialog"
      aria-modal="true"
      aria-label={`Details for ${headerTitle}`}
    >
      <div className="flex flex-col h-full overflow-hidden">
        {/* Header — pinned. */}
        <div className="flex items-start justify-between gap-3 px-5 py-4 border-b border-slate-200/60">
          <div className="flex items-start gap-3 min-w-0">
            <AgentAvatar name={headerAvatarName} size="md" />
            <div className="min-w-0">
              <h2 className="text-sm font-semibold text-slate-900 leading-tight truncate" title={headerTitle}>
                {headerTitle}
              </h2>
              {headerBadge && (
                <div className="mt-1">
                  <span
                    className={`inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium ${headerBadge.cls}`}
                    title="MCP kind"
                  >
                    {headerBadge.label}
                  </span>
                </div>
              )}
              {selected.kind === 'node' && (
                <div className="mt-1 text-xs text-slate-500 capitalize">
                  {selected.node.data.nodeType}
                </div>
              )}
            </div>
          </div>
          <button
            ref={closeRef}
            type="button"
            onClick={onClose}
            aria-label="Close"
            className="shrink-0 inline-flex items-center justify-center h-7 w-7 rounded-md text-slate-400 hover:text-slate-700 hover:bg-slate-100 transition-colors"
          >
            <span aria-hidden="true" className="text-base leading-none">×</span>
          </button>
        </div>

        {/* Body — scrolls. */}
        <div className="px-5 py-4 space-y-5 overflow-y-auto">
          {selected.kind === 'node' ? (
            <NodeBody node={selected.node} />
          ) : (
            <EdgeBody selection={selected} />
          )}
        </div>

        {/* Footer — Close + (for agent/mcp nodes) an "Open detail page →" link. */}
        <div className="flex items-center justify-end gap-2 px-5 py-4 border-t border-slate-200/60">
          <button type="button" onClick={onClose} className={CLOSE_BTN}>
            Close
          </button>
          {selected.kind === 'node' && selected.node.data.nodeType === 'agent' && (
            <Link to={`/agents/${selected.node.data.refId}`} className={LINK_CTA} onClick={onClose}>
              Open detail page →
            </Link>
          )}
          {selected.kind === 'node' && selected.node.data.nodeType === 'mcp' && (
            <Link to={`/mcp-servers/${selected.node.data.refId}`} className={LINK_CTA} onClick={onClose}>
              Open detail page →
            </Link>
          )}
        </div>
      </div>
    </div>
  );
}

// --- Node body: agent/mcp = registry metadata rows; user/group = lazy Entra ----
function NodeBody({ node }: { node: FlowNode }) {
  const t = node.data.nodeType;
  if (t === 'agent') return <AgentNodeBody node={node} />;
  if (t === 'mcp') return <McpNodeBody node={node} />;
  return <PrincipalNodeBody node={node} />;
}

function Row({ label, value }: { label: string; value: string }) {
  if (!value) return null;
  return (
    <div className={ROW}>
      <span className={ROW_KEY}>{label}</span>
      <span className={ROW_VAL}>{value}</span>
    </div>
  );
}

const LIFECYCLE_STATES: ReadonlySet<string> = new Set<LifecycleState>([
  'approved', 'proposed', 'pending_approval', 'rejected', 'deprecated',
]);

function AgentNodeBody({ node }: { node: FlowNode }) {
  const lifecycleRaw = meta(node, 'lifecycle_state');
  const badge = LIFECYCLE_STATES.has(lifecycleRaw)
    ? lifecycleBadge(lifecycleRaw as LifecycleState)
    : null;

  return (
    <section>
      <h3 className={SECTION_TITLE}>Agent</h3>
      <div className="space-y-1.5">
        <Row label="Origin" value={meta(node, 'origin')} />
        {/* Lifecycle: pill badge (same tint as AgentDetail/AgentsList) instead of raw enum. */}
        {badge && (
          <div className={ROW}>
            <span className={ROW_KEY}>Lifecycle</span>
            <span
              className={`inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium ${badge.cls}`}
            >
              {badge.label}
            </span>
          </div>
        )}
        <Row label="Identity" value={meta(node, 'identity_status')} />
        <Row label="Platform" value={meta(node, 'platform')} />
      </div>
    </section>
  );
}

function McpNodeBody({ node }: { node: FlowNode }) {
  const enforcementRaw = meta(node, 'cedar_enforcement_mode');
  const enforcementLabel =
    enforcementRaw in ENFORCEMENT_LABEL
      ? ENFORCEMENT_LABEL[enforcementRaw as CedarEnforcementMode]
      : enforcementRaw;

  return (
    <section>
      <h3 className={SECTION_TITLE}>MCP server</h3>
      <div className="space-y-1.5">
        <Row label="Kind" value={meta(node, 'kind')} />
        <Row label="Cedar enforcement" value={enforcementLabel} />
        <Row label="Identity" value={meta(node, 'identity_status')} />
      </div>
    </section>
  );
}

// User/Group node → lazy Entra detail (governanceGraphApi.principal).
function PrincipalNodeBody({ node }: { node: FlowNode }) {
  const kind = node.data.nodeType === 'group' ? 'group' : 'user';
  const oid = node.data.refId;
  const [detail, setDetail] = useState<PrincipalDetail | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError(null);
    setDetail(null);
    governanceGraphApi
      .principal(oid, kind)
      .then((d) => {
        if (!cancelled) setDetail(d);
      })
      .catch(() => {
        if (!cancelled) setError('Could not load directory detail for this principal.');
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [oid, kind]);

  return (
    <section>
      <h3 className={SECTION_TITLE}>{kind === 'group' ? 'Group' : 'User'} · directory detail</h3>
      {loading && <p className="text-sm text-slate-400">Loading directory detail…</p>}
      {error && <p className="text-sm text-rose-600">{error}</p>}
      {detail && (
        <div className="space-y-1.5">
          <Row label="Display name" value={detail.display_name} />
          <Row label="UPN" value={detail.user_principal_name ?? ''} />
          <Row label="Mail" value={detail.mail ?? ''} />
          <Row label="Job title" value={detail.job_title ?? ''} />
          {detail.group_names && detail.group_names.length > 0 && (
            <div className="pt-1.5">
              <p className="text-xs text-slate-500 mb-1.5">Group memberships</p>
              <div className="flex flex-wrap gap-1.5">
                {detail.group_names.map((g) => (
                  <span key={g} className={CHIP}>{g}</span>
                ))}
              </div>
            </div>
          )}
        </div>
      )}
    </section>
  );
}

// --- Edge body: source→target + role; can_call+hasPolicy → lazy Cedar permits --
function EdgeBody({ selection }: { selection: Extract<GraphSelection, { kind: 'edge' }> }) {
  const { edge, sourceLabel, targetLabel, targetMcpRefId } = selection;
  const role = edge.data?.role ?? '';
  const edgeType = edge.data?.edgeType;
  const hasPolicy = edge.data?.hasPolicy ?? false;
  const showPolicy = edgeType === 'can_call' && hasPolicy && !!targetMcpRefId;

  return (
    <>
      <section>
        <h3 className={SECTION_TITLE}>
          {edgeType === 'can_call' ? 'Can call' : 'Has access'}
        </h3>
        <div className="space-y-1.5">
          <Row label="From" value={sourceLabel ?? ''} />
          <Row label="To" value={targetLabel ?? ''} />
          <Row label="Role" value={role} />
        </div>
      </section>
      {showPolicy && <CedarPanel mcpRefId={targetMcpRefId!} />}
    </>
  );
}

// The Cedar enforcement caveat — quoted in the brief; do NOT reword to imply a
// policy targets the specific agent (policies are (user, tool)-scoped).
const CEDAR_CAVEAT = 'Cedar enforcement is ON for this MCP gateway.';

function CedarPanel({ mcpRefId }: { mcpRefId: string }) {
  const [set, setSet] = useState<CedarPolicySet | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError(null);
    setSet(null);
    cedarPoliciesApi
      .list(mcpRefId)
      .then((s) => {
        if (!cancelled) setSet(s);
      })
      .catch(() => {
        if (!cancelled) setError('Could not load Cedar policies for this MCP gateway.');
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [mcpRefId]);

  return (
    <section>
      <h3 className={SECTION_TITLE}>Cedar policy</h3>
      {/* The honest, (user,tool)-scoped framing — never agent-scoped. */}
      <p className="text-xs text-amber-800 bg-amber-50 border border-amber-200 rounded-lg px-3 py-2 leading-relaxed mb-3">
        {CEDAR_CAVEAT}
      </p>
      {loading && <p className="text-sm text-slate-400">Loading policies…</p>}
      {error && <p className="text-sm text-rose-600">{error}</p>}
      {set && (
        <div className="space-y-2.5">
          <div className={ROW}>
            <span className={ROW_KEY}>Enforcement mode</span>
            <span className={ROW_VAL}>{set.enforcement_mode}</span>
          </div>
          {set.policies.length === 0 ? (
            <p className="text-sm text-slate-400">No (user, tool) permits configured.</p>
          ) : (
            <div className="overflow-hidden rounded-lg border border-slate-200">
              <table className="w-full text-left text-xs">
                <thead className="bg-slate-50 text-slate-500">
                  <tr>
                    <th className="px-2.5 py-1.5 font-medium">User</th>
                    <th className="px-2.5 py-1.5 font-medium">Tool</th>
                    <th className="px-2.5 py-1.5 font-medium">Effect</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-slate-100">
                  {set.policies.map((p) => (
                    <tr key={p.policy_id} className="text-slate-700">
                      <td className="px-2.5 py-1.5">{p.user_label ?? '—'}</td>
                      <td className="px-2.5 py-1.5">{p.tool ?? 'All tools'}</td>
                      <td className="px-2.5 py-1.5">
                        <span
                          className={`inline-flex items-center px-1.5 py-0.5 rounded-full text-[10px] font-medium ${
                            p.effect === 'deny'
                              ? 'bg-rose-50 text-rose-700'
                              : 'bg-emerald-50 text-emerald-700'
                          }`}
                        >
                          {p.effect}
                        </span>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>
      )}
    </section>
  );
}
