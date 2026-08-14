// ProjectDetail — one project container, as a TABBED page (Epic 20 / Task 16;
// tab shell added E27/T11). useParams id → projectsApi.get(id) → header (name,
// org, repo count, tenant accounts) inside the OpsPage frame, then the active
// tab's body.
//
// This file is now the page frame + tab shell ONLY. The two tab bodies live in
// siblings, so adding the Access tab SHRANK this file instead of growing it:
//   • Repositories → ProjectRepositoriesTab.tsx — the repos table, the
//     add-from-template modal, the E25C live materialize timeline, delete-repo.
//   • Access       → ProjectAccessTab.tsx — the per-project role roster (E27)
//     with its groups-first principal picker.
// Project-level delete stays here: it acts on the project, not on either tab.
//
// The pill tab bar keeps the OperationsAdmin.tsx:54-69 VISUALS verbatim (identical
// class strings — active tint + shadow-sm pill, transition-colors only, no
// scale/translate motion) but carries real WAI-ARIA tab semantics: tablist/tab/
// aria-selected/aria-controls + tabpanel, and a roving tabindex with Left/Right
// arrow keys so the bar is ONE tab stop. `aria-current="page"` is deliberately gone —
// it announces a client-side tab as a page the URL contradicts (WCAG 4.1.2). The
// registry and the arrow-key arithmetic live in the pure `projectDetailTabs.ts` so
// they are testable (only `src/**/*.test.ts` is collected).
//
// House style: emerald-on-glass Ops tokens (opsUi.ts), the OpsPage frame,
// Tailwind v4 utility strings, 2-space indent.

import { useCallback, useEffect, useMemo, useRef, useState, type JSX } from 'react';
import { useNavigate, useParams } from 'react-router-dom';

import {
  connectionsApi,
  projectsApi,
  type Connection,
  type ProjectDetail as ProjectDetailData,
} from '../../api/client';
import { useUser } from '../../contexts/UserContext';
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
// The stage-name ordering, shared with the repository detail page's environment strip so the
// two surfaces cannot disagree about which stages a tenant has or what order they read in.
import { sortedStageNames } from './repositoryDetailTabs';
import { orgLabel } from './opsLabels';
import { OPS_CARD } from './opsUi';

// The E25C step-timeline helpers moved to ProjectRepositoriesTab with the timeline
// they drive. Re-exported here because ProjectDetail.stepTimeline.test.ts imports
// them from './ProjectDetail' — the extraction must not move a pinned test's path.
export { isMaterializeTerminal, nextBadgeFromSteps } from './ProjectRepositoriesTab';

const FIELD_LABEL = 'block text-[11px] uppercase tracking-wide text-slate-400 font-medium mb-1';

// E28/T10 removed the local `orgLabel` — the third copy of this one line. It rendered the
// RAW wire enum (`github · acme`) while `TemplatesAdmin` rendered the brand casing
// (`GitHub · acme`): the same fact spelled two ways on one surface, which an operator sees
// the moment they open a project from the list. It is now `opsLabels.orgLabel`, imported
// above and pinned by `opsLabels.test.ts` — the call site and its data shape are unchanged.

export default function ProjectDetail(): JSX.Element {
  const { id } = useParams<{ id: string }>();
  const navigate = useNavigate();

  // Tenant account panel (E24/T6): the project's tenant_id resolves to the
  // matching useUser().tenants membership; admins fall back to the directory.
  const { user } = useUser();
  const tenantDirectory = useTenantDirectory((user?.role_level ?? 0) >= 2);

  const [detail, setDetail] = useState<ProjectDetailData | null>(null);
  const [connection, setConnection] = useState<Connection | undefined>(undefined);
  const [loading, setLoading] = useState(true);
  const [notFound, setNotFound] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [reloadNonce, setReloadNonce] = useState(0);

  const [activeKey, setActiveKey] = useState<string>(PROJECT_DETAIL_TABS[0].key);

  // Roving-tabindex focus management. Only the SELECTED tab is in the tab sequence, so
  // an arrow-key selection must move DOM focus itself — otherwise focus would stay on a
  // button that just became `tabIndex={-1}`.
  const tabRefs = useRef<Record<string, HTMLButtonElement | null>>({});
  const handleTabKeyDown = useCallback((e: React.KeyboardEvent<HTMLButtonElement>) => {
    const next = nextTabKey(
      PROJECT_DETAIL_TABS.map((t) => t.key),
      e.currentTarget.dataset.tabKey ?? '',
      e.key,
    );
    if (!next) return; // not part of the tablist model — leave the press alone
    e.preventDefault();
    setActiveKey(next);
    tabRefs.current[next]?.focus();
  }, []);

  // Project-delete ConfirmDialog visibility + in-flight/error state.
  const [confirmDeleteProject, setConfirmDeleteProject] = useState(false);
  const [deletingProject, setDeletingProject] = useState(false);
  const [projectDeleteError, setProjectDeleteError] = useState<string | null>(null);

  // Load the project + its repositories, then resolve the org label from the
  // connections list. A 404 (project missing) is surfaced as a dedicated state.
  useEffect(() => {
    if (!id) {
      setLoading(false);
      setNotFound(true);
      return;
    }
    let cancelled = false;
    setLoading(true);
    projectsApi
      .get(id)
      .then(async (data) => {
        if (cancelled) return;
        setDetail(data);
        setNotFound(false);
        setError(null);
        // Best-effort org label — a connections failure must not blank the page.
        try {
          const rows = await connectionsApi.list();
          if (!cancelled) {
            setConnection(rows.find((c) => c.id === data.project.connection_id));
          }
        } catch {
          if (!cancelled) setConnection(undefined);
        }
      })
      .catch((err: unknown) => {
        if (cancelled) return;
        const message = err instanceof Error ? err.message : 'Failed to load the project.';
        if (/not found/i.test(message) || /404/.test(message)) {
          setNotFound(true);
        } else {
          setError(message);
        }
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [id, reloadNonce]);

  const refetch = useCallback(() => setReloadNonce((n) => n + 1), []);

  // Project-level delete — only reachable when the project holds no repositories
  // (the button is disabled otherwise). On success, navigate back to the list.
  //
  // The catch maps the route's FIXED literals to sentences (E27/T11 fix): the gate is
  // OWNER, and a caller whose role changed since this page loaded would otherwise read
  // the raw lowercase fragment `insufficient project role` in a red banner.
  const handleDeleteProject = useCallback(async () => {
    if (!id || deletingProject) return;
    setDeletingProject(true);
    setProjectDeleteError(null);
    try {
      await projectsApi.remove(id);
      navigate('/ops/projects');
    } catch (err: unknown) {
      setProjectDeleteError(
        destructiveActionMessage(
          err instanceof Error ? err.message : '',
          'project',
          'Failed to delete the project.',
        )
      );
      setConfirmDeleteProject(false);
      setDeletingProject(false);
    }
  }, [id, deletingProject, navigate]);

  const project = detail?.project;
  const repositories = useMemo(() => detail?.repositories ?? [], [detail]);

  // The caller's EFFECTIVE role on this project, as the SERVER reported it on this very
  // read. This is the one signal the browser cannot compute for itself: a role may be
  // granted to an Entra GROUP, and nothing client-side evaluates group membership — so a
  // roster-derived guess hides Grant (and, from T12, Promote) from exactly the
  // group-derived owners the groups-first design recommends. A UI HINT ONLY: `may()`
  // server-side stays the gate, and every affordance it drives is CONDITIONALLY RENDERED.
  const heldRole = effectiveRole(detail);

  // Whether the SERVER says this project holds no role rows at all (design §3). Passed
  // to the Access tab so its empty state can state the ungoverned case from the
  // authoritative bit instead of inferring it from a roster read that degrades to `[]`.
  const ungoverned = detail?.ungoverned;

  // The OWNER-gated DESTRUCTIVE verbs — Delete project here, Delete repository per row.
  // Both routes gate on OWNER server-side and the §3 ungoverned fallback stops at
  // MAINTAINER, so it never supplies either. Conditionally rendered, never `disabled`.
  const mayDestroy = canDestroy(heldRole, user?.role_level ?? 0);

  // --- Loading / not-found / error framing --------------------------------
  // FIRST load only — see `showPageSkeleton`. Every `refetch()` sets `loading` true, and a
  // role mutation now triggers one, so blanking on `loading` alone replaced the whole page
  // and REMOUNTED both tab bodies on every grant / change / revoke (destroying the Access
  // tab's `rosterLoaded`, replaying the Grant pop-in, and re-reading the roster twice).
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
            onClick={refetch}
            className="mt-3 px-3 py-1.5 rounded-lg bg-white border border-slate-300 text-slate-700 text-xs font-medium hover:bg-slate-50 transition-colors"
          >
            Retry
          </button>
        </div>
      </OpsPage>
    );
  }

  const repoCount = repositories.length;

  // The full membership record for this project's tenant (null when the
  // project pre-dates E24 or the tenant isn't visible to this caller).
  const tenantAccount = findTenantAccount(project.tenant_id, user?.tenants, tenantDirectory);

  return (
    <OpsPage
      backTo="/ops/projects"
      title={project.name}
      subtitle={project.description || undefined}
      action={
        // Role-gated by RENDERING (E27/T11 fix): the route is OWNER-gated, so a Viewer or
        // Maintainer is not offered a button whose every click 403s.
        //
        // The remaining `disabled={repoCount > 0}` is NOT a role gate and stays: it is a
        // referential-integrity precondition the operator can act on (remove the repos,
        // stated in the title), i.e. "not yet", where the role case is "not you" — and a
        // vanishing button would leave no explanation of what to do next.
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
      {/* Meta strip — org + repo count + the tenant's AWS accounts (E24). */}
      <div className={`${OPS_CARD} px-4 py-3 mb-6 flex flex-wrap items-center gap-x-8 gap-y-2`}>
        <div>
          <span className={FIELD_LABEL}>Org</span>
          <span className="text-sm text-slate-700">
            {orgLabel(connection, project.connection_id)}
          </span>
        </div>
        <div>
          <span className={FIELD_LABEL}>Repositories</span>
          <span className="text-sm text-slate-700 tabular-nums">{repoCount}</span>
        </div>
        {tenantAccount && (
          <>
            <div>
              <span className={FIELD_LABEL}>Tenant</span>
              <span className="text-sm text-slate-700">{tenantAccount.name}</span>
            </div>
            {/* PER-STAGE, FROM DATA (E28/T11, contract C5). These three cells used to index two
                FIXED stage keys off `stages` (an account id from each, plus a region) — which
                was a live CRASH, not a styling issue: `TenantInfo.stages` is
                `Dict[str, TenantStageConfig]` on the backend and E28/T6 removed the guarantee
                that both keys exist, so a tenant whose only stage is `uat` made this panel
                throw on `undefined.account_id`. (The old TS type asserted the pair, so nothing
                caught it — `noUncheckedIndexedAccess` is off in this project, so widening the
                type to `Record<string, …>` does not make the old reads a compile error either.
                They had to be rewritten, not merely re-typed.)

                Now it iterates whatever stages the tenant carries, alphabetically, via the same
                pure derivation the repository detail page's environment strip uses — so a
                stage the API does not return simply does not render, and no quoted stage-name
                literal appears anywhere in this file. */}
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

      {/* Pill tab bar — OperationsAdmin's class strings verbatim, with real tab
          semantics: one tab stop (roving tabindex), Left/Right to move, and the
          tab↔panel relationship expressed via aria-controls/aria-labelledby. */}
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

      {/* Active tab body. Each is its own labelled panel. `tabIndex={0}` makes the
          panel itself the next tab stop after the bar, which is what gives the
          single-tab-stop tablist somewhere to hand focus on. */}
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
            repositories={repositories}
            heldRole={heldRole}
            // The MAINTAINER verbs in this tab (create-from-template, retry) gate through
            // `_require_project_role_or_ungoverned`, so unlike the OWNER verbs they DO get
            // the design-§3 fallback. Without this bit the tab could not tell an ungoverned
            // project (where a role-less caller may still add a repo) from a governed one.
            ungoverned={ungoverned}
            onChanged={refetch}
          />
        </div>
      )}
      {activeKey === 'access' && (
        <div role="tabpanel" id={tabPanelId('access')} aria-labelledby={tabId('access')} tabIndex={0}>
          <ProjectAccessTab
            projectId={project.id}
            heldRole={heldRole}
            ungoverned={ungoverned}
            // A role mutation can change the CALLER's own standing (with two owners, a
            // self-demotion succeeds), and `heldRole` lives on THIS read. Re-reading the
            // project detail is what refreshes it — one refresh signal shared by both tabs
            // rather than a second source of truth for "what do I hold here?".
            onRolesChanged={refetch}
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
