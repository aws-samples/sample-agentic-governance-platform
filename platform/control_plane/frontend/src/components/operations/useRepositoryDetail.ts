import { useCallback, useEffect, useRef } from 'react';

import {
  agentMcpGrantsApi,
  agentOpsApi,
  agentsApi,
  connectionsApi,
  grantsApi,
  mcpGrantsApi,
  mcpServersApi,
  projectsApi,
  pullRequestsApi,
  repositoriesApi,
  type Agent,
  type Connection,
  type Deployment,
  type McpServer,
  type PullRequestView,
  type Repository,
  type RuntimeStatus,
  type TenantView,
  type UserInfo,
} from '../../api/client';
import { useLoad } from '../../api/useLoad';
import { findTenantAccount } from '../governance/tenantUi';
import { effectiveRole } from './projectRoles';
import type { MaybeCount } from './repo-tabs/AccessTab.tsx';
import { prProbeFrom, prTabVisibility } from './repo-tabs/pullRequestsTabModel.ts';
import { probeStages, repoKindOf, settledFrom, shouldPollRepo, sortedStageNames } from './repositoryDetailTabs';

type ProjectRead = {
  detail: Awaited<ReturnType<typeof projectsApi.get>> | null;
  connection: Connection | undefined;
};
type RecordRead = { agent: Agent | null; mcp: McpServer | null };
const EMPTY_DEPLOYMENTS: Deployment[] = [];
const EMPTY_PULL_REQUESTS: PullRequestView[] = [];
const EMPTY_RUNTIME_BY_STAGE: Record<string, RuntimeStatus | undefined> = {};

export function useRepositoryDetail(
  id: string | undefined,
  user: UserInfo | null,
  tenantDirectory: TenantView[] | null,
) {
  const repoLoad = useLoad(
    () =>
      id
        ? repositoriesApi.list().then((rows) => rows.find((r) => r.id === id) ?? null)
        : Promise.resolve<Repository | null>(null),
    [id],
    'Failed to load the repository.',
  );
  const repo = repoLoad.state.data;
  const patchRepo = repoLoad.patch;
  const loading = repoLoad.state.kind === 'loading';
  const repoMessage = repoLoad.state.kind === 'error' ? repoLoad.state.message : null;
  const notFound = repoLoad.state.kind === 'ready' && repo === null;
  const error = repoMessage !== null && !notFound ? repoMessage : null;

  const hasRepo = repo !== null;
  const repoId = repo?.id ?? null;
  const repoProjectId = repo?.project_id ?? null;
  const isMcpRepo = repo !== null && repoKindOf(repo) === 'mcp_server';
  const agentId = repo?.agent_id ?? null;
  const mcpId = repo?.mcp_server_id ?? null;

  const projectLoad = useLoad(
    () =>
      repoProjectId
        ? projectsApi
            .get(repoProjectId)
            .then(async (detail): Promise<ProjectRead> => {
              const conns = await connectionsApi.list().catch(() => null);
              return { detail, connection: conns?.find((c) => c.id === detail.project.connection_id) };
            })
            .catch((): ProjectRead => ({ detail: null, connection: undefined }))
        : Promise.resolve<ProjectRead | null>(null),
    [repoProjectId],
  );
  const projectRead = settledFrom(projectLoad.state);
  const detail = projectLoad.state.data?.detail ?? null;
  const heldRole = detail ? effectiveRole(detail) : null;
  const ungoverned = detail?.ungoverned;
  const tenantId = detail?.project.tenant_id ?? null;
  const projectName = detail?.project.name || null;
  const connectionId = detail?.project.connection_id || null;
  const connection = projectLoad.state.data?.connection;

  const recordLoad = useLoad(
    () =>
      !hasRepo
        ? Promise.resolve<RecordRead>({ agent: null, mcp: null })
        : isMcpRepo
          ? mcpServersApi.get(mcpId ?? '').then((mcp): RecordRead => ({ agent: null, mcp }))
          : agentsApi.get(agentId ?? '').then((agent): RecordRead => ({ agent, mcp: null })),
    [hasRepo, isMcpRepo, mcpId, agentId],
  );
  const agent = recordLoad.state.data?.agent ?? null;
  const mcp = recordLoad.state.data?.mcp ?? null;
  const agentError = recordLoad.state.kind === 'error';

  const runtimeLoad = useLoad(
    () =>
      hasRepo && !isMcpRepo
        ? agentOpsApi.runtime(agentId ?? '').catch((): RuntimeStatus | undefined => undefined)
        : Promise.resolve<RuntimeStatus | undefined>(undefined),
    [hasRepo, isMcpRepo, agentId],
  );
  const runtime = runtimeLoad.state.data ?? undefined;

  const historyLoad = useLoad(
    () =>
      !hasRepo
        ? Promise.resolve<Deployment[]>([])
        : isMcpRepo
          ? projectsApi.repoDeployments(repoProjectId ?? '', repoId ?? '')
          : agentOpsApi.deployments(agentId ?? ''),
    [hasRepo, isMcpRepo, repoProjectId, repoId, agentId],
  );
  const deployments = historyLoad.state.kind === 'error' ? EMPTY_DEPLOYMENTS : historyLoad.state.data ?? EMPTY_DEPLOYMENTS;
  const historyError = historyLoad.state.kind === 'error';
  const historyLoading = historyLoad.state.kind === 'loading';

  const inboundLoad = useLoad(
    async (): Promise<MaybeCount> => {
      if (!hasRepo) return null;
      try {
        const grants = isMcpRepo ? await mcpGrantsApi.list(mcpId ?? '') : await grantsApi.list(agentId ?? '');
        return grants.length;
      } catch {
        return null;
      }
    },
    [hasRepo, isMcpRepo, mcpId, agentId],
  );
  const inboundGrants = inboundLoad.state.data ?? null;

  const outboundLoad = useLoad(
    async (): Promise<MaybeCount> => {
      if (!hasRepo || isMcpRepo) return null;
      try {
        return (await agentMcpGrantsApi.list(agentId ?? '')).length;
      } catch {
        return null;
      }
    },
    [hasRepo, isMcpRepo, agentId],
  );
  const outboundMcps = outboundLoad.state.data ?? null;

  const prsLoad = useLoad(
    () => (repoId ? pullRequestsApi.list(repoId) : Promise.resolve<PullRequestView[] | null>(null)),
    [repoId],
  );
  const pullRequests = prsLoad.state.kind === 'error' ? EMPTY_PULL_REQUESTS : prsLoad.state.data ?? EMPTY_PULL_REQUESTS;
  const prVisibility = prTabVisibility(prProbeFrom(prsLoad.state));

  const tenantAccount = findTenantAccount(tenantId, user?.tenants, tenantDirectory);
  const stagesUnknown = tenantId !== null && tenantAccount === null;

  const stageNames = sortedStageNames(tenantAccount?.stages);
  const stageKey = JSON.stringify(stageNames);
  const runtimeByStageLoad = useLoad(
    () => {
      const stages = JSON.parse(stageKey) as string[];
      if (agentId === null || stages.length === 0) {
        return Promise.resolve<Record<string, RuntimeStatus | undefined>>({});
      }
      return probeStages(stages, (stage) => agentOpsApi.runtime(agentId, { stage }));
    },
    [agentId, stageKey],
  );
  const runtimeByStage = runtimeByStageLoad.state.data ?? EMPTY_RUNTIME_BY_STAGE;

  const reloadRepo = repoLoad.reload;
  const reloadProject = projectLoad.reload;
  const reloadRecord = recordLoad.reload;
  const reloadRuntime = runtimeLoad.reload;
  const reloadHistory = historyLoad.reload;
  const reloadInbound = inboundLoad.reload;
  const reloadOutbound = outboundLoad.reload;
  const reloadPrs = prsLoad.reload;
  const reloadRuntimeByStage = runtimeByStageLoad.reload;
  const refetch = useCallback(() => {
    reloadRepo();
    reloadProject();
    reloadRecord();
    reloadRuntime();
    reloadHistory();
    reloadInbound();
    reloadOutbound();
    reloadPrs();
    reloadRuntimeByStage();
  }, [
    reloadRepo,
    reloadProject,
    reloadRecord,
    reloadRuntime,
    reloadHistory,
    reloadInbound,
    reloadOutbound,
    reloadPrs,
    reloadRuntimeByStage,
  ]);

  const pollRef = useRef<number | null>(null);
  const polling = repo !== null && shouldPollRepo(repo);
  const repoReady = repoLoad.state.kind === 'ready';
  useEffect(() => {
    if (repoId === null || repoProjectId === null || !polling) return;
    let cancelled = false;
    const stop = () => {
      if (pollRef.current !== null) {
        window.clearInterval(pollRef.current);
        pollRef.current = null;
      }
    };
    pollRef.current = window.setInterval(async () => {
      const fresh = await projectsApi.getRepoStatus(repoProjectId, repoId).catch(() => null);
      if (cancelled || fresh === null) return;
      if (repoReady) patchRepo(() => fresh);
      if (!shouldPollRepo(fresh)) {
        stop();
        refetch();
      }
    }, 3000);
    return () => {
      cancelled = true;
      stop();
    };
  }, [repoId, repoProjectId, polling, refetch, patchRepo, repoReady]);

  return {
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
  };
}
