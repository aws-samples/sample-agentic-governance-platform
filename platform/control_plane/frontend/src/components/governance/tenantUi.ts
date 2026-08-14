// Pure tenant-surfacing helpers (Epic 24 / T12). Framework-free so vitest
// (src/**/*.test.ts only) can unit-test them; the registry/marketplace/ops
// components import from here. The visual pill (tenantBadge) lives in
// agentUi.tsx next to the other badge helpers.
import type { UserTenant } from '../../api/client';

// ---------------------------------------------------------------------------
// tenantSelectOptions — the option list for a required tenant <select>.
// Non-admin callers are LOCKED to their memberships (spec §6); an admin picks
// from the full directory (tenantsAdminApi.list()) once it has loaded, and
// degrades to memberships while it loads / when the fetch failed (null).
// Sorted by name; non-mutating.
// ---------------------------------------------------------------------------
export function tenantSelectOptions(
  memberships: UserTenant[],
  directory: UserTenant[] | null,
  isAdmin: boolean,
): UserTenant[] {
  const source = isAdmin && directory ? directory : memberships;
  return [...source].sort((a, b) => a.name.localeCompare(b.name));
}

// ---------------------------------------------------------------------------
// resolveTenantName — tenant_id → display name from the first directory that
// knows the id (memberships first, then e.g. the admin list). null when the
// id is missing or unresolvable — callers fall back to the raw id / a dash.
// ---------------------------------------------------------------------------
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

// ---------------------------------------------------------------------------
// findTenantAccount — the full membership record (dev/prod accounts + region)
// for a tenant id; memberships first, then the admin directory. null when
// unknown (the ops account panel then renders nothing).
// ---------------------------------------------------------------------------
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

// ---------------------------------------------------------------------------
// derivePublishState — everything the detail-page Published control needs.
// `published` is tolerant of undefined (pre-E24 records hydrate as false).
// Toggling is OPERATOR+ (role_level >= 1), mirroring the backend publish
// routes' require_role(Role.OPERATOR).
// ---------------------------------------------------------------------------
export interface PublishState {
  published: boolean;
  badge: { cls: string; label: string };
  actionLabel: 'Publish' | 'Unpublish';
  /** The value to send: PUT …/publish { published: next }. */
  next: boolean;
  canToggle: boolean;
}

export function derivePublishState(
  published: boolean | null | undefined,
  roleLevel: number,
): PublishState {
  const isPublished = published === true;
  return {
    published: isPublished,
    badge: isPublished
      ? { cls: 'bg-sky-50 text-sky-700', label: 'Published' }
      : { cls: 'bg-slate-100 text-slate-500', label: 'Private' },
    actionLabel: isPublished ? 'Unpublish' : 'Publish',
    next: !isPublished,
    canToggle: roleLevel >= 1,
  };
}

// ---------------------------------------------------------------------------
// canEditShared — the MCP `shared` flag is ADMIN-only (role_level >= 2),
// mirroring the backend's _reject_shared_if_not_admin.
// ---------------------------------------------------------------------------
export function canEditShared(roleLevel: number): boolean {
  return roleLevel >= 2;
}

// sharedBadge — violet when platform-shared (visible to every tenant), neutral
// slate when tenant-scoped. Distinct tints so the state reads at a glance.
export function sharedBadge(shared: boolean): { cls: string; label: string } {
  return shared
    ? { cls: 'bg-violet-50 text-violet-700', label: 'Shared with all tenants' }
    : { cls: 'bg-slate-100 text-slate-500', label: 'Tenant-scoped' };
}

// ---------------------------------------------------------------------------
// tenantNames — the sidebar "Tenants: X, Y" line. Empty string when the
// caller has no memberships (the line is then hidden).
// ---------------------------------------------------------------------------
export function tenantNames(tenants: UserTenant[]): string {
  return tenants.map((t) => t.name).join(', ');
}
