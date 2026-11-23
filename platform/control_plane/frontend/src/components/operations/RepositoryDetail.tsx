import { useCallback, useEffect, useRef, useState, type JSX } from 'react';
import { Link, useLocation, useNavigate, useParams } from 'react-router-dom';

import { projectsApi, type Repository } from '../../api/client';
import { useUser } from '../../contexts/useUser';
import { useTenantDirectory } from '../governance/useTenantDirectory';
import DeleteRepositoryModal from './DeleteRepositoryModal';
import EnvironmentStrip from './EnvironmentStrip.tsx';
import OpsPage from './OpsPage';
import { OBJECT_LIST_PATH, OBJECT_NOUN_PLURAL, objectKindFromPath, objectListPathFor, objectNoun } from './objectRoutes';
import PromoteConfirm from './PromoteConfirm.tsx';
import { GatewayCard, RepoSummaryCard } from './RepoSummaryCard';
import TabStrip from './TabStrip.tsx';
import AccessTab from './repo-tabs/AccessTab.tsx';
import OverviewTab from './repo-tabs/OverviewTab.tsx';
import DeploymentsTab from './repo-tabs/DeploymentsTab.tsx';
import ObservabilityTab from './repo-tabs/ObservabilityTab.tsx';
import PullRequestsTab from './repo-tabs/PullRequestsTab.tsx';
import ResourcesTab from './repo-tabs/ResourcesTab.tsx';
import EvaluationsTab from './repo-tabs/EvaluationsTab.tsx';
import { evalsTabVisible } from './repo-tabs/evaluationsLogic.ts';
import { useRepoEvals } from './repo-tabs/useRepoEvals.ts';
import { useRepositoryDetail } from './useRepositoryDetail';
import { isEvalGateRefusal, promoteGateView } from './promoteGate';
import { OPS_CARD } from './opsUi';
import {
  isNothingToRetry,
  maintainerActionMessage,
  mayMaintainProject,
  prodCandidateView,
  promotionActionMessage,
} from './projectRoles';
import {
  buildNowStartedNotice,
  BUILD_NOW_STARTING_LABEL,
  buildNowActionMessage,
  canBuildNow,
  deliveryFailureReason,
  deliveryHeaderState,
  environmentRows,
  gatewayStatus,
  headerActions,
  initialTabKey,
  isTabSelectable,
  repoArtifactLink,
  repoKindOf,
  tabsForKind,
  prodGovernanceState,
  prodServingState,
  promotionArtifact,
  repoBackLink,
  tabId,
  tabPanelId,
} from './repositoryDetailTabs';
import { useReturnFocus } from '../shared/useReturnFocus';

export default function RepositoryDetail(): JSX.Element {
  const { id } = useParams<{ id: string }>();
  const navigate = useNavigate();
  const location = useLocation();
  const routeKind = objectKindFromPath(location.pathname);
  const { user } = useUser();
  const tenantDirectory = useTenantDirectory((user?.role_level ?? 0) >= 2);


  const [activeKey, setActiveKey] = useState<string>(() => initialTabKey(location.search, routeKind ?? 'agent'));
  const [openRun, setOpenRun] = useState<string | null>(null);
  const [promoting, setPromoting] = useState(false);
  const [promoteError, setPromoteError] = useState<string | null>(null);
  const [promoteNotice, setPromoteNotice] = useState<string | null>(null);
  const [promoteConfirm, setPromoteConfirm] = useState(false);
  const promoteTriggerRef = useRef<HTMLButtonElement>(null);
  const promoteConfirmRef = useRef<HTMLButtonElement>(null);
  const returnFocus = useReturnFocus(promoteTriggerRef);
  const [retrying, setRetrying] = useState(false);
  const [building, setBuilding] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);
  const [deleteOpen, setDeleteOpen] = useState(false);

  const {
    repo,
    patchRepo,
    loading,
    notFound,
    error,
    repoId,
    repoProjectId,
    projectRead,
    heldRole,
    ungoverned,
    projectName,
    connectionId,
    connection,
    agent,
    mcp,
    agentError,
    runtime,
    deployments,
    historyError,
    historyLoading,
    inboundGrants,
    outboundMcps,
    pullRequests,
    prVisibility,
    tenantAccount,
    stagesUnknown,
    runtimeByStage,
    refetch,
  } = useRepositoryDetail(id, user, tenantDirectory);

  const refreshRepo = useCallback(() => {
    if (repoId === null || repoProjectId === null) return;
    void projectsApi
      .getRepoStatus(repoProjectId, repoId)
      .then((fresh) => patchRepo(() => fresh))
      .catch(() => undefined);
  }, [repoId, repoProjectId, patchRepo]);

  const evals = useRepoEvals(
    repo && evalsTabVisible(repo) ? repo.project_id : null,
    repo && evalsTabVisible(repo) ? repo.id : null,
    mayMaintainProject(heldRole, user?.role_level ?? 0, ungoverned),
    refreshRepo,
  );

  const handlePromote = useCallback(async (overrideReason?: string) => {
    if (!repo || promoting) return;
    setPromoting(true);
    setPromoteError(null);
    setPromoteNotice(null);
    setActionError(null);
    try {
      const updated = await projectsApi.promoteRepo(repo.project_id, repo.id, overrideReason);
      patchRepo(() => updated);
      setPromoteConfirm(false);
      returnFocus();
      setPromoteNotice(`Promoting ${repo.name} to prod. Refreshing this page now.`);
      refetch();
    } catch (err: unknown) {
      setPromoteError(promotionActionMessage(err, 'The promotion could not be started.'));
      if (isEvalGateRefusal(err)) refetch();
    } finally {
      setPromoting(false);
    }
  }, [repo, promoting, refetch, patchRepo, returnFocus]);

  const closePromoteConfirm = useCallback(() => {
    setPromoteConfirm(false);
    setPromoteError(null);
    returnFocus();
  }, [returnFocus]);

  useEffect(() => {
    if (promoteConfirm) promoteConfirmRef.current?.focus();
  }, [promoteConfirm]);

  useEffect(() => {
    if (!promoteConfirm) return;
    const onKeyDown = (e: KeyboardEvent) => {
      if (e.key === 'Escape' && !promoting) {
        e.preventDefault();
        closePromoteConfirm();
      }
    };
    document.addEventListener('keydown', onKeyDown);
    return () => document.removeEventListener('keydown', onKeyDown);
  }, [promoteConfirm, promoting, closePromoteConfirm]);

  const handleRetry = useCallback(async () => {
    if (!repo || retrying) return;
    setRetrying(true);
    setActionError(null);
    setPromoteError(null);
    setPromoteNotice(null);
    try {
      const resumed = await projectsApi.retryRepo(repo.project_id, repo.id);
      patchRepo(() => resumed);
      setPromoteNotice(`Resuming ${resumed.name} from its failed step.`);
    } catch (err: unknown) {
      if (isNothingToRetry(err)) {
        setPromoteNotice('There is nothing left to retry — this repository already completed.');
        refetch();
      } else {
        setActionError(maintainerActionMessage(err, 'The retry could not be started.'));
      }
    } finally {
      setRetrying(false);
    }
  }, [repo, retrying, refetch, patchRepo]);

  const handleBuildNow = useCallback(async () => {
    if (!repo || building) return;
    setBuilding(true);
    setActionError(null);
    setPromoteError(null);
    setPromoteNotice(null);
    try {
      const started = await projectsApi.buildRepo(repo.project_id, repo.id);
      patchRepo(() => started);
      setPromoteNotice(buildNowStartedNotice(repoKindOf(repo)));
      refetch();
    } catch (err: unknown) {
      setActionError(buildNowActionMessage(err, 'The build could not be started.'));
    } finally {
      setBuilding(false);
    }
  }, [repo, building, refetch, patchRepo]);

  const pendingNoun = objectNoun(null, routeKind);
  const listPath = objectListPathFor(routeKind);
  if (loading && repo === null) {
    return (
      <OpsPage backTo={listPath} title={pendingNoun}>
        <div className={`${OPS_CARD} p-8 text-center text-slate-400 text-sm`}>
          Loading {pendingNoun.toLowerCase()}…
        </div>
      </OpsPage>
    );
  }

  if (notFound) {
    return (
      <OpsPage backTo={listPath} title={`${pendingNoun} not found`}>
        <div className={`${OPS_CARD} p-8`}>
          <h3 className="text-sm font-semibold text-slate-800">{pendingNoun} not found</h3>
          <p className="text-sm text-slate-500 mt-1">
            It doesn’t exist or is no longer available to you.
          </p>
        </div>
      </OpsPage>
    );
  }

  if (error || !repo) {
    return (
      <OpsPage backTo={listPath} title={pendingNoun}>
        <div className="bg-white/80 backdrop-blur rounded-xl border border-rose-200/70 shadow-sm p-6">
          <h3 className="text-sm font-semibold text-rose-700">Couldn’t load {pendingNoun.toLowerCase()}</h3>
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

  const rows = environmentRows({
    stages: tenantAccount?.stages,
    stagesUnknown,
    deployments,
    historyError,
  });
  const delivery = deliveryHeaderState({
    cicdStatus: repo.cicd_status,
    environmentRows: rows,
    historyError,
  });
  const deliveryFailure = delivery.inFlight ? null : deliveryFailureReason(repo.cicd_status, deployments);
  const serving = prodServingState({
    lastPromotionBuildId: repo.last_promotion_build_id,
    lastPromotedAt: repo.last_promoted_at,
    lastPromotedImageTag: repo.last_promoted_image_tag,
    deployments,
    historyError,
    historyLoading,
  });
  const kind = repoKindOf(repo);
  const isMcp = kind === 'mcp_server';
  const artifactLink = repoArtifactLink(repo);
  const gateway = gatewayStatus(repo);
  const prodGovernance = isMcp ? ('none' as const) : prodGovernanceState({
    serving,
    lifecycleState: agent?.lifecycle_state,
    agentRead: !agentError,
  });
  const promoteNeedsOverride = promoteGateView(repo.eval_gate, repo.eval_evidence?.status).needsOverride;
  const may = headerActions({
    held: heldRole,
    roleLevel: user?.role_level ?? 0,
    ungoverned,
    cicdStatus: repo.cicd_status,
    prodCandidateStatus: repo.prod_candidate_status,
    devOutcomeForCandidate: repo.dev_outcome_for_candidate,
    steps: repo.steps,
    artifactKind: kind,
    devEval: evals.devEval,
    hasAgent: evalsTabVisible(repo),
  });
  const runEvalsButton = (
    <button
      type="button"
      onClick={() => {
        setActiveKey('evaluations');
        if (!evals.blocked) void evals.startManual().then((run) => run && setOpenRun(run.run_id));
      }}
      disabled={evals.starting}
      className={
        may.evalsLead
          ? 'px-3.5 py-1.5 rounded-lg bg-emerald-600 text-white text-sm font-medium hover:bg-emerald-700 transition-colors disabled:opacity-40'
          : 'px-3.5 py-1.5 rounded-lg bg-white border border-slate-300 text-slate-700 text-sm font-medium hover:bg-slate-50 transition-colors disabled:opacity-40'
      }
    >
      {evals.starting ? 'Starting…' : 'Run evals'}
    </button>
  );
  const buildNow = canBuildNow(
    repo,
    mayMaintainProject(heldRole, user?.role_level ?? 0, ungoverned),
    delivery.inFlight,
  );
  const tabs = tabsForKind(kind).filter(
    (t) =>
      (t.key !== 'pull-requests' || prVisibility === 'visible')
      && (t.key !== 'evaluations' || evalsTabVisible(repo)),
  );
  const tabKeys = tabs.map((t) => t.key);
  const selectedKey =
    isTabSelectable(activeKey, kind) && tabKeys.includes(activeKey) ? activeKey : tabKeys[0];
  const backLink = repoBackLink({ projectId: repo.project_id, projectName, settled: projectRead });

  const tabStripRendered = tabs.length >= 2;
  const panelProps = (key: string) =>
    tabStripRendered
      ? { role: 'tabpanel', id: tabPanelId(key), 'aria-labelledby': tabId(key), tabIndex: 0 }
      : {};

  return (
    <OpsPage
      backTo={backLink.to}
      backLabel={backLink.label}
      eyebrow={
        <Link to={OBJECT_LIST_PATH[kind]} className="hover:text-emerald-800 transition-colors">
          {OBJECT_NOUN_PLURAL[kind]} › {objectNoun(repo, routeKind)}
        </Link>
      }
      title={repo.name}
      subtitle={agent?.purpose || mcp?.description || undefined}
      action={
        may.promote || may.retry || may.destroy || buildNow.visible ? (
          <div className="flex items-center gap-2">
            {buildNow.visible && (
              <>
                <button
                  type="button"
                  onClick={() => void handleBuildNow()}
                  disabled={building || buildNow.disabledReason !== null}
                  title={(buildNow.disabledReason ?? buildNow.hint) ?? undefined}
                  aria-describedby={
                    buildNow.disabledReason || buildNow.hint ? 'build-now-reason' : undefined
                  }
                  className="px-3.5 py-1.5 rounded-lg bg-white border border-emerald-300 text-emerald-700 text-sm font-medium hover:bg-emerald-50 transition-colors disabled:opacity-40 disabled:cursor-not-allowed"
                  data-testid="repo-build-now"
                >
                  {building ? BUILD_NOW_STARTING_LABEL : buildNow.label}
                </button>
                {buildNow.disabledReason && (
                  <span id="build-now-reason" className="sr-only">
                    {buildNow.disabledReason}
                  </span>
                )}
                {!buildNow.disabledReason && buildNow.hint && (
                  <span id="build-now-reason" className="text-xs text-amber-700 max-w-[16rem]">
                    {buildNow.hint}
                  </span>
                )}
              </>
            )}
            {may.retry && (
              <button
                type="button"
                onClick={() => void handleRetry()}
                disabled={retrying}
                className="px-3.5 py-1.5 rounded-lg bg-white border border-slate-300 text-slate-700 text-sm font-medium hover:bg-slate-50 transition-colors disabled:opacity-40 disabled:cursor-not-allowed"
              >
                {retrying ? 'Retrying…' : 'Retry from failed step'}
              </button>
            )}
            {may.destroy && (
              <button
                type="button"
                onClick={() => setDeleteOpen(true)}
                className="px-3.5 py-1.5 rounded-lg bg-white border border-rose-300 text-rose-700 text-sm font-medium hover:bg-rose-50 transition-colors"
              >
                Delete
              </button>
            )}
            {may.runEvals && !may.evalsLead && runEvalsButton}
            {may.promote && !promoteConfirm && (
              <button
                ref={promoteTriggerRef}
                type="button"
                onClick={() => {
                  setPromoteConfirm(true);
                  setPromoteError(null);
                  setPromoteNotice(null);
                  setActionError(null);
                }}
                disabled={promoting}
                className={
                  may.evalsLead
                    ? 'px-3.5 py-1.5 rounded-lg bg-white border border-slate-300 text-slate-700 text-sm font-medium hover:bg-slate-50 transition-colors disabled:opacity-40 disabled:cursor-not-allowed'
                    : 'px-3.5 py-1.5 rounded-lg bg-emerald-600 text-white text-sm font-medium hover:bg-emerald-700 transition-colors disabled:opacity-40 disabled:cursor-not-allowed'
                }
              >
                {promoting ? 'Promoting…' : promoteNeedsOverride ? 'Promote (needs override)' : 'Promote to prod'}
              </button>
            )}
            {may.evalsLead && runEvalsButton}
          </div>
        ) : undefined
      }
    >
      <RepoSummaryCard
        repo={repo}
        agent={agent}
        mcp={mcp}
        agentError={agentError}
        isMcp={isMcp}
        artifactLink={artifactLink}
        projectName={projectName}
        projectRead={projectRead}
        connection={connection}
        connectionId={connectionId}
        runtime={runtime}
        delivery={delivery}
        deliveryFailure={deliveryFailure}
        gateway={gateway}
        setActiveKey={setActiveKey}
      />

      {prodGovernance === 'ungoverned' && (
        <div
          role="alert"
          className="mb-6 rounded-xl border border-amber-200/70 bg-amber-50/70 px-4 py-3"
        >
          <h3 className="text-sm font-semibold text-amber-800">
            Serving production without governance approval
          </h3>
          <p className="text-sm text-amber-800/90 mt-1">
            This agent has deployed to production while it is still recorded as proposed. Approval is requested and granted on the{' '}
            <Link to={`/agents/${repo.agent_id}`} className="underline font-medium">
              agent’s governance record
            </Link>
            .
          </p>
        </div>
      )}

      {prodGovernance === 'unknown' && (
        <div
          role="status"
          className="mb-6 rounded-xl border border-slate-200/70 bg-slate-50/70 px-4 py-3"
        >
          <h3 className="text-sm font-semibold text-slate-700">
            Production governance could not be confirmed
          </h3>
          <p className="text-sm text-slate-600 mt-1">
            Either this agent’s governance record could not be read, or what production is
            serving is not established from the records available here — so this is neither a report
            that something unapproved is running nor confirmation that nothing is. Check the{' '}
            <Link to={`/agents/${repo.agent_id}`} className="underline font-medium">
              agent’s governance record
            </Link>{' '}
            and the Deployments tab directly.
          </p>
        </div>
      )}

      {may.promote && promoteConfirm && (
        <div className="mb-4">
          <PromoteConfirm
            repoName={repo.name}
            candidate={prodCandidateView(repo)}
            artifact={promotionArtifact(repo)}
            devImageTag={repo.last_dev_image_tag ?? null}
            evalGate={repo.eval_gate}
            evalRegressions={repo.eval_regressions}
            evalEvidence={repo.eval_evidence}
            lifecycleState={agent?.lifecycle_state}
            onViewRun={(runId) => {
              closePromoteConfirm();
              setOpenRun(runId);
              setActiveKey('evaluations');
            }}
            pending={promoting}
            onConfirm={(overrideReason) => void handlePromote(overrideReason)}
            onCancel={closePromoteConfirm}
            confirmRef={promoteConfirmRef}
          />
        </div>
      )}

      {promoteError && (
        <p className="text-sm text-red-600 mb-4" role="alert">
          {promoteError}
        </p>
      )}
      {actionError && (
        <p className="text-sm text-red-600 mb-4" role="alert">
          {actionError}
        </p>
      )}
      {promoteNotice && (
        <p className="text-sm text-emerald-700 mb-4" role="status">
          {promoteNotice}
        </p>
      )}

      {isMcp ? (
        <GatewayCard repo={repo} mcp={mcp} deployed={gateway.deployed} label={gateway.label} />
      ) : (
      <EnvironmentStrip
        source={{ stages: tenantAccount?.stages, stagesUnknown, deployments, historyError }}
        runtimeByStage={runtimeByStage}
        loading={historyLoading}
      />
      )}

      <TabStrip
        tabs={tabs}
        activeKey={selectedKey}
        onSelect={setActiveKey}
        ariaLabel={`${objectNoun(repo, routeKind)} sections`}
        tabId={tabId}
        tabPanelId={tabPanelId}
      />

      {selectedKey === 'overview' && (
        <div {...panelProps('overview')}>
          <OverviewTab repo={repo} agent={agent} mcp={mcp} />
        </div>
      )}
      {selectedKey === 'deployments' && (
        <div {...panelProps('deployments')}>
          <DeploymentsTab
            repo={repo}
            deployments={deployments}
            historyError={historyError}
            loading={historyLoading}
            heldRole={heldRole}
            roleLevel={user?.role_level ?? 0}
            onChanged={(updated: Repository) => {
              patchRepo(() => updated);
              refetch();
            }}
          />
        </div>
      )}
      {selectedKey === 'pull-requests' && (
        <div {...panelProps('pull-requests')}>
          <PullRequestsTab
            repoId={repo.id}
            pullRequests={pullRequests}
            heldRole={heldRole}
            roleLevel={user?.role_level ?? 0}
            ungoverned={ungoverned}
            loading={loading}
            onChanged={refetch}
            onRefresh={refetch}
          />
        </div>
      )}
      {selectedKey === 'access' && (
        <div {...panelProps('access')}>
          <AccessTab
            kind={kind}
            artifactId={artifactLink.id}
            inbound={inboundGrants}
            outbound={outboundMcps}
            onNavigateAway={() => navigate(artifactLink.to)}
          />
        </div>
      )}
      {selectedKey === 'observability' && (
        <div {...panelProps('observability')}>
          <ObservabilityTab repo={repo} mayWrite={mayMaintainProject(heldRole, user?.role_level ?? 0, ungoverned)} />
        </div>
      )}
      {selectedKey === 'evaluations' && (
        <div {...panelProps('evaluations')}>
          <EvaluationsTab
            repo={repo}
            mayWrite={mayMaintainProject(heldRole, user?.role_level ?? 0, ungoverned)}
            viewer={user ? { oid: user.oid, name: user.name, email: user.email } : null}
            openRunId={openRun}
            onOpenRun={setOpenRun}
            evals={evals}
          />
        </div>
      )}
      {selectedKey === 'resources' && (
        <div {...panelProps('resources')}>
          <ResourcesTab repo={repo} />
        </div>
      )}

      {deleteOpen && (
        <DeleteRepositoryModal
          repo={repo}
          projectId={repo.project_id}
          onClose={() => {
            setDeleteOpen(false);
            refetch();
          }}
          onDeleted={() => {
            setDeleteOpen(false);
            navigate(OBJECT_LIST_PATH[kind]);
          }}
        />
      )}
    </OpsPage>
  );
}
