import { useCallback, useMemo, useRef, useState, type JSX } from 'react';
import { useNavigate, useParams } from 'react-router-dom';

import {
  connectionsApi,
  projectsApi,
  type Connection,
  type ProjectDetail as ProjectDetailData,
} from '../../api/client';
import { useLoad } from '../../api/useLoad';
import { useUser } from '../../contexts/useUser';
import ConfirmDialog from '../ConfirmDialog';
import { findTenantAccount } from '../governance/tenantUi';
import { useTenantDirectory } from '../governance/useTenantDirectory';
import OpsPage from './OpsPage';
import ProjectAccessTab from './ProjectAccessTab';
import ProjectRepositoriesTab from './ProjectRepositoriesTab';
import {
  PROJECT_DETAIL_TABS,
  nextTabKey,
  showPageSkeleton,
  tabId,
  tabPanelId,
} from './projectDetailTabs';
import { canDestroy, destructiveActionMessage, effectiveRole } from './projectRoles';
import { sortedStageNames } from './repositoryDetailTabs';
import { orgLabel } from './opsLabels';
import { OPS_CARD } from './opsUi';
import { tenantPlatformOf } from './templateChain';

const FIELD_LABEL = 'block text-[11px] uppercase tracking-wide text-slate-400 font-medium mb-1';

export default function ProjectDetail(): JSX.Element {
  const { id } = useParams<{ id: string }>();
  const navigate = useNavigate();

  const { user } = useUser();
  const tenantDirectory = useTenantDirectory((user?.role_level ?? 0) >= 2);

  const { state, reload } = useLoad(
    async (): Promise<{ detail: ProjectDetailData; connection: Connection | undefined }> => {
      if (!id) throw new Error('Project not found');
      const detail = await projectsApi.get(id);
      const connection = await connectionsApi.list().then(
        (rows) => rows.find((c) => c.id === detail.project.connection_id),
        () => undefined,
      );
      return { detail, connection };
    },
    [id],
    'Failed to load the project.',
  );
  const detail = state.data?.detail ?? null;
  const connection = state.data?.connection;
  const loading = state.kind === 'loading';
  const notFound = !id || (state.kind === 'error' && state.failure?.code === 'project.not_found');
  const error = state.kind === 'error' && !notFound ? state.message : null;

  const [activeKey, setActiveKey] = useState<string>(PROJECT_DETAIL_TABS[0].key);

  const tabRefs = useRef<Record<string, HTMLButtonElement | null>>({});
  const handleTabKeyDown = useCallback((e: React.KeyboardEvent<HTMLButtonElement>) => {
    const next = nextTabKey(
      PROJECT_DETAIL_TABS.map((t) => t.key),
      e.currentTarget.dataset.tabKey ?? '',
      e.key,
    );
    if (!next) return;
    e.preventDefault();
    setActiveKey(next);
    tabRefs.current[next]?.focus();
  }, []);

  const [confirmDeleteProject, setConfirmDeleteProject] = useState(false);
  const [deletingProject, setDeletingProject] = useState(false);
  const [projectDeleteError, setProjectDeleteError] = useState<string | null>(null);

  const handleDeleteProject = useCallback(async () => {
    if (!id || deletingProject) return;
    setDeletingProject(true);
    setProjectDeleteError(null);
    try {
      await projectsApi.remove(id);
      navigate('/ops/projects');
    } catch (err: unknown) {
      setProjectDeleteError(
        destructiveActionMessage(err, 'project', 'Failed to delete the project.')
      );
      setConfirmDeleteProject(false);
      setDeletingProject(false);
    }
  }, [id, deletingProject, navigate]);

  const project = detail?.project;
  const repositories = useMemo(() => detail?.repositories ?? [], [detail]);

  const heldRole = effectiveRole(detail);

  const ungoverned = detail?.ungoverned;

  const mayDestroy = canDestroy(heldRole, user?.role_level ?? 0);

  if (showPageSkeleton(loading, detail !== null)) {
    return (
      <OpsPage backTo="/ops/projects" title="Project">
        <div className={`${OPS_CARD} p-8 text-center text-slate-400 text-sm`}>Loading project…</div>
      </OpsPage>
    );
  }

  if (notFound) {
    return (
      <OpsPage backTo="/ops/projects" title="Project not found">
        <div className={`${OPS_CARD} p-8`}>
          <h3 className="text-sm font-semibold text-slate-800">Project not found</h3>
          <p className="text-sm text-slate-500 mt-1">
            This project doesn’t exist or is no longer available.
          </p>
        </div>
      </OpsPage>
    );
  }

  if (error || !project) {
    return (
      <OpsPage backTo="/ops/projects" title="Project">
        <div className="bg-white/80 backdrop-blur rounded-xl border border-rose-200/70 shadow-sm p-6">
          <h3 className="text-sm font-semibold text-rose-700">Couldn’t load project</h3>
          <p className="text-sm text-slate-600 mt-1">{error ?? 'Unknown error.'}</p>
          <button
            type="button"
            onClick={reload}
            className="mt-3 px-3 py-1.5 rounded-lg bg-white border border-slate-300 text-slate-700 text-xs font-medium hover:bg-slate-50 transition-colors"
          >
            Retry
          </button>
        </div>
      </OpsPage>
    );
  }

  const repoCount = repositories.length;

  const tenantAccount = findTenantAccount(project.tenant_id, user?.tenants, tenantDirectory);
  const tenantPlatform = tenantPlatformOf(tenantAccount);

  return (
    <OpsPage
      backTo="/ops/projects"
      title={project.name}
      subtitle={project.description || undefined}
      action={
        mayDestroy ? (
          <button
            type="button"
            onClick={() => setConfirmDeleteProject(true)}
            disabled={repoCount > 0}
            title={repoCount > 0 ? 'Remove its repositories first' : 'Delete this project'}
            className="px-3.5 py-1.5 rounded-lg bg-white border border-rose-300 text-rose-700 text-sm font-medium hover:bg-rose-50 transition-colors disabled:opacity-40 disabled:cursor-not-allowed"
          >
            Delete project
          </button>
        ) : undefined
      }
    >
      <div className={`${OPS_CARD} px-4 py-3 mb-6 flex flex-wrap items-center gap-x-8 gap-y-2`}>
        <div>
          <span className={FIELD_LABEL}>Org</span>
          <span className="text-sm text-slate-700">
            {orgLabel(connection, project.connection_id)}
          </span>
        </div>
        <div>
          <span className={FIELD_LABEL}>Agents &amp; MCP servers</span>
          <span className="text-sm text-slate-700 tabular-nums">{repoCount}</span>
        </div>
        {tenantAccount && (
          <>
            <div>
              <span className={FIELD_LABEL}>Tenant</span>
              <span className="text-sm text-slate-700">{tenantAccount.name}</span>
            </div>
            {sortedStageNames(tenantAccount.stages).map((stage) => {
              const config = tenantAccount.stages[stage];
              return (
                <div key={stage}>
                  <span className={FIELD_LABEL}>{stage} account</span>
                  <span className="text-sm text-slate-700 font-mono tabular-nums">
                    {config.account_id}
                  </span>
                  <span className="block text-[11px] text-slate-400">{config.region}</span>
                </div>
              );
            })}
          </>
        )}
      </div>

      {projectDeleteError && (
        <p className="text-sm text-red-600 mb-4" role="alert">
          {projectDeleteError}
        </p>
      )}

      <div
        role="tablist"
        aria-label="Project sections"
        className="flex items-center gap-1 p-1 mb-4 bg-emerald-50/60 rounded-xl w-fit overflow-x-auto"
      >
        {PROJECT_DETAIL_TABS.map((tab) => {
          const selected = activeKey === tab.key;
          return (
            <button
              key={tab.key}
              ref={(el) => {
                tabRefs.current[tab.key] = el;
              }}
              type="button"
              role="tab"
              id={tabId(tab.key)}
              data-tab-key={tab.key}
              aria-selected={selected}
              aria-controls={tabPanelId(tab.key)}
              tabIndex={selected ? 0 : -1}
              onClick={() => setActiveKey(tab.key)}
              onKeyDown={handleTabKeyDown}
              className={`px-3.5 py-1.5 rounded-lg text-sm font-medium transition-colors whitespace-nowrap ${selected ? 'bg-white text-slate-900 shadow-sm' : 'text-slate-500 hover:text-slate-700'}`}
            >
              {tab.label}
            </button>
          );
        })}
      </div>

      {activeKey === 'repositories' && (
        <div
          role="tabpanel"
          id={tabPanelId('repositories')}
          aria-labelledby={tabId('repositories')}
          tabIndex={0}
        >
          <ProjectRepositoriesTab
            projectId={project.id}
            connectionId={project.connection_id}
            tenantPlatform={tenantPlatform}
            repositories={repositories}
            heldRole={heldRole}
            ungoverned={ungoverned}
            onChanged={reload}
          />
        </div>
      )}
      {activeKey === 'access' && (
        <div role="tabpanel" id={tabPanelId('access')} aria-labelledby={tabId('access')} tabIndex={0}>
          <ProjectAccessTab
            projectId={project.id}
            heldRole={heldRole}
            ungoverned={ungoverned}
            onRolesChanged={reload}
          />
        </div>
      )}

      <ConfirmDialog
        open={confirmDeleteProject}
        title="Delete project"
        message="Delete this project? This cannot be undone."
        confirmText={deletingProject ? 'Deleting…' : 'Delete'}
        variant="danger"
        onConfirm={() => void handleDeleteProject()}
        onCancel={() => {
          if (!deletingProject) setConfirmDeleteProject(false);
        }}
      />
    </OpsPage>
  );
}
