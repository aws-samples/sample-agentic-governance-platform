import type {
  DriftEntry,
  IdentityStatus,
  Platform,
  TenantPlatform,
  TenantReadiness,
  Verb,
  VerbReadiness,
} from '../../api/client';
import { warnOnce } from '../operations/warnOnce';

export interface PlatformLabel {
  /** Product name for detail pages, filter menus and distribution tables. */
  full: string;
  /** Compact hosting tag for a graph node: the cloud or vendor, not the product name. */
  host: string;
}

export const PLATFORM_LABEL: Record<Platform, PlatformLabel> = {
  aws_bedrock: { full: 'Amazon Bedrock AgentCore', host: 'AWS' },
  azure: { full: 'Azure', host: 'Azure' },
  salesforce: { full: 'Salesforce', host: 'Salesforce' },
  sap: { full: 'SAP', host: 'SAP' },
  databricks: { full: 'Databricks', host: 'Databricks' },
  google: { full: 'Google', host: 'Google' },
  on_prem: { full: 'On-prem', host: 'On-prem' },
  other: { full: 'Other', host: 'Other' },
};

export function platformLabel(p?: Platform | null): string {
  if (!p) return '—';
  if (Object.prototype.hasOwnProperty.call(PLATFORM_LABEL, p)) return PLATFORM_LABEL[p].full;
  warnOnce('[platformLabels] platform this frontend does not know; shown as sent:', p);
  return p;
}

export function platformLabelOr(p: Platform | null | undefined, fallback: string): string {
  if (!p || !Object.prototype.hasOwnProperty.call(PLATFORM_LABEL, p)) return fallback;
  return PLATFORM_LABEL[p].full;
}

/** The graph node's hosting tag, or '' (render no tag) for a platform this build does not know. */
export function platformHostLabel(platform: string): string {
  return Object.prototype.hasOwnProperty.call(PLATFORM_LABEL, platform)
    ? PLATFORM_LABEL[platform as Platform].host
    : '';
}

export function mcpHostLabel(kind: string): string {
  if (kind === 'gateway' || kind === 'runtime') return 'AWS';
  if (kind === 'standard') return 'External';
  return '';
}

export const PLATFORM_OPTIONS: { value: Platform; label: string }[] = (
  Object.entries(PLATFORM_LABEL) as [Platform, PlatformLabel][]
).map(([value, label]) => ({ value, label: label.full }));

export interface RuntimeHandleSource {
  platform?: Platform | null;
  agent_arn?: string | null;
  runtime_handle?: string | null;
}

export interface RuntimeHandleField {
  label: string;
  /** null means the field applies to this agent but carries no value yet ("Not set"). */
  value: string | null;
  /** Lower-case noun for the copy button's aria-label ("Copy app URL"). */
  copyLabel: string;
}

/** How to render the agent's runtime handle, or null to render no row at all (distinct from a row reading "Not set"). */
export function runtimeHandleField(agent: RuntimeHandleSource): RuntimeHandleField | null {
  if (agent.platform === 'databricks') {
    return { label: 'App URL', value: agent.runtime_handle || null, copyLabel: 'app URL' };
  }
  if (agent.agent_arn) {
    return { label: 'Agent ARN', value: agent.agent_arn, copyLabel: 'agent ARN' };
  }
  if (agent.runtime_handle) {
    return { label: 'Runtime handle', value: agent.runtime_handle, copyLabel: 'runtime handle' };
  }
  return null;
}

export const HIDDEN_TAB_IDS_BY_PLATFORM: Partial<Record<Platform, readonly string[]>> = {
  databricks: ['guardrails'],
};

export function isTabVisibleForPlatform(tabId: string, platform?: Platform | null): boolean {
  if (!platform) return true;
  if (tabId === 'containment' && platform !== 'aws_bedrock') return false;
  return !(HIDDEN_TAB_IDS_BY_PLATFORM[platform] ?? []).includes(tabId);
}

export interface BindingModeBadge {
  label: string;
  /** What this mode means for the caller; always present, so the badge never relies on colour alone. */
  hint: string;
  tint: string;
}

const BINDING_MODE_TINT: Record<BadgedBindingMode, string> = {
  federation: 'bg-emerald-50 text-emerald-700',
  invoke_unavailable: 'bg-red-50 text-red-700',
  sp_secret: 'bg-amber-50 text-amber-700',
};

export function bindingModeBadge(mode?: string | null): BindingModeBadge | null {
  const label = bindingModeLabel(mode ?? undefined);
  if (label === null) return null;
  const hint = BINDING_MODE_HINT[mode as BadgedBindingMode];
  const tint = BINDING_MODE_TINT[mode as BadgedBindingMode];
  if (hint === undefined || tint === undefined) return null;
  return { label, hint, tint };
}

export { bindingModeLabel, BINDING_MODE_HINT } from './admin/tenantsAdminForm';
import {
  bindingModeLabel,
  BINDING_MODE_HINT,
  isValidWorkspaceUrl,
  isDatabricksStage,
  type BadgedBindingMode,
} from './admin/tenantsAdminForm';

export const GOVERNABLE_PLATFORM: Record<Platform, TenantPlatform | null> = {
  aws_bedrock: 'aws',
  databricks: 'databricks',
  azure: null,
  salesforce: null,
  sap: null,
  google: null,
  on_prem: null,
  other: null,
};

function governableTenantPlatform(p?: Platform | null): TenantPlatform | null {
  if (!p || !Object.prototype.hasOwnProperty.call(GOVERNABLE_PLATFORM, p)) return null;
  return GOVERNABLE_PLATFORM[p];
}

export function isInventoryOnly(p?: Platform | null): boolean {
  if (!p || !Object.prototype.hasOwnProperty.call(GOVERNABLE_PLATFORM, p)) return false;
  return GOVERNABLE_PLATFORM[p] === null;
}

export const INVENTORY_ONLY_NOTE =
  'Registry entry only — AGP cannot govern or invoke agents on this platform';

export const INVENTORY_ONLY_BADGE_LABEL = 'Inventory only';

export function inventoryOnlyBadge(p?: Platform | null): BindingModeBadge | null {
  if (!isInventoryOnly(p)) return null;
  return {
    label: INVENTORY_ONLY_BADGE_LABEL,
    hint: INVENTORY_ONLY_NOTE,
    tint: 'bg-slate-100 text-slate-600',
  };
}

export const MARKETPLACE_PUBLISH: Record<TenantPlatform, boolean> = {
  aws: true,
  databricks: false,
};

/** The refusal kinds, spelled exactly as the wire spells them. */
export type PublishRefusalKind = 'databricks_publish_unsupported' | 'publish_unsupported_platform';

export const PUBLISH_REFUSAL_NOTE: Record<PublishRefusalKind, string> = {
  databricks_publish_unsupported:
    'Databricks-governed agents cannot be published to the marketplace yet — a subscription would ' +
    'grant the Entra role without the workspace ACL, access the app itself would refuse.',
  publish_unsupported_platform:
    'This agent’s platform does not offer marketplace publication yet.',
};

export function publishRefusal(
  p?: Platform | null,
): { kind: PublishRefusalKind; note: string } | null {
  const tenantPlatform = governableTenantPlatform(p);
  if (tenantPlatform === null) return null;
  if (MARKETPLACE_PUBLISH[tenantPlatform]) return null;
  const kind: PublishRefusalKind =
    tenantPlatform === 'databricks'
      ? 'databricks_publish_unsupported'
      : 'publish_unsupported_platform';
  return { kind, note: PUBLISH_REFUSAL_NOTE[kind] };
}

export const VERB_LABEL: Record<Verb, string> = {
  discover: 'Discover',
  invoke: 'Invoke',
  mirror: 'Mirror platform access',
  deploy: 'Deploy',
  publish: 'Publish',
  guardrails: 'Guardrails',
  mcp_delivery: 'MCP delivery to the runtime',
  observability: 'Observability',
};

export const READINESS_STATE_LABEL: Record<VerbReadiness['state'], string> = {
  ready: 'ready',
  unavailable: 'unavailable',
  not_applicable: 'not applicable',
};

export const READINESS_STATE_TINT: Record<VerbReadiness['state'], string> = {
  ready: 'bg-emerald-50 text-emerald-700',
  unavailable: 'bg-red-50 text-red-700',
  not_applicable: 'bg-slate-100 text-slate-600',
};

export const READINESS_REASON_NOTE: Record<VerbReadiness['reason'], string> = {
  '': '',
  unsupported: 'This platform does not offer it.',
  deploy_role_not_assumable: 'AGP could not assume this tenant’s deploy role.',
  probe_unreachable: 'AGP could not verify it for this tenant.',
  invoke_unavailable: BINDING_MODE_HINT.invoke_unavailable,
};

export interface ReadinessBadge {
  label: string;
  /** Always non-empty: a badge whose consequence is unstated is decoration. */
  hint: string;
  tint: string;
}

export function readinessBadge(verb: Verb, readiness: VerbReadiness): ReadinessBadge {
  const name = Object.prototype.hasOwnProperty.call(VERB_LABEL, verb) ? VERB_LABEL[verb] : 'This';
  const reasonNote = Object.prototype.hasOwnProperty.call(READINESS_REASON_NOTE, readiness.reason)
    ? READINESS_REASON_NOTE[readiness.reason]
    : '';
  const state: VerbReadiness['state'] = Object.prototype.hasOwnProperty.call(
    READINESS_STATE_TINT,
    readiness.state,
  )
    ? readiness.state
    : 'unavailable';
  const stateSentence =
    state === 'not_applicable'
      ? `${name} does not exist on this platform.`
      : `${name} is ${READINESS_STATE_LABEL[state]} for this tenant.`;
  const stageClause = readiness.stage ? ` Affected environment: ${readiness.stage}.` : '';
  return {
    label: `${name} — ${READINESS_STATE_LABEL[state]}`,
    hint: `${reasonNote.length > 0 ? reasonNote : stateSentence}${stageClause}`,
    tint: READINESS_STATE_TINT[state],
  };
}

export interface InvokeReadinessSource {
  readiness?: TenantReadiness | null;
  binding_mode?: string | null;
}

const BINDING_MODE_READINESS: Record<BadgedBindingMode, VerbReadiness> = {
  federation: { verb: 'invoke', state: 'ready', reason: '', stage: null },
  sp_secret: { verb: 'invoke', state: 'ready', reason: '', stage: null },
  invoke_unavailable: { verb: 'invoke', state: 'unavailable', reason: 'invoke_unavailable', stage: null },
};

export function invokeReadiness(source: InvokeReadinessSource): VerbReadiness | null {
  const fromWire = source.readiness?.invoke;
  if (fromWire) return fromWire;
  const mode = source.binding_mode;
  if (!mode || !Object.prototype.hasOwnProperty.call(BINDING_MODE_READINESS, mode)) return null;
  return { ...BINDING_MODE_READINESS[mode as BadgedBindingMode] };
}

function modeAgreesWithVerdict(mode: string | null | undefined, verdict: VerbReadiness): boolean {
  if (!mode || !Object.prototype.hasOwnProperty.call(BINDING_MODE_READINESS, mode)) return false;
  if (verdict.stage) return false;
  const shimmed = BINDING_MODE_READINESS[mode as BadgedBindingMode];
  return shimmed.state === verdict.state && shimmed.reason === verdict.reason;
}

export function invokeBadge(source: InvokeReadinessSource): BindingModeBadge | null {
  const verdict = invokeReadiness(source);
  if (verdict === null) return null;
  const fromMode = bindingModeBadge(source.binding_mode);
  if (fromMode && modeAgreesWithVerdict(source.binding_mode, verdict)) return fromMode;
  return readinessBadge('invoke', verdict);
}

export const DRIFT_PANEL_TITLE = 'Platform access drift';

export const DRIFT_PANEL_NOTE =
  'The app’s platform permissions no longer match this agent’s AGP grants.';

export const DRIFT_REASSERT_LABEL = 'Re-assert access';

export const DRIFT_REASSERT_CONFIRM =
  'Re-asserting rewrites the app’s platform permissions to match AGP’s grants. ' +
  'Platform access granted by hand will be removed. Continue?';

export const DRIFT_CLEAN_NOTE = 'Platform permissions match this agent’s AGP grants.';

export const DRIFT_UNAVAILABLE_NOTE = 'Platform access could not be checked right now.';

export const DRIFT_DIRECTION_NOTE: Record<DriftEntry['direction'], string> = {
  unauthorized_acl: 'Has platform access without an AGP grant',
  missing_acl: 'AGP grant not enforced on the platform',
};

export const DRIFT_KIND_LABEL: Record<DriftEntry['kind'], string> = {
  user: 'User',
  group: 'Group',
  service_principal: 'Service principal',
};

export interface AccessSurfaceSource {
  platform?: Platform | null;
  identity_status: IdentityStatus;
  entra_sp_id?: string | null;
  agent_arn?: string | null;
  runtime_handle?: string | null;
}

export type AccessSurface =
  /** No Entra identity was ever minted (a metadata-only agent); nothing to assign roles against. */
  | { kind: 'no-identity' }
  /** Provisioning failed; grants unavailable, Re-provision is the recovery path. */
  | { kind: 'failed' }
  /** Identity minted, runtime not wired yet; AgentCore-shaped only. */
  | { kind: 'awaiting-runtime' }
  /** Mid-provision, no SP yet; a spinner, and Re-provision once it looks stranded. */
  | { kind: 'provisioning' }
  /** The grants table; notReadyNote is set while the identity exists but is not provisioned, and the table is read-only. */
  | { kind: 'grants'; notReadyNote: string | null };

export const GRANTS_PENDING_NOTE =
  'Identity provisioning has not finished, so access grants cannot be read or changed yet. ' +
  'This list becomes live once provisioning completes.';

export function accessSurface(agent: AccessSurfaceSource): AccessSurface {
  if (agent.identity_status === 'none') return { kind: 'no-identity' };
  if (agent.identity_status === 'failed') return { kind: 'failed' };
  if (agent.identity_status === 'provisioned') return { kind: 'grants', notReadyNote: null };
  if (!agent.entra_sp_id) return { kind: 'provisioning' };
  if (agent.agent_arn) return { kind: 'provisioning' };
  if (agent.platform === 'aws_bedrock') return { kind: 'awaiting-runtime' };
  return { kind: 'grants', notReadyNote: GRANTS_PENDING_NOTE };
}

export const MCP_NOT_DELIVERED_NOTE =
  'Recorded in the registry — not delivered to the runtime on this platform yet.';

export function mcpDeliveryNote(platform?: Platform | null): string | null {
  return platform === 'databricks' ? MCP_NOT_DELIVERED_NOTE : null;
}

export interface ObservabilityPointers {
  langfuse: {
    provisioned: boolean;
    note: string;
  };
  /** The workspace's own telemetry, pointers only; null when no workspace URL is known for the tenant. */
  workspace: WorkspaceObservability | null;
}

export interface WorkspaceObservability {
  workspaceUrl: string;
  auditUrl: string;
  auditLabel: string;
  inferenceNote: string;
}

export const LANGFUSE_PROJECT_PRESENT_NOTE =
  'A Langfuse project is provisioned for this agent — invocations that reach it appear on the Traces tab.';

export const LANGFUSE_PROJECT_ABSENT_NOTE =
  'No Langfuse project is provisioned for this agent, so AGP is not collecting its traces.';

export const DATABRICKS_INFERENCE_TABLES_NOTE =
  'Per-request prompts and responses are logged by Databricks itself, in the workspace’s inference tables.';

export const DATABRICKS_AUDIT_LINK_LABEL = 'Workspace system tables & audit logs';

// Databricks serves Catalog Explorer from the workspace host; this path opens the system catalog, or its "not enabled" page where system tables are off.
const DATABRICKS_AUDIT_PATH = '/explore/data/system';

function workspaceOrigin(url: string | null | undefined): string | null {
  if (!url || !isValidWorkspaceUrl(url)) return null;
  return url;
}

export function workspaceObservability(
  workspaceUrl: string | null | undefined,
): WorkspaceObservability | null {
  const origin = workspaceOrigin(workspaceUrl);
  if (origin === null) return null;
  return {
    workspaceUrl: origin,
    auditUrl: `${origin}${DATABRICKS_AUDIT_PATH}`,
    auditLabel: DATABRICKS_AUDIT_LINK_LABEL,
    inferenceNote: DATABRICKS_INFERENCE_TABLES_NOTE,
  };
}

/** The one workspace URL every Databricks stage of the tenant agrees on; null when they disagree or none is Databricks. */
export function tenantWorkspaceUrl(
  stages: Record<string, unknown> | null | undefined,
): string | null {
  if (!stages) return null;
  const urls = new Set<string>();
  for (const config of Object.values(stages)) {
    if (isDatabricksStage(config)) urls.add(config.workspace_url);
  }
  if (urls.size !== 1) return null;
  const [only] = urls;
  return only;
}

export interface ObservabilitySource {
  platform?: Platform | null;
  langfuse_project_id?: string | null;
}

export function agentObservability(
  agent: ObservabilitySource,
  workspaceUrl?: string | null,
): ObservabilityPointers | null {
  if (agent.platform !== 'databricks') return null;
  const provisioned = !!agent.langfuse_project_id;
  return {
    langfuse: {
      provisioned,
      note: provisioned ? LANGFUSE_PROJECT_PRESENT_NOTE : LANGFUSE_PROJECT_ABSENT_NOTE,
    },
    workspace: workspaceObservability(workspaceUrl),
  };
}

export function identityStatusNote(
  identityStatus: 'none' | 'pending' | 'provisioned' | 'failed' | string | undefined,
): string {
  return identityStatus === 'failed'
    ? 'Identity provisioning failed'
    : 'Identity provisioning pending';
}
