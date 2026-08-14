export interface ProjectCreate {
  project_name: string;
  framework: 'langraph' | 'strands';
  iac_type: 'terraform' | 'cdk' | 'cloudformation';
  aws_region: string;
  tags?: Record<string, string>;
}

export interface ProjectResponse {
  id: string;
  project_name: string;
  framework: string;
  template_name: string;
  iac_type: string;
  aws_region: string;
  tags?: Record<string, string>;
  s3_url: string;
  expires_at: string;
  created_by: string;
  created_at: string;
}

export interface ApiError {
  detail: string;
  error?: string;
}

// Observability (Epic 26) ----------------------------------------------------
// Mirror the backend C3/C4 shapes exactly (snake_case as the API emits). The
// platform renders its OWN dashboards from these — the Langfuse metrics API is
// fanned out + aggregated server-side per scope (agent/project/tenant/platform).

// GET /observability/settings — `configured` = bool(LANGFUSE_HOST); host is the
// embed/"open in new tab" target for the Langfuse tab (null when unset).
export interface ObservabilitySettings {
  langfuse_host: string | null;
  configured: boolean;
}

// GET /agents/{id}/metrics and the aggregate half of /observability/metrics.
// `daily[]` is the normalized daily-metrics rows; `by_model[]` is per-model cost
// + token usage. All numeric fields are zeroed (lists empty) for an
// unprovisioned / no-data agent — never absent.
export interface AgentMetrics {
  totals: { traces: number; cost_usd: number; tokens: number };
  daily: { date: string; traces: number; cost_usd: number; tokens: number }[];
  by_model: { model: string; cost_usd: number; tokens: number }[];
  // Spend attributed to the invoking user (the Entra identity the agent stamps on each
  // trace). `user_id` is "" for traces with no caller, e.g. a direct runtime invoke.
  by_user: { user_id: string; cost_usd: number; traces: number }[];
}

// GET /observability/metrics?scope=… — AgentMetrics ⊕ the per-agent breakdown
// (one row per visible agent in scope; `tenant_id` may be null for pre-E24
// records). The dashboard's by-agent table + drill-down links read `by_agent`.
export interface ScopeMetrics extends AgentMetrics {
  by_agent: {
    agent_id: string;
    agent_name: string;
    tenant_id: string | null;
    totals: { traces: number; cost_usd: number; tokens: number };
  }[];
}

// GET /agents/{id}/traces — one row per Langfuse trace (the Traces tab list).
// `timestamp`, `name`, `latency_ms` and `user_id` mirror the backend TraceRow's
// Optional fields (langfuse_metrics_service.py) — Langfuse may omit any of them
// on a given trace, so the Traces tab renders them null-safely (a "—" cell).
// `id` and `cost_usd` are non-null server-side (cost_usd defaults to 0.0).
export interface TraceRow {
  id: string;
  timestamp: string | null;
  name: string | null;
  user_id: string | null;
  latency_ms: number | null;
  cost_usd: number;
}

// --- Guardrails ---

export type GuardrailFilterStrength = 'NONE' | 'LOW' | 'MEDIUM' | 'HIGH';
export type GuardrailFilterType = 'HATE' | 'INSULTS' | 'SEXUAL' | 'VIOLENCE' | 'MISCONDUCT' | 'PROMPT_ATTACK';
export type GuardrailPiiAction = 'BLOCK' | 'ANONYMIZE';
export type GuardrailStatus = 'draft' | 'creating' | 'active' | 'updating' | 'failed' | 'deleting' | 'deleted';

export interface ContentFilterConfig {
  type: GuardrailFilterType;
  input_strength: GuardrailFilterStrength;
  output_strength: GuardrailFilterStrength;
}

export interface DeniedTopic {
  name: string;
  definition: string;
  examples: string[];
}

export interface PiiEntityConfig {
  type: string;
  action: GuardrailPiiAction;
}

export interface SensitiveRegexConfig {
  name: string;
  pattern: string;
  description?: string;
  action: GuardrailPiiAction;
}

export interface WordFilterConfig {
  enable_profanity: boolean;
  blocked_words: string[];
}

export interface ContextualGroundingConfig {
  enabled: boolean;
  grounding_threshold: number;
  relevance_threshold: number;
}

export interface GuardrailTemplateCreate {
  name: string;
  description?: string;
  content_filters: ContentFilterConfig[];
  denied_topics: DeniedTopic[];
  pii_entities: PiiEntityConfig[];
  sensitive_regexes: SensitiveRegexConfig[];
  word_filter?: WordFilterConfig;
  contextual_grounding?: ContextualGroundingConfig;
}

export interface GuardrailTemplate {
  template_id: string;
  name: string;
  description?: string;
  status: GuardrailStatus;
  guardrail_id?: string;
  guardrail_arn?: string;
  guardrail_version?: string;
  content_filters: ContentFilterConfig[];
  denied_topics: DeniedTopic[];
  pii_entities: PiiEntityConfig[];
  sensitive_regexes: SensitiveRegexConfig[];
  word_filter?: WordFilterConfig;
  contextual_grounding?: ContextualGroundingConfig;
  status_history: { status: string; timestamp: string; message?: string }[];
  created_by?: string;
  created_at: string;
  updated_at: string;
}

export interface GuardrailPreset {
  id: string;
  name: string;
  description: string;
  tags: string[];
  config: GuardrailTemplateCreate;
}

export interface GuardrailMetrics {
  guardrail_id: string;
  total_invocations: number;
  blocked_count: number;
  allowed_count: number;
  anonymized_count: number;
  block_rate: number;
  top_triggered_filter?: string;
  filter_breakdown: Record<string, number>;
  time_series: { timestamp: string; invocations: number }[];
  recent_events: GuardrailEvent[];
}

export interface GuardrailEvent {
  timestamp: string;
  guardrail_id: string;
  guardrail_name?: string;
  action: string;
  filter_type?: string;
  input_snippet?: string;
  details?: Record<string, any>;
}

