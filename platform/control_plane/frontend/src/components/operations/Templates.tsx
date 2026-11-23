import { useMemo, useRef, useState, type JSX } from 'react';
import { Link, useSearchParams } from 'react-router-dom';

import { connectionsApi, githubTemplatesApi, infraTemplatesApi } from '../../api/client';
import type { InfraTemplateView, TemplateView } from '../../api/client';
import { useLoad } from '../../api/useLoad';
import { useUser } from '../../contexts/useUser';
import OpsPage from './OpsPage.tsx';
import RolloutTemplatesModal from './RolloutTemplatesModal.tsx';
import TemplateCardGrid from './TemplateCardGrid.tsx';
import { formatTemplateDate } from './templateCardStyles.ts';
import { EditTemplateModal, UploadTemplateModal } from './TemplateModals.tsx';
import { OPS_CARD, OPS_PRIMARY_BTN, OPS_TABLE_DIVIDE, OPS_TABLE_HEAD } from './opsUi.ts';
import {
  EMPTY_CATALOG_READ_ONLY_BODY,
  EMPTY_STATE_COPY,
  SHOW_FILTER_STRIP,
  SHOW_TAB_BAR,
  TEMPLATES_SUBTITLE,
  TEMPLATES_TABS,
  canMutateTemplates,
  classifyTemplateError,
  deleteTemplateConfirm,
  orgLabel,
  orgOptions,
  pickSelectedOrg,
  templatesBodyState,
  templatesTabId,
  templatesTabPanelId,
} from './templatesView.ts';
import { nextTabKey } from './projectDetailTabs.ts';
import {
  INFRA_TAB_EMPTY,
  INFRA_TAB_SUBTITLE,
  BUILD_KIND_CHIP,
  PLATFORM_CHIP,
  buildKindLabel,
  infraBodyState,
} from './templateChain.ts';

const ICON_FUNNEL =
  'M12 3c2.755 0 5.455.232 8.083.678.533.09.917.556.917 1.096v1.044a2.25 2.25 0 01-.659 1.591l-5.432 5.432a2.25 2.25 0 00-.659 1.591v2.927a2.25 2.25 0 01-1.244 2.013L9.75 21v-6.568a2.25 2.25 0 00-.659-1.591L3.659 7.409A2.25 2.25 0 013 5.818V4.774c0-.54.384-1.006.917-1.096A48.32 48.32 0 0112 3z';
const ICON_PLUG =
  'M13.19 8.688a4.5 4.5 0 011.242 7.244l-4.5 4.5a4.5 4.5 0 01-6.364-6.364l1.757-1.757m13.35-.622l1.757-1.757a4.5 4.5 0 00-6.364-6.364l-4.5 4.5a4.5 4.5 0 001.242 7.244';
const ICON_UPLOAD =
  'M12 16.5V9.75m0 0l3 3m-3-3l-3 3M6.75 19.5a4.5 4.5 0 01-1.41-8.775 5.25 5.25 0 0110.233-2.33 3 3 0 013.758 3.848A3.752 3.752 0 0118 19.5H6.75z';

function StateCard(props: { iconPath: string; headline: string; body: string; children?: JSX.Element }) {
  const { iconPath, headline, body, children } = props;
  return (
    <div className={`${OPS_CARD} px-6 py-14 flex flex-col items-center text-center`}>
      <div className="w-11 h-11 rounded-xl bg-emerald-50 flex items-center justify-center text-emerald-600">
        <svg
          className="w-5 h-5"
          fill="none"
          viewBox="0 0 24 24"
          strokeWidth={1.6}
          stroke="currentColor"
          aria-hidden="true"
        >
          <path strokeLinecap="round" strokeLinejoin="round" d={iconPath} />
        </svg>
      </div>
      <h2 className="mt-4 text-base font-semibold text-slate-900">{headline}</h2>
      <p className="mt-1.5 text-sm text-slate-500 max-w-md">{body}</p>
      {children && <div className="mt-5">{children}</div>}
    </div>
  );
}

export default function Templates(): JSX.Element {
  const { user } = useUser();
  const canMutate = canMutateTemplates(user?.role_level ?? 0);

  const { state: connectionsState, reload: reloadConnections } = useLoad(
    () => connectionsApi.list(),
    [],
    'Failed to load org connections.',
  );
  const connections = useMemo(() => connectionsState.data ?? [], [connectionsState.data]);
  const connError = connectionsState.kind === 'error' ? connectionsState.message : null;
  const [pickedConnectionId, setPickedConnectionId] = useState<string>(
    () => new URLSearchParams(window.location.search).get('connection') ?? '',
  );
  const connectionId =
    connectionsState.data === null
      ? pickedConnectionId
      : pickSelectedOrg(connectionsState.data, pickedConnectionId);

  const { state: catalogState, reload: reloadCatalog } = useLoad<TemplateView[]>(
    () => (connectionId ? githubTemplatesApi.list(connectionId) : Promise.resolve([])),
    [connectionId],
    'Failed to load templates.',
  );
  const catalogLoading = catalogState.kind === 'loading';
  const templates = catalogState.kind === 'ready' ? catalogState.data : [];
  const error =
    catalogState.kind === 'error'
      ? classifyTemplateError(catalogState.failure ?? catalogState.message, 'Failed to load templates.')
      : null;

  const [actionPendingName, setActionPendingName] = useState<string | null>(null);
  const [cardError, setCardError] = useState<{ name: string; message: string } | null>(null);

  const { state: infraCatalogState, reload: reloadInfraCatalog } = useLoad<InfraTemplateView[]>(
    () => (connectionId ? infraTemplatesApi.list(connectionId) : Promise.resolve([])),
    [connectionId],
    'Failed to load infra templates.',
  );
  const infraLoading = infraCatalogState.kind === 'loading';
  const infraTemplates = infraCatalogState.kind === 'ready' ? infraCatalogState.data : [];
  const infraError =
    infraCatalogState.kind === 'error'
      ? classifyTemplateError(infraCatalogState.failure ?? infraCatalogState.message, 'Failed to load infra templates.')
      : null;

  const [activeTab, setActiveTab] = useState<string>(TEMPLATES_TABS[0].key);
  const tabRefs = useRef<Record<string, HTMLButtonElement | null>>({});

  const [showUpload, setShowUpload] = useState(false);
  const [editTarget, setEditTarget] = useState<TemplateView | null>(null);

  const [searchParams] = useSearchParams();
  const seedParam = searchParams.get('seed') === '1';
  const [surfaceOverride, setSurfaceOverride] = useState<string | null>(null);

  const refetch = () => {
    setCardError(null);
    reloadCatalog();
    reloadInfraCatalog();
  };

  const selectedConnection = useMemo(
    () => connections.find((c) => c.id === connectionId) ?? null,
    [connections, connectionId],
  );

  const reconcileTarget = useMemo(() => {
    const wanted = surfaceOverride ?? searchParams.get('connection') ?? '';
    if (!wanted) return null;
    return connections.find((c) => c.id === wanted) ?? null;
  }, [connections, searchParams, surfaceOverride]);
  const seedPrompt = seedParam && surfaceOverride === null;
  const options = useMemo(() => orgOptions(connections), [connections]);

  const handleDelete = (t: TemplateView) => {
    if (actionPendingName || !connectionId) return;
    if (!window.confirm(deleteTemplateConfirm(t.name))) return;
    setActionPendingName(t.name);
    setCardError(null);
    githubTemplatesApi
      .remove(connectionId, t.name)
      .then(() => refetch())
      .catch((err: unknown) => {
        setCardError({
          name: t.name,
          message: classifyTemplateError(err, 'The template could not be deregistered.').message,
        });
      })
      .finally(() => setActionPendingName(null));
  };

  const state = templatesBodyState({
    connectionsLoading: connectionsState.kind === 'loading',
    connectionsError: connError !== null,
    connectionCount: connections.length,
    connectionId,
    catalogLoading,
    catalogError: error?.message ?? null,
    templateCount: templates.length,
  });

  const hasConnection = connectionId.length > 0;
  const showFilterStrip = SHOW_FILTER_STRIP[state];

  const infraState = infraBodyState({
    loading: infraLoading,
    error: infraError?.message ?? null,
    count: infraTemplates.length,
  });

  const handleTabKeyDown = (e: React.KeyboardEvent<HTMLButtonElement>) => {
    const from = e.currentTarget.dataset.tabKey ?? '';
    const next = nextTabKey(
      TEMPLATES_TABS.map((t) => t.key),
      from,
      e.key,
    );
    if (!next) return;
    e.preventDefault();
    setActiveTab(next);
    tabRefs.current[next]?.focus();
  };

  return (
    <OpsPage
      backTo="/ops"
      title="Templates"
      subtitle={TEMPLATES_SUBTITLE}
      action={canMutate ? (
        <div className="flex items-center gap-2">
          <button
            type="button"
            onClick={() => {
              setSurfaceOverride(connectionId);
            }}
            disabled={!hasConnection}
            title="Compare this org against AGP’s registry"
            className="px-3.5 py-1.5 rounded-lg bg-white border border-slate-300 text-slate-700 text-sm font-medium hover:bg-slate-50 transition-colors disabled:opacity-40"
          >
            Roll out templates
          </button>
          <button
            type="button"
            onClick={() => setShowUpload(true)}
            disabled={!hasConnection}
            className={`${OPS_PRIMARY_BTN} disabled:opacity-40`}
          >
            Upload template
          </button>
        </div>
      ) : null}
    >
      {connError && state !== 'connections-unreadable' && (
        <div className="bg-white/80 backdrop-blur rounded-xl border border-rose-200/70 shadow-sm p-6 mb-4">
          <h2 className="text-sm font-semibold text-rose-700">Couldn’t load org connections</h2>
          <p className="text-sm text-slate-600 mt-1">{connError}</p>
        </div>
      )}

      {showFilterStrip && (
        <div
          className={`${OPS_CARD} px-4 py-3 mb-4 flex flex-wrap items-center justify-between gap-x-6 gap-y-3`}
        >
          <div className="flex items-center gap-2.5">
            <svg
              className="w-4 h-4 text-emerald-600 shrink-0"
              fill="none"
              viewBox="0 0 24 24"
              strokeWidth={1.6}
              stroke="currentColor"
              aria-hidden="true"
            >
              <path strokeLinecap="round" strokeLinejoin="round" d={ICON_FUNNEL} />
            </svg>
            <label
              htmlFor="templates-org"
              className="text-[11px] uppercase tracking-wide text-slate-400 font-medium"
            >
              Org
            </label>
            <select
              id="templates-org"
              value={connectionId}
              onChange={(e) => {
                setPickedConnectionId(e.target.value);
                setCardError(null);
              }}
              disabled={options.length === 0}
              className="px-3 py-1.5 text-sm rounded-lg border border-slate-300 bg-white text-slate-700 focus:outline-none focus:ring-2 focus:ring-emerald-500/40 disabled:opacity-40"
            >
              {options.length === 0 ? (
                <option value="">No orgs connected</option>
              ) : (
                options.map((o) => (
                  <option key={o.value} value={o.value}>
                    {o.label}
                  </option>
                ))
              )}
            </select>
          </div>

          <div className="flex items-center gap-3">
            <span className="text-xs text-slate-400 tabular-nums">
              {activeTab === 'infra'
                ? infraState === 'list' || infraState === 'empty'
                  ? `${infraTemplates.length} infra template${infraTemplates.length === 1 ? '' : 's'}`
                  : '—'
                : state === 'grid' || state === 'empty-catalog'
                  ? `${templates.length} template${templates.length === 1 ? '' : 's'}`
                  : '—'}
            </span>
            <button
              type="button"
              onClick={refetch}
              disabled={!hasConnection || catalogLoading || infraLoading}
              title="Refresh this org’s template catalogs"
              className="px-3 py-1.5 rounded-lg bg-white border border-slate-300 text-slate-700 text-sm font-medium hover:bg-slate-50 transition-colors disabled:opacity-40"
            >
              {catalogLoading ? 'Refreshing…' : 'Refresh'}
            </button>
          </div>
        </div>
      )}

      {SHOW_TAB_BAR[state] && (
        <div
          role="tablist"
          aria-label="Template catalogs"
          className="flex items-center gap-1 p-1 mb-4 bg-emerald-50/60 rounded-xl w-fit overflow-x-auto"
        >
          {TEMPLATES_TABS.map((tab) => {
            const selected = activeTab === tab.key;
            return (
              <button
                key={tab.key}
                ref={(el) => {
                  tabRefs.current[tab.key] = el;
                }}
                type="button"
                role="tab"
                id={templatesTabId(tab.key)}
                data-tab-key={tab.key}
                aria-selected={selected}
                aria-controls={templatesTabPanelId(tab.key)}
                tabIndex={selected ? 0 : -1}
                onClick={() => setActiveTab(tab.key)}
                onKeyDown={handleTabKeyDown}
                className={`px-3.5 py-1.5 rounded-lg text-sm font-medium transition-colors whitespace-nowrap ${selected ? 'bg-white text-slate-900 shadow-sm' : 'text-slate-500 hover:text-slate-700'}`}
              >
                {tab.label}
              </button>
            );
          })}
        </div>
      )}

      {state === 'loading-orgs' && (
        <div className={`${OPS_CARD} p-8 text-center`}>
          <p className="text-sm text-slate-400">Loading orgs…</p>
        </div>
      )}

      {state === 'connections-unreadable' && (
        <StateCard
          iconPath={ICON_PLUG}
          headline={EMPTY_STATE_COPY['connections-unreadable'].headline}
          body={EMPTY_STATE_COPY['connections-unreadable'].body}
        >
          <div className="flex flex-col items-center gap-3">
            {connError && <p className="text-xs text-slate-400 max-w-md">{connError}</p>}
            <button
              type="button"
              onClick={reloadConnections}
              className={OPS_PRIMARY_BTN}
            >
              {EMPTY_STATE_COPY['connections-unreadable'].cta.label}
            </button>
          </div>
        </StateCard>
      )}

      {state === 'no-connection' && (
        <StateCard
          iconPath={ICON_PLUG}
          headline={EMPTY_STATE_COPY['no-connection'].headline}
          body={EMPTY_STATE_COPY['no-connection'].body}
        >
          <Link to={EMPTY_STATE_COPY['no-connection'].cta.to} className={OPS_PRIMARY_BTN}>
            {EMPTY_STATE_COPY['no-connection'].cta.label} →
          </Link>
        </StateCard>
      )}

      {state === 'no-selection' && (
        <StateCard
          iconPath={ICON_FUNNEL}
          headline={EMPTY_STATE_COPY['no-selection'].headline}
          body={EMPTY_STATE_COPY['no-selection'].body}
        />
      )}

      {SHOW_TAB_BAR[state] && activeTab === 'agent' && (
        <div
          role="tabpanel"
          id={templatesTabPanelId('agent')}
          aria-labelledby={templatesTabId('agent')}
          tabIndex={0}
        >
      {state === 'loading' && (
        <div className={`${OPS_CARD} p-8 text-center`}>
          <p className="text-sm text-slate-400">Loading templates…</p>
        </div>
      )}

      {state === 'error' && error && (
        <div
          className={`bg-white/80 backdrop-blur rounded-xl border shadow-sm p-6 ${
            error.retryable ? 'border-amber-200/70' : 'border-rose-200/70'
          }`}
        >
          <h2
            className={`text-sm font-semibold ${
              error.retryable ? 'text-amber-800' : 'text-rose-700'
            }`}
          >
            {error.retryable ? 'Template catalog temporarily unavailable' : 'Couldn’t load templates'}
          </h2>
          <p className="text-sm text-slate-600 mt-1">{error.message}</p>
          {error.retryable && (
            <button
              type="button"
              onClick={refetch}
              className="mt-3 px-3 py-1.5 rounded-lg bg-white border border-slate-300 text-slate-700 text-xs font-medium hover:bg-slate-50 transition-colors"
            >
              Retry
            </button>
          )}
        </div>
      )}

      {state === 'empty-catalog' && (
        <StateCard
          iconPath={ICON_UPLOAD}
          headline={
            selectedConnection
              ? `No templates in ${selectedConnection.org} yet`
              : EMPTY_STATE_COPY['empty-catalog'].headline
          }
          body={canMutate ? EMPTY_STATE_COPY['empty-catalog'].body : EMPTY_CATALOG_READ_ONLY_BODY}
        >
          {canMutate ? (
            <div className="flex flex-wrap items-center justify-center gap-2">
              <button
                type="button"
                onClick={() => setSurfaceOverride(connectionId)}
                className={OPS_PRIMARY_BTN}
              >
                Seed or adopt templates
              </button>
              <button
                type="button"
                onClick={() => setShowUpload(true)}
                className="px-3.5 py-1.5 rounded-lg bg-white border border-slate-300 text-slate-700 text-sm font-medium hover:bg-slate-50 transition-colors"
              >
                {EMPTY_STATE_COPY['empty-catalog'].cta.label}
              </button>
            </div>
          ) : undefined}
        </StateCard>
      )}

      {state === 'grid' && (
        <TemplateCardGrid
          templates={templates}
          actionPendingName={actionPendingName}
          cardError={cardError}
          canMutate={canMutate}
          onEdit={setEditTarget}
          onDelete={handleDelete}
        />
      )}
        </div>
      )}

      {SHOW_TAB_BAR[state] && activeTab === 'infra' && (
        <div
          role="tabpanel"
          id={templatesTabPanelId('infra')}
          aria-labelledby={templatesTabId('infra')}
          tabIndex={0}
        >
          <p className="text-sm text-slate-500 mb-4 max-w-3xl">{INFRA_TAB_SUBTITLE}</p>

          {infraState === 'loading' && (
            <div className={`${OPS_CARD} p-8 text-center`}>
              <p className="text-sm text-slate-400">Loading infra templates…</p>
            </div>
          )}

          {infraState === 'error' && infraError && (
            <div
              className={`bg-white/80 backdrop-blur rounded-xl border shadow-sm p-6 ${
                infraError.retryable ? 'border-amber-200/70' : 'border-rose-200/70'
              }`}
            >
              <h2
                className={`text-sm font-semibold ${
                  infraError.retryable ? 'text-amber-800' : 'text-rose-700'
                }`}
              >
                {infraError.retryable
                  ? 'Infra catalog temporarily unavailable'
                  : 'Couldn’t load infra templates'}
              </h2>
              <p className="text-sm text-slate-600 mt-1">{infraError.message}</p>
              {infraError.retryable && (
                <button
                  type="button"
                  onClick={refetch}
                  className="mt-3 px-3 py-1.5 rounded-lg bg-white border border-slate-300 text-slate-700 text-xs font-medium hover:bg-slate-50 transition-colors"
                >
                  Retry
                </button>
              )}
            </div>
          )}

          {infraState === 'empty' && (
            <StateCard
              iconPath={ICON_PLUG}
              headline="No infra templates in this org"
              body={INFRA_TAB_EMPTY}
            />
          )}

          {infraState === 'list' && (
            <div className={`${OPS_CARD} overflow-hidden`}>
              <table className="w-full text-sm">
                <thead className={OPS_TABLE_HEAD}>
                  <tr>
                    <th className="text-left font-medium px-4 py-2.5">Name</th>
                    <th className="text-left font-medium px-4 py-2.5">Platform</th>
                    <th className="text-left font-medium px-4 py-2.5">IaC</th>
                    <th className="text-left font-medium px-4 py-2.5">Build</th>
                    <th className="text-left font-medium px-4 py-2.5">Repo</th>
                    <th className="text-left font-medium px-4 py-2.5">Version</th>
                    <th className="text-left font-medium px-4 py-2.5">Updated</th>
                  </tr>
                </thead>
                <tbody className={OPS_TABLE_DIVIDE}>
                  {infraTemplates.map((t) => (
                    <tr key={t.id}>
                      <td className="px-4 py-2.5">
                        <span className="text-slate-700 font-medium">{t.name}</span>
                        <span className="block text-[11px] text-slate-400 font-mono">{t.id}</span>
                        {t.description && (
                          <span className="block text-[11px] text-slate-500 mt-0.5">
                            {t.description}
                          </span>
                        )}
                      </td>
                      <td className="px-4 py-2.5">
                        <span className={`text-[10px] font-medium px-1.5 py-0.5 rounded ${PLATFORM_CHIP}`}>
                          {t.platform}
                        </span>
                      </td>
                      <td className="px-4 py-2.5 text-slate-500">{t.iac_type}</td>
                      <td className="px-4 py-2.5">
                        <span
                          className={`text-[10px] font-medium px-1.5 py-0.5 rounded ${BUILD_KIND_CHIP}`}
                        >
                          {buildKindLabel(t.build_kind)}
                        </span>
                      </td>
                      <td className="px-4 py-2.5 text-slate-500 font-mono text-xs">{t.repo_name}</td>
                      <td className="px-4 py-2.5 text-slate-500 tabular-nums">{t.version}</td>
                      <td className="px-4 py-2.5 text-slate-400 text-xs">
                        {formatTemplateDate(t.updated_at)}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>
      )}

      {showUpload && hasConnection && (
        <UploadTemplateModal
          orgLabel={selectedConnection ? orgLabel(selectedConnection) : 'this org'}
          onClose={() => setShowUpload(false)}
          onSubmit={async (file, meta) => {
            await githubTemplatesApi.upload(connectionId, file, meta);
            setShowUpload(false);
            refetch();
          }}
        />
      )}

      {reconcileTarget && (
        <RolloutTemplatesModal
          connection={reconcileTarget}
          seedPrompt={seedPrompt}
          onClose={() => {
            setSurfaceOverride('');
            refetch();
          }}
        />
      )}

      {editTarget && hasConnection && (
        <EditTemplateModal
          template={editTarget}
          onClose={() => setEditTarget(null)}
          onSubmit={async (patch) => {
            await githubTemplatesApi.patch(connectionId, editTarget.name, patch);
            setEditTarget(null);
            refetch();
          }}
        />
      )}
    </OpsPage>
  );
}
