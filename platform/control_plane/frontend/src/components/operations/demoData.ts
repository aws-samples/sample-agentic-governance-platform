// Shared "Claims Triage" demo scenario — the single source of seed data for the
// Operations Build & Run demo (Epic 18). Pure data + pure selectors so every Ops
// page (Studio, Playground, Experiments, Tenants, Model Catalog, Projects,
// Overview, …) renders the same reconciled world. House style mirrors
// govern/mockData.ts: TS interfaces + `export const` arrays + pure helper fns.

// ─────────────────────────── Skills ───────────────────────────
export interface Skill {
  id: string;
  name: string;
  description: string;
  category: string;
}

export const SKILLS: Skill[] = [
  { id: 'pdf-extract', name: 'PDF Extract', description: 'Pull structured fields from claim PDFs and scanned attachments.', category: 'Document' },
  { id: 'email-classify', name: 'Email Classify', description: 'Classify inbound claims emails by intent, line of business, and urgency.', category: 'Classification' },
  { id: 'sentiment', name: 'Sentiment', description: 'Score claimant tone to flag escalations and dissatisfaction.', category: 'Analysis' },
  { id: 'policy-lookup', name: 'Policy Lookup', description: 'Resolve the policy number against the claims database for coverage and limits.', category: 'Retrieval' },
  { id: 'route', name: 'Route', description: 'Route the triaged claim to the right adjuster queue or workflow.', category: 'Routing' },
  { id: 'summarize', name: 'Summarize', description: 'Produce a concise adjuster-ready summary of the claim context.', category: 'Analysis' },
];

// ─────────────────────────── MCP servers ───────────────────────────
export interface McpServer {
  id: string;
  name: string;
  description: string;
}

export const MCP_SERVERS: McpServer[] = [
  { id: 'claims-db', name: 'Claims DB', description: 'Read-only MCP over the claims of record — policies, coverage, and claim history.' },
  { id: 'document-store', name: 'Document Store', description: 'MCP exposing the claims document repository for attachments and forms.' },
  { id: 'notifications', name: 'Notifications', description: 'MCP for adjuster and claimant notifications across email and SMS.' },
];

// ─────────────────────────── Models ───────────────────────────
export interface ModelOption {
  id: string;
  label: string;
  provider: 'Anthropic' | 'Google' | 'OpenAI';
  inputPer1k: number;
  outputPer1k: number;
}

export const MODELS: ModelOption[] = [
  { id: 'claude-opus-4-8', label: 'Claude Opus 4.8', provider: 'Anthropic', inputPer1k: 0.015, outputPer1k: 0.075 },
  { id: 'gemini-2-5-pro', label: 'Gemini 2.5 Pro', provider: 'Google', inputPer1k: 0.00125, outputPer1k: 0.005 },
  { id: 'gpt-5', label: 'GPT-5', provider: 'OpenAI', inputPer1k: 0.00125, outputPer1k: 0.01 },
];

// ─────────────────────────── Tenants (sandbox + production accounts) ───────────────────────────
export interface Tenant {
  id: string;
  account: string;
  name: string;
  businessUnit: string;
  region: string;
  kind: 'sandbox' | 'production';
  status: 'active' | 'provisioning';
}

export const TENANTS: Tenant[] = [
  { id: 'aws://4471-2093-8856', account: 'aws://4471-2093-8856', name: 'Claims Sandbox', businessUnit: 'Insurance', region: 'us-east-1', kind: 'sandbox', status: 'active' },
  { id: 'aws://4471-2093-9921', account: 'aws://4471-2093-9921', name: 'FNOL Sandbox', businessUnit: 'Insurance', region: 'us-west-2', kind: 'sandbox', status: 'active' },
  { id: 'aws://5582-3104-7733', account: 'aws://5582-3104-7733', name: 'Claims Production', businessUnit: 'Insurance', region: 'us-east-1', kind: 'production', status: 'active' },
  { id: 'aws://5582-3104-8810', account: 'aws://5582-3104-8810', name: 'Contact Center Production', businessUnit: 'Customer Operations', region: 'eu-west-1', kind: 'production', status: 'provisioning' },
];

// ─────────────────────────── Selectors ───────────────────────────

/** Per-1k token cost: (in/1000 * inputPer1k) + (out/1000 * outputPer1k). */
export function estimateCost(model: ModelOption, inTok: number, outTok: number): number {
  return (inTok / 1000) * model.inputPer1k + (outTok / 1000) * model.outputPer1k;
}

// Keyword → skill-id mapping for the Claims Triage scenario. Order of the
// resulting Skill[] follows SKILLS order so the chain reads naturally.
const USE_CASE_KEYWORDS: { keyword: string; skillIds: string[] }[] = [
  { keyword: 'claim', skillIds: ['pdf-extract', 'policy-lookup', 'route'] },
  { keyword: 'email', skillIds: ['email-classify', 'sentiment', 'route'] },
  { keyword: 'pdf', skillIds: ['pdf-extract'] },
  { keyword: 'sentiment', skillIds: ['sentiment'] },
  { keyword: 'policy', skillIds: ['policy-lookup'] },
  { keyword: 'route', skillIds: ['route'] },
  { keyword: 'triage', skillIds: ['email-classify', 'policy-lookup', 'route'] },
];

// Default chain when nothing matches — never returns empty.
const DEFAULT_SKILL_IDS = ['email-classify', 'policy-lookup', 'route'];

/**
 * Deterministic keyword match → ordered, de-duplicated Skills. Lowercases the
 * input, collects skill ids from every matched keyword, then maps back through
 * SKILLS (so order + identity are stable). Falls back to a 3-skill chain when
 * no keyword matches, so the result is never empty.
 */
export function findSkillsForUseCase(useCase: string): Skill[] {
  const text = useCase.toLowerCase();
  const matchedIds = new Set<string>();
  for (const { keyword, skillIds } of USE_CASE_KEYWORDS) {
    if (text.includes(keyword)) {
      for (const id of skillIds) matchedIds.add(id);
    }
  }
  const ids = matchedIds.size > 0 ? matchedIds : new Set(DEFAULT_SKILL_IDS);
  return SKILLS.filter((s) => ids.has(s.id));
}
