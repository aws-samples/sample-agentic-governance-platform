// Custom React Flow node components for the Governance Graph (Epic 11, Task 8).
//
// Four visually-DISTINCT node cards — User / Group / Agent / MCP — each a small
// rounded-2xl Tailwind card in the governance idiom (white bg, border, slate text,
// blue-600/semantic accents). Every type uses one consistent IconChip + glyph +
// rail accent (no per-agent avatar) so the type — not the instance — drives the
// color: User=blue, Group=cyan, Agent=teal, MCP=its kindBadge family (violet/
// indigo/slate). MCP keeps the shared `kindBadge` tint from agentUi so a node reads
// the same as its row elsewhere in governance.
//
// E11 polish: Agent vs MCP were the easiest pair to confuse (a STANDARD MCP and an
// Agent both used a slate rail). They're now pulled apart on TWO axes:
//   (1) color — Agent owns a distinct TEAL rail/chip family (unused by any MCP kind,
//       and not emerald — emerald carries the "approved/deployed" semantic), while
//       MCP keeps its kind palette (gateway=violet / runtime=indigo / standard=slate);
//   (2) words — both cards carry an explicit "Agent" / "MCP" type tag plus a compact
//       "where running" hosting tag, so the two read unambiguously even at a glance.
//
// Nodes are NOT user-connectable (this is a read-only governance view, not an
// editor) — handles exist only as edge anchors for the LR dagre layout, so they're
// rendered invisible and `isConnectable={false}`.
//
// `data.metadata` is `Record<string, unknown>` from the backend, so every field is
// read defensively (String(... ?? '')) — the node never assumes a key is present.

import { Handle, Position } from '@xyflow/react';
import type { NodeProps } from '@xyflow/react';
import type { FlowNode } from './layout';
import { kindBadge } from '../agentUi';
import { platformHostLabel, mcpHostLabel } from '../platformLabels';
import type { McpServerKind } from '../../../api/client';

// ---------------------------------------------------------------------------
// Shared card chrome. The base card is the governance rounded-2xl white card;
// each node overrides only its left accent bar + icon tint so the four types
// stay visually distinct while sharing one footprint (matching layout.ts's
// NODE_WIDTH/NODE_HEIGHT = 180×60 so dagre positions line up with the render).
// ---------------------------------------------------------------------------
const CARD =
  'group/node relative flex items-center gap-2.5 w-[180px] h-[60px] px-3 ' +
  'rounded-2xl border bg-white shadow-sm transition-shadow hover:shadow-md ' +
  'cursor-grab active:cursor-grabbing overflow-hidden';
const LABEL = 'text-sm font-semibold text-slate-900 leading-tight truncate';
const SUBLABEL = 'text-[11px] text-slate-500 leading-tight truncate';
// A thin colored rail down the left edge — the per-type accent.
const RAIL = 'absolute inset-y-0 left-0 w-1';
// Pill sub-badge (reuses the agentUi badge class strings verbatim by passing the
// `.cls` through; this only adds the shared pill geometry).
const PILL = 'inline-flex items-center px-1.5 py-0.5 rounded-full text-[10px] font-medium';
// Neutral "where running" tag — ONE style shared by Agent + MCP so the hosting
// hint reads the same on both cards and never fights the colored type/kind badges.
const HOST_TAG =
  'inline-flex items-center px-1.5 py-0.5 rounded-full text-[10px] font-medium ' +
  'bg-slate-100 text-slate-500';

// React Flow handles: invisible (read-only graph) but present so edges anchor on
// the left (target) / right (source) for the LR flow.
const HANDLE = '!w-1 !h-1 !min-w-0 !border-0 !bg-transparent';

function NodeHandles() {
  return (
    <>
      <Handle type="target" position={Position.Left} isConnectable={false} className={HANDLE} />
      <Handle type="source" position={Position.Right} isConnectable={false} className={HANDLE} />
    </>
  );
}

// ---------------------------------------------------------------------------
// Inline glyphs (no icon lib — matches the repo's inline-SVG idiom in agentsNav).
// ---------------------------------------------------------------------------
function PersonGlyph({ className }: { className?: string }) {
  return (
    <svg viewBox="0 0 20 20" fill="currentColor" aria-hidden="true" className={className}>
      <path d="M10 10a3.5 3.5 0 1 0 0-7 3.5 3.5 0 0 0 0 7Zm0 1.5c-3.1 0-6 1.6-6 3.8V17h12v-1.7c0-2.2-2.9-3.8-6-3.8Z" />
    </svg>
  );
}

function GroupGlyph({ className }: { className?: string }) {
  return (
    <svg viewBox="0 0 22 20" fill="currentColor" aria-hidden="true" className={className}>
      <path d="M7 9a3 3 0 1 0 0-6 3 3 0 0 0 0 6Zm8 0a3 3 0 1 0 0-6 3 3 0 0 0 0 6ZM7 10.5c-2.6 0-5 1.3-5 3.3V16h10v-2.2c0-2-2.4-3.3-5-3.3Zm8 0c-.5 0-1 .05-1.5.15 1 .8 1.5 1.9 1.5 3.15V16h5v-2.2c0-2-2.4-3.3-5-3.3Z" />
    </svg>
  );
}

function McpGlyph({ className }: { className?: string }) {
  // A small server/stack glyph for MCP servers.
  return (
    <svg viewBox="0 0 20 20" fill="currentColor" aria-hidden="true" className={className}>
      <path d="M4 3h12a1 1 0 0 1 1 1v3a1 1 0 0 1-1 1H4a1 1 0 0 1-1-1V4a1 1 0 0 1 1-1Zm0 9h12a1 1 0 0 1 1 1v3a1 1 0 0 1-1 1H4a1 1 0 0 1-1-1v-3a1 1 0 0 1 1-1Zm2-7.5a.75.75 0 1 0 0 1.5.75.75 0 0 0 0-1.5Zm0 9a.75.75 0 1 0 0 1.5.75.75 0 0 0 0-1.5Z" />
    </svg>
  );
}

function AgentGlyph({ className }: { className?: string }) {
  // A small "bot" head — a distinct silhouette from the person/group/server
  // glyphs so the Agent reads as a non-human, non-server actor at a glance.
  return (
    <svg viewBox="0 0 20 20" fill="currentColor" aria-hidden="true" className={className}>
      <path d="M9 1.5a1 1 0 0 1 2 0V3h1.5A3.5 3.5 0 0 1 16 6.5v6A3.5 3.5 0 0 1 12.5 16h-5A3.5 3.5 0 0 1 4 12.5v-6A3.5 3.5 0 0 1 7.5 3H9V1.5ZM7.5 7a1.25 1.25 0 1 0 0 2.5 1.25 1.25 0 0 0 0-2.5Zm5 0a1.25 1.25 0 1 0 0 2.5 1.25 1.25 0 0 0 0-2.5ZM7 12a.75.75 0 0 0 0 1.5h6a.75.75 0 0 0 0-1.5H7ZM2 8a1 1 0 0 1 1 1v2a1 1 0 1 1-2 0V9a1 1 0 0 1 1-1Zm16 0a1 1 0 0 1 1 1v2a1 1 0 1 1-2 0V9a1 1 0 0 1 1-1Z" />
    </svg>
  );
}

// Small circular icon chip behind a glyph — the per-type accent lives here.
function IconChip({ tint, children }: { tint: string; children: React.ReactNode }) {
  return (
    <span
      aria-hidden="true"
      className={`shrink-0 h-9 w-9 rounded-lg flex items-center justify-center ${tint}`}
    >
      {children}
    </span>
  );
}

// ---------------------------------------------------------------------------
// Type tag — the in-words "Agent" / "MCP" marker that rides on the sub-line so
// the two are never confused even before color registers. Tinted to its own
// family (teal=Agent, slate=MCP) so it reinforces the rail accent rather than
// adding a third color. Same compact pill geometry as the badges.
// ---------------------------------------------------------------------------
function TypeTag({ tint, children }: { tint: string; children: React.ReactNode }) {
  return <span className={`${PILL} ${tint}`}>{children}</span>;
}

// "Where running" hosting labels — a small neutral tag on each Agent/MCP card.
//
// Both helpers now live in the shared `platformLabels` module (E29/T9). This file used to keep its
// own platform map, one of five copies; it was the odd one out because a 180px node needs the
// SHORT vocabulary ('AWS') rather than the product name ('Amazon Bedrock AgentCore'). That
// difference is now a named field on one entry (`host` vs `full`) instead of a second map, so the
// divergence is legible and adding a platform cannot leave a node uncaptioned.
//
// `platformHostLabel` reads the platform, so an agent node is captioned by what its record
// actually says — a `databricks` node reads 'Databricks'. `mcpHostLabel` still DERIVES the host
// from `kind`, and that stays correct rather than being a leftover: `gateway` and `runtime` name
// AgentCore Gateway and AgentCore Runtime, which are AWS-only Bedrock constructs, so 'AWS' is a
// fact about the kind. Both return '' for anything unknown — a node with no honest tag shows none.

// ---------------------------------------------------------------------------
// UserNode — blue accent, person glyph. A human principal from Entra.
// ---------------------------------------------------------------------------
export function UserNode({ data }: NodeProps<FlowNode>) {
  return (
    <div className={`${CARD} border-slate-200`} title={data.label}>
      <span className={`${RAIL} bg-blue-500`} aria-hidden="true" />
      <IconChip tint="bg-blue-50 text-blue-600">
        <PersonGlyph className="h-5 w-5" />
      </IconChip>
      <div className="min-w-0 flex-1">
        <div className={LABEL}>{data.label}</div>
        <div className={SUBLABEL}>User</div>
      </div>
      <NodeHandles />
    </div>
  );
}

// ---------------------------------------------------------------------------
// GroupNode — cyan accent, multi-person glyph + a "Group" mini-label so it can
// never be mistaken for a single user.
// ---------------------------------------------------------------------------
export function GroupNode({ data }: NodeProps<FlowNode>) {
  return (
    <div className={`${CARD} border-cyan-200`} title={data.label}>
      <span className={`${RAIL} bg-cyan-500`} aria-hidden="true" />
      <IconChip tint="bg-cyan-50 text-cyan-600">
        <GroupGlyph className="h-5 w-5" />
      </IconChip>
      <div className="min-w-0 flex-1">
        <div className={LABEL}>{data.label}</div>
        <div className="mt-0.5">
          <span className={`${PILL} bg-cyan-50 text-cyan-700`}>Group</span>
        </div>
      </div>
      <NodeHandles />
    </div>
  );
}

// ---------------------------------------------------------------------------
// AgentNode — a teal IconChip + bot glyph (consistent per-type accent) + name +
// a sub-line that now leads with an explicit teal "Agent" type tag (so it can
// never read as an MCP) followed by the "where running" host tag derived from
// metadata.platform. The agent owns a TEAL rail/chip family — distinct from every
// MCP kind (incl. standard-slate) and from emerald (which means "approved"). The
// lifecycle detail still lives in the drawer; the card stays uncluttered.
// ---------------------------------------------------------------------------
const AGENT_RAIL = 'bg-teal-500';
const AGENT_TYPE_TAG = 'bg-teal-50 text-teal-700';

export function AgentNode({ data }: NodeProps<FlowNode>) {
  const host = platformHostLabel(String(data.metadata.platform ?? ''));

  return (
    <div className={`${CARD} border-teal-200`} title={data.label}>
      <span className={`${RAIL} ${AGENT_RAIL}`} aria-hidden="true" />
      <IconChip tint="bg-teal-50 text-teal-600">
        <AgentGlyph className="h-5 w-5" />
      </IconChip>
      <div className="min-w-0 flex-1">
        <div className={LABEL}>{data.label}</div>
        <div className="mt-0.5 flex items-center gap-1 min-w-0">
          <TypeTag tint={AGENT_TYPE_TAG}>Agent</TypeTag>
          {host && <span className={HOST_TAG}>{host}</span>}
        </div>
      </div>
      <NodeHandles />
    </div>
  );
}

// ---------------------------------------------------------------------------
// McpNode — name + the kindBadge (gateway=violet / runtime=indigo / standard=
// slate). A gateway MCP is the one that can carry Cedar enforcement, so marking
// the kind is what tells the eye "policy can live here".
// ---------------------------------------------------------------------------
const MCP_KINDS: ReadonlySet<string> = new Set<McpServerKind>(['gateway', 'runtime', 'standard']);

// Match the icon-chip tint to the kindBadge family so the MCP node's accent is
// consistent with its badge (gateway=violet, runtime=indigo, standard=slate).
function mcpChipTint(kind: string): string {
  if (kind === 'gateway') return 'bg-violet-50 text-violet-600';
  if (kind === 'runtime') return 'bg-indigo-50 text-indigo-600';
  return 'bg-slate-100 text-slate-500';
}
function mcpRailTint(kind: string): string {
  if (kind === 'gateway') return 'bg-violet-500';
  if (kind === 'runtime') return 'bg-indigo-500';
  return 'bg-slate-300';
}

export function McpNode({ data }: NodeProps<FlowNode>) {
  const kindRaw = String(data.metadata.kind ?? '');
  const badge = MCP_KINDS.has(kindRaw) ? kindBadge(kindRaw as McpServerKind) : null;
  const host = mcpHostLabel(kindRaw);

  // The type tag reads the explicit word "MCP" tinted to the kind family
  // (gateway=violet / runtime=indigo / standard=slate) — one pill carries BOTH
  // "this is an MCP" and "what kind", symmetric with the Agent's single tag. The
  // kind label (Gateway/Runtime/Standard) titles the tag for a hover read.
  const typeTint = badge ? badge.cls : 'bg-slate-100 text-slate-600';
  const kindTitle = badge ? `MCP server · ${badge.label}` : 'MCP server';

  return (
    <div className={`${CARD} border-slate-200`} title={data.label}>
      <span className={`${RAIL} ${mcpRailTint(kindRaw)}`} aria-hidden="true" />
      <IconChip tint={mcpChipTint(kindRaw)}>
        <McpGlyph className="h-5 w-5" />
      </IconChip>
      <div className="min-w-0 flex-1">
        <div className={LABEL}>{data.label}</div>
        <div className="mt-0.5 flex items-center gap-1 min-w-0">
          <span className={`${PILL} ${typeTint}`} title={kindTitle}>MCP</span>
          {host && <span className={HOST_TAG}>{host}</span>}
        </div>
      </div>
      <NodeHandles />
    </div>
  );
}

// React Flow node-type registry — keys MUST match the API node `type`
// ('user'|'group'|'agent'|'mcp'), which layout.toFlow() copies into Node.type.
export const nodeTypes = {
  user: UserNode,
  group: GroupNode,
  agent: AgentNode,
  mcp: McpNode,
};
