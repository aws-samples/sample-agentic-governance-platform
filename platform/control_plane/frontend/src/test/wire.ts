import type {
  ContainmentView,
  DatasetItemOutput,
  DatasheetOutput,
  Deployment,
  PrincipalHit,
  PublishRequest,
  Repository,
  StepState,
  TemplateView,
  TenantStageConfigOutput,
} from '../api/client';

export function deployment(overrides: Partial<Deployment> = {}): Deployment {
  return {
    actor: null,
    actor_kind: null,
    agent_id: 'a-1',
    build_id: null,
    completed_at: null,
    error: null,
    id: 'dep-11111111',
    image_tag: 'a-1-tree000',
    mcp_server_id: null,
    outcome: 'succeeded',
    repo_id: 'repo-1',
    seq_key: 'repo-1#uat#2026-07-31T10:00:00Z#1111',
    source_sha: null,
    stage: 'uat',
    started_at: '2026-07-31T10:00:00Z',
    ...overrides,
  };
}

export function tenantStageConfig(overrides: Partial<TenantStageConfigOutput> = {}): TenantStageConfigOutput {
  return {
    account_id: '111122223333',
    region: 'us-east-1',
    ecr_repo_uri: '',
    push_role_arn: '',
    deploy_role_arn: '',
    ...overrides,
  };
}

export function stepState(overrides: Partial<StepState> = {}): StepState {
  return {
    completed_at: null,
    error: null,
    key: 's-1',
    label: 'step',
    started_at: null,
    status: 'pending',
    ...overrides,
  };
}

export function datasetItem(overrides: Partial<DatasetItemOutput> = {}): DatasetItemOutput {
  return {
    assertions: [],
    expected_output: null,
    expected_tools: [],
    id: 'i1',
    input: 'now',
    source_trace_id: null,
    tags: [],
    ...overrides,
  };
}

export function containmentView(overrides: Partial<ContainmentView> = {}): ContainmentView {
  return {
    agent_id: 'a-1',
    state: null,
    job: null,
    since: null,
    by_name: null,
    by_oid: null,
    reason: null,
    signal_at: null,
    steps: [],
    signals: [],
    monitor: { enabled: true },
    audit: [],
    cloudtrail_url: 'https://console.aws.amazon.com/cloudtrail/home#/events?EventName=CreatePolicy',
    ...overrides,
  };
}

export function datasheet(overrides: Partial<DatasheetOutput> = {}): DatasheetOutput {
  return {
    owner_team: 'Claims Platform',
    support_contact: 'claims-support@example.com',
    data_classification: 'Internal',
    sla_tier: null,
    compliance: [],
    support_hours: null,
    version: null,
    region: null,
    guardrails: [],
    pitch: null,
    ...overrides,
  };
}

export function publishRequest(overrides: Partial<PublishRequest> = {}): PublishRequest {
  return {
    id: 'pub-abc1234567',
    product_type: 'agent',
    product_id: 'agent-1',
    product_name: 'FNOL Agent',
    tenant_id: null,
    datasheet: datasheet(),
    status: 'pending',
    requested_by: 'oid-1',
    requested_by_email: null,
    decided_by: null,
    decided_at: null,
    decision_reason: null,
    error: null,
    created_at: '2026-08-10T00:00:00Z',
    updated_at: '2026-08-10T00:00:00Z',
    ...overrides,
  };
}

export function principalHit(overrides: Partial<PrincipalHit> = {}): PrincipalHit {
  return { id: 'u1', display_name: 'Ana', type: 'user', mail: null, ...overrides };
}

export function repository(overrides: Partial<Repository> = {}): Repository {
  return {
    agent_id: null,
    artifact_kind: 'agent',
    build_kind: null,
    cicd_status: 'provisioning',
    created_at: '',
    created_by: 'lars',
    dev_outcome_for_candidate: null,
    eval_evidence: null,
    eval_gate: null,
    eval_regressions: null,
    gateway_arn: null,
    gateway_id: null,
    gateway_url: null,
    id: 'repo-1',
    last_build_started_at: null,
    last_dev_digest: null,
    last_dev_image_tag: null,
    last_promoted_at: null,
    last_promoted_by: null,
    last_promoted_digest: null,
    last_promoted_image_tag: null,
    last_promotion_build_id: null,
    mcp_server_id: null,
    name: 'repo',
    prod_candidate_actor: null,
    prod_candidate_at: null,
    prod_candidate_digest: null,
    prod_candidate_image_tag: null,
    prod_candidate_sha: null,
    prod_candidate_status: null,
    project_id: 'p1',
    repo_url: null,
    status: 'provisioning',
    steps: [],
    template_name: 'template',
    updated_at: '',
    ...overrides,
  };
}

export function templateView(overrides: Partial<TemplateView> = {}): TemplateView {
  return {
    artifact_kind: 'agent',
    aws_services: [],
    build_kind: 'container',
    description: '',
    framework: '',
    html_url: '',
    infra_template_id: 'x',
    name: 'template',
    platform: 'aws',
    preview: false,
    tags: [],
    updated_at: '',
    ...overrides,
  };
}
