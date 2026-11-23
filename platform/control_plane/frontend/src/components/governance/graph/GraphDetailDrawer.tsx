import { useEffect, useRef } from 'react';
import { Link } from 'react-router-dom';
import type { FlowNode, FlowEdge } from './layout';
import {
  governanceGraphApi,
  cedarPoliciesApi,
} from '../../../api/client';
import type { Kind, LifecycleState, McpServer } from '../../../api/client';
import { useLoad } from '../../../api/useLoad';
import { AgentAvatar } from '../agentUi';
import { kindBadge, lifecycleBadge } from '../agentBadges';
import { CEDAR_CAVEAT } from './cedarCopy';
import { ENFORCEMENT_COPY } from '../cedarPosture';

export type GraphSelection =
  | { kind: 'node'; node: FlowNode; edge?: undefined; sourceLabel?: undefined; targetLabel?: undefined }
  | {
      kind: 'edge';
      edge: FlowEdge;
      node?: undefined;
      sourceLabel?: string;
      targetLabel?: string;
      targetMcpRefId?: string;
    };

const SECTION_TITLE =
  'text-[11px] uppercase tracking-wide text-slate-400 font-semibold mb-2';
const ROW = 'flex items-baseline justify-between gap-3 text-sm';
const ROW_KEY = 'text-slate-500 shrink-0';
const ROW_VAL = 'text-slate-800 font-medium text-right';
const CHIP =
  'inline-flex items-center px-2 py-0.5 rounded-md text-xs font-medium bg-slate-100 text-slate-600';

const LINK_CTA =
  'px-3.5 py-1.5 rounded-lg bg-blue-600 text-white text-sm font-medium hover:bg-blue-700 transition-colors';
const CLOSE_BTN =
  'px-3.5 py-1.5 rounded-lg bg-white border border-slate-300 text-slate-700 text-sm font-medium hover:bg-slate-50 transition-colors';

const MCP_KINDS: ReadonlySet<string> = new Set<Kind>(['gateway', 'runtime', 'standard']);

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

  let headerAvatarName = '';
  let headerTitle = '';
  let headerBadge: { cls: string; label: string } | null = null;

  if (selected.kind === 'node') {
    const { data } = selected.node;
    headerAvatarName = data.label;
    headerTitle = data.label;
    if (data.nodeType === 'mcp') {
      const kindRaw = meta(selected.node, 'kind');
      headerBadge = MCP_KINDS.has(kindRaw) ? kindBadge(kindRaw as Kind) : null;
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

        <div className="px-5 py-4 space-y-5 overflow-y-auto">
          {selected.kind === 'node' ? (
            <NodeBody node={selected.node} />
          ) : (
            <EdgeBody selection={selected} />
          )}
        </div>

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
    enforcementRaw in ENFORCEMENT_COPY
      ? ENFORCEMENT_COPY[enforcementRaw as McpServer['cedar_enforcement_mode']].heading
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

function PrincipalNodeBody({ node }: { node: FlowNode }) {
  const kind = node.data.nodeType === 'group' ? 'group' : 'user';
  const oid = node.data.refId;
  const { state } = useLoad(() => governanceGraphApi.principal(oid, kind), [oid, kind]);

  return (
    <section>
      <h3 className={SECTION_TITLE}>{kind === 'group' ? 'Group' : 'User'} · directory detail</h3>
      {state.kind === 'loading' && <p className="text-sm text-slate-400">Loading directory detail…</p>}
      {state.kind === 'error' && (
        <p className="text-sm text-rose-600">Could not load directory detail for this principal.</p>
      )}
      {state.kind === 'ready' && (
        <div className="space-y-1.5">
          <Row label="Display name" value={state.data.display_name} />
          <Row label="UPN" value={state.data.user_principal_name ?? ''} />
          <Row label="Mail" value={state.data.mail ?? ''} />
          <Row label="Job title" value={state.data.job_title ?? ''} />
          {state.data.group_names && state.data.group_names.length > 0 && (
            <div className="pt-1.5">
              <p className="text-xs text-slate-500 mb-1.5">Group memberships</p>
              <div className="flex flex-wrap gap-1.5">
                {state.data.group_names.map((g) => (
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

function CedarPanel({ mcpRefId }: { mcpRefId: string }) {
  const { state } = useLoad(() => cedarPoliciesApi.list(mcpRefId), [mcpRefId]);

  return (
    <section>
      <h3 className={SECTION_TITLE}>Cedar policy</h3>
      <p className="text-xs text-amber-800 bg-amber-50 border border-amber-200 rounded-lg px-3 py-2 leading-relaxed mb-3">
        {CEDAR_CAVEAT}
      </p>
      {state.kind === 'loading' && <p className="text-sm text-slate-400">Loading policies…</p>}
      {state.kind === 'error' && (
        <p className="text-sm text-rose-600">Could not load Cedar policies for this MCP gateway.</p>
      )}
      {state.kind === 'ready' && (
        <div className="space-y-2.5">
          <div className={ROW}>
            <span className={ROW_KEY}>Enforcement mode</span>
            <span className={ROW_VAL}>{state.data.enforcement_mode}</span>
          </div>
          {state.data.policies.length === 0 ? (
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
                  {state.data.policies.map((p) => (
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
