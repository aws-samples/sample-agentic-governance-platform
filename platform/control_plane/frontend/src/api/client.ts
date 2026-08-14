import axios, { AxiosError } from 'axios';
import {
  AUTH_401_COUNT_KEY,
  AUTH_401_HALT_MESSAGE,
  AUTH_401_RELOAD_MESSAGE,
  clearsAuth401Suspicion,
  decide401,
  readAttempts,
  safeStorage,
} from './authRetry';
import type {
  ApiError,
  GuardrailTemplate,
  GuardrailTemplateCreate,
  GuardrailPreset,
  GuardrailMetrics,
  ObservabilitySettings,
  AgentMetrics,
  ScopeMetrics,
  TraceRow,
} from '../types';

const API_URL = import.meta.env.VITE_API_URL || 'http://localhost:8000';

const client = axios.create({
  baseURL: API_URL,
  headers: {
    'Content-Type': 'application/json',
  },
});

// Request interceptor to add auth token and user email
client.interceptors.request.use((config) => {
  const token = localStorage.getItem('auth_token');
  if (token) {
    config.headers.Authorization = `Bearer ${token}`;
  }

  // For dev mode: send x-user-email header to simulate different users
  const devUserEmail = localStorage.getItem('dev_user_email');
  if (devUserEmail) {
    config.headers['x-user-email'] = devUserEmail;
  }

  return config;
});

/**
 * The 401 decision for THIS PAGE LOAD, latched on the first 401 (E36/T19 fix round 1, D-1).
 *
 * A burst of parallel 401s is the ORDINARY case here, not an edge one: `Home.tsx` — the default
 * route — fires `Promise.all([agentsApi.list(), mcpServersApi.list()])`, and several other pages
 * have the same shape. Deciding per REQUEST meant the first read 0 and reloaded while the second
 * read the 1 it had just written and HALTED, so one ordinary token expiry emitted the terminal
 * "ask an administrator to check the sign-in configuration" — a false diagnosis — and left the
 * counter at 2 rather than the documented one.
 *
 * Module scope is right here and ONLY here, precisely BECAUSE the reload discards it: this latch
 * bounds one incident WITHIN a page load, while `sessionStorage` — which survives the navigation —
 * is what bounds it ACROSS loads. The two are not redundant, and neither can do the other's job.
 */
let latched401: 'reload' | 'halt' | null = null;

// Response interceptor for error handling
client.interceptors.response.use(
  (response) => {
    // An AUTHENTICATED 2xx retires the 401 suspicion: the backend just accepted this token, which
    // is the only evidence that actually clears it. Keeps the budget per-INCIDENT rather than
    // per-session, so a long session whose token expires twice gets a reload both times.
    //
    // SCOPED TO REQUESTS THAT CARRIED A BEARER (E36/T19 fix round 1, D-3). Clearing on any 2xx
    // made the bound an accident of what is currently wired: `healthApi` below calls a route that
    // takes no principal, so a status indicator or uptime poll on it would clear the counter every
    // tick and restore `401 → reload → 401` forever. The predicate lives in `authRetry.ts` so it
    // is pinned by a test rather than by this arm, which the DOM-less lane cannot import.
    //
    // THROUGH `safeStorage` (E36 final review, F1 / D-5). A raw storage call here is the worst
    // placement of one in the app: this arm runs on every authenticated 2xx, so a blocked-storage
    // `SecurityError` would propagate out of the SUCCESS interceptor and reject every otherwise-
    // fine response app-wide. The wrapper cannot throw, so the counter degrades and the response
    // does not.
    if (clearsAuth401Suspicion(response.config.headers?.Authorization)) {
      safeStorage.remove(AUTH_401_COUNT_KEY);
    }
    return response;
  },
  (error: AxiosError<ApiError>) => {
    // Handle 401 Unauthorized - token expired or invalid.
    //
    // BOUNDED SINCE E36/T19. This used to reload on EVERY 401, which recovers expiry and
    // nothing else: on a structural 401 (mismatched audience, roles on the wrong app
    // registration, the wrong scope baked into the bundle) the re-acquired token is rejected
    // identically and the tab loops 401 → reload → 401, never settling long enough to render
    // the error. The budget is one reload per incident and the decision lives in
    // `authRetry.ts`, where it is testable without a DOM; the `window` call stays here and the
    // storage calls go through that module's `safeStorage`.
    if (error.response?.status === 401) {
      // ONE DECISION PER PAGE LOAD, and the side effects with it: every other 401 of the same
      // burst reuses the latched answer instead of reading a counter its own sibling just wrote.
      if (latched401 === null) {
        const attempts = readAttempts(safeStorage.read(AUTH_401_COUNT_KEY));
        latched401 = decide401(attempts);
        if (latched401 === 'reload') {
          // Written BEFORE the reload — the navigation below discards everything else, which is
          // why a module-scope latch cannot do this job on its own. Deliberately NOT incremented
          // on the halt path: this is a decision input, not a tally, and any value at or above
          // one already halts — so the count lands on the documented one.
          //
          // Both storage calls go through `safeStorage` (F1): a throw in this arm would replace
          // the 401 rejection with an unrelated storage error and lose the diagnostic, which is
          // the same reason `decide401` is total.
          safeStorage.write(AUTH_401_COUNT_KEY, String(attempts + 1));
          // Clear auth token and reload to trigger SignIn
          localStorage.removeItem('auth_token');
          window.location.reload();
        }
      }
      if (latched401 === 'halt') {
        // Terminal: no reload, and `auth_token` is deliberately KEPT. Clearing it would flip
        // the app to a signed-out state, which asserts "your session ended" — a different and
        // false claim about a token the provider issued and the backend refuses. The message
        // is the whole surface, on this interceptor's existing idiom (every consumer reads
        // `err.message`).
        return Promise.reject(new Error(AUTH_401_HALT_MESSAGE));
      }
      return Promise.reject(new Error(AUTH_401_RELOAD_MESSAGE));
    }
    const errorMessage = error.response?.data?.detail || error.message;
    return Promise.reject(new Error(errorMessage));
  }
);

// Health API
export const healthApi = {
  check: async () => {
    const response = await client.get('/health');
    return response.data;
  },

  ping: async () => {
    const response = await client.get('/ping');
    return response.data;
  },
};

// User API
export const userApi = {
  // `tenants` = the caller's resolved tenant memberships (E24/T4); the backend
  // degrades to [] on a failed resolve, so it is always present.
  getCurrentUser: async (): Promise<{ email: string; role: string; role_level: number; can_deploy: boolean; oid: string | null; name: string | null; tenants: UserTenant[] }> => {
    const response = await client.get('/api/v1/users/me');
    return response.data;
  },
};

export default client;

// Guardrails API
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

  update: async (templateId: string, data: Partial<GuardrailTemplateCreate>): Promise<GuardrailTemplate> => {
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

// Agent Registry API (Epic 4) -----------------------------------------------

export type LifecycleState = 'proposed' | 'pending_approval' | 'approved' | 'rejected' | 'deprecated';
export type Platform = 'aws_bedrock' | 'azure' | 'salesforce' | 'sap' | 'databricks' | 'google' | 'on_prem' | 'other';
export type DataClassification = 'Public' | 'Internal' | 'Confidential' | 'Restricted';
export type Origin = 'Deployed' | 'Registered';
export type AuthType = 'none' | 'entra' | 'api_key';

export interface Agent {
  id: string;
  name: string;
  purpose?: string;
  sponsor_oid?: string | null;
  sponsor_email?: string | null;
  business_unit?: string | null;
  region?: string | null;
  data_classification?: DataClassification | null;
  platform?: Platform | null;
  framework?: string | null;
  mcp_server_ids: string[];
  origin: Origin;
  entra_app_id?: string | null;
  entra_api_app_id?: string | null;
  entra_sp_id?: string | null;
  entra_app_audience?: string | null;
  invoker_role_id?: string | null;
  admin_role_id?: string | null;
  identity_status: 'none' | 'pending' | 'provisioned' | 'failed';
  lifecycle_state: LifecycleState;
  // Invocation info (Epic 4b) — how callers reach this agent.
  endpoint_url?: string | null;
  auth_type: AuthType;
  agent_arn?: string | null;
  /**
   * Every runtime the agent owns, as `stage -> ARN` (E28A/T1, contract C-A2). The backend has
   * serialized this since E28A; it was missing from this type until E36/T2, which is why the
   * invoke panel could not offer a stage. `agent_arn` KEEPS its own meaning ("whichever stage
   * deployed last") and is NOT a duplicate to be filtered out.
   *
   * ABSENT/EMPTY is a LEGACY RECORD (pre-E28A, or an agent whose next deploy has not run under
   * T1b's buildspec), NOT an error — such a record owns one runtime nobody can attribute to a
   * stage, so a caller must not caption it with one. Callers that offer a stage choice therefore
   * key off THIS map only, never off `agent_arn`.
   */
  agent_arns?: Record<string, string> | null;
  // Databricks-hosted runtimes — the READ half of contract C-4 (E29/T5 landed the backend
  // model; T9 surfaces it). All optional: a pre-E29 record, and every AgentCore record,
  // hydrates them as null. `ENVELOPE_SCHEMA_VERSION` stays 1 — these are additive keys.
  //
  // The WRITE half on `AgentCreate` is deliberately NARROWER (two of six — see the contract
  // note there). Here all six are readable because the governance surfaces have to be able to
  // TELL THE TRUTH about a record they did not write: `runtime_handle` is the App URL the
  // detail page labels (never as `agent_arn`, which stays AgentCore-only across the stack),
  // and `binding_mode` is what the detail badge reports.
  //
  // ⚠ `binding_mode` here is a REPORTING copy, not an authorization input. It is copied from
  // the tenant at provisioning time, and the invoke path deliberately re-reads the mode from
  // the TENANT rather than trusting this field. Read it to badge; never to decide.
  //
  // Typed as a plain `string` rather than `TenantBindingMode`, deliberately: this is the field
  // most likely to hold a LEGACY word. Records provisioned before E29/T14 carry `'sp_secret'`
  // (see the note on `TenantBindingMode`), and a narrower type here would only lie about what
  // the wire delivers. `bindingModeBadge` is the one reader, and it answers `null` for anything
  // this build does not recognise.
  runtime_handle?: string | null;
  runtime_kind?: string | null;
  binding_mode?: string | null;
  databricks_sp_id?: string | null;
  // The ARN only — a Secrets Manager pointer. The secret itself never travels on a read.
  databricks_sp_secret_arn?: string | null;
  oauth2_app_client_id?: string | null;
  /**
   * The agent's own Langfuse project id, or null when none was provisioned (E26; DECLARED here
   * by E29/T11).
   *
   * ALREADY ON THE WIRE — this line adds no backend field. `models/agent.py::to_envelope` has
   * carried it since E26 and the route's `response_model` returns it; the frontend type simply
   * never named it, so the one fact AGP knows about an agent's trace destination was
   * unreachable from any component without an `as` cast. Additive and optional: a record
   * registered while Langfuse was disabled (provisioning is deliberately best-effort — an
   * outage must not fail a registration) hydrates it null.
   *
   * A PRESENCE FLAG, NOT A TRACE COUNT. Its value answers "does a project exist", which is a
   * record fact; whether anything has landed in that project is a Langfuse query, and the
   * Traces tab is where that is already asked against the real data. `agentObservability` in
   * `components/governance/platformLabels.ts` is the only reader and states exactly that much.
   *
   * `langfuse_key_secret_name` is deliberately NOT declared: it is a Secrets Manager pointer
   * with no display use, and a name no surface should be teaching an operator to look up.
   */
  langfuse_project_id?: string | null;
  // Multi-tenancy (E24): owning tenant + the cross-tenant publish flag.
  // Pre-E24 records hydrate tenant_id as null and published as false.
  tenant_id?: string | null;
  published?: boolean;
  // Marketplace publication (E33) — READ-ONLY here. Written by the backend ONLY when an
  // admin approves a publish request, so it is absent from `AgentCreate`/the update body
  // and no client may forge it (`declared_by` comes from the approving principal). An
  // unpublish keeps the block with `published: false`, retaining the declared history.
  // NOT the E24 `published` flag above — that one is cross-tenant visibility.
  // `MarketplacePublicationBlock` is defined with the marketplace types further down this file.
  marketplace?: MarketplacePublicationBlock | null;
  created_at: string;
  updated_at: string;
  created_by?: string | null;
}

export interface AgentCreate {
  name: string;
  purpose?: string;
  sponsor_oid?: string;
  sponsor_email?: string;
  business_unit?: string;
  region?: string;
  data_classification?: DataClassification;
  platform?: Platform;
  framework?: string;
  mcp_server_ids?: string[];
  origin?: Origin;
  // Invocation info (Epic 4b).
  endpoint_url?: string;
  auth_type?: AuthType;
  agent_arn?: string;
  // Databricks-hosted runtimes (E29/T5, contract C-4). Exactly TWO of C-4's six fields are
  // settable here, and the split is a contract rather than an omission:
  //
  //   • These two are DESCRIPTIVE facts the caller legitimately supplies — discovery (C-3)
  //     lists the tenant's apps and the wizard posts back the `runtime_handle` the PLATFORM
  //     reported, plus its `kind`. Same category as `agent_arn`, which is likewise
  //     caller-supplied.
  //   • `binding_mode`, `databricks_sp_id`, `databricks_sp_secret_arn` and
  //     `oauth2_app_client_id` are deliberately ABSENT. The backend model accepts them, but a
  //     client must not send them: `binding_mode` is COMPUTED by the tenant probe and copied
  //     onto the agent at provisioning, and the invoke path explicitly re-reads it from the
  //     TENANT because trusting the agent's copy would let a caller pick the weaker credential
  //     path on a federation tenant. The other three are service-written (Secrets Manager
  //     ARNs, Entra ids). Sending any of them would be a client asserting a fact it cannot
  //     know — the same rule that keeps `sp_client_secret_arn` echoed-not-invented in
  //     `tenantsAdminForm`.
  //
  // `agent_arn` stays AgentCore-ONLY and is never reused as a generic handle: the delete
  // cascade and the runtime-status probe parse it as a Bedrock ARN.
  runtime_handle?: string;
  runtime_kind?: string;
  // REQUIRED by the backend since E24 — every new agent belongs to one tenant.
  tenant_id: string;
}

export interface AgentListFilters {
  lifecycle_state?: LifecycleState;
  sponsor_oid?: string;
  business_unit?: string;
  region?: string;
  platform?: Platform;
}

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
  update: async (id: string, data: Partial<AgentCreate>): Promise<Agent> => {
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
    const response = await client.post<Agent>(`/api/v1/agents/${id}/transitions`, { action, reason });
    return response.data;
  },
  // Cross-tenant publish flag (E24/T5) — OPERATOR+, visibility-gated 404.
  publish: async (id: string, published: boolean): Promise<Agent> =>
    (await client.put<Agent>(`/api/v1/agents/${id}/publish`, { published })).data,
};

// --- Runtime status + deployment history (Epic 28, contract C2) -------------
// The two per-agent OPERATIONS reads behind the repository detail page. Both are
// VIEWER-gated and tenant-scoped server-side, on the byte-identical gate the per-agent
// `/metrics` and `/traces` reads use (a foreign or unknown agent gets the same
// "Agent not found" 404 — never a 403 that would confirm it exists).

/**
 * `GET /agents/{agent_id}/runtime` (E28/T5, D9).
 *
 * Structurally identical to `RuntimeStatus` in `components/operations/opsStatus.ts`, which is
 * where the closed unions and the narrowing functions live. NEITHER side imports the other,
 * on purpose: `opsStatus.ts` must stay pure (vitest collects only `.ts`, and this module pulls
 * in axios), and structural typing means the two assign to each other without a dependency.
 * That is a tolerated ONE-time duplication of a shape, not of a decision — there is no table,
 * no default and no judgement here. **Change one, change the other.**
 *
 * `status` is typed `string`, deliberately, and every consumer must pass it through
 * `toRuntimeStatus` before rendering. Typing it as the union here would ASSERT the narrowing
 * instead of performing it — the mistake `opsStatus.ts` exists to stop.
 *
 * `stage` IS EVIDENCE, NOT DECORATION — and since E28A it is often real evidence. An agent owns
 * one runtime PER STAGE and the record names them, so the route reports the stage it actually
 * probed whenever it can attribute one; a LEGACY scalar-only record answers the unattributable
 * sentinel, because its single runtime genuinely cannot be assigned to a stage.
 *
 * SO THE RULE IS NOT "never caption a pill with it" — it is "never caption a pill with it
 * UNATTRIBUTED", and that judgement is not made here or in any `.tsx`. `runtimeScope` decides
 * whether a reading is attributable at all, `stageRuntimeCell` decides whether it may sit on a
 * given stage's row (both in `components/operations/repositoryDetailTabs.ts`, both selector-
 * tested). A caller that reads this field and renders a caption directly has re-derived that
 * decision, which is the fabrication those two functions exist to make unreachable.
 */
export interface RuntimeStatus {
  agent_id: string;
  /**
   * The stage this reading DESCRIBES, or the unattributable sentinel — see above. Never
   * rendered as a caption without going through `runtimeScope` / `stageRuntimeCell`.
   */
  stage: string;
  /** A bare wire value: `toRuntimeStatus` it before use. Absent ⇒ unknown, never ready. */
  status: string;
  runtime_arn?: string | null;
  image_tag?: string | null;
  checked_at: string;
  /** A SAFE short hint only — never an ARN, a token or a response body. */
  detail?: string | null;
}

/** An append-only deployment row's terminal state (C1). Nothing ever updates a row. */
export type DeploymentOutcome = 'started' | 'succeeded' | 'failed';

/**
 * `GET /agents/{agent_id}/deployments?stage=&limit=` (E28, D7 / contract C1).
 *
 * One attempt to put one image tag onto one stage of one repo. APPEND-ONLY: a new attempt is
 * a new row, which is what makes history and rollback possible at all — the `last_promoted_*`
 * scalars on `Repository` are overwritten wholesale, so one promote used to erase the evidence
 * of the previous one. Those scalars stay as a denormalized "latest" cache for the list row.
 *
 * Mirrors `backend/src/models/deployment.py` field-for-field.
 *
 * THREE PROPERTIES OF THE DATA THAT A RENDERER MUST NOT ASSUME AWAY:
 *   • `stage` is FREE-FORM (D8) and is never validated against a dev/prod literal.
 *   • A BUILD-WRITTEN terminal row carries NO `actor`/`actor_kind` and no `source_sha`, and
 *     sets `started_at` to the COMPLETION time — so a duration derived from it is 0. Render a
 *     missing actor as ABSENT, never as "unknown user".
 *   • A terminal row can exist with NO matching `started` row (the build ran while AGP was
 *     unreachable). A renderer that pairs them up would silently drop it.
 */
export interface Deployment {
  id: string;                       // "dep-<8 hex>"
  repo_id: string;
  agent_id: string;
  /** Free-form (D8) — a tenant's stage set is open. Never compare against a literal. */
  stage: string;
  seq_key: string;                  // the DDB sk, mirrored for round-tripping
  image_tag: string;
  source_sha?: string | null;
  build_id?: string | null;         // CodeBuild id
  outcome: DeploymentOutcome;
  /**
   * The OIDC-proven GitHub login, or an Entra oid for an AGP promote — WHICH ONE is
   * `actor_kind`'s job to say. A GitHub login and an Entra oid are two different currencies
   * and must never be rendered as one (E27A §6), so every display of this field branches on
   * `actor_kind`: `"github"` → `@login`; `"entra"` → the oid, as a platform id.
   */
  actor?: string | null;
  actor_kind?: string | null;       // "github" | "entra"
  started_at: string;               // ISO-8601 UTC
  completed_at?: string | null;
  /** A SAFE short hint only — never a token or a response body. */
  error?: string | null;
}

export const agentOpsApi = {
  // Absent/failed ⇒ the CALLER renders unknown (`toRuntimeStatus(undefined)`), never ready.
  //
  // `stage` IS OPTIONAL AND ADDITIVE (E28C/T7, D-C4c), mirroring `deployments` below so the two
  // sibling reads take the same parameter. Omitted ⇒ the byte-for-byte pre-E28A call: the route
  // probes the runtime the `agent_arn` scalar names ("whichever stage deployed last") and reports
  // which stage that was only when the record can attribute it. Given a stage, it probes THAT
  // stage's runtime, and a stage the agent owns no runtime for answers not-deployed WITHOUT an
  // AWS call — never another stage's reading dressed as an answer to the question asked.
  //
  // The parameter has existed on the backend since E28A (`agents.py:695-731`) and no caller
  // passed it, which is why every stage row on the repository page showed the same
  // not-attributable note. Free-form (D8): a tenant's stage set is open, so this is never
  // validated against a conventional stage name — the caller passes the tenant's own key.
  runtime: async (agentId: string, params?: { stage?: string }): Promise<RuntimeStatus> =>
    (await client.get<RuntimeStatus>(`/api/v1/agents/${agentId}/runtime`, {
      params: { stage: params?.stage },
    })).data,
  // Newest-first. `stage` omitted ⇒ every stage, merged chronologically server-side. There is
  // deliberately NO `repo_id` param: the route resolves the repo from the agent, because
  // accepting one would let a caller read another repo's history under an agent they can see.
  // An agent that owns no repository answers `[]` — a state, not an error.
  deployments: async (
    agentId: string,
    params?: { stage?: string; limit?: number },
  ): Promise<Deployment[]> =>
    (await client.get<Deployment[]>(`/api/v1/agents/${agentId}/deployments`, {
      params: { stage: params?.stage, limit: params?.limit },
    })).data,
};

// --- Pull requests on a repository (Epic 28, contract C2 / D14+D15) ---------
// The routes these four call are built by E28/T14; the CLIENT calls live here because T14 may
// not edit this file. **They 404 until T14 lands** — that is expected and correct, and no UI
// shipped before T14 calls them.
//
// Error literals surface via the response interceptor as `err.message`, per the C2 pins.

export interface PullRequestView {
  number: number;
  title: string;
  state: string;                    // "open" | "closed" | "merged"
  /** A GITHUB login. Not an AGP principal, and never joined to an Entra oid (design §6). */
  author: string;
  head_sha: string;
  url: string;
  /**
   * FALSE when the linked human IS the author (D15) — a self-approval is not an approval.
   * A capability the caller does not hold is ABSENT from the UI, not disabled, so this
   * decides whether the affordance renders at all.
   */
  can_approve: boolean;
  /** Why `can_approve` is false. Present so the refusal can be stated rather than implied. */
  approve_blocked_reason?: string | null;
  /** `null` when the provider has not computed mergeability yet — NOT the same as `false`. */
  mergeable?: boolean | null;
}

export const pullRequestsApi = {
  list: async (repoId: string): Promise<PullRequestView[]> =>
    (await client.get<PullRequestView[]>(`/api/v1/repositories/${repoId}/pull-requests`)).data,
  // 201.
  create: async (
    repoId: string,
    body: { title: string; head: string; base?: string; body?: string },
  ): Promise<PullRequestView> =>
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

// User → Agent access via Microsoft Entra (Epic 6) --------------------------
// Grants are live Entra app-role assignments (no DynamoDB). `principal_type` in
// the READ shape is capitalized ('User' | 'Group', Graph's principalType) while
// the add BODY uses lowercase ('user' | 'group') — this asymmetry is intentional
// and matches the backend contract.

export interface Grant { assignment_id: string; principal_id: string; principal_display: string; principal_type: 'User' | 'Group' | 'ServicePrincipal'; role: 'Invoker' | 'Admin'; }
export interface PrincipalHit { id: string; display_name: string; type: 'user' | 'group' | 'agent'; mail?: string | null; }

// Platform-ACL drift (E29/T13, design §3A) ----------------------------------
// On a Databricks-governed agent the Entra app-role assignment is the TRUTH and the app's
// per-user `CAN_USE` entry is its one-way mirror. Drift is the diff, in both directions:
//
//   • `unauthorized_acl` — the platform ACL grants access AGP never granted (a workspace
//     owner hand-edited around the platform). Fails OPEN.
//   • `missing_acl` — an AGP grant is not on the platform ACL (a half-completed grant or
//     revoke, or an admin removed the entry). Fails CLOSED.
//
// `level` is the platform's own permission word (e.g. 'CAN_USE'), passed through rather than
// translated: it is Databricks' vocabulary and AGP does not own it.
export type AccessDriftDirection = 'unauthorized_acl' | 'missing_acl';
export interface AccessDriftEntry {
  principal: string;
  kind: 'user' | 'group' | 'service_principal';
  level: string;
  direction: AccessDriftDirection;
}
export interface AccessDrift { entries: AccessDriftEntry[] }

export const grantsApi = {
  list: async (agentId: string): Promise<Grant[]> => (await client.get<Grant[]>(`/api/v1/agents/${agentId}/grants`)).data,
  add: async (agentId: string, body: { principal_id: string; principal_type: 'user' | 'group'; role: 'Invoker' | 'Admin' }): Promise<Grant> => (await client.post<Grant>(`/api/v1/agents/${agentId}/grants`, body)).data,
  remove: async (agentId: string, assignmentId: string): Promise<void> => { await client.delete(`/api/v1/agents/${agentId}/grants/${assignmentId}`); },
  // Databricks-governed agents only (the routes 404/409 elsewhere — the Access tab gates the
  // call on the same platform fact, so that never fires from the UI).
  drift: async (agentId: string): Promise<AccessDrift> => (await client.get<AccessDrift>(`/api/v1/agents/${agentId}/grants/drift`)).data,
  // Rewrites the platform ACL from AGP's grants and answers with FRESH drift — empty entries
  // means the re-assert converged, so the response is the new state, not an ack.
  reassert: async (agentId: string): Promise<AccessDrift> => (await client.post<AccessDrift>(`/api/v1/agents/${agentId}/grants/reassert`)).data,
};
export const principalsApi = {
  search: async (q: string): Promise<PrincipalHit[]> => (await client.get<PrincipalHit[]>(`/api/v1/entra/principals/search`, { params: { q } })).data,
};
export const invokeApi = {
  // `stage` IS OPTIONAL AND ADDITIVE (E36/T2), on `agentOpsApi.runtime`'s idiom above so the
  // invoke call and the status read take the same parameter. Given a stage, THAT stage's runtime
  // is invoked; a stage the agent owns no runtime for answers 404 `unknown stage` rather than
  // falling through to another stage's runtime. Omitted ⇒ the pre-E36 call byte for byte: the
  // runtime `agent_arn` names, i.e. whichever stage deployed last — which is exactly the
  // ambiguity a caller with more than one stage should not leave to chance.
  // Free-form (D8): a tenant's stage set is open, so the caller passes the agent's own map key.
  invoke: async (
    agentId: string,
    prompt: string,
    params?: { stage?: string },
  ): Promise<{ response: unknown }> =>
    (await client.post(`/api/v1/agents/${agentId}/invoke`, { prompt }, {
      params: { stage: params?.stage },
    })).data,
};

// MCP Server Catalog API (Epic 5) -------------------------------------------
// Parallel to the Agent Registry section above. MCP servers are MCP-type AWS
// Agent Registry records; `kind` ('gateway' | 'standard') is the discriminator
// that gates the (stubbed) Policies tab. Reuses the agent LifecycleState /
// DataClassification types — do not redefine them.

export type McpServerKind = 'gateway' | 'standard' | 'runtime';

export interface McpTool {
  name: string;
  description?: string;
  input_schema: Record<string, unknown>;
}

// Cedar per-tool authorization (Epic 8) -------------------------------------
// Declared above McpServer because the interface references CedarEnforcementMode
// in its service-written cedar engine block below.
export type CedarEnforcementMode = 'none' | 'log_only' | 'enforce';

// Conditional Cedar policies (Epic 10) — parameter conditions + explicit Deny.
export type CedarEffect = 'allow' | 'deny';

export interface CedarCondition {
  param: string;
  op: string;          // '=' | '!=' | '<' | '<=' | '>' | '>='
  value: string;
  type: string;        // 'number' | 'string'
}

export interface CedarPolicyRow {
  policy_id: string;
  user_oid?: string | null;
  user_label?: string | null;
  tool?: string | null;       // null === "All tools" (or a foreign policy)
  effect: string;             // "allow" | "deny"
  conditions?: CedarCondition[];
  managed?: boolean;
  cedar_text: string;
}
export interface CedarPolicySet {
  enforcement_mode: CedarEnforcementMode;
  engine_id?: string | null;
  policies: CedarPolicyRow[];
}

export interface McpServer {
  id: string;
  name: string;
  description?: string;
  kind: McpServerKind;
  owner_oid?: string | null;
  owner_email?: string | null;
  business_unit?: string | null;
  region?: string | null;
  data_classification?: DataClassification | null;
  endpoint_url?: string | null;
  version: string;
  available_tools: McpTool[];
  gateway_arn?: string | null;
  // Cedar Policy Engine fields (Epic 8) — SERVICE-WRITTEN, gateway-only.
  cedar_policy_engine_id?: string | null;
  cedar_policy_engine_arn?: string | null;
  cedar_enforcement_mode?: CedarEnforcementMode;
  entra_app_id?: string | null;
  // Identity fields (Epic 7) — mirror the Agent identity block.
  entra_sp_id?: string | null;
  entra_app_audience?: string | null;
  invoker_role_id?: string | null;
  admin_role_id?: string | null;
  gateway_id?: string | null;
  gateway_url?: string | null;
  runtime_arn?: string | null;
  identity_status: 'none' | 'pending' | 'provisioned' | 'failed';
  lifecycle_state: LifecycleState;
  // Multi-tenancy (E24): owning tenant, the cross-tenant publish flag, and the
  // ADMIN-only platform-shared flag (visible to every tenant). Pre-E24 records
  // hydrate tenant_id as null and published/shared as false.
  tenant_id?: string | null;
  published?: boolean;
  shared?: boolean;
  // Marketplace publication (E33 Amendment 1 / C8) — READ-ONLY here, exactly like
  // `Agent.marketplace`: the backend writes it ONLY when an admin approves a publish request,
  // so it is absent from `McpServerCreate`/the update body and no client may forge it. An
  // unpublish keeps the block with `published: false`, retaining the declared history.
  // NOT the E24 `published`/`shared` flags above — those are cross-tenant visibility.
  marketplace?: MarketplacePublicationBlock | null;
  created_at: string;
  updated_at: string;
  created_by?: string | null;
}

export interface McpServerCreate {
  name: string;
  description?: string;
  kind?: McpServerKind;
  owner_oid?: string;
  owner_email?: string;
  business_unit?: string;
  region?: string;
  data_classification?: DataClassification;
  endpoint_url?: string;
  gateway_arn?: string;
  runtime_arn?: string;
  version?: string;
  available_tools?: McpTool[];
  // REQUIRED by the backend since E24 — every new MCP server belongs to one
  // tenant. `shared` is settable only by ADMIN (403 otherwise) so the wizard
  // omits it; only the admin-only detail control writes it.
  tenant_id: string;
  shared?: boolean;
}

export interface McpServerListFilters {
  lifecycle_state?: LifecycleState;
  kind?: McpServerKind;
  owner_oid?: string;
  business_unit?: string;
  region?: string;
}

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
  update: async (id: string, data: Partial<McpServerCreate>): Promise<McpServer> => {
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
    const response = await client.post<McpServer>(`/api/v1/mcp-servers/${id}/transitions`, { action, reason });
    return response.data;
  },
  // Gateway-only (Epic 7, T-REFRESH-TOOLS): re-read the gateway's tools natively
  // and return the updated McpServer synchronously (fresh `available_tools` in
  // the response). 409 for non-gateway; best-effort (never wipes on a transient
  // empty read).
  refreshTools: async (id: string): Promise<McpServer> =>
    (await client.post<McpServer>(`/api/v1/mcp-servers/${id}/refresh-tools`)).data,
  // Cross-tenant publish flag (E24/T5) — OPERATOR+, visibility-gated 404.
  publish: async (id: string, published: boolean): Promise<McpServer> =>
    (await client.put<McpServer>(`/api/v1/mcp-servers/${id}/publish`, { published })).data,
};

// Agent → MCP grants via Microsoft Entra (Epic 7) ---------------------------
// Parallel to grantsApi (the user→agent grants above), pointed at the MCP SP.
// Reuses the Grant interface. The add BODY's principal_type is 'agent' (display
// only; Graph assigns identically) and the READ shape returns Graph's
// principalType ('ServicePrincipal' for an agent). Agents are sourced via
// agentsApi.list() — there is no separate principals endpoint here.
export const mcpGrantsApi = {
  list: async (mcpId: string): Promise<Grant[]> => (await client.get<Grant[]>(`/api/v1/mcp-servers/${mcpId}/grants`)).data,
  add: async (mcpId: string, body: { principal_id: string; principal_type: 'agent'; role: 'Invoker' | 'Admin' }): Promise<Grant> => (await client.post<Grant>(`/api/v1/mcp-servers/${mcpId}/grants`, body)).data,
  remove: async (mcpId: string, assignmentId: string): Promise<void> => { await client.delete(`/api/v1/mcp-servers/${mcpId}/grants/${assignmentId}`); },
};

// Cedar per-tool authorization on a gateway MCP (Epic 8) --------------------
// Gateway-only. `list` returns the enforcement mode + the friendly policy rows
// (the backend reads them from the native AgentCore Policy Engine — no local
// mirror). `add` POSTs a picked user oid + tool selection → a generated Cedar
// permit policy (first policy creates+attaches the engine in ENFORCE).
// `setEnforcement` flips the gateway mode; 'disabled' detaches the engine.
export const cedarPoliciesApi = {
  list: async (mcpId: string): Promise<CedarPolicySet> =>
    (await client.get<CedarPolicySet>(`/api/v1/mcp-servers/${mcpId}/policies`)).data,
  add: async (mcpId: string, body: { principal_oid?: string | null; principal_label: string; tool_name?: string | null; all_tools?: boolean; effect?: CedarEffect; conditions?: CedarCondition[] }): Promise<CedarPolicyRow> =>
    (await client.post<CedarPolicyRow>(`/api/v1/mcp-servers/${mcpId}/policies`, body)).data,
  remove: async (mcpId: string, policyId: string): Promise<void> => { await client.delete(`/api/v1/mcp-servers/${mcpId}/policies/${policyId}`); },
  setEnforcement: async (mcpId: string, mode: CedarEnforcementMode | 'disabled'): Promise<{ enforcement_mode: CedarEnforcementMode }> =>
    (await client.put<{ enforcement_mode: CedarEnforcementMode }>(`/api/v1/mcp-servers/${mcpId}/policy-enforcement`, { mode })).data,
};

// Governance Graph API (Epic 11) -------------------------------------------
// Read-only node-link graph of Users/Groups→Agents and Agents→MCP access from
// Microsoft Entra. Node `id` is "<type>:<entity-id>" (globally unique across
// types — React Flow requires it); `ref_id` carries the bare entity id for
// detail-page links. `has_policy` (agent→MCP edges) means the target MCP gateway
// has Cedar enforcement on — NOT that a policy targets the specific agent. The
// principal lookup is a lazy Entra by-oid read for a clicked user/group node.
export interface GraphNode {
  type: 'user' | 'group' | 'agent' | 'mcp';
  id: string;
  label: string;
  ref_id: string;
  metadata: Record<string, unknown>;
}
export interface GraphEdge {
  id: string;
  source: string;
  target: string;
  type: 'access' | 'can_call';
  role: string;
  has_policy?: boolean;
}
export interface GovernanceGraph { nodes: GraphNode[]; edges: GraphEdge[]; }
export interface PrincipalDetail {
  id: string; display_name: string; kind: 'user' | 'group';
  user_principal_name?: string | null; mail?: string | null;
  job_title?: string | null; group_names?: string[];
}
export const governanceGraphApi = {
  get: async (): Promise<GovernanceGraph> =>
    (await client.get<GovernanceGraph>('/api/v1/governance-graph')).data,
  principal: async (oid: string, kind: 'user' | 'group'): Promise<PrincipalDetail> =>
    (await client.get<PrincipalDetail>(`/api/v1/governance-graph/principals/${oid}`, { params: { kind } })).data,
};

// Agent-direction reverse read (Epic 7, T-ROUTES' agent_mcp_router) ----------
// "Which MCP servers can this agent reach?" — the inverse of mcpGrantsApi.list
// (which lists the agents granted on one MCP). The list lives on the /agents
// router (GET /api/v1/agents/{id}/mcp-grants); each row's assignment lives on the
// MCP's SP, so a revoke goes through mcpGrantsApi.remove(mcp_id, assignment_id)
// (the DELETE on the /mcp-servers route) — there is no agent-side revoke route.
// Field names match the backend AgentMcpGrant pydantic model exactly.
export interface AgentMcpGrant {
  mcp_id: string;
  mcp_name: string;
  role: string;       // "Invoker" | "Admin" | "Unknown" (backend is tolerant of foreign role ids)
  assignment_id: string;
}
export const agentMcpGrantsApi = {
  list: async (agentId: string): Promise<AgentMcpGrant[]> =>
    (await client.get<AgentMcpGrant[]>(`/api/v1/agents/${agentId}/mcp-grants`)).data,
};

// Marketplace API (Epic 9) --------------------------------------------------
// Consumer-facing discovery + subscribe for agent blueprints and gateway-MCP
// servers, with an admin approve/reject/retry panel. On MCP approval the backend
// applies the real, live E7 agent→MCP grant. ProductCard is the read-model
// (catalog ⊕ listing ⊕ the caller's own subscription state). The axios client
// auto-injects the bearer; errors surface via the interceptor as Error.message.
export type MarketplaceProductType = 'agent' | 'mcp';
export type MarketplaceStatus = 'pending' | 'approved' | 'rejected' | 'failed' | 'revoked';

export interface ProductCard {
  product_type: MarketplaceProductType; product_id: string; name: string;
  pitch?: string | null; category?: string | null; capabilities: string[]; icon?: string | null;
  available: boolean; auto_approve: boolean;
  kind?: string | null; owner_email?: string | null; business_unit?: string | null;
  // MCP governance metadata (F3) — surfaced as chips; mirror the backend ProductCard.
  data_classification?: string | null; region?: string | null; version?: string | null;
  created_at?: string | null; updated_at?: string | null;
  // Multi-tenancy (E24) — tenant_name is resolved display-only by the backend
  // (null when unresolvable); published/shared mirror the registry record.
  tenant_id?: string | null; tenant_name?: string | null;
  published?: boolean; shared?: boolean;
  // Agent "mesh product" datasheet (E33) — DECLARED values only: every field here is
  // publisher-asserted in the approved publish request, never measured. The old
  // `uptime_30d`/`latency_p95_ms`/`rating`/`rating_count`/`status` fields are GONE because
  // nothing measures them; do not reintroduce them without a real telemetry source.
  // All optional so existing call sites compile. `consumers` is computed LIVE.
  // `support_contact`/`declared_at` are typed per contract C6 (no `| null`): the card
  // projection omits them entirely when the datasheet has no value, so a consumer sees
  // `undefined`, not null.
  owner_team?: string | null; support_contact?: string;
  sla_tier?: string | null; support_hours?: string | null; lifecycle?: string | null;
  declared_at?: string;
  consumers?: number | null;
  compliance?: string[] | null; guardrails?: string[] | null;
  my_status?: MarketplaceStatus | null; my_subscription_id?: string | null; my_agent_id?: string | null;
}
export interface MarketplaceSubscription {
  id: string; product_type: MarketplaceProductType; product_id: string; product_name: string;
  agent_id?: string | null; agent_name?: string | null;
  requester_oid: string; requester_email?: string | null; message?: string | null;
  status: MarketplaceStatus; auto_approved: boolean;
  decided_by?: string | null; decided_at?: string | null; decision_reason?: string | null; error?: string | null;
  revoked_by?: string | null; revoked_at?: string | null; revoke_reason?: string | null;
  created_at: string; updated_at: string;
}
export interface MarketplaceMetrics {
  total: number; pending: number; approved: number; rejected: number; failed: number; revoked: number;
  approval_rate: number; by_type: Record<string, number>;
  top_products: { product_id: string; product_name: string; count: number }[];
}
export interface MarketplaceListing { product_type: MarketplaceProductType; product_id: string; available: boolean; auto_approve: boolean; pitch?: string | null; }

// The lightweight agent shape the MCP subscribe picker consumes (Epic 9, F1).
// Returned by GET /marketplace/eligible-agents — the agents the caller may
// subscribe an MCP on behalf of (sponsor OR granted, server-enforced).
export interface EligibleAgent {
  id: string;
  name: string;
  identity_status?: string | null;
  entra_sp_id?: string | null;
  sponsor_oid?: string | null;
}

// --- Marketplace publication (Epic 33) -------------------------------------
// Publishing an agent to the marketplace is a DECLARATION, not a measurement: the
// publisher (OPERATOR) submits a datasheet, an ADMIN approves it, and only then does the
// agent appear as a product card. Every field below is publisher-asserted.
//
// This is a DIFFERENT feature from the E24 tenant `published` flag (`Agent.published` /
// `agentsApi.publish`), which controls cross-tenant visibility inside the platform. Never
// conflate the two — everything marketplace-side carries the `marketplace` prefix.
export interface PublishDatasheet {
  // Mandatory in the backend model (owner_team / support_contact / data_classification);
  // required here too so a caller cannot submit a half-filled datasheet.
  owner_team: string;
  support_contact: string;
  data_classification: string;
  sla_tier?: string | null;
  compliance?: string[];
  support_hours?: string | null;
  version?: string | null;
  region?: string | null;
  guardrails?: string[];
  pitch?: string | null;
}
// The publication block the backend writes onto BOTH registry records (`Agent.marketplace`
// and `McpServer.marketplace`). READ-ONLY for every client: it is service-written only, on
// admin approval, so `declared_by` always comes from the approving principal. Shared alias
// rather than two inline literals so the two product types cannot drift (contract C11).
export interface MarketplacePublicationBlock {
  published: boolean;
  datasheet: PublishDatasheet;
  declared_by: string;
  declared_at: string;
}
// One record per (product_type, product_id) PAIR server-side — a re-publish overwrites it, so
// the decision audit fields below ARE the history. `error` is a fixed safe literal, never a raw
// exception. `product_type` says WHICH registry holds the product, so it is never optional: the
// two id spaces are not distinguishable by shape (Amendment 1 / C9 — the pre-amendment
// `agent_id`/`agent_name` names are gone, nothing was deployed).
export interface MarketplacePublishRequest {
  id: string;
  product_type: MarketplaceProductType; product_id: string; product_name: string;
  tenant_id?: string | null;
  datasheet: PublishDatasheet;
  status: 'pending' | 'approved' | 'rejected';
  requested_by: string; requested_by_email?: string | null;
  decided_by?: string | null; decided_at?: string | null; decision_reason?: string | null;
  error?: string | null;
  created_at: string; updated_at: string;
}

export const marketplaceApi = {
  listAgentProducts: async (): Promise<ProductCard[]> => (await client.get<ProductCard[]>(`/api/v1/marketplace/agent-products`)).data,
  listEligibleAgents: async (): Promise<EligibleAgent[]> => (await client.get<EligibleAgent[]>(`/api/v1/marketplace/eligible-agents`)).data,
  listMcpProducts:   async (): Promise<ProductCard[]> => (await client.get<ProductCard[]>(`/api/v1/marketplace/mcp-products`)).data,
  subscribe: async (body: { product_type: MarketplaceProductType; product_id: string; agent_id?: string | null; message?: string | null }): Promise<MarketplaceSubscription> =>
    (await client.post<MarketplaceSubscription>(`/api/v1/marketplace/subscriptions`, body)).data,
  mySubscriptions:    async (): Promise<MarketplaceSubscription[]> => (await client.get<MarketplaceSubscription[]>(`/api/v1/marketplace/subscriptions`)).data,
  adminSubscriptions: async (params?: { status?: string; product_type?: string }): Promise<MarketplaceSubscription[]> => (await client.get<MarketplaceSubscription[]>(`/api/v1/marketplace/admin/subscriptions`, { params })).data,
  approve: async (id: string): Promise<MarketplaceSubscription> => (await client.post<MarketplaceSubscription>(`/api/v1/marketplace/subscriptions/${id}/approve`)).data,
  reject:  async (id: string, reason?: string): Promise<MarketplaceSubscription> => (await client.post<MarketplaceSubscription>(`/api/v1/marketplace/subscriptions/${id}/reject`, { reason })).data,
  revoke:  async (id: string, reason?: string): Promise<MarketplaceSubscription> => (await client.post<MarketplaceSubscription>(`/api/v1/marketplace/subscriptions/${id}/revoke`, { reason })).data,
  retry:   async (id: string): Promise<MarketplaceSubscription> => (await client.post<MarketplaceSubscription>(`/api/v1/marketplace/subscriptions/${id}/retry`)).data,
  metrics: async (): Promise<MarketplaceMetrics> => (await client.get<MarketplaceMetrics>(`/api/v1/marketplace/admin/metrics`)).data,
  setListing: async (productType: MarketplaceProductType, productId: string, body: { available?: boolean; auto_approve?: boolean; pitch?: string | null }): Promise<MarketplaceListing> =>
    (await client.put<MarketplaceListing>(`/api/v1/marketplace/listings/${productType}/${productId}`, body)).data,
  // Publication flow (Epic 33; generalized to both product types by Amendment 1 / C9-C11).
  // create is OPERATOR (the publisher); list/approve/reject/unpublish are ADMIN. 409s
  // (lifecycle not approved / identity not provisioned / duplicate pending / illegal state) and
  // the 502 registry-write failure surface as Error.message via the interceptor.
  createPublishRequest: async (body: { product_type: MarketplaceProductType; product_id: string; datasheet: PublishDatasheet }): Promise<MarketplacePublishRequest> =>
    (await client.post<MarketplacePublishRequest>(`/api/v1/marketplace/publish-requests`, body)).data,
  publishRequests: async (status?: string): Promise<MarketplacePublishRequest[]> =>
    (await client.get<MarketplacePublishRequest[]>(`/api/v1/marketplace/publish-requests`, { params: { status } })).data,
  // 404 means "this product has never been published" AND "not your tenant", byte-identically
  // on purpose — the caller treats a 404 as "no request", nothing more.
  publishRequestForProduct: async (productType: MarketplaceProductType, productId: string): Promise<MarketplacePublishRequest> =>
    (await client.get<MarketplacePublishRequest>(`/api/v1/marketplace/publish-requests/product/${productType}/${productId}`)).data,
  approvePublish: async (id: string): Promise<MarketplacePublishRequest> =>
    (await client.post<MarketplacePublishRequest>(`/api/v1/marketplace/publish-requests/${id}/approve`)).data,
  rejectPublish: async (id: string, reason?: string): Promise<MarketplacePublishRequest> =>
    (await client.post<MarketplacePublishRequest>(`/api/v1/marketplace/publish-requests/${id}/reject`, { reason })).data,
  // Delisting only: the product keeps its declared datasheet with published=false. 204, no body.
  unpublishProduct: async (productType: MarketplaceProductType, productId: string): Promise<void> => {
    await client.post(`/api/v1/marketplace/products/${productType}/${productId}/unpublish`);
  },
};

// --- Platform Users admin (Epic 16) ----------------------------------------
// Entra/Graph-backed platform role management. list/add/change/remove operate on
// appRoleAssignedTo on the platform SP; directory search reuses the existing
// `principalsApi.search` picker (same GET /entra/principals/search + PrincipalHit).
export type PlatformRole = 'admin' | 'operator' | 'viewer';
export interface PlatformUser {
  principal_id: string;
  display_name: string;
  principal_type: string;   // "User" | "Group" | "ServicePrincipal"
  role: PlatformRole;
}
export const usersAdminApi = {
  list: async (): Promise<PlatformUser[]> => (await client.get<PlatformUser[]>(`/api/v1/admin/users`)).data,
  add: async (body: { principal_id: string; role: PlatformRole }): Promise<PlatformUser> =>
    (await client.post<PlatformUser>(`/api/v1/admin/users`, body)).data,
  changeRole: async (principalId: string, role: PlatformRole): Promise<PlatformUser> =>
    (await client.put<PlatformUser>(`/api/v1/admin/users/${principalId}/role`, { role })).data,
  remove: async (principalId: string): Promise<void> => { await client.delete(`/api/v1/admin/users/${principalId}`); },
};

// --- Tenants admin (Epic 24 / E25) ------------------------------------------
// Admin-gated CRUD over line-of-business tenants (/api/v1/admin/tenants).
// Mirrors the backend Tenant read model: >= 1 Entra group per tenant + a nested
// per-stage (dev/prod) cross-account CICD config (E25). Each stage carries a
// 12-digit AWS account id plus region and the ECR/role targets used to provision
// into that account.
// Error `detail` literals surface via the response interceptor as err.message:
// 404 "Tenant not found", 409 "tenant name already exists" /
// "tenant is referenced by existing resources", 400 "invalid tenant".
export interface TenantStageConfig {
  account_id: string;
  region: string;
  // The three cross-account CICD targets are OPTIONAL, which is what the backend model has
  // always said they are: they "default empty and are populated as the cross-account CICD
  // provisions them" (E25). Marking them so has a second, deliberate effect — it makes
  // `DatabricksStageConfig` structurally assignable here, so the E29 `StageConfig` union
  // collapses at the AWS-only Ops signatures that legitimately read only `account_id` and
  // `region`, instead of forcing platform-narrowing edits into files this epic does not own.
  // Writers (the admin form) still supply all five; see `StageDraft` in tenantsAdminForm.ts.
  ecr_repo_uri?: string;
  push_role_arn?: string;
  deploy_role_arn?: string;
}
// --- E29: tenants are platform-typed ---------------------------------------
// A tenant's `platform` picks which runtime its agents live on and therefore which
// stage-config shape its `stages` map carries. Immutable after create: the backend's
// `TenantUpdate` model carries no `platform` field at all, so a PUT body containing one
// is DROPPED rather than obeyed — the admin UI renders it as a static label on edit and
// only offers the picker on create.
export type TenantPlatform = 'aws' | 'databricks';

// Per-stage Databricks workspace config (backend `DatabricksStageConfig`). Two notes that
// are contract, not style:
//   • `workspace_id` is a digits-STRING, not a number — it is an opaque identifier and "0"
//     is a real value (a workspace URL that carries no `o=` parameter).
//   • `sp_client_secret_arn` is a Secrets Manager ARN. The secret itself is NEVER in
//     DynamoDB and never travels on a read — a non-empty ARN is the only "a secret is
//     stored" signal a client gets (the `Connection.has_secret` idiom, spelled as an ARN
//     because that is what the record already carries).
export interface DatabricksStageConfig {
  workspace_url: string;   // https origin, no trailing slash (validated on write)
  workspace_id: string;    // digits-string; "0" is legal
  cloud: string;           // 'aws' | 'azure' | 'gcp'
  region: string;
  account_id: string;      // Databricks account UUID; REQUIRED for federation mode only
  sp_client_id: string;
  sp_client_secret_arn: string;
}
// A stage entry is one shape or the other, chosen by the tenant's `platform`. Readers must
// narrow on the tenant's platform before touching platform-specific fields — the two shapes
// share only `account_id` and `region`, and those mean DIFFERENT things (an AWS account id
// vs a Databricks account UUID).
export type StageConfig = TenantStageConfig | DatabricksStageConfig;

// Write-only credential input for one Databricks stage (create/update bodies only, never a
// read shape): the workspace SP's client secret, which the backend puts in Secrets Manager
// and replaces with an ARN. Mirrors `ConnectionCreate.token` — the field exists on the way
// in and has no counterpart on the way out.
export interface DatabricksStageCredentials {
  sp_client_secret?: string;
}
// Capability flags written by the connect-time probe (E29/T3) and READ-ONLY to clients: the
// probe is the only writer, so no create/update body carries them. An ABSENT key is not
// `false` — it means the probe never ran (a tenant created before probing, or a probe that
// could not reach the workspace at all). Renderers must keep those two apart.
export interface TenantCapabilities {
  can_discover?: boolean;
  account_admin?: boolean;
  user_sync?: boolean;
}
// The identity-binding mode the probe COMPUTED for this tenant (never client-supplied):
// '' for AWS tenants, 'federation' when the account-admin + user-sync probes both passed,
// 'invoke_unavailable' otherwise. The UI displays it; it never derives it.
//
// `'sp_secret'` is DORMANT/LEGACY and kept in the union deliberately (§3B, E29/T14): the connect
// flow can no longer produce it — the backend's probe emits only `federation` or
// `invoke_unavailable`, and the tenant form cannot ask for it — but records written before T14
// (tenants, and the reporting copy on every agent registered from one) still carry the word, and
// the wire will keep delivering it. Dropping it from the union would make those records render as
// an unknown mode, i.e. as no badge at all, which is exactly the silent story §3B forbids: a
// shared-service-principal tenant must keep saying so, loudly. Removing it is a data-migration
// decision, not a type-cleanup one.
export type TenantBindingMode = '' | 'federation' | 'invoke_unavailable' | 'sp_secret';
// `stages` is an OPEN MAP, not a dev/prod pair (E28/T11, contract C5 + design D8).
//
// It was declared here as a pair of two FIXED, named stage keys, which is three separate
// problems in one line:
//
//   • The BACKEND model is `Dict[str, TenantStageConfig]` and `TenantService` requires at
//     least ONE stage, not a conventional pair. E28/T6 removed the guarantee that both keys
//     exist, so a single-stage tenant is now a legitimate record — and under the old type every
//     reader indexing a fixed stage key on such a tenant is a LIVE CRASH
//     (`undefined.account_id`), not a type error, because `noUncheckedIndexedAccess` is off
//     project-wide. Widening the type therefore does NOT make the old reads a compile error;
//     the three readers in `ProjectDetail.tsx` had to be rewritten, not merely re-typed.
//   • C5 forbids a hardcoded stage name anywhere in `frontend/` outright: stages come from the
//     API, and a stage the API does not return must not render. (Which is why this comment does
//     not spell the two conventional names out — the guard enforcing C5 reads raw source and
//     skips neither comments nor a stage used as a property key, deliberately.)
//   • Two spellings of one contract is the drift this epic exists to remove.
//
// `Record<string, TenantStageConfig>` — iterate the keys, never index a literal.
//
// OPEN SEAM, recorded rather than fixed: the admin TENANT surface under
// `components/governance/admin/` still models a two-stage draft, and it does not merely ASSIGN
// into this type — `TenantsAdmin.tsx` and `TenantModal.tsx` also READ two fixed stage keys off a
// tenant record. On a single-stage tenant those reads throw exactly as the Ops project panel did
// before E28/T11 rewrote it. NOT a regression (they were equally type-invisible under the old
// pair type, since `noUncheckedIndexedAccess` is off project-wide) and NOT this epic's to
// change — `components/governance/**` is off-limits by design. It needs its own migration, and
// the honest fix there is the same one applied on the Ops side: iterate the keys.
export interface TenantInfo {
  id: string;
  name: string;
  line_of_business: string;
  entra_group_ids: string[];
  // E29: which shape the values carry is decided by `platform` — narrow on it before
  // reading anything past `account_id`/`region`.
  //
  // `platform` and `capabilities` are OPTIONAL because a record written before E29 carries
  // neither. The backend hydrates an absent platform to "aws" on read, but typing them as
  // present would still be a promise the wire does not keep for anything already stored —
  // and it is the reason every reader here is written defensively (`t.platform ?? 'aws'`,
  // `tenant?.capabilities`). The type now says what those reads already assume.
  platform?: TenantPlatform;
  stages: Record<string, StageConfig>;
  capabilities?: TenantCapabilities;
  // Optional for the same reason, and consistently so: also absent on a pre-E29 record.
  // `bindingModeLabel` already accepts `undefined` and renders no badge for it, which is
  // the honest outcome — an unprobed tenant has no mode to claim.
  binding_mode?: TenantBindingMode;
  // The Secrets Manager ARN of the tenant's account-admin credential — the POINTER only, exactly
  // as `sp_client_secret_arn` is per stage. Neither half of the credential ever travels on a
  // read, so a non-empty ARN is the only "one is stored" signal a client gets, and that signal is
  // load-bearing since E29/T14: the tenant form REQUIRES an account-admin credential, and this is
  // what lets an EDIT satisfy that requirement without forcing the admin to re-type a secret that
  // is already stored (`draftFromTenant` → `has_account_admin_credential`).
  account_admin_secret_arn?: string;
  description: string;
  created_by: string;
  created_at: string;
  updated_at: string;
}
export interface TenantCreate {
  name: string;
  line_of_business: string;
  entra_group_ids: string[];
  // Settable on CREATE only (omitted ⇒ the backend defaults to 'aws'). There is no
  // `platform` on the update path at all — see the note on TenantPlatform.
  platform?: TenantPlatform;
  // Databricks stages additionally carry the write-only `sp_client_secret` on the way in.
  stages: Record<string, StageConfig | (DatabricksStageConfig & DatabricksStageCredentials)>;
  // Write-only account-admin credential (Databricks only). Optional, and all-or-nothing:
  // supplying BOTH is what lets the connect-time probe reach the account-level federation
  // policy API, which is what can unlock `federation` binding mode. Never read back.
  account_admin_client_id?: string;
  account_admin_secret?: string;
  description?: string;
  // `capabilities`/`binding_mode` are deliberately absent: the connect-time probe is
  // their only writer, so a client cannot assert them.
}
// The caller's tenant memberships as returned by GET /users/me (E24/T4).
//
// E29: `stages` carries the same union as `TenantInfo`, because `/users/me` projects
// `t.stages` straight off the tenant record (`users._resolve_tenants`) — a Databricks
// membership really does put `DatabricksStageConfig` values on this wire, and the admin
// directory (`TenantInfo[]`) is passed as `UserTenant[]` to the `tenantUi` lookups.
//
// This widening is TYPE-SAFE for today's readers and it is worth being explicit about why:
// no consumer outside the admin tenant surface reads an AWS-ONLY stage field. The Ops
// project/repository panels touch `account_id` and `region` only, and those exist on both
// shapes — so nothing here CRASHES on a Databricks membership.
//
// E29/T9 — TWO changes, and both close obligations the type used to hide.
//
// **OB-14: `platform` and `binding_mode` are now projected.** They were absent, and the cost was
// concrete: `agentRegistrationWizardModel.inferPlatform` defaults an absent platform to `'aws'`
// (the backend's own `hydrate_tenant_item` rule), so a NON-ADMIN operator on a Databricks tenant
// got the AgentCore registration branch — an ARN field for a platform that has no ARNs. The
// wizard failed in the safe direction (no Databricks affordances offered, never a fabricated
// Databricks claim), but the honest fix is for the wire to carry the fact. Both stay OPTIONAL for
// the same reason they are optional on `TenantInfo`: a record written before E29 carries neither,
// and every reader here is already written defensively.
//
// **OB-9: a Databricks membership's projected stage no longer carries a MISLEADING account id.**
// The seam recorded below was live, not theoretical — `ProjectDetail.tsx` renders `{stage} account`
// straight from `config.account_id`, and on a Databricks stage that field holds the Databricks
// ACCOUNT UUID. The panel would have printed a plausible-looking wrong answer under an "account"
// heading: not a crash, which is worse, because nothing signals it.
//
// `users._resolve_tenants` now projects a Databricks stage as `{workspace_url, workspace_id,
// region, account_id: ""}` — the three fields that are true, plus the AWS-shaped KEY with an EMPTY
// value. Keeping the key and blanking the value (rather than omitting the key) is deliberate on
// two counts: an empty string is not a claim, so the AWS-shaped readers render nothing instead of
// something false; and it preserves the structural assignability the whole `StageConfig` union
// rests on, so the fix does not force platform-narrowing edits into Ops files this epic does not
// own. `cloud`, `sp_client_id` and `sp_client_secret_arn` are dropped outright — no reader on this
// wire wants them, and a Secrets Manager pointer had no business being handed to every member of
// the tenant. The AWS projection is byte-identical to before; that branch is the fence.
//
// Still open, deliberately: the Ops panels keep their AWS-shaped HEADINGS and will show a blank
// value for a Databricks stage. That is the honest floor, not the destination — teaching them
// platform-aware labels is a redesign of a surface E29 does not own.
export type UserTenant = Pick<TenantInfo, 'id' | 'name' | 'line_of_business' | 'stages'> & {
  platform?: TenantPlatform;
  binding_mode?: TenantBindingMode;
};
// --- E29/T3: tenant runtime discovery (contract C-3) ------------------------
// One agent as its hosting platform reports it — the backend `DiscoveredAgent` projection.
// EVIDENCE about a platform at a moment in time, not a governance claim, and every field
// here is shaped by that distinction:
//
//   • `runtime_handle` is READ from the platform's response, never constructed. It is the
//     app URL for Databricks and the runtime ARN for AgentCore, and it is the value the
//     registration wizard posts back — so re-deriving it client-side would be inventing a
//     binding pin the platform already stated.
//   • `state` is the platform's RAW state string and is DISPLAY-ONLY. It is deliberately NOT
//     the six-value runtime-status union: that union is a governance claim with one producer
//     (`agent_identity_service`), and a discovery listing that also asserted runtime status
//     would be a second producer of the same fact with no way to tell them apart.
//   • `already_registered` is computed by the ROUTE (handle matches a registry agent's
//     `runtime_handle` or `agent_arn`), never by an adapter. It fails OPEN: the flag is a
//     convenience on a read-only listing, so an unreadable registry drops the badge rather
//     than hiding real agents, and registration re-checks the duplicate downstream anyway.
export interface DiscoveredAgent {
  name: string;
  runtime_handle: string;
  kind: 'app' | 'serving_endpoint' | 'agentcore_runtime' | string;
  state: string;
  created_by: string;
  already_registered: boolean;
}
// The discovery envelope. It carries `platform` rather than leaving the client to re-derive it
// from the tenant, because `platform` is what decides which create body a selected row becomes
// (see `agentRegistrationWizardModel`).
export interface DiscoveredAgentsResponse {
  agents: DiscoveredAgent[];
  platform: TenantPlatform;
}
export const tenantsAdminApi = {
  list: async (): Promise<TenantInfo[]> => (await client.get<TenantInfo[]>(`/api/v1/admin/tenants`)).data,
  create: async (body: TenantCreate): Promise<TenantInfo> =>
    (await client.post<TenantInfo>(`/api/v1/admin/tenants`, body)).data,
  update: async (id: string, body: Partial<TenantCreate>): Promise<TenantInfo> =>
    (await client.put<TenantInfo>(`/api/v1/admin/tenants/${id}`, body)).data,
  remove: async (id: string): Promise<void> => { await client.delete(`/api/v1/admin/tenants/${id}`); },
  /**
   * `GET /admin/tenants/{id}/discovered-agents?stage=<name>` (E29/T3, contract C-3).
   *
   * `stage` is REQUIRED and has NO client-side default — an absent one is a 422, by design.
   * AGP names no stages (E28/D8 opened the axis), so guessing one would silently discover the
   * wrong workspace on any tenant that does not happen to have it. Callers pick a stage off
   * the tenant's own `stages` map (`discoveryStageNames`), never from a literal (C5).
   *
   * The failure taxonomy each answer a DIFFERENT operator question and the caller must keep
   * them apart: 400 "the stage does not exist on this tenant", 502 "the platform could not be
   * reached" (safe code only), and **200 with an empty `agents` array = the platform WAS
   * reached and reports no agents**. A UI that renders all three as a blank list sends an
   * operator hunting a credential that is fine.
   */
  discoveredAgents: async (id: string, stage: string): Promise<DiscoveredAgentsResponse> =>
    (
      await client.get<DiscoveredAgentsResponse>(
        `/api/v1/admin/tenants/${id}/discovered-agents`,
        { params: { stage } },
      )
    ).data,
};

// --- Git org connections admin (Epic 19) -----------------------------------
// Provider org connections (GitHub/GitLab) with a PAT stored in Secrets Manager.
// `ConnectionAuthType` is named to avoid colliding with the Agent Registry (Epic 4)
// `AuthType` declared earlier in this module — the wire value is still `'pat'`.
export type Provider = 'github' | 'gitlab';
export type ConnectionAuthType = 'pat' | 'github_app';
export type ConnStatus = 'connected' | 'error' | 'pending';
export interface Connection {
  id: string;
  provider: Provider;
  org: string;
  base_url: string | null;
  auth_type: ConnectionAuthType;
  app_id: string | null;          // NEW (non-secret) — set for github_app connections
  installation_id: string | null; // NEW (non-secret) — null until the App is installed
  client_id: string | null;       // E27B (non-secret) — the App's OAuth client id; distinct from app_id
  has_oauth_client: boolean;      // E27B — true once a client_secret is stored (mirrors has_secret)
  status: ConnStatus;
  status_detail: string | null;
  account_login: string | null;
  secret_arn: string;
  has_secret: boolean;
  last_verified_at: string | null;
  created_by: string;
  created_at: string;
  updated_at: string;
}
// PAT create (unchanged) + App-manual create. `auth_type` defaults to 'pat'
// server-side when omitted; the github_app fields are only sent in manual mode.
export interface ConnectionCreate {
  provider: Provider;
  org: string;
  base_url?: string | null;
  auth_type?: ConnectionAuthType;
  token?: string;                 // pat
  app_id?: string;                // github_app manual
  installation_id?: string;       // github_app manual
  private_key?: string;           // github_app manual (write-only)
}
export interface ConnectionTokenReplace { token: string; }
// Manifest onboarding (E20/U2): start returns the org-scoped GitHub register form
// target + the manifest JSON to POST; callback converts the returned code into a
// (pending or connected) Connection; finalize resolves/attaches the installation.
export interface ManifestStartResponse { post_url: string; manifest: Record<string, unknown>; state: string; }
export interface ManifestCallbackResponse { connection: Connection; needs_install: boolean; install_url: string | null; }
export const connectionsApi = {
  list: async (): Promise<Connection[]> => (await client.get<Connection[]>(`/api/v1/admin/connections`)).data,
  create: async (body: ConnectionCreate): Promise<Connection> =>
    (await client.post<Connection>(`/api/v1/admin/connections`, body)).data,
  test: async (id: string): Promise<Connection> =>
    (await client.post<Connection>(`/api/v1/admin/connections/${id}/test`)).data,
  replaceToken: async (id: string, body: ConnectionTokenReplace): Promise<Connection> =>
    (await client.put<Connection>(`/api/v1/admin/connections/${id}/token`, body)).data,
  remove: async (id: string): Promise<void> => { await client.delete(`/api/v1/admin/connections/${id}`); },
  manifestStart: async (body: { org: string; base_url?: string | null; redirect_url: string }): Promise<ManifestStartResponse> =>
    (await client.post<ManifestStartResponse>(`/api/v1/admin/connections/manifest/start`, body)).data,
  manifestCallback: async (body: { code: string; state: string }): Promise<ManifestCallbackResponse> =>
    (await client.post<ManifestCallbackResponse>(`/api/v1/admin/connections/manifest/callback`, body)).data,
  finalize: async (id: string, body: { installation_id?: string | null }): Promise<Connection> =>
    (await client.post<Connection>(`/api/v1/admin/connections/${id}/finalize`, body)).data,
  // E27B: the one-time admin paste of the App's OAuth client (id + secret). The response is
  // the ordinary Connection read model, so the secret cannot be echoed back.
  setOauthClient: async (id: string, body: { client_id: string; client_secret: string }): Promise<Connection> =>
    (await client.put<Connection>(`/api/v1/admin/connections/${id}/oauth-client`, body)).data,
};

// --- Per-user GitHub account link (Epic 27B) -------------------------------
// A human links their PERSONAL GitHub account to an org connection so their own
// deployment actions appear on GitHub as THEM rather than as the platform App. The
// router is mounted under `/me`: the principal oid comes from the validated token, never
// from a path or a body, so one human can never address another's link. No route here
// returns 401 (the api-client's response interceptor would log the human out on one) —
// failures are drawn from {400, 404, 409, 502}.
//
// `github_login` is a GITHUB identity, established only by the OAuth consent the human
// just gave. It is never an AGP principal and AGP has verified nothing about it beyond
// that consent — do not join it to an Entra oid.
export interface GitHubLinkStatus {
  connection_id: string;
  org: string;
  linked: boolean;
  status: 'linked' | 'refreshing' | 'unlinked';
  github_login: string | null;
  last_verified_at: string | null;
}
export interface LinkableConnection { connection_id: string; org: string; oauth_client_ready: boolean; }
export interface GitHubLinkView { links: GitHubLinkStatus[]; connections: LinkableConnection[]; }

// Declared here (NOT in githubLink.ts) so the api-client owns its own paths and the
// route-drift test has something pure to assert against.
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
  start: async (body: { connection_id: string; redirect_uri: string }): Promise<{ authorize_url: string; state: string }> =>
    (await client.post<{ authorize_url: string; state: string }>(GITHUB_LINK_PATHS.start, body)).data,
  callback: async (body: { code: string; state: string }): Promise<GitHubLinkStatus> =>
    (await client.post<GitHubLinkStatus>(GITHUB_LINK_PATHS.callback, body)).data,
  verify: async (connectionId: string): Promise<GitHubLinkStatus> =>
    (await client.post<GitHubLinkStatus>(GITHUB_LINK_PATHS.verify(connectionId))).data,
  unlink: async (connectionId: string): Promise<void> => { await client.delete(GITHUB_LINK_PATHS.unlink(connectionId)); },
};

// --- Projects (containers) + Repositories (Epic 20 / Task 10) --------------
// A Project is an EMPTY CONTAINER scoped to one org (connection); the repos it
// holds are materialized from ops-templates via `addRepo`. Each Repository is the
// persisted record of "operator materialized a repo+agent from a template into a
// project" (NEVER a token). `get` returns the project + its repositories.
export interface Project {
  id: string;
  name: string;
  connection_id: string;
  // Owning tenant (E24/T6) — REQUIRED on new projects; pre-E24 records may
  // hydrate it as null.
  tenant_id?: string | null;
  description: string;
  created_by: string;
  created_at: string;
  updated_at: string;
}
// One row of the live materialize timeline (E25C). The 5 background steps advance
// pending → running → done (or failed). `error` is a SAFE short hint only (never a
// token/body); timestamps are ISO8601 strings mirroring the backend StepState.
//
// FIVE SINCE E28B/T3 (D-B2), and the count is NOT a contract any consumer may rely on.
// `MATERIALIZE_STEPS` held eight keys and was treated as frozen; E28B deleted five of them
// outright (a branch cut, two GitHub Environments, their variables) because materialize now makes
// ONE write instead of six racing ones. Historical and in-flight records still carry the OLD eight
// keys.
//
// SO: RENDER `label`, AND USE `key` ONLY AS A LIST KEY. There is deliberately no key→label map
// anywhere in `src/` and nothing may add one — an old record must keep rendering its own stored
// labels, and a key this list no longer names must degrade to the stored label rather than crash or
// blank a row. Any consumer that must reason about the total takes it from `steps.length`, never
// from a literal (`materializeSummary` takes a `stepCount` and special-cases only 0).
export interface StepState {
  key: string;
  label: string;
  status: 'pending' | 'running' | 'done' | 'failed';
  error?: string | null;
  started_at?: string | null;
  completed_at?: string | null;
}
export interface Repository {
  id: string;
  project_id: string;
  name: string;
  repo_url: string | null;
  agent_id: string;
  template_name: string;
  cicd_status: string;
  status: string;
  created_by: string;
  created_at: string;
  updated_at: string;
  // Live per-step materialize timeline (E25C). Additive — older records without it
  // hydrate a full pending timeline server-side, so this is always present.
  steps: StepState[];
  // The last image tag that successfully deployed to DEV (E27). Written OUT-OF-BAND by the
  // dev buildspec, never by a caller — it is the whole reason no tag is ever hand-entered:
  // `promoteRepo` resolves it server-side. Optional/nullable because a repo that has never
  // deployed to dev has none.
  //
  // NO LONGER the promote precondition (E27A) — that is `prod_candidate_status` below. This
  // is now shown as WHAT DEV IS CURRENTLY RUNNING, so an owner reading the row can see
  // whether promoting would make prod match dev or move it somewhere dev never ran.
  last_dev_image_tag?: string | null;
  // The single PROD CANDIDATE (E27A) — one merge to `main`, awaiting an OWNER's approval.
  // Written ONLY by `POST /builds/prod-candidate`, from a validated GitHub OIDC token, and
  // CLEARED by a successful promote. There is no queue and no history: a newer merge to
  // `main` overwrites all five fields, so this is always "the tip of main as of the last
  // merge", never a backlog.
  //
  //   prod_candidate_status — "pending" or null/absent. THE promote precondition
  //     (`canPromote` / `promoteBlockedReason`): with nothing pending the route answers 409
  //     "no prod candidate to promote", so the affordance is not rendered at all.
  //   prod_candidate_sha — main's merge commit sha, FULL. Truncate to 7 for display.
  //   prod_candidate_actor — a GITHUB LOGIN (e.g. "jorge"), rendered `@login`. Deliberately
  //     NOT the same currency as `last_promoted_by`, which stays a raw Entra oid: the
  //     provider identity and the platform identity are not joined (design §6), and showing
  //     each in its own currency is the honest presentation rather than a guessed mapping.
  //   prod_candidate_image_tag — the image the merge built, `{agent_id}-{tree_sha[:7]}`.
  //     Displayed, never sent: `promoteRepo` still takes no body.
  //   prod_candidate_at — ISO-8601 UTC.
  //
  // All optional: a repo whose `main` has not moved since E27A shipped simply has no
  // candidate, which is a state, not a migration gap.
  prod_candidate_image_tag?: string | null;
  prod_candidate_sha?: string | null;
  prod_candidate_actor?: string | null;
  prod_candidate_at?: string | null;
  prod_candidate_status?: string | null;
  // E28B/T4 (D-B3) — THE APPROVED ARTIFACT, and the reason the four digest fields below are
  // declared at all: they have existed on the backend record since T4 and had NO TS declaration,
  // so every one of them was unreadable from the console. A field the wire carries and the client
  // type omits is not an absent field, it is an invisible one.
  //
  // `prod_candidate_digest` is what an OWNER's production approval actually attests to: the exact
  // BYTES, which `promote_repo` passes to the deploy verbatim (`<repo>@<digest>`) rather than
  // re-resolving the tag — because between approval and deploy the tag may point somewhere else.
  //
  // OPTIONAL BY DESIGN, and the optionality is a UI CONCERN, not just a type detail. A materialized
  // repo carries a COMMITTED copy of its build workflow, so a repo whose `build.yml` predates this
  // epic keeps registering candidates with no digest, and the backend accepts them tag-only rather
  // than 422-ing the deploy path out of existence. That means the epic's headline guarantee is
  // CONDITIONAL ON THE DEPLOYED TEMPLATE — so absence here must be RENDERED, not silently treated
  // as "no data". See `PROMOTION_TAG_ONLY_LABEL` / `shortDigest` in
  // `components/operations/repoRowModel.ts`, which own that presentation for every surface.
  prod_candidate_digest?: string | null;
  // The digests their `*_image_tag` counterparts label. Written by the buildspec helper, never by
  // a caller — `last_dev_*` is CodeBuild-EXCLUSIVE and the backend's own save path skips it.
  last_promoted_digest?: string | null;
  last_dev_digest?: string | null;
  // Promotion audit (E27) — who promoted what, when, and which build ran it. Surfaced on
  // the repo row as the "last promoted" line an FSI reviewer looks for. The actor comes
  // from the validated principal server-side, never from a request body.
  last_promoted_by?: string | null;
  last_promoted_at?: string | null;
  last_promoted_image_tag?: string | null;
  last_promotion_build_id?: string | null;
}
export interface ProjectDetail {
  project: Project;
  repositories: Repository[];
  // The caller's OWN standing on this project (E27/T11) — a UI HINT, never an authority.
  // The browser cannot compute it: a role may be granted to an Entra GROUP and nothing
  // client-side evaluates group membership, so without these a group-derived owner is
  // indistinguishable from a role-less caller. `may()` server-side is still the gate; these
  // only decide which affordances are worth RENDERING.
  //   effective_role — 'viewer' | 'maintainer' | 'owner', or null when the caller holds
  //     none. A PLATFORM ADMIN is reported as 'owner' (they may do everything).
  //   ungoverned — the project holds NO role rows, so the design-§3 fallback applies and any
  //     tenant-visible caller acts as MAINTAINER for maintainer-level verbs. Deliberately
  //     separate from effective_role: the STRICT gates (role CRUD, promote) ignore the
  //     fallback, so reporting it as a held role would promise an owner verb that 403s.
  // Both are optional so a pre-fix/other-shaped response reads as "no hint".
  effective_role?: string | null;
  ungoverned?: boolean;
}
// agent.config is free-form (the ops-template scaffold owns the full contract); the
// backend validates framework === 'strands' + the agent_name regex.
export interface ProjectAgentConfig {
  agent_name: string;
  framework: string;
  model_id?: string;
  [key: string]: unknown;
}
export interface ProjectCreate {
  name: string;
  connection_id: string;
  // REQUIRED by the backend since E24/T6 — every project belongs to one tenant.
  tenant_id: string;
  description?: string;
}
export interface RepositoryCreate {
  name: string;
  // The GitHub template repo to generate from (keyed by name — E22/T4; renamed from
  // the old opaque `template_id`).
  template_name: string;
  agent_config: ProjectAgentConfig;
  // Governance description for the pre-registered Agent record; optional (backend
  // derives a name-based default when omitted).
  purpose?: string;
  // Governance attributes for the pre-registered Agent (E22/T4). Optional — sponsor is
  // back-filled from the principal; these ride through.
  business_unit?: string;
  region?: string;
  // MUST carry the CAPITALIZED enum value the backend expects (e.g. "Confidential",
  // "Internal", "Restricted", "Public") — see DataClassification above.
  data_classification?: string;
  // Per-repo scaffold variable overrides passed to the template materialization.
  repo_overrides?: Record<string, string>;
}
// Delete capability (E23/T4,T6). Selection flags choose which artifacts to tear down;
// the backend defaults all to true. `RepoDeleteResult` reports the per-item outcome +
// whether the repo record itself was removed.
export interface RepoDeleteSelection {
  record: boolean;
  github: boolean;
  image: boolean;
  runtime: boolean;
  identity: boolean;
}
export interface RepoDeleteItemResult {
  item: string;
  outcome: string;
  reason?: string;
}
export interface RepoDeleteResult {
  items: RepoDeleteItemResult[];
  record_removed: boolean;
}
// Delete pre-check (E23/T11,T12). Probes what still exists before the modal offers
// checkboxes: `item` mirrors RepoDeleteSelection keys; `state` is "present" | "gone" |
// "unknown". present/unknown → offer it (checked); gone → shown as already-deleted.
export interface RepoDeletePreviewItem {
  item: string;
  state: string;
}
export interface RepoDeletePreview {
  items: RepoDeletePreviewItem[];
}
export const projectsApi = {
  list: async (): Promise<Project[]> => (await client.get<Project[]>(`/api/v1/projects`)).data,
  get: async (id: string): Promise<ProjectDetail> => (await client.get<ProjectDetail>(`/api/v1/projects/${id}`)).data,
  create: async (body: ProjectCreate): Promise<Project> =>
    (await client.post<Project>(`/api/v1/projects`, body)).data,
  // 202 Accepted (E25C): returns the PENDING record with a full pending `steps[]`
  // timeline; the 5 side-effecting steps then advance in the background. Poll
  // `getRepoStatus` to watch them; do NOT treat the 202 as done.
  //
  // FIVE since E28B/T3 — see `StepState` above for why the count moved and why no caller may
  // hardcode it. Poll until the steps are terminal, never until a fixed number of them are done.
  addRepo: async (id: string, body: RepositoryCreate): Promise<Repository> =>
    (await client.post<Repository>(`/api/v1/projects/${id}/repos`, body)).data,
  // Live materialize timeline read (E25C/T3) — the record + current `steps[]`.
  getRepoStatus: async (projectId: string, repoId: string): Promise<Repository> =>
    (await client.get<Repository>(`/api/v1/projects/${projectId}/repos/${repoId}/status`)).data,
  // Resume materialize from the first failed step (E25C/T3). 202 + the record with
  // failed steps reset to pending. 409 "Nothing to retry" when nothing is retryable —
  // callers treat that as already-complete (surface via the interceptor's Error).
  retryRepo: async (projectId: string, repoId: string): Promise<Repository> =>
    (await client.post<Repository>(`/api/v1/projects/${projectId}/repos/${repoId}/retry`)).data,
  // APPROVE the repo's pending PROD CANDIDATE (E27/T8, narrowed by E27A) — the epic's
  // headline action. 202 + the record at `cicd_status: "promoting"`; poll `getRepoStatus` to
  // watch it reach `deployed` / `failed` (the SAME 3s poll the materialize timeline uses —
  // there is no second poller and no promotion-specific status endpoint).
  //
  // NO BODY, deliberately: the route's signature accepts none and the image tag is resolved
  // server-side from `prod_candidate_image_tag` (E27A; was `last_dev_image_tag`), so there is
  // no input through which an arbitrary image could reach production. `promoted_by` likewise
  // comes from the validated principal. Sending a body would be silently ignored — don't.
  //
  // A successful start CLEARS all five `prod_candidate_*` fields, so the affordance
  // disappears on its own until the next merge to `main` registers a new candidate.
  //
  // OWNER-gated on the STRICT gate — the design-§3 ungoverned fallback does NOT apply, so
  // this needs a real OWNER row even on a pre-migration project. Gate the affordance on
  // `effective_role` (via `canPromote`), NEVER on `ungoverned`.
  //
  // Error literals (surfaced as `err.message` by the response interceptor) — map them with
  // `promotionActionMessage` from `components/operations/projectRoles.ts`, never show raw:
  //   403 "insufficient project role"           — the caller is not an Owner
  //   409 "no prod candidate to promote"        — nothing is pending (E27A)
  //   409 "no dev deployment to promote"        — the pre-E27A refusal, retained so rows
  //                                               predating the candidate fields behave
  //   409 "a promotion is already in flight"    — a prod build is already running
  //   502 "failed to start the promotion build" — the build never STARTED (nothing deployed)
  //   404 "Repository not found"                — unknown repo, or one under another project
  promoteRepo: async (projectId: string, repoId: string): Promise<Repository> =>
    (await client.post<Repository>(`/api/v1/projects/${projectId}/repos/${repoId}/promote`)).data,
  // REDEPLOY a previously-succeeded image tag (E28/T4+T12) — promote's counterpart, and the verb
  // that makes the append-only `Deployment` history actionable rather than merely readable. 202 +
  // the record at `cicd_status: "promoting"`; poll `getRepoStatus` exactly as promote does.
  //
  // UNLIKE `promoteRepo` THIS TAKES A BODY, because a rollback has to name its target — and that is
  // precisely why the service VALIDATES it rather than trusting it: `image_tag` is accepted only if
  // it has a SUCCEEDED deployment row for this repo IN THIS STAGE. Both clauses are load-bearing.
  // The tenant container registry is shared by every materialized agent, so another repo's tag names
  // a real, pullable image; and an artifact proven good in a non-prod stage is not thereby approved
  // for production. An unvalidated tag here would be a deploy-anything-to-prod primitive.
  //
  //   image_tag — REQUIRED, no default. A rollback with no target must be a 422, never a deploy of
  //     some server-chosen tag. Pass the tag off the ATTEMPT the operator picked.
  //   stage — FREE-FORM (D8), and OMITTED here rather than defaulted: the route defaults it
  //     server-side. Never validate it against a stage literal and never send a hardcoded one (C5);
  //     send the attempt's OWN stage. A BLANK stage is rejected by the model (`min_length=1`)
  //     because empty does not mean "unknown stage" — it DISABLES the stage-scoped validation above.
  //
  // The prod candidate is NOT consumed by a rollback (it is not an approval of `main`), so the
  // Promote affordance survives and the owner can still ship the fix once the incident is over.
  //
  // OWNER-gated on the STRICT gate — the SAME helper and threshold as promote, deliberately not a
  // second gate of its own, and it does NOT ride the design-§3 ungoverned fallback. Gate the
  // affordance on `effective_role` (via `mayRollback`), NEVER on `ungoverned`.
  //
  // Error literals (surfaced as `err.message` by the response interceptor) — map them with
  // `rollbackActionMessage` from `components/operations/repo-tabs/deploymentsTab.ts`, never raw:
  //   403 "insufficient project role"                    — the caller is not an Owner
  //   409 "no such succeeded deployment to roll back to"  — the tag was REFUSED. An ordinary state
  //         the UI renders calmly, not a server fault; the detail is FIXED and never echoes the
  //         rejected tag back, so there is no tag in the response to display.
  //   409 "a promotion is already in flight"              — a delivery is running (promote's own
  //         bounded guard, reused so the two verbs serialize in either order)
  //   502 "failed to start the rollback build"            — the build never STARTED, so nothing
  //         reached production and retrying is safe
  //   404 "Repository not found"                          — unknown repo, or one under another project
  rollbackRepo: async (
    projectId: string,
    repoId: string,
    body: { image_tag: string; stage?: string },
  ): Promise<Repository> =>
    (await client.post<Repository>(
      `/api/v1/projects/${projectId}/repos/${repoId}/rollback`,
      body,
    )).data,
  deleteRepo: async (projectId: string, repoId: string, selection: RepoDeleteSelection): Promise<RepoDeleteResult> =>
    (await client.delete<RepoDeleteResult>(`/api/v1/projects/${projectId}/repos/${repoId}`, { data: selection })).data,
  deletePreview: async (projectId: string, repoId: string): Promise<RepoDeletePreview> =>
    (await client.get<RepoDeletePreview>(`/api/v1/projects/${projectId}/repos/${repoId}/delete-preview`)).data,
  remove: async (projectId: string): Promise<void> => { await client.delete(`/api/v1/projects/${projectId}`); },
};
export const repositoriesApi = {
  list: async (): Promise<Repository[]> => (await client.get<Repository[]>(`/api/v1/repositories`)).data,
};

// --- Per-project roles (Epic 27 / Task 3 routes) ---------------------------
// One project→principal→role edge. `principal_id` is an Entra object id — a USER
// oid or a GROUP object id (a group grant covers its members), which is why the
// picker is groups-first: a group grant is expressed in the currency a team-synced
// customer already administers, whereas an individual oid must be re-entered.
// `role` is the lowercase wire name ('viewer' | 'maintainer' | 'owner'); it is typed
// as `string` here to mirror the backend read-model exactly — narrow it with
// `isProjectRoleName` from `components/operations/projectRoles.ts`.
// A role row carries only metadata: no credential, no secret.
export interface ProjectRoleRecord {
  project_id: string;
  principal_id: string;
  principal_type: string; // "user" | "group"
  principal_display: string; // display name at grant time (best-effort, for UI)
  role: string;
  granted_by: string;
  granted_at: string;
}
// Write body. `granted_by` is deliberately ABSENT — the backend takes the grantor
// from the validated Principal, never from the body.
export interface ProjectRoleCreate {
  principal_id: string;
  principal_type: string; // "user" | "group"
  principal_display?: string;
  role: string;
}
// Error literals these routes pin (surfaced as `err.message` by the response
// interceptor) — map them with `roleActionMessage`, never show them raw:
//   403 "insufficient project role"          — caller is not an Owner
//   409 "project must keep at least one owner" — the last-owner refusal
//   503 "could not verify project ownership"  — the roster was unreadable, so the
//        write was refused rather than risk a zero-owner project (retryable)
//   400 "invalid project role"                — malformed id / unknown role
export const projectRolesApi = {
  // VIEWER-gated: anyone holding a role may see who else holds one (governance
  // metadata, not a credential).
  list: async (projectId: string): Promise<ProjectRoleRecord[]> =>
    (await client.get<ProjectRoleRecord[]>(`/api/v1/projects/${projectId}/roles`)).data,
  // 201. OWNER-gated. The store UPSERTS on (project, principal), so granting an
  // existing principal changes their role rather than duplicating the row.
  grant: async (projectId: string, body: ProjectRoleCreate): Promise<ProjectRoleRecord> =>
    (await client.post<ProjectRoleRecord>(`/api/v1/projects/${projectId}/roles`, body)).data,
  // OWNER-gated. The PATH principal_id is authoritative — the backend overwrites any
  // body `principal_id`, so this can never retarget a different principal than the URL
  // names. Because it is a re-grant, the last-owner guard applies (409).
  //
  // The body is a FULL ProjectRoleCreate, not just the new role: the backend's update IS
  // the store's upsert, so it rewrites the whole row. Sending a partial body would
  // silently rewrite `principal_type`/`principal_display` to the defaults — flipping a
  // group row to "user" and blanking its display name. Callers pass the existing row's
  // values through with only `role` changed.
  update: async (
    projectId: string,
    principalId: string,
    body: ProjectRoleCreate,
  ): Promise<ProjectRoleRecord> =>
    (
      await client.put<ProjectRoleRecord>(`/api/v1/projects/${projectId}/roles/${principalId}`, body)
    ).data,
  // 204. OWNER-gated. Refuses to strip the LAST owner (409) — an ownerless project is
  // unadministerable, since nobody could grant the role back.
  revoke: async (projectId: string, principalId: string): Promise<void> => {
    await client.delete(`/api/v1/projects/${projectId}/roles/${principalId}`);
  },
};

// --- GitHub-backed template catalog (Epic 22 / Task 2) ---------------------
// The connection-scoped successor to opsTemplatesApi (S3+DDB). Templates ARE repos
// in the org, keyed by `name` (NO opaque id / s3_key / version / file_count).
// `TemplateView` is the read-model; metadata (framework / aws_services / tags) rides
// on the repo's GitHub topics server-side. Every op is scoped to a `connectionId`.
export interface TemplateView {
  name: string;
  description: string;
  framework: string;
  aws_services: string[];
  tags: string[];
  html_url: string;
  updated_at: string;
}
// Write-only PATCH body — editable metadata only (never the name).
export interface TemplatePatch {
  description?: string;
  aws_services?: string[];
  tags?: string[];
  framework?: string;
}
export const githubTemplatesApi = {
  list: async (connectionId: string): Promise<TemplateView[]> =>
    (await client.get<TemplateView[]>(`/api/v1/github-templates`, { params: { connection_id: connectionId } })).data,
  // Multipart POST: `file` (the zip) + form fields. `aws_services`/`tags` are backend
  // FastAPI `Form([])` REPEATED fields — append each value under the SAME key (NOT CSV/JSON).
  upload: async (
    connectionId: string,
    file: File,
    meta: { name: string; framework: string; description?: string; aws_services?: string[]; tags?: string[] }
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

// --- The RECONCILE surface (Epic 22 / Task 5, rebuilt in E28C/T3-T6) --------
// The ONE place AGP's template registry and a connected org's actual repositories are
// compared, plus the three verbs that act on what it found. All ADMIN-gated, on the
// /admin/connections router.
//
// `reconcile` is the only READ in this codebase that talks to a provider: one paginated
// `list_repos`, one registry read, and a `read_repo` probe per seed-or-registered row.
// That cost is why it is spent on a modal's OPEN and its explicit Refresh only — no
// list or page route calls it, so the Templates page stays registry-only and instant.
//
// THE BOOLEAN IS GONE, NOT RENAMED, and the three fields it came with are deleted from
// the wire (their names are grep-forbidden in `src/` by `reconcileView.test.ts`, which is
// why this comment describes them rather than quoting them). Both were wrong in ways that
// cost a live repository on 2026-08-04:
//   • The "does this exist in the org?" boolean was answered from AGP's own DDB catalog —
//     evidence about AGP's store, never about the org — so a registered template whose
//     repo had been deleted read as present (nothing offered; the only route back was a
//     hand delete of the record), and a repo that already carried a seed's name read as
//     absent (rollout POSTed `/orgs/{org}/repos`, GitHub 422'd, and it surfaced as a 502
//     inside "connecting failed").
//   • A pair of flags expressed the infra repo's forcedness and its template-vs-infra
//     kind. Forcedness is STRUCTURAL now — `infra_repo` is its own field below — so it
//     cannot be turned into a choice by flipping a boolean.
// `state` replaces all three and has four values, so there is nothing for a boolean to
// carry. `frontend/src/components/operations/reconcileView.ts` maps each state to the
// ONE action behind it.
export interface ReconcileItem {
  name: string;
  /** "seed" = AGP ships a scaffold; "registry" = a record with no seed; "org" = found, unknown. */
  origin: 'seed' | 'registry' | 'org';
  state: 'registered_present' | 'registered_missing' | 'unregistered_present' | 'seed_absent';
  /** What `read_repo` found. `null` = nothing to read. */
  default_branch: string | null;
  /**
   * `null` for an absent repo, for an org-origin row (present by construction from the
   * listing and deliberately NOT re-probed — the ruled cost model), AND for a repository
   * that exists with no commit yet. So a null head is never evidence of absence.
   */
  head_sha: string | null;
}
export interface ReconcileView {
  templates: ReconcileItem[];
  /** SEPARATE, not a flagged row: always ensured by every rollout, never a choice. */
  infra_repo: ReconcileItem;
}
export interface RolloutResult {
  items: {
    name: string;
    // Every word is derived from the OBSERVED state, so none of them overstates what
    // happened. "overwritten" = a re-push on top of a registered template (E28C/T3
    // deleted delete+recreate, so nothing is destroyed and history is preserved);
    // "recreated" = the record existed but the repo was gone and was rebuilt from seed;
    // "created" = genuinely new. A "skipped" row for a repo that exists but is not a
    // registered template carries a `reason` pointing at ADOPT — rollout refuses that
    // state outright, whatever `overwrite` says.
    action: 'created' | 'overwritten' | 'recreated' | 'skipped' | 'adopted';
    reason?: string;
  }[];
}
export const rolloutApi = {
  reconcile: async (connectionId: string): Promise<ReconcileView> =>
    (await client.get<ReconcileView>(`/api/v1/admin/connections/${connectionId}/rollout/reconcile`)).data,
  // TWO SEPARATE CONSENTS (E28D), both required and both defaulted `false` server-side.
  // `overwrite` re-pushes the SELECTED TEMPLATES; `overwrite_infra` re-pushes AGP's Terraform module
  // into an `agp-runtime-infra` that already exists. They were one flag with two consumers, so
  // ticking a template re-push wrote to the org's infra repo and reported "overwritten" — a write
  // nobody named. Neither is optional in this type: the surface must state which consent it is
  // sending, and an omitted field would silently mean "false" for a request that meant to ask.
  // Creating the infra repo when it is ABSENT stays unconditional and needs no flag.
  rollout: async (
    connectionId: string,
    body: { template_names: string[]; overwrite: boolean; overwrite_infra: boolean },
  ): Promise<RolloutResult> =>
    (await client.post<RolloutResult>(`/api/v1/admin/connections/${connectionId}/rollout`, body)).data,
  // ADOPT: register an existing org repository as one of this org's templates (E28C/T3).
  // A governance statement — NO content inspection and NO push, because materialize reads
  // the template repo at use-time anyway and pushing would overwrite the very repository
  // the operator is adopting BECAUSE they wrote it. Returns the ordinary `TemplateView`, so
  // an adopted template is indistinguishable from an uploaded one downstream.
  //
  // There is deliberately no `created_by` in the body: the actor comes from the validated
  // principal server-side. 404 = not in the org, 409 = AGP already accounts for it (already
  // registered / the infra repo / a materialized agent repo — the server enforces all three
  // and its message names which), 422 = an illegal name. Classify with
  // `classifyRolloutError`, never render the raw message.
  adopt: async (
    connectionId: string,
    body: { repo_name: string; description?: string },
  ): Promise<TemplateView> =>
    (await client.post<TemplateView>(`/api/v1/admin/connections/${connectionId}/templates/adopt`, body)).data,
};

// --- Observability (Epic 26) -----------------------------------------------
// The platform's own Langfuse-backed dashboards (C4→C5). `getSettings` is the
// detection probe (`configured` = the backend has LANGFUSE_HOST) + the embed
// host for the Langfuse tab. `getMetrics` fans the per-agent Langfuse reads out
// server-side and returns totals ⊕ by_agent[] for the chosen scope; the per-agent
// `getAgentMetrics`/`getAgentTraces` back the AgentDetail Traces + Cost tabs (T9).
// All VIEWER-readable + tenant-scoped server-side (foreign agent → 404). Types
// mirror C3/C4 exactly; the axios interceptor injects auth + unwraps errors.
export const observabilityApi = {
  getSettings: async (): Promise<ObservabilitySettings> =>
    (await client.get<ObservabilitySettings>(`/api/v1/observability/settings`)).data,
  getMetrics: async (
    scope: 'platform' | 'tenant' | 'project',
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
  ): Promise<{ data: TraceRow[]; total: number }> =>
    (await client.get<{ data: TraceRow[]; total: number }>(`/api/v1/agents/${agentId}/traces`, {
      params: { page: params.page, limit: params.limit },
    })).data,
};
