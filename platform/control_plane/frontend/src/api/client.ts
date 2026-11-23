import axios, { AxiosError, type AxiosResponse } from 'axios';
import {
  AUTH_401_COUNT_KEY,
  AUTH_401_HALT_MESSAGE,
  AUTH_401_RELOAD_MESSAGE,
  clearsAuth401Suspicion,
  decide401,
  readAttempts,
  safeStorage,
} from './authRetry';
import { ApiFailure, errorBodyOf, formatApiErrorDetail, isApiFailure } from './failure';
import { safeLocalStorage } from './safeLocalStorage';
import type {
  AddPolicyRequest,
  AdoptRequest,
  Agent,
  AgentCreate,
  AgentGrantCreate,
  AgentGrantRead,
  AgentLite,
  AgentMcpGrant,
  AgentMetrics,
  AgentRepositoryView,
  AgentUpdate,
  BodyUploadTemplateApiV1GithubTemplatesPost,
  CedarPolicyRow,
  CedarPolicySet,
  Connection,
  ConnectionCreate,
  ConnectionFinalize,
  ConnectionOAuthClient,
  ConnectionTokenReplace,
  ContainmentView,
  DatasetAdded,
  DatasetItemInput,
  DatasetRequest,
  Deployment,
  DiscoveredAgents,
  DriftRead,
  EnforcementRequest,
  EnforcementState,
  EvalConfigInput,
  EvalFiles,
  EvalRun,
  EvalRuns,
  GitHubLinkStatus,
  GitHubLinkView,
  GovernanceGraph,
  GuardrailMetrics,
  GuardrailPreset,
  GuardrailTemplate,
  GuardrailTemplateCreate,
  GuardrailTemplateUpdate,
  HeadSha,
  HealthResponse,
  InfraTemplateView,
  InvokeRequest,
  InvokeResult,
  LinkCallbackRequest,
  LinkStartRequest,
  LinkStartResponse,
  Listing,
  ListingUpdate,
  ManifestCallback,
  ManifestCallbackResponse,
  ManifestStart,
  ManifestStartResponse,
  MarketplaceMetrics,
  McpGrantCreate,
  McpGrantRead,
  McpServer,
  McpServerCreate,
  McpServerUpdate,
  MissingMcpGrant,
  MonitorRequest,
  MonitorState,
  ObservabilitySettings,
  PingResponse,
  PlatformUser,
  PrincipalDetail,
  PrincipalHit,
  ProductCard,
  ProductType,
  Project,
  ProjectCreate,
  ProjectDetail,
  ProjectRoleCreate,
  ProjectRoleRead,
  PublishRequest,
  PublishRequestCreate,
  PullRequestCreate,
  PullRequestView,
  ReasonRequest,
  ReconcileViewResponse,
  RejectRequest,
  RepoDeletePreview,
  RepoDeleteResult,
  RepoDeleteSelection,
  RepoPromoteRequest,
  RepoRollbackRequest,
  Repository,
  RepositoryCreate,
  RevokeRequest,
  RoleAssignmentCreate,
  RoleChange,
  RolloutRequest,
  RolloutResultResponse,
  RunDetail,
  RuntimeStatus,
  ScopeMetrics,
  StartRunRequest,
  Subscription,
  SubscriptionCreate,
  TemplatePatch,
  TemplateView,
  Tenant,
  TenantCreate,
  TenantUpdate,
  TenantView,
  TraceList,
  TracePage,
  TransitionRequest,
  UserInfo,
  UserTenant,
  ValidateRequest,
  ValidationResult,
  Verb,
  VerbReadiness,
  operations,
} from './openapi';

// Decision: docs/adr/frontend-wire-types-are-generated.md
export type * from './openapi';

export type TenantReadiness = Partial<Record<Verb, VerbReadiness>>;
export type AgentListFilters = operations['list_agents_api_v1_agents_get']['parameters']['query'];
export type McpServerListFilters = operations['list_mcp_servers_api_v1_mcp_servers_get']['parameters']['query'];
export type StageConfig = TenantView['stages'][string] | UserTenant['stages'][string];
export type ArtifactKind = TemplateView['artifact_kind'];

const API_URL = import.meta.env.VITE_API_URL || 'http://localhost:8000';

const client = axios.create({
  baseURL: API_URL,
  headers: {
    'Content-Type': 'application/json',
  },
});

async function acquireMsalToken(opts?: { forceRefresh?: boolean }): Promise<string | null> {
  try {
    const { currentAccessToken } = await import('../auth/tokenBroker');
    return await currentAccessToken(opts);
  } catch {
    return null;
  }
}

// Rationale: docs/token-propagation.md §1–§2 (token homes, 401 budget)
client.interceptors.request.use(async (config) => {
  const token = await acquireMsalToken();
  const fallbackToken = token ?? safeLocalStorage.getItem('auth_token');
  if (fallbackToken) {
    config.headers.Authorization = `Bearer ${fallbackToken}`;
  }

  return config;
});

let latched401: 'reload' | 'halt' | null = null;

export async function rejectResponse(error: AxiosError<unknown>): Promise<AxiosResponse> {
  if (error.response?.status === 401) {
    const config = error.config as
      | (NonNullable<typeof error.config> & { _agpForceRefreshRetried?: boolean })
      | undefined;
    if (config && !config._agpForceRefreshRetried) {
      const refreshed = await acquireMsalToken({ forceRefresh: true });
      if (refreshed) {
        config._agpForceRefreshRetried = true;
        config.headers.Authorization = `Bearer ${refreshed}`;
        return client(config);
      }
    }
    if (latched401 === null) {
      const attempts = readAttempts(safeStorage.read(AUTH_401_COUNT_KEY));
      latched401 = decide401(attempts);
      if (latched401 === 'reload') {
        safeStorage.write(AUTH_401_COUNT_KEY, String(attempts + 1));
        safeLocalStorage.removeItem('auth_token');
        window.location.reload();
      }
    }
    if (latched401 === 'halt') {
      return Promise.reject(new ApiFailure(401, null, undefined, AUTH_401_HALT_MESSAGE));
    }
    return Promise.reject(new ApiFailure(401, null, undefined, AUTH_401_RELOAD_MESSAGE));
  }
  const { detail, code } = errorBodyOf(error.response?.data);
  return Promise.reject(new ApiFailure(error.response?.status ?? 0, code, detail, formatApiErrorDetail(detail, error.message)));
}

client.interceptors.response.use(
  (response) => {
    if (clearsAuth401Suspicion(response.config.headers?.Authorization)) {
      safeStorage.remove(AUTH_401_COUNT_KEY);
    }
    return response;
  },
  rejectResponse,
);

export const healthApi = {
  check: async (): Promise<HealthResponse> => {
    const response = await client.get<HealthResponse>('/health');
    return response.data;
  },

  ping: async (): Promise<PingResponse> => {
    const response = await client.get<PingResponse>('/ping');
    return response.data;
  },
};

export const userApi = {
  getCurrentUser: async (): Promise<UserInfo> => {
    const response = await client.get<UserInfo>('/api/v1/users/me');
    return response.data;
  },
};

export default client;

export const guardrailsApi = {
  list: async (status?: string): Promise<GuardrailTemplate[]> => {
    const params = status ? { status } : {};
    const response = await client.get<GuardrailTemplate[]>('/api/v1/guardrails', { params });
    return response.data;
  },

  get: async (templateId: string): Promise<GuardrailTemplate> => {
    const response = await client.get<GuardrailTemplate>(`/api/v1/guardrails/${templateId}`);
    return response.data;
  },

  create: async (data: GuardrailTemplateCreate): Promise<GuardrailTemplate> => {
    const response = await client.post<GuardrailTemplate>('/api/v1/guardrails', data);
    return response.data;
  },

  update: async (templateId: string, data: GuardrailTemplateUpdate): Promise<GuardrailTemplate> => {
    const response = await client.put<GuardrailTemplate>(`/api/v1/guardrails/${templateId}`, data);
    return response.data;
  },

  delete: async (templateId: string): Promise<GuardrailTemplate> => {
    const response = await client.delete<GuardrailTemplate>(`/api/v1/guardrails/${templateId}`);
    return response.data;
  },

  publish: async (templateId: string): Promise<GuardrailTemplate> => {
    const response = await client.post<GuardrailTemplate>(`/api/v1/guardrails/${templateId}/publish`);
    return response.data;
  },

  getMetrics: async (templateId: string, hours: number = 24): Promise<GuardrailMetrics> => {
    const response = await client.get<GuardrailMetrics>(`/api/v1/guardrails/${templateId}/metrics`, { params: { hours } });
    return response.data;
  },

  getPresets: async (): Promise<GuardrailPreset[]> => {
    const response = await client.get<GuardrailPreset[]>('/api/v1/guardrails/presets');
    return response.data;
  },
};

export const agentsApi = {
  list: async (filters?: AgentListFilters): Promise<Agent[]> => {
    const response = await client.get<Agent[]>('/api/v1/agents', { params: filters });
    return response.data;
  },
  get: async (id: string): Promise<Agent> => {
    const response = await client.get<Agent>(`/api/v1/agents/${id}`);
    return response.data;
  },
  create: async (data: AgentCreate): Promise<Agent> => {
    const response = await client.post<Agent>('/api/v1/agents', data);
    return response.data;
  },
  update: async (id: string, data: AgentUpdate): Promise<Agent> => {
    const response = await client.put<Agent>(`/api/v1/agents/${id}`, data);
    return response.data;
  },
  remove: async (id: string): Promise<Agent> => {
    const response = await client.delete<Agent>(`/api/v1/agents/${id}`);
    return response.data;
  },
  submit: async (id: string): Promise<Agent> => {
    const response = await client.post<Agent>(`/api/v1/agents/${id}/submit`);
    return response.data;
  },
  transition: async (id: string, action: 'approve' | 'reject' | 'deprecate', reason: string): Promise<Agent> => {
    const body: TransitionRequest = { action, reason };
    const response = await client.post<Agent>(`/api/v1/agents/${id}/transitions`, body);
    return response.data;
  },
};

export const agentOpsApi = {
  runtime: async (agentId: string, params?: { stage?: string }): Promise<RuntimeStatus> =>
    (await client.get<RuntimeStatus>(`/api/v1/agents/${agentId}/runtime`, {
      params: { stage: params?.stage },
    })).data,
  /** Newest first; `stage` omitted means every stage. */
  deployments: async (
    agentId: string,
    params?: { stage?: string; limit?: number },
  ): Promise<Deployment[]> =>
    (await client.get<Deployment[]>(`/api/v1/agents/${agentId}/deployments`, {
      params: { stage: params?.stage, limit: params?.limit },
    })).data,
  repository: async (agentId: string): Promise<AgentRepositoryView | null> => {
    try {
      return (await client.get<AgentRepositoryView>(`/api/v1/agents/${agentId}/repository`)).data;
    } catch (e) {
      if (isApiFailure(e) && e.code === 'project.no_repository') return null;
      throw e;
    }
  },
  evalRuns: async (agentId: string): Promise<EvalRuns> =>
    (await client.get<EvalRuns>(`/api/v1/agents/${agentId}/evals/runs`)).data,
};

export const pullRequestsApi = {
  list: async (repoId: string): Promise<PullRequestView[]> =>
    (await client.get<PullRequestView[]>(`/api/v1/repositories/${repoId}/pull-requests`)).data,
  create: async (repoId: string, body: PullRequestCreate): Promise<PullRequestView> =>
    (await client.post<PullRequestView>(`/api/v1/repositories/${repoId}/pull-requests`, body))
      .data,
  approve: async (repoId: string, number: number): Promise<PullRequestView> =>
    (await client.post<PullRequestView>(
      `/api/v1/repositories/${repoId}/pull-requests/${number}/approve`,
    )).data,
  merge: async (repoId: string, number: number): Promise<PullRequestView> =>
    (await client.post<PullRequestView>(
      `/api/v1/repositories/${repoId}/pull-requests/${number}/merge`,
    )).data,
};

export const grantsApi = {
  list: async (agentId: string): Promise<AgentGrantRead[]> => (await client.get<AgentGrantRead[]>(`/api/v1/agents/${agentId}/grants`)).data,
  add: async (agentId: string, body: AgentGrantCreate): Promise<AgentGrantRead> => (await client.post<AgentGrantRead>(`/api/v1/agents/${agentId}/grants`, body)).data,
  remove: async (agentId: string, assignmentId: string): Promise<void> => { await client.delete(`/api/v1/agents/${agentId}/grants/${assignmentId}`); },
  drift: async (agentId: string): Promise<DriftRead> => (await client.get<DriftRead>(`/api/v1/agents/${agentId}/grants/drift`)).data,
  /** Answers the drift after the rewrite: empty entries means the re-assert converged. */
  reassert: async (agentId: string): Promise<DriftRead> => (await client.post<DriftRead>(`/api/v1/agents/${agentId}/grants/reassert`)).data,
};
export const principalsApi = {
  search: async (q: string): Promise<PrincipalHit[]> => (await client.get<PrincipalHit[]>(`/api/v1/entra/principals/search`, { params: { q } })).data,
};
export const invokeApi = {
  invoke: async (
    agentId: string,
    prompt: string,
    params?: { stage?: string },
  ): Promise<InvokeResult> => {
    // `tool_calls` null means the platform gave no tool-call source, not that no tools were called; `session_id` is null on Databricks.
    const body: InvokeRequest = { prompt };
    return (await client.post<InvokeResult>(`/api/v1/agents/${agentId}/invoke`, body, {
      params: { stage: params?.stage },
    })).data;
  },
};

export const mcpServersApi = {
  list: async (filters?: McpServerListFilters): Promise<McpServer[]> => {
    const response = await client.get<McpServer[]>('/api/v1/mcp-servers', { params: filters });
    return response.data;
  },
  get: async (id: string): Promise<McpServer> => {
    const response = await client.get<McpServer>(`/api/v1/mcp-servers/${id}`);
    return response.data;
  },
  create: async (data: McpServerCreate): Promise<McpServer> => {
    const response = await client.post<McpServer>('/api/v1/mcp-servers', data);
    return response.data;
  },
  update: async (id: string, data: McpServerUpdate): Promise<McpServer> => {
    const response = await client.put<McpServer>(`/api/v1/mcp-servers/${id}`, data);
    return response.data;
  },
  remove: async (id: string): Promise<McpServer> => {
    const response = await client.delete<McpServer>(`/api/v1/mcp-servers/${id}`);
    return response.data;
  },
  submit: async (id: string): Promise<McpServer> => {
    const response = await client.post<McpServer>(`/api/v1/mcp-servers/${id}/submit`);
    return response.data;
  },
  transition: async (id: string, action: 'approve' | 'reject' | 'deprecate', reason: string): Promise<McpServer> => {
    const body: TransitionRequest = { action, reason };
    const response = await client.post<McpServer>(`/api/v1/mcp-servers/${id}/transitions`, body);
    return response.data;
  },
  /** Gateway only (409 otherwise); the response carries the re-read `available_tools`. */
  refreshTools: async (id: string): Promise<McpServer> =>
    (await client.post<McpServer>(`/api/v1/mcp-servers/${id}/refresh-tools`)).data,
};

export const mcpGrantsApi = {
  list: async (mcpId: string): Promise<McpGrantRead[]> => (await client.get<McpGrantRead[]>(`/api/v1/mcp-servers/${mcpId}/grants`)).data,
  add: async (mcpId: string, body: McpGrantCreate): Promise<McpGrantRead> => (await client.post<McpGrantRead>(`/api/v1/mcp-servers/${mcpId}/grants`, body)).data,
  remove: async (mcpId: string, assignmentId: string): Promise<void> => { await client.delete(`/api/v1/mcp-servers/${mcpId}/grants/${assignmentId}`); },
};

export const cedarPoliciesApi = {
  list: async (mcpId: string): Promise<CedarPolicySet> =>
    (await client.get<CedarPolicySet>(`/api/v1/mcp-servers/${mcpId}/policies`)).data,
  add: async (mcpId: string, body: AddPolicyRequest): Promise<CedarPolicyRow> =>
    (await client.post<CedarPolicyRow>(`/api/v1/mcp-servers/${mcpId}/policies`, body)).data,
  remove: async (mcpId: string, policyId: string): Promise<void> => { await client.delete(`/api/v1/mcp-servers/${mcpId}/policies/${policyId}`); },
  /** `'disabled'` detaches the policy engine; the first `add` creates and attaches it in enforce mode. */
  setEnforcement: async (mcpId: string, mode: EnforcementRequest['mode']): Promise<EnforcementState> => {
    const body: EnforcementRequest = { mode };
    return (await client.put<EnforcementState>(`/api/v1/mcp-servers/${mcpId}/policy-enforcement`, body)).data;
  },
};

export const containmentApi = {
  get: async (agentId: string): Promise<ContainmentView> =>
    (await client.get<ContainmentView>(`/api/v1/agents/${agentId}/containment`)).data,
  setMonitor: async (agentId: string, enabled: boolean): Promise<MonitorState> => {
    const body: MonitorRequest = { enabled };
    return (await client.put<MonitorState>(`/api/v1/agents/${agentId}/containment/monitor`, body)).data;
  },
  /** 202 with the view before the steps run, so callers poll `get`; `restore` answers 409 to the person who contained. */
  contain: async (agentId: string, reason: string): Promise<ContainmentView> => {
    const body: ReasonRequest = { reason };
    return (await client.post<ContainmentView>(`/api/v1/agents/${agentId}/containment/contain`, body)).data;
  },
  restore: async (agentId: string, reason: string): Promise<ContainmentView> => {
    const body: ReasonRequest = { reason };
    return (await client.post<ContainmentView>(`/api/v1/agents/${agentId}/containment/restore`, body)).data;
  },
};

export const governanceGraphApi = {
  get: async (): Promise<GovernanceGraph> =>
    (await client.get<GovernanceGraph>('/api/v1/governance-graph')).data,
  principal: async (oid: string, kind: PrincipalDetail['kind']): Promise<PrincipalDetail> =>
    (await client.get<PrincipalDetail>(`/api/v1/governance-graph/principals/${oid}`, { params: { kind } })).data,
};

/** A revoke goes through `mcpGrantsApi.remove(mcp_id, assignment_id)`; there is no agent-side revoke route. */
export const agentMcpGrantsApi = {
  list: async (agentId: string): Promise<AgentMcpGrant[]> =>
    (await client.get<AgentMcpGrant[]>(`/api/v1/agents/${agentId}/mcp-grants`)).data,
  missing: async (agentId: string): Promise<MissingMcpGrant[]> =>
    (await client.get<MissingMcpGrant[]>(`/api/v1/agents/${agentId}/mcp-grants/missing`)).data,
};

export const marketplaceApi = {
  listAgentProducts: async (): Promise<ProductCard[]> => (await client.get<ProductCard[]>(`/api/v1/marketplace/agent-products`)).data,
  listEligibleAgents: async (): Promise<AgentLite[]> => (await client.get<AgentLite[]>(`/api/v1/marketplace/eligible-agents`)).data,
  listMcpProducts:   async (): Promise<ProductCard[]> => (await client.get<ProductCard[]>(`/api/v1/marketplace/mcp-products`)).data,
  subscribe: async (body: SubscriptionCreate): Promise<Subscription> =>
    (await client.post<Subscription>(`/api/v1/marketplace/subscriptions`, body)).data,
  mySubscriptions:    async (): Promise<Subscription[]> => (await client.get<Subscription[]>(`/api/v1/marketplace/subscriptions`)).data,
  adminSubscriptions: async (
    params?: operations['list_admin_subscriptions_api_v1_marketplace_admin_subscriptions_get']['parameters']['query'],
  ): Promise<Subscription[]> => (await client.get<Subscription[]>(`/api/v1/marketplace/admin/subscriptions`, { params })).data,
  approve: async (id: string): Promise<Subscription> => (await client.post<Subscription>(`/api/v1/marketplace/subscriptions/${id}/approve`)).data,
  reject:  async (id: string, reason?: string): Promise<Subscription> => {
    const body: RejectRequest = { reason };
    return (await client.post<Subscription>(`/api/v1/marketplace/subscriptions/${id}/reject`, body)).data;
  },
  revoke:  async (id: string, reason?: string): Promise<Subscription> => {
    const body: RevokeRequest = { reason };
    return (await client.post<Subscription>(`/api/v1/marketplace/subscriptions/${id}/revoke`, body)).data;
  },
  retry:   async (id: string): Promise<Subscription> => (await client.post<Subscription>(`/api/v1/marketplace/subscriptions/${id}/retry`)).data,
  metrics: async (): Promise<MarketplaceMetrics> => (await client.get<MarketplaceMetrics>(`/api/v1/marketplace/admin/metrics`)).data,
  setListing: async (productType: ProductType, productId: string, body: ListingUpdate): Promise<Listing> =>
    (await client.put<Listing>(`/api/v1/marketplace/listings/${productType}/${productId}`, body)).data,
  createPublishRequest: async (body: PublishRequestCreate): Promise<PublishRequest> =>
    (await client.post<PublishRequest>(`/api/v1/marketplace/publish-requests`, body)).data,
  publishRequests: async (
    status?: NonNullable<operations['list_publish_requests_api_v1_marketplace_publish_requests_get']['parameters']['query']>['status'],
  ): Promise<PublishRequest[]> =>
    (await client.get<PublishRequest[]>(`/api/v1/marketplace/publish-requests`, { params: { status } })).data,
  /** 404 means no request, for a never-published product and a foreign tenant alike. */
  publishRequestForProduct: async (productType: ProductType, productId: string): Promise<PublishRequest> =>
    (await client.get<PublishRequest>(`/api/v1/marketplace/publish-requests/product/${productType}/${productId}`)).data,
  approvePublish: async (id: string): Promise<PublishRequest> =>
    (await client.post<PublishRequest>(`/api/v1/marketplace/publish-requests/${id}/approve`)).data,
  rejectPublish: async (id: string, reason?: string): Promise<PublishRequest> => {
    const body: RejectRequest = { reason };
    return (await client.post<PublishRequest>(`/api/v1/marketplace/publish-requests/${id}/reject`, body)).data;
  },
  unpublishProduct: async (productType: ProductType, productId: string): Promise<void> => {
    await client.post(`/api/v1/marketplace/products/${productType}/${productId}/unpublish`);
  },
};

export const usersAdminApi = {
  list: async (): Promise<PlatformUser[]> => (await client.get<PlatformUser[]>(`/api/v1/admin/users`)).data,
  add: async (body: RoleAssignmentCreate): Promise<PlatformUser> =>
    (await client.post<PlatformUser>(`/api/v1/admin/users`, body)).data,
  changeRole: async (principalId: string, role: RoleChange['role']): Promise<PlatformUser> => {
    const body: RoleChange = { role };
    return (await client.put<PlatformUser>(`/api/v1/admin/users/${principalId}/role`, body)).data;
  },
  remove: async (principalId: string): Promise<void> => { await client.delete(`/api/v1/admin/users/${principalId}`); },
};

export const tenantsAdminApi = {
  list: async (): Promise<TenantView[]> => (await client.get<TenantView[]>(`/api/v1/admin/tenants`)).data,
  create: async (body: TenantCreate): Promise<Tenant> =>
    (await client.post<Tenant>(`/api/v1/admin/tenants`, body)).data,
  update: async (id: string, body: TenantUpdate): Promise<Tenant> =>
    (await client.put<Tenant>(`/api/v1/admin/tenants/${id}`, body)).data,
  remove: async (id: string): Promise<void> => { await client.delete(`/api/v1/admin/tenants/${id}`); },
  /** Re-runs the connect-time probe and writes `capabilities` and `binding_mode` onto the record; 502 means the platform was unreachable. */
  probeTenant: async (id: string): Promise<TenantView> =>
    (await client.post<TenantView>(`/api/v1/admin/tenants/${id}/probe`)).data,
  /** `stage` is required (422 when absent); 400 means the stage is not on the tenant, 502 that the platform was unreachable, and 200 with no agents that it was reached and reports none. */
  discoveredAgents: async (id: string, stage: string): Promise<DiscoveredAgents> =>
    (
      await client.get<DiscoveredAgents>(
        `/api/v1/admin/tenants/${id}/discovered-agents`,
        { params: { stage } },
      )
    ).data,
};

export const connectionsApi = {
  list: async (): Promise<Connection[]> => (await client.get<Connection[]>(`/api/v1/admin/connections`)).data,
  create: async (body: ConnectionCreate): Promise<Connection> =>
    (await client.post<Connection>(`/api/v1/admin/connections`, body)).data,
  test: async (id: string): Promise<Connection> =>
    (await client.post<Connection>(`/api/v1/admin/connections/${id}/test`)).data,
  replaceToken: async (id: string, body: ConnectionTokenReplace): Promise<Connection> =>
    (await client.put<Connection>(`/api/v1/admin/connections/${id}/token`, body)).data,
  remove: async (id: string): Promise<void> => { await client.delete(`/api/v1/admin/connections/${id}`); },
  manifestStart: async (body: ManifestStart): Promise<ManifestStartResponse> =>
    (await client.post<ManifestStartResponse>(`/api/v1/admin/connections/manifest/start`, body)).data,
  manifestCallback: async (body: ManifestCallback): Promise<ManifestCallbackResponse> =>
    (await client.post<ManifestCallbackResponse>(`/api/v1/admin/connections/manifest/callback`, body)).data,
  finalize: async (id: string, body: ConnectionFinalize): Promise<Connection> =>
    (await client.post<Connection>(`/api/v1/admin/connections/${id}/finalize`, body)).data,
  setOauthClient: async (id: string, body: ConnectionOAuthClient): Promise<Connection> =>
    (await client.put<Connection>(`/api/v1/admin/connections/${id}/oauth-client`, body)).data,
};

export const GITHUB_LINK_PATHS = {
  view: '/api/v1/me/github-link',
  start: '/api/v1/me/github-link/start',
  callback: '/api/v1/me/github-link/callback',
  verify: (id: string) => `/api/v1/me/github-link/${id}/verify`,
  unlink: (id: string) => `/api/v1/me/github-link/${id}`,
} as const;

export const githubLinkApi = {
  get: async (): Promise<GitHubLinkView> =>
    (await client.get<GitHubLinkView>(GITHUB_LINK_PATHS.view)).data,
  start: async (body: LinkStartRequest): Promise<LinkStartResponse> =>
    (await client.post<LinkStartResponse>(GITHUB_LINK_PATHS.start, body)).data,
  callback: async (body: LinkCallbackRequest): Promise<GitHubLinkStatus> =>
    (await client.post<GitHubLinkStatus>(GITHUB_LINK_PATHS.callback, body)).data,
  verify: async (connectionId: string): Promise<GitHubLinkStatus> =>
    (await client.post<GitHubLinkStatus>(GITHUB_LINK_PATHS.verify(connectionId))).data,
  unlink: async (connectionId: string): Promise<void> => { await client.delete(GITHUB_LINK_PATHS.unlink(connectionId)); },
};

export const projectsApi = {
  list: async (): Promise<Project[]> => (await client.get<Project[]>(`/api/v1/projects`)).data,
  get: async (id: string): Promise<ProjectDetail> => (await client.get<ProjectDetail>(`/api/v1/projects/${id}`)).data,
  create: async (body: ProjectCreate): Promise<Project> =>
    (await client.post<Project>(`/api/v1/projects`, body)).data,
  /** 202 with the pending record; the steps advance in the background, so poll `getRepoStatus` until every step is terminal. */
  addRepo: async (id: string, body: RepositoryCreate): Promise<Repository> =>
    (await client.post<Repository>(`/api/v1/projects/${id}/repos`, body)).data,
  getRepoStatus: async (projectId: string, repoId: string): Promise<Repository> =>
    (await client.get<Repository>(`/api/v1/projects/${projectId}/repos/${repoId}/status`)).data,
  /** 202 with the failed steps reset to pending; 409 `Nothing to retry` when nothing is retryable, which callers read as already complete. */
  retryRepo: async (projectId: string, repoId: string): Promise<Repository> =>
    (await client.post<Repository>(`/api/v1/projects/${projectId}/repos/${repoId}/retry`)).data,
  /** Builds a config repo's dev stage at its default-branch head: 202 with `cicd_status: "promoting"`, polled through `getRepoStatus` like a promote; 422 for a container repo. */
  buildRepo: async (projectId: string, repoId: string): Promise<Repository> =>
    (await client.post<Repository>(`/api/v1/projects/${projectId}/repos/${repoId}/build`)).data,
  /** Approves the pending prod candidate: 202 with `cicd_status: "promoting"`, polled through `getRepoStatus`; no image in the body, the server resolves `prod_candidate_image_tag` and clears the candidate fields;
   * owner-gated on the strict gate, so `ungoverned` never unlocks it; `override_reason` is the one body field and bypasses the eval gate. */
  promoteRepo: async (projectId: string, repoId: string, overrideReason?: string): Promise<Repository> => {
    const body: RepoPromoteRequest | undefined = overrideReason ? { override_reason: overrideReason } : undefined;
    return (
      await client.post<Repository>(
        `/api/v1/projects/${projectId}/repos/${repoId}/promote`,
        body,
      )
    ).data;
  },
  /** Redeploys an image tag that has a succeeded deployment row for this repo in the given stage (409 otherwise): 202 with `cicd_status: "promoting"`, polled through `getRepoStatus`;
   * `stage` omitted means the server default and a blank stage is a 422; the prod candidate survives; owner-gated on the strict gate like `promoteRepo`. */
  rollbackRepo: async (
    projectId: string,
    repoId: string,
    body: RepoRollbackRequest,
  ): Promise<Repository> =>
    (await client.post<Repository>(
      `/api/v1/projects/${projectId}/repos/${repoId}/rollback`,
      body,
    )).data,
  /** Newest first; `stage` omitted means every stage; keyed by the repo, so an MCP-server repo with no agent still has one. */
  repoDeployments: async (projectId: string, repoId: string, stage?: string): Promise<Deployment[]> =>
    (await client.get<Deployment[]>(`/api/v1/projects/${projectId}/repos/${repoId}/deployments`, {
      params: { stage },
    })).data,
  deleteRepo: async (projectId: string, repoId: string, selection: RepoDeleteSelection): Promise<RepoDeleteResult> =>
    (await client.delete<RepoDeleteResult>(`/api/v1/projects/${projectId}/repos/${repoId}`, { data: selection })).data,
  deletePreview: async (projectId: string, repoId: string): Promise<RepoDeletePreview> =>
    (await client.get<RepoDeletePreview>(`/api/v1/projects/${projectId}/repos/${repoId}/delete-preview`)).data,
  remove: async (projectId: string): Promise<void> => { await client.delete(`/api/v1/projects/${projectId}`); },
};
export const repositoriesApi = {
  list: async (): Promise<Repository[]> => (await client.get<Repository[]>(`/api/v1/repositories`)).data,
};

export const projectRolesApi = {
  list: async (projectId: string): Promise<ProjectRoleRead[]> =>
    (await client.get<ProjectRoleRead[]>(`/api/v1/projects/${projectId}/roles`)).data,
  /** Upserts on (project, principal): granting an existing principal changes their role. */
  grant: async (projectId: string, body: ProjectRoleCreate): Promise<ProjectRoleRead> =>
    (await client.post<ProjectRoleRead>(`/api/v1/projects/${projectId}/roles`, body)).data,
  /** Rewrites the whole row from the body, so callers pass the existing row's values with only `role` changed; the path's principal id is authoritative, and the last-owner guard applies (409). */
  update: async (
    projectId: string,
    principalId: string,
    body: ProjectRoleCreate,
  ): Promise<ProjectRoleRead> =>
    (
      await client.put<ProjectRoleRead>(`/api/v1/projects/${projectId}/roles/${principalId}`, body)
    ).data,
  /** Refuses to strip the last owner (409). */
  revoke: async (projectId: string, principalId: string): Promise<void> => {
    await client.delete(`/api/v1/projects/${projectId}/roles/${principalId}`);
  },
};

export const infraTemplatesApi = {
  /** A registry read with no provider call; a store fault answers 503 rather than an empty list, so callers render the failure. */
  list: async (connectionId: string): Promise<InfraTemplateView[]> =>
    (
      await client.get<InfraTemplateView[]>(`/api/v1/projects/templates/infra`, {
        params: { connection_id: connectionId },
      })
    ).data,
};
export const githubTemplatesApi = {
  list: async (connectionId: string): Promise<TemplateView[]> =>
    (await client.get<TemplateView[]>(`/api/v1/github-templates`, { params: { connection_id: connectionId } })).data,
  upload: async (
    connectionId: string,
    file: File,
    meta: Omit<BodyUploadTemplateApiV1GithubTemplatesPost, 'connection_id' | 'file'>,
  ): Promise<TemplateView> => {
    const formData = new FormData();
    formData.append('connection_id', connectionId);
    formData.append('name', meta.name);
    formData.append('framework', meta.framework);
    formData.append('description', meta.description ?? '');
    (meta.aws_services ?? []).forEach((s) => formData.append('aws_services', s));
    (meta.tags ?? []).forEach((t) => formData.append('tags', t));
    formData.append('file', file);
    return (await client.post<TemplateView>(
      `/api/v1/github-templates`,
      formData,
      { headers: { 'Content-Type': 'multipart/form-data' } }
    )).data;
  },
  patch: async (connectionId: string, name: string, patch: TemplatePatch): Promise<TemplateView> =>
    (await client.patch<TemplateView>(`/api/v1/github-templates/${name}`, patch, { params: { connection_id: connectionId } })).data,
  remove: async (connectionId: string, name: string): Promise<void> => {
    await client.delete(`/api/v1/github-templates/${name}`, { params: { connection_id: connectionId } });
  },
};

export const rolloutApi = {
  reconcile: async (connectionId: string): Promise<ReconcileViewResponse> =>
    (await client.get<ReconcileViewResponse>(`/api/v1/admin/connections/${connectionId}/rollout/reconcile`)).data,
  /** Two separate consents: `overwrite` re-pushes the selected templates, `overwrite_infra` re-pushes the Terraform module into an existing infra repo; creating an absent infra repo needs no flag. */
  rollout: async (connectionId: string, body: RolloutRequest): Promise<RolloutResultResponse> =>
    (await client.post<RolloutResultResponse>(`/api/v1/admin/connections/${connectionId}/rollout`, body)).data,
  /** Registers an existing org repository as a template with no push and no content inspection; 404 when not in the org, 409 when AGP already accounts for it, 422 for an illegal name, classified by `classifyRolloutError`. */
  adopt: async (connectionId: string, body: AdoptRequest): Promise<TemplateView> =>
    (await client.post<TemplateView>(`/api/v1/admin/connections/${connectionId}/templates/adopt`, body)).data,
};

export const observabilityApi = {
  getSettings: async (): Promise<ObservabilitySettings> =>
    (await client.get<ObservabilitySettings>(`/api/v1/observability/settings`)).data,
  getMetrics: async (
    scope: NonNullable<operations['get_scope_metrics_api_v1_observability_metrics_get']['parameters']['query']>['scope'],
    params: { projectId?: string; dateFrom: string; dateTo: string },
  ): Promise<ScopeMetrics> =>
    (await client.get<ScopeMetrics>(`/api/v1/observability/metrics`, {
      params: { scope, project_id: params.projectId, date_from: params.dateFrom, date_to: params.dateTo },
    })).data,
  getAgentMetrics: async (
    agentId: string,
    params: { dateFrom: string; dateTo: string },
  ): Promise<AgentMetrics> =>
    (await client.get<AgentMetrics>(`/api/v1/agents/${agentId}/metrics`, {
      params: { date_from: params.dateFrom, date_to: params.dateTo },
    })).data,
  getAgentTraces: async (
    agentId: string,
    params: { page: number; limit: number },
  ): Promise<TracePage> =>
    (await client.get<TracePage>(`/api/v1/agents/${agentId}/traces`, {
      params: { page: params.page, limit: params.limit },
    })).data,
};

const evalsPath = (projectId: string, repoId: string) => `/api/v1/projects/${projectId}/repos/${repoId}/evals`;
export const evalsApi = {
  get: async (projectId: string, repoId: string): Promise<EvalFiles> =>
    (await client.get<EvalFiles>(evalsPath(projectId, repoId))).data,
  validate: async (projectId: string, repoId: string, body: ValidateRequest): Promise<ValidationResult> =>
    (await client.post<ValidationResult>(`${evalsPath(projectId, repoId)}/validate`, body)).data,
  putConfig: async (projectId: string, repoId: string, config: EvalConfigInput): Promise<HeadSha> =>
    (await client.put<HeadSha>(`${evalsPath(projectId, repoId)}/config`, config)).data,
  addItems: async (
    projectId: string,
    repoId: string,
    items: DatasetItemInput[],
    mode: DatasetRequest['mode'] = 'append',
  ): Promise<DatasetAdded> => {
    const body: DatasetRequest = { items, mode };
    return (await client.post<DatasetAdded>(`${evalsPath(projectId, repoId)}/dataset`, body)).data;
  },
  listRuns: async (projectId: string, repoId: string): Promise<EvalRuns> =>
    (await client.get<EvalRuns>(`${evalsPath(projectId, repoId)}/runs`)).data,
  getRun: async (projectId: string, repoId: string, runId: string): Promise<RunDetail> =>
    (await client.get<RunDetail>(`${evalsPath(projectId, repoId)}/runs/${runId}`)).data,
  startRun: async (projectId: string, repoId: string, trigger: StartRunRequest['trigger'] = 'manual'): Promise<EvalRun | null> => {
    const body: StartRunRequest = { trigger };
    return (await client.post<EvalRun | null>(`${evalsPath(projectId, repoId)}/runs`, body)).data;
  },
  traces: async (
    projectId: string,
    repoId: string,
    opts: operations['list_eval_traces_api_v1_projects__id__repos__repo_id__evals_traces_get']['parameters']['query'] = {},
  ): Promise<TraceList> =>
    (await client.get<TraceList>(`${evalsPath(projectId, repoId)}/traces`, { params: { stage: opts?.stage, limit: opts?.limit } })).data,
};
