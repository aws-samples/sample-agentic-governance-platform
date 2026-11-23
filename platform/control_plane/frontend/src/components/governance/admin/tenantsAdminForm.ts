import type {
  DatabricksStageConfigInput,
  DatabricksStageConfigOutput,
  PrincipalHit,
  TenantCreate,
  TenantUpdate,
  TenantView,
  TenantPlatform,
  TenantStageConfigInput,
} from '../../../api/client';

export interface TenantGroup {
  id: string;
  display_name: string;
}

export type StageDraft = Required<TenantStageConfigInput>;

export interface DatabricksStageDraft {
  workspace_url: string;
  workspace_id: string;
  cloud: string;
  region: string;
  account_id: string;
  sp_client_id: string;
  /** Write-only: what the admin typed; blank on an edit keeps the stored secret. */
  sp_client_secret: string;
  /** The stored pointer, re-emitted because the update replaces the whole stage object. */
  sp_client_secret_arn: string;
  has_sp_secret: boolean;
}

export interface TenantDraft {
  name: string;
  line_of_business: string;
  description: string;
  platform?: TenantPlatform;
  /** The ordered stage list; absent means the keys of the platform's own stage map. */
  stageKeys?: string[];
  stages: Record<string, StageDraft>;
  databricks?: Record<string, DatabricksStageDraft>;
  /** Write-only account-admin pair, Databricks only: both halves or neither. */
  account_admin_client_id?: string;
  account_admin_secret?: string;
  /** A credential is stored on the record (its Secrets Manager pointer is present); blank boxes on an edit keep it. */
  has_account_admin_credential?: boolean;
  groups: TenantGroup[];
}

export function draftPlatform(d: TenantDraft): TenantPlatform {
  return d.platform ?? 'aws';
}

export function draftDatabricksStages(d: TenantDraft): Record<string, DatabricksStageDraft> {
  const existing = d.databricks ?? {};
  const out: Record<string, DatabricksStageDraft> = {};
  for (const key of draftStageKeys(d)) out[key] = existing[key] ?? emptyDatabricksStage();
  return out;
}

function emptyStage(): StageDraft {
  return { account_id: '', region: 'us-east-1', ecr_repo_uri: '', push_role_arn: '', deploy_role_arn: '' };
}

function emptyDatabricksStage(): DatabricksStageDraft {
  return {
    workspace_url: '',
    workspace_id: '0',
    cloud: 'aws',
    region: '',
    account_id: '',
    sp_client_id: '',
    sp_client_secret: '',
    sp_client_secret_arn: '',
    has_sp_secret: false,
  };
}

export const EMPTY_TENANT_DRAFT: TenantDraft = {
  name: '',
  line_of_business: '',
  description: '',
  platform: 'aws',
  stages: { dev: emptyStage() },
  databricks: { dev: emptyDatabricksStage() },
  account_admin_client_id: '',
  account_admin_secret: '',
  has_account_admin_credential: false,
  groups: [],
};

export const SEED_STAGE_KEY = 'dev';
const SUGGESTED_STAGE_KEY = 'prod';

export type StageKey = string;

export function draftStageKeys(d: TenantDraft): string[] {
  if (d.stageKeys) return d.stageKeys;
  const map = draftPlatform(d) === 'databricks' ? (d.databricks ?? {}) : d.stages;
  return Object.keys(map);
}

export function nextSuggestedStageKey(d: TenantDraft): string | null {
  return draftStageKeys(d).includes(SUGGESTED_STAGE_KEY) ? null : SUGGESTED_STAGE_KEY;
}

export function addStage(d: TenantDraft, key: string): TenantDraft {
  const k = key.trim();
  const keys = draftStageKeys(d);
  if (k.length === 0 || keys.includes(k)) return d;
  return {
    ...d,
    stageKeys: [...keys, k],
    stages: { ...d.stages, [k]: emptyStage() },
    databricks: { ...(d.databricks ?? {}), [k]: emptyDatabricksStage() },
  };
}

export function removeStage(d: TenantDraft, key: string): TenantDraft {
  const keys = draftStageKeys(d);
  if (!keys.includes(key) || keys.length <= 1) return d;
  const stages = { ...d.stages };
  delete stages[key];
  const databricks = { ...(d.databricks ?? {}) };
  delete databricks[key];
  return { ...d, stageKeys: keys.filter((x) => x !== key), stages, databricks };
}

export function isValidAccountId(v: string): boolean {
  return /^\d{12}$/.test(v);
}

const WORKSPACE_URL_RE = new RegExp('^https://[a-z0-9][a-z0-9.-]+[a-z0-9]$');

export function isValidWorkspaceUrl(v: string): boolean {
  const m = WORKSPACE_URL_RE.exec(v);
  return m !== null && m[0].length === v.length;
}

const WORKSPACE_ID_RE = new RegExp('^[0-9]+$');

export function isValidWorkspaceId(v: string): boolean {
  const m = WORKSPACE_ID_RE.exec(v);
  return m !== null && m[0].length === v.length;
}

export function isDatabricksStageComplete(s: DatabricksStageDraft): boolean {
  return (
    isValidWorkspaceUrl(s.workspace_url.trim()) &&
    isValidWorkspaceId(s.workspace_id.trim()) &&
    s.sp_client_id.trim().length > 0 &&
    (s.sp_client_secret.length > 0 || s.has_sp_secret)
  );
}

export function isAccountAdminCredentialUsable(d: TenantDraft): boolean {
  const id = (d.account_admin_client_id ?? '').trim();
  const secret = d.account_admin_secret ?? '';
  return (id.length === 0 && secret.length === 0) || (id.length > 0 && secret.length > 0);
}

export function hasAccountAdminCredential(d: TenantDraft): boolean {
  const id = (d.account_admin_client_id ?? '').trim();
  const secret = d.account_admin_secret ?? '';
  return (id.length > 0 && secret.length > 0) || d.has_account_admin_credential === true;
}

export function canSubmit(d: TenantDraft, isEdit = false): boolean {
  const sharedOk =
    d.name.trim().length > 0 && d.line_of_business.trim().length > 0 && d.groups.length > 0;
  if (!sharedOk) return false;
  const keys = draftStageKeys(d);
  if (keys.length === 0) return false;
  if (draftPlatform(d) === 'databricks') {
    const db = draftDatabricksStages(d);
    return (
      isAccountAdminCredentialUsable(d) &&
      (isEdit || hasAccountAdminCredential(d)) &&
      keys.every((k) => isDatabricksStageComplete(db[k]))
    );
  }
  return keys.every((k) => isValidAccountId((d.stages[k]?.account_id ?? '').trim()));
}

function trimStage(s: StageDraft): TenantStageConfigInput {
  return {
    account_id: s.account_id.trim(),
    region: s.region.trim(),
    ecr_repo_uri: s.ecr_repo_uri.trim(),
    push_role_arn: s.push_role_arn.trim(),
    deploy_role_arn: s.deploy_role_arn.trim(),
  };
}

function trimDatabricksStage(
  s: DatabricksStageDraft,
): DatabricksStageConfigInput {
  const out: DatabricksStageConfigInput = {
    workspace_url: s.workspace_url.trim(),
    workspace_id: s.workspace_id.trim(),
    cloud: s.cloud.trim(),
    region: s.region.trim(),
    account_id: s.account_id.trim(),
    sp_client_id: s.sp_client_id.trim(),
    sp_client_secret_arn: s.sp_client_secret_arn.trim(),
  };
  if (s.sp_client_secret.length > 0) out.sp_client_secret = s.sp_client_secret;
  return out;
}

export function buildCreatePayload(d: TenantDraft): TenantCreate {
  const base = {
    name: d.name.trim(),
    line_of_business: d.line_of_business.trim(),
    description: d.description.trim(),
    entra_group_ids: d.groups.map((g) => g.id),
  };
  const keys = draftStageKeys(d);
  if (draftPlatform(d) === 'databricks') {
    const db = draftDatabricksStages(d);
    const stages: Record<string, DatabricksStageConfigInput> = {};
    for (const k of keys) stages[k] = trimDatabricksStage(db[k]);
    const payload: TenantCreate = { ...base, platform: 'databricks', stages };
    const adminId = (d.account_admin_client_id ?? '').trim();
    const adminSecret = d.account_admin_secret ?? '';
    if (adminId.length > 0 && adminSecret.length > 0) {
      payload.account_admin_client_id = adminId;
      payload.account_admin_secret = adminSecret;
    }
    return payload;
  }
  const stages: Record<string, TenantStageConfigInput> = {};
  for (const k of keys) stages[k] = trimStage(d.stages[k] ?? emptyStage());
  return { ...base, stages };
}

export function buildUpdatePayload(d: TenantDraft): TenantUpdate {
  const payload = buildCreatePayload(d);
  delete payload.platform;
  return payload;
}

export function addGroup(groups: TenantGroup[], hit: PrincipalHit): TenantGroup[] {
  if (groups.some((g) => g.id === hit.id)) return groups;
  return [...groups, { id: hit.id, display_name: hit.display_name }];
}

export function removeGroup(groups: TenantGroup[], id: string): TenantGroup[] {
  return groups.filter((g) => g.id !== id);
}

export function groupsFromIds(ids: string[]): TenantGroup[] {
  return ids.map((id) => ({ id, display_name: id }));
}

export function filterGroupHits(hits: PrincipalHit[], selected: TenantGroup[]): PrincipalHit[] {
  const selectedIds = new Set(selected.map((g) => g.id));
  return hits.filter((h) => h.type === 'group' && !selectedIds.has(h.id));
}

export const PLATFORM_OPTIONS: { value: TenantPlatform; label: string; hint: string }[] = [
  { value: 'aws', label: 'AWS', hint: 'Agents run on Bedrock AgentCore in the tenant’s AWS accounts.' },
  {
    value: 'databricks',
    label: 'Databricks',
    hint: 'Agents run as Databricks Apps or serving endpoints in the tenant’s workspaces.',
  },
];

export function platformLabel(p: TenantPlatform): string {
  return PLATFORM_OPTIONS.find((o) => o.value === p)?.label ?? p;
}

// Rationale: docs/databricks-onboarding.md "After the tenant connects", "Troubleshooting" (binding mode badges)
export type BadgedBindingMode = Exclude<TenantView['binding_mode'], ''>;

export const BINDING_MODE_LABEL: Record<BadgedBindingMode, string> = {
  federation: 'Federation',
  invoke_unavailable: 'Invoke unavailable — federation required',
  sp_secret: 'SP secret',
};

export function bindingModeLabel(mode: TenantView['binding_mode'] | string | undefined): string | null {
  return BINDING_MODE_LABEL[mode as BadgedBindingMode] ?? null;
}

const COMPACT_BINDING_MODE_LABEL: Record<BadgedBindingMode, string> = {
  federation: 'Federation',
  invoke_unavailable: 'Invoke unavailable',
  sp_secret: 'SP secret',
};

export function compactBindingModeLabel(
  mode: TenantView['binding_mode'] | string | undefined,
): string | null {
  return COMPACT_BINDING_MODE_LABEL[mode as BadgedBindingMode] ?? null;
}

export const PRIVILEGE_STATEMENT: Record<TenantPlatform, string> = {
  aws:
    'AGP assumes the per-stage deploy role you supply below to provision each stage’s runtime, ' +
    'which is what lets AGP deploy and invoke this tenant’s agents. The push role is never ' +
    'assumed by AGP — it is trusted to GitHub Actions, which builds and pushes this tenant’s ' +
    'images with it. Given only an account id and no assumable deploy role, AGP can still ' +
    'discover and catalogue the tenant’s agents but stays inventory-only — governed and ' +
    'observable, never deployed or invoked by AGP.',
  databricks:
    'AGP uses the account-admin credential you supply below to establish federation, so ' +
    'Databricks and Unity Catalog see the real caller and AGP can invoke this tenant’s agents ' +
    'under each user’s own identity. Without it the tenant is still discoverable, registrable ' +
    'and observable, but invoke is refused. Federation also needs user sync from Entra to ' +
    'Databricks.',
};

export const BINDING_MODE_HINT: Record<BadgedBindingMode, string> = {
  federation:
    'Databricks and Unity Catalog see the real caller — per-caller audit and tools are possible.',
  invoke_unavailable:
    'Invoke is refused for this tenant — discovery, registration and observability still work. Federation needs an account-admin credential (client id + secret) on the tenant and user sync from Entra to Databricks.',
  sp_secret:
    'Calls are attributed to the platform’s service principal in Databricks audit logs — not the individual caller.',
};

export interface CapabilityBadge {
  key: 'can_discover' | 'account_admin' | 'user_sync';
  label: string;
  /** `undefined` when the probe never ran; never read as "no". */
  on: boolean | undefined;
}

const CAPABILITY_LABELS: { key: CapabilityBadge['key']; label: string }[] = [
  { key: 'can_discover', label: 'Discovery' },
  { key: 'account_admin', label: 'Account admin' },
  { key: 'user_sync', label: 'User sync' },
];

export function capabilityBadges(
  platform: TenantPlatform,
  capabilities: TenantView['capabilities'] | undefined,
): CapabilityBadge[] {
  if (platform !== 'databricks') return [];
  return CAPABILITY_LABELS.map(({ key, label }) => ({ key, label, on: capabilities?.[key] }));
}

export function hasBeenProbed(capabilities: TenantView['capabilities'] | undefined): boolean {
  if (!capabilities) return false;
  return CAPABILITY_LABELS.some(({ key }) => capabilities[key] !== undefined);
}

export function databricksStageDraftFromConfig(
  c: DatabricksStageConfigOutput | undefined,
): DatabricksStageDraft {
  if (!c) return emptyDatabricksStage();
  return {
    workspace_url: c.workspace_url,
    workspace_id: c.workspace_id,
    cloud: c.cloud || 'aws',
    region: c.region,
    account_id: c.account_id,
    sp_client_id: c.sp_client_id,
    sp_client_secret: '',
    sp_client_secret_arn: c.sp_client_secret_arn,
    has_sp_secret: c.sp_client_secret_arn.length > 0,
  };
}

export function draftFromTenant(t: {
  name: string;
  line_of_business: string;
  description: string;
  platform: TenantPlatform;
  stages: TenantView['stages'];
  entra_group_ids: string[];
  account_admin_secret_arn: string;
}): TenantDraft {
  const platform = t.platform;
  const awsStage = (c: TenantView['stages'][string] | undefined): StageDraft => {
    if (!c || isDatabricksStage(c)) return emptyStage();
    return {
      account_id: c.account_id,
      region: c.region,
      ecr_repo_uri: c.ecr_repo_uri,
      push_role_arn: c.push_role_arn,
      deploy_role_arn: c.deploy_role_arn,
    };
  };
  const dbStage = (c: TenantView['stages'][string] | undefined): DatabricksStageDraft =>
    databricksStageDraftFromConfig(isDatabricksStage(c) ? c : undefined);
  const stageKeys = Object.keys(t.stages);
  const keys = stageKeys.length > 0 ? stageKeys : [SEED_STAGE_KEY];
  const stages: Record<string, StageDraft> = {};
  const databricks: Record<string, DatabricksStageDraft> = {};
  for (const k of keys) {
    const c = t.stages[k];
    stages[k] = awsStage(c);
    databricks[k] = dbStage(c);
  }
  return {
    name: t.name,
    line_of_business: t.line_of_business,
    description: t.description,
    platform,
    stageKeys: keys,
    stages,
    databricks,
    account_admin_client_id: '',
    account_admin_secret: '',
    has_account_admin_credential: t.account_admin_secret_arn.length > 0,
    groups: groupsFromIds(t.entra_group_ids),
  };
}

export function isDatabricksStage(c: unknown): c is DatabricksStageConfigOutput {
  return !!c && typeof (c as DatabricksStageConfigOutput).workspace_url === 'string';
}

export function compareStageKeys(a: string, b: string): number {
  const rank = (k: string) => (k === 'dev' ? 0 : k === 'prod' ? 2 : 1);
  const ra = rank(a);
  const rb = rank(b);
  if (ra !== rb) return ra - rb;
  return a.localeCompare(b);
}

export function unionStageKeys(tenants: TenantView[]): string[] {
  const seen = new Set<string>();
  for (const t of tenants) {
    for (const key of Object.keys(t.stages)) {
      seen.add(key);
    }
  }
  return [...seen].sort(compareStageKeys);
}
