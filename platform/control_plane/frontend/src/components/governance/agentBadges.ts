import type { LifecycleState, Origin, Kind } from '../../api/client';

export function emailAlias(email?: string | null): string {
  if (!email) return 'Unassigned';
  const trimmed = email.trim();
  if (!trimmed) return 'Unassigned';
  const at = trimmed.indexOf('@');
  return at === -1 ? trimmed : trimmed.slice(0, at);
}

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

export function originBadge(origin: Origin): { cls: string; label: string } {
  return origin === 'Deployed'
    ? { cls: 'bg-emerald-50 text-emerald-700', label: 'Deployed' }
    : { cls: 'bg-amber-50 text-amber-700', label: 'Registered' };
}

export function kindBadge(kind: Kind): { cls: string; label: string } {
  if (kind === 'gateway') return { cls: 'bg-violet-50 text-violet-700', label: 'Gateway' };
  if (kind === 'runtime') return { cls: 'bg-indigo-50 text-indigo-700', label: 'Runtime' };
  return { cls: 'bg-slate-100 text-slate-600', label: 'Standard' };
}

export function tenantBadge(name?: string | null): { cls: string; label: string } {
  const trimmed = (name ?? '').trim();
  if (!trimmed) return { cls: 'bg-slate-100 text-slate-400', label: '—' };
  return { cls: 'bg-teal-50 text-teal-700', label: trimmed };
}
