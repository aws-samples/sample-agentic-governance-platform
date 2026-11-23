import type { UserTenant } from '../../api/client';

export function tenantSelectOptions(
  memberships: UserTenant[],
  directory: UserTenant[] | null,
  isAdmin: boolean,
): UserTenant[] {
  const source = isAdmin && directory ? directory : memberships;
  return [...source].sort((a, b) => a.name.localeCompare(b.name));
}

export function resolveTenantName(
  id: string | null | undefined,
  ...directories: (UserTenant[] | null | undefined)[]
): string | null {
  if (!id) return null;
  for (const dir of directories) {
    const hit = dir?.find((t) => t.id === id);
    if (hit) return hit.name;
  }
  return null;
}

export function findTenantAccount(
  id: string | null | undefined,
  ...directories: (UserTenant[] | null | undefined)[]
): UserTenant | null {
  if (!id) return null;
  for (const dir of directories) {
    const hit = dir?.find((t) => t.id === id);
    if (hit) return hit;
  }
  return null;
}

export function canEditShared(roleLevel: number): boolean {
  return roleLevel >= 2;
}

export function sharedBadge(shared: boolean): { cls: string; label: string } {
  return shared
    ? { cls: 'bg-violet-50 text-violet-700', label: 'Shared with all tenants' }
    : { cls: 'bg-slate-100 text-slate-500', label: 'Tenant-scoped' };
}

export function tenantNames(tenants: UserTenant[]): string {
  return tenants.map((t) => t.name).join(', ');
}
