// Shared agent-registry UI helpers (Epic 4b, Decision 9).
//
// One home for the lifecycle/origin badge maps (previously duplicated in
// AgentsList + AgentDetail), the owner email-alias helper, and the per-agent
// AgentAvatar. Consumers (AgentsList/AgentDetail/AgentsOverview) import from
// here so the visuals stay consistent — the class strings below are copied
// verbatim from the pre-refactor inline maps so swapping is visually neutral.

import type { ReactElement } from 'react';
import type { LifecycleState, Origin, McpServerKind } from '../../api/client';

// ---------------------------------------------------------------------------
// emailAlias — the part before "@" (Decision 5). "Unassigned" when falsy.
// ---------------------------------------------------------------------------
export function emailAlias(email?: string | null): string {
  if (!email) return 'Unassigned';
  const trimmed = email.trim();
  if (!trimmed) return 'Unassigned';
  const at = trimmed.indexOf('@');
  return at === -1 ? trimmed : trimmed.slice(0, at);
}

// ---------------------------------------------------------------------------
// lifecycleBadge — tailwind class + human label per lifecycle state.
// Classes copied verbatim from the inline LIFECYCLE_BADGE maps.
// ---------------------------------------------------------------------------
const LIFECYCLE_BADGE_CLS: Record<LifecycleState, string> = {
  approved: 'bg-emerald-50 text-emerald-700',
  proposed: 'bg-slate-100 text-slate-600',
  pending_approval: 'bg-amber-50 text-amber-700',
  rejected: 'bg-red-50 text-red-700',
  deprecated: 'bg-slate-100 text-slate-400',
};

const LIFECYCLE_LABEL: Record<LifecycleState, string> = {
  approved: 'Approved',
  proposed: 'Proposed',
  pending_approval: 'Pending approval',
  rejected: 'Rejected',
  deprecated: 'Deprecated',
};

export function lifecycleBadge(state: LifecycleState): { cls: string; label: string } {
  return { cls: LIFECYCLE_BADGE_CLS[state], label: LIFECYCLE_LABEL[state] };
}

// ---------------------------------------------------------------------------
// originBadge — Deployed=emerald, Registered=amber (copied verbatim).
// ---------------------------------------------------------------------------
export function originBadge(origin: Origin): { cls: string; label: string } {
  return origin === 'Deployed'
    ? { cls: 'bg-emerald-50 text-emerald-700', label: 'Deployed' }
    : { cls: 'bg-amber-50 text-amber-700', label: 'Registered' };
}

// ---------------------------------------------------------------------------
// kindBadge — Gateway=violet, Runtime=indigo, Standard=slate (MCP catalog,
// Epic 5/7). Distinct from the agent originBadge's emerald/amber so the two
// catalogs read differently at a glance.
// ---------------------------------------------------------------------------
export function kindBadge(kind: McpServerKind): { cls: string; label: string } {
  if (kind === 'gateway') return { cls: 'bg-violet-50 text-violet-700', label: 'Gateway' };
  if (kind === 'runtime') return { cls: 'bg-indigo-50 text-indigo-700', label: 'Runtime' };
  return { cls: 'bg-slate-100 text-slate-600', label: 'Standard' };
}

// ---------------------------------------------------------------------------
// tenantBadge — teal pill for the owning tenant (E24). Teal is unused by the
// lifecycle/origin/kind maps above so a tenant reads distinctly at a glance.
// A record without a tenant (pre-E24) gets a neutral em-dash pill label.
// ---------------------------------------------------------------------------
export function tenantBadge(name?: string | null): { cls: string; label: string } {
  const trimmed = (name ?? '').trim();
  if (!trimmed) return { cls: 'bg-slate-100 text-slate-400', label: '—' };
  return { cls: 'bg-teal-50 text-teal-700', label: trimmed };
}

// ---------------------------------------------------------------------------
// AgentAvatar — deterministic-color rounded square with the agent's initials.
// ---------------------------------------------------------------------------

// ~8 tasteful tailwind bg+text pairs (slate intentionally excluded so a real
// avatar reads differently from the neutral blank placeholder below).
const AVATAR_PALETTE: string[] = [
  'bg-blue-100 text-blue-700',
  'bg-emerald-100 text-emerald-700',
  'bg-violet-100 text-violet-700',
  'bg-amber-100 text-amber-700',
  'bg-rose-100 text-rose-700',
  'bg-cyan-100 text-cyan-700',
  'bg-indigo-100 text-indigo-700',
  'bg-teal-100 text-teal-700',
];

const AVATAR_SIZE: Record<'sm' | 'md' | 'lg', string> = {
  sm: 'h-7 w-7 text-xs',
  md: 'h-9 w-9 text-sm',
  lg: 'h-12 w-12 text-base',
};

// Initials: for a kebab/space/dot-segmented name take the first letter of the
// first 1-2 segments ("claims-triage-de" -> "CT"); else first 1-2 chars.
// Exported so the Sidebar footer can reuse the same derivation for the
// signed-in user's avatar without reinventing it.
export function initialsFor(name: string): string {
  const trimmed = name.trim();
  if (!trimmed) return '?';
  const segments = trimmed.split(/[\s._-]+/).filter(Boolean);
  if (segments.length >= 2) {
    return (segments[0][0] + segments[1][0]).toUpperCase();
  }
  const single = segments[0] ?? trimmed;
  return single.slice(0, 2).toUpperCase();
}

// Stable hash of the name (no Math.random) -> palette index.
function hashIndex(name: string, modulo: number): number {
  let hash = 0;
  for (let i = 0; i < name.length; i++) {
    hash = (hash * 31 + name.charCodeAt(i)) >>> 0;
  }
  return hash % modulo;
}

export function AgentAvatar({
  name,
  size = 'md',
}: {
  name: string;
  size?: 'sm' | 'md' | 'lg';
}): ReactElement {
  const base = `${AVATAR_SIZE[size]} rounded-lg font-semibold flex items-center justify-center shrink-0`;
  const trimmed = (name ?? '').trim();
  if (!trimmed) {
    return <div aria-hidden="true" className={`${base} bg-slate-100 text-slate-400`}>?</div>;
  }
  const color = AVATAR_PALETTE[hashIndex(trimmed, AVATAR_PALETTE.length)];
  return (
    <div aria-hidden="true" className={`${base} ${color}`}>
      {initialsFor(trimmed)}
    </div>
  );
}
