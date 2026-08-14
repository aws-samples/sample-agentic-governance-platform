// useTenantDirectory — ADMIN-only, fail-silent fetch of the full tenant
// directory (tenantsAdminApi.list(), Epic 24). Returns null while loading, on
// error, and for non-admins — consumers then degrade to the caller's own
// useUser().tenants memberships (see tenantUi.tenantSelectOptions). Mirrors
// the Sidebar's admin-only pending-approvals effect idiom: never blocks
// render, never throws, non-admins make no request.
import { useEffect, useState } from 'react';
import { tenantsAdminApi, type TenantInfo } from '../../api/client';

export function useTenantDirectory(isAdmin: boolean): TenantInfo[] | null {
  const [directory, setDirectory] = useState<TenantInfo[] | null>(null);

  useEffect(() => {
    if (!isAdmin) return; // non-admins make no request
    let cancelled = false;
    tenantsAdminApi
      .list()
      .then((rows) => {
        if (!cancelled) setDirectory(rows);
      })
      .catch(() => {
        /* fail-silent: consumers use the membership list */
      });
    return () => {
      cancelled = true;
    };
  }, [isAdmin]);

  // Derive rather than reset-in-effect: on an admin → non-admin flip (dev
  // user-switch) any previously fetched directory is masked immediately.
  return isAdmin ? directory : null;
}
