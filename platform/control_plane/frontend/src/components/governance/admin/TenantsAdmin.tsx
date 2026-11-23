import { useCallback, useMemo, useState } from 'react';
import { tenantsAdminApi } from '../../../api/client';
import type { TenantView } from '../../../api/client';
import { useLoad } from '../../../api/useLoad';
import { useUser } from '../../../contexts/useUser';
import { bindingModeBadge, readinessBadge } from '../platformLabels';
import {
  buildCreatePayload,
  buildUpdatePayload,
  capabilityBadges,
  compactBindingModeLabel,
  isDatabricksStage,
  platformLabel,
} from './tenantsAdminForm';
import { unionStageKeys } from './tenantsAdminForm';
import { probeNotice } from './tenantProbeNotice';
import TenantModal from './TenantModal';

function stageHeading(key: string): string {
  return key.length > 0 ? key.charAt(0).toUpperCase() + key.slice(1) : key;
}

const NO_TENANTS: TenantView[] = [];
const CARD = 'bg-white/70 backdrop-blur rounded-xl border border-slate-200/60 shadow-sm';
const PILL = 'inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium';

function StageCell({ tenant, stage }: { tenant: TenantView; stage: string }) {
  const config = tenant.stages?.[stage];
  if (!config) return <span className="text-slate-300">—</span>;
  if (isDatabricksStage(config)) {
    const host = config.workspace_url.replace(/^https:\/\//, '');
    return (
      <span className="block truncate max-w-[16rem]" title={config.workspace_url}>
        {host || <span className="text-slate-300">—</span>}
      </span>
    );
  }
  return <>{config.account_id || <span className="text-slate-300">—</span>}</>;
}

export default function TenantsAdmin() {
  const { user } = useUser();
  const isAdmin = (user?.role_level ?? 0) >= 2;

  const { state, reload: refetch } = useLoad(
    () => (isAdmin ? tenantsAdminApi.list() : Promise.resolve(NO_TENANTS)),
    [isAdmin],
    'Failed to load tenants.',
  );
  const tenants = state.data ?? NO_TENANTS;
  const loading = state.kind === 'loading';

  const [actionPendingId, setActionPendingId] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [probeNotices, setProbeNotices] = useState<Record<string, string>>({});

  const [modal, setModal] = useState<'new' | TenantView | null>(null);

  const runAction = useCallback(
    async (id: string, fn: () => Promise<unknown>) => {
      if (actionPendingId) return;
      setActionPendingId(id);
      setActionError(null);
      try {
        await fn();
        refetch();
      } catch (err: unknown) {
        setActionError(err instanceof Error ? err.message : 'Action failed.');
      } finally {
        setActionPendingId(null);
      }
    },
    [actionPendingId, refetch],
  );

  const handleDelete = useCallback(
    (t: TenantView) => {
      if (!window.confirm(`Delete tenant "${t.name}"? This cannot be undone.`)) {
        return;
      }
      void runAction(t.id, () => tenantsAdminApi.remove(t.id));
    },
    [runAction],
  );

  const handleReprobe = useCallback(
    async (t: TenantView) => {
      if (actionPendingId) return;
      setActionPendingId(t.id);
      setActionError(null);
      let notice: string;
      try {
        const fresh = await tenantsAdminApi.probeTenant(t.id);
        notice = probeNotice(new Date(), { ok: true, tenant: fresh });
        refetch();
      } catch (err: unknown) {
        notice = probeNotice(new Date(), {
          ok: false,
          message: err instanceof Error ? err.message : 'Probe failed.',
        });
      } finally {
        setActionPendingId(null);
      }
      setProbeNotices((prev) => ({ ...prev, [t.id]: notice }));
    },
    [actionPendingId, refetch],
  );

  const stageColumns = useMemo(() => unionStageKeys(tenants), [tenants]);
  const columnCount = 4 + stageColumns.length + 1;

  if (!isAdmin) return null;

  return (
    <div className="space-y-3">
      <div className="flex items-start justify-between gap-3">
        <p className="text-xs text-slate-500">
          Tenants are line-of-business units that own agents, MCP servers and projects. Each maps
          to one or more Entra groups and to the LoB’s dev/prod runtime — AWS accounts, or
          Databricks workspaces.
        </p>
        <button
          type="button"
          onClick={() => setModal('new')}
          className="shrink-0 px-3.5 py-1.5 rounded-lg bg-blue-600 text-white text-sm font-medium hover:bg-blue-700 transition-colors"
        >
          Create tenant
        </button>
      </div>

      {actionError && (
        <p className="text-sm text-red-600" role="alert">
          {actionError}
        </p>
      )}

      {state.kind === 'error' ? (
        <div className="bg-white/70 backdrop-blur rounded-xl border border-red-200/70 shadow-sm p-6">
          <h3 className="text-sm font-semibold text-red-700">Couldn’t load tenants</h3>
          <p className="text-sm text-slate-600 mt-1">{state.message}</p>
          <button
            onClick={refetch}
            className="mt-3 px-3 py-1.5 rounded-lg bg-white border border-slate-300 text-slate-700 text-xs font-medium hover:bg-slate-50 transition-colors"
          >
            Retry
          </button>
        </div>
      ) : (
        <div className={`${CARD} overflow-x-auto`}>
          <table className="w-full text-sm">
            <thead className="bg-slate-50/80 text-slate-500 text-xs uppercase tracking-wide">
              <tr>
                <th className="text-left font-medium px-4 py-2">Name</th>
                <th className="text-left font-medium px-4 py-2">Line of business</th>
                <th className="text-left font-medium px-4 py-2">Platform</th>
                <th className="text-left font-medium px-4 py-2">Groups</th>
                {stageColumns.map((key) => (
                  <th key={key} className="text-left font-medium px-4 py-2">
                    {stageHeading(key)}
                  </th>
                ))}
                <th className="text-right font-medium px-4 py-2">Actions</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-slate-100">
              {loading && (
                <tr>
                  <td colSpan={columnCount} className="px-4 py-8 text-center text-slate-400 text-sm">
                    Loading tenants…
                  </td>
                </tr>
              )}

              {!loading &&
                tenants.map((t) => {
                  const rowPending = actionPendingId === t.id;
                  const anyPending = actionPendingId !== null;
                  const platform = t.platform ?? 'aws';
                  const modeBadge = bindingModeBadge(t.binding_mode);
                  const modeLabel = compactBindingModeLabel(t.binding_mode);
                  const badges = capabilityBadges(platform, t.capabilities);
                  const awsDeployBadge =
                    platform === 'aws' && t.readiness?.deploy
                      ? readinessBadge('deploy', t.readiness.deploy)
                      : null;
                  return (
                    <tr key={t.id} className="hover:bg-blue-50/40 transition-colors">
                      <td className="px-4 py-2">
                        <span className="block text-slate-700 font-medium truncate" title={t.name}>
                          {t.name}
                        </span>
                        {t.description && (
                          <span className="block text-xs text-slate-400 truncate" title={t.description}>
                            {t.description}
                          </span>
                        )}
                        {probeNotices[t.id] && (
                          <span role="status" className="block text-[11px] text-slate-500 mt-0.5 max-w-[20rem] break-words">
                            {probeNotices[t.id]}
                          </span>
                        )}
                      </td>
                      <td className="px-4 py-2 text-slate-600">{t.line_of_business}</td>
                      <td className="px-4 py-2">
                        <div className="flex flex-wrap items-center gap-1.5">
                          <span className={`${PILL} bg-slate-100 text-slate-700`}>
                            {platformLabel(platform)}
                          </span>
                          {modeBadge && (
                            <span className={`${PILL} ${modeBadge.tint}`} title={modeBadge.hint}>
                              {modeLabel ?? modeBadge.label}
                              <span className="sr-only"> — {modeBadge.hint}</span>
                            </span>
                          )}
                          {awsDeployBadge && (
                            <span
                              className={`${PILL} ${awsDeployBadge.tint}`}
                              title={awsDeployBadge.hint}
                            >
                              {awsDeployBadge.label}
                              <span className="sr-only"> — {awsDeployBadge.hint}</span>
                            </span>
                          )}
                          {badges
                            .filter((b) => b.on !== undefined)
                            .map((b) => (
                              <span
                                key={b.key}
                                className={`${PILL} ${
                                  b.on
                                    ? 'bg-emerald-50 text-emerald-700'
                                    : 'bg-slate-100 text-slate-500'
                                }`}
                                title={`${b.label}: ${b.on ? 'available' : 'unavailable'}`}
                              >
                                {b.label}
                              </span>
                            ))}
                        </div>
                      </td>
                      <td className="px-4 py-2">
                        <span className={`${PILL} bg-violet-50 text-violet-700`}>
                          {t.entra_group_ids.length}{' '}
                          {t.entra_group_ids.length === 1 ? 'group' : 'groups'}
                        </span>
                      </td>
                      {stageColumns.map((stage) => (
                        <td key={stage} className="px-4 py-2 text-slate-600 font-mono text-xs">
                          <StageCell tenant={t} stage={stage} />
                        </td>
                      ))}
                      <td className="px-4 py-2">
                        <div className="flex items-center justify-end gap-1.5">
                          <button
                            type="button"
                            onClick={() => setModal(t)}
                            disabled={rowPending || anyPending}
                            className="whitespace-nowrap px-2.5 py-1 rounded-md bg-white border border-slate-300 text-slate-700 text-xs font-medium hover:bg-slate-50 transition-colors disabled:opacity-40"
                          >
                            Edit
                          </button>
                          <button
                            type="button"
                            onClick={() => void handleReprobe(t)}
                            disabled={rowPending || anyPending}
                            title="Re-run the connect-time probe and refresh this tenant’s readiness"
                            className="whitespace-nowrap px-2.5 py-1 rounded-md bg-white border border-slate-300 text-slate-700 text-xs font-medium hover:bg-slate-50 transition-colors disabled:opacity-40"
                          >
                            {rowPending ? '…' : 'Re-probe'}
                          </button>
                          <button
                            type="button"
                            onClick={() => handleDelete(t)}
                            disabled={rowPending || anyPending}
                            className="whitespace-nowrap px-2.5 py-1 rounded-md bg-white border border-rose-300 text-rose-600 text-xs font-medium hover:bg-rose-50 transition-colors disabled:opacity-40"
                          >
                            Delete
                          </button>
                        </div>
                      </td>
                    </tr>
                  );
                })}

              {!loading && tenants.length === 0 && (
                <tr>
                  <td colSpan={columnCount} className="px-4 py-8 text-center text-slate-400 text-sm">
                    No tenants yet.
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
      )}

      {modal !== null && (
        <TenantModal
          tenant={modal === 'new' ? null : modal}
          onClose={() => setModal(null)}
          onSubmit={async (draft) => {
            if (modal === 'new') {
              await tenantsAdminApi.create(buildCreatePayload(draft));
            } else {
              await tenantsAdminApi.update(modal.id, buildUpdatePayload(draft));
            }
            setActionError(null);
            setModal(null);
            refetch();
          }}
        />
      )}
    </div>
  );
}
