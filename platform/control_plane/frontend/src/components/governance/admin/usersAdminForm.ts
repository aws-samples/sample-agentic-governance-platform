import type { PlatformUser, RoleChange, PrincipalHit } from '../../../api/client';

export const PLATFORM_ROLES = ['admin', 'operator', 'viewer'] as const satisfies readonly RoleChange['role'][];

export const ROLE_BADGE: Record<PlatformUser['role'], { cls: string; label: string }> = {
  admin: { cls: 'bg-violet-50 text-violet-700', label: 'Admin' },
  operator: { cls: 'bg-blue-50 text-blue-700', label: 'Operator' },
  viewer: { cls: 'bg-slate-100 text-slate-600', label: 'Viewer' },
  '': { cls: 'bg-slate-100 text-slate-600', label: '' },
};

export function roleLabel(role: PlatformUser['role']): string {
  return ROLE_BADGE[role].label;
}

export function filterNewPrincipals(hits: PrincipalHit[], existingIds: Set<string>): PrincipalHit[] {
  return hits.filter((h) => !existingIds.has(h.id));
}
