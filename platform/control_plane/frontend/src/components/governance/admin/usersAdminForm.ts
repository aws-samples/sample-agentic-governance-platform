// Pure helpers for the Users admin tab (Epic 16). Kept framework-free so vitest
// (src/**/*.test.ts only) can unit-test them; UsersAdmin.tsx imports these.
import type { PlatformRole, PrincipalHit } from '../../../api/client';

export const PLATFORM_ROLES: PlatformRole[] = ['admin', 'operator', 'viewer'];

// Role badge tints — violet=admin (most privilege), blue=operator, slate=viewer.
// Local map mirroring the marketplace STATUS_BADGE idiom.
export const ROLE_BADGE: Record<PlatformRole, { cls: string; label: string }> = {
  admin: { cls: 'bg-violet-50 text-violet-700', label: 'Admin' },
  operator: { cls: 'bg-blue-50 text-blue-700', label: 'Operator' },
  viewer: { cls: 'bg-slate-100 text-slate-600', label: 'Viewer' },
};

export function isPlatformRole(v: string): v is PlatformRole {
  return v === 'admin' || v === 'operator' || v === 'viewer';
}

export function roleLabel(role: string): string {
  return isPlatformRole(role) ? ROLE_BADGE[role].label : role;
}

// Hide principals who already hold a platform role from the Add-user search results.
export function filterNewPrincipals(hits: PrincipalHit[], existingIds: Set<string>): PrincipalHit[] {
  return hits.filter((h) => !existingIds.has(h.id));
}
