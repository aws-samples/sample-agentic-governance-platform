import { tenantsAdminApi, type TenantView } from '../../api/client';
import { useLoad } from '../../api/useLoad';

export function useTenantDirectory(isAdmin: boolean): TenantView[] | null {
  const { state } = useLoad<TenantView[] | null>(
    () => (isAdmin ? tenantsAdminApi.list() : Promise.resolve(null)),
    [isAdmin],
  );
  return isAdmin ? state.data : null;
}
