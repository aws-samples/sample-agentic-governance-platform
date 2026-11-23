import type {
  AgentCreate,
  AgentAuthType,
  DataClassification,
  DiscoveredAgent,
  Origin,
  Platform,
  StageConfig,
  TenantPlatform,
} from '../../api/client';
import type {
  ApiFailure,
  ErrorCode,
} from '../../api/failure';
import { refusalKind } from '../../api/refusalKind';
import { INVENTORY_ONLY_NOTE, isInventoryOnly } from './platformLabels';
import { warnOnce } from '../operations/warnOnce';

export interface TenantLike {
  id: string;
  name: string;
  /** Absent on a /users/me membership: UserTenant carries no platform key. */
  platform?: TenantPlatform;
  stages?: Record<string, StageConfig>;
}

/** The directory's reading of the tenant's platform; when a discovery response is in hand, resolvePlatform outranks it. */
export function inferPlatform(tenant: TenantLike | null | undefined): TenantPlatform {
  return tenant?.platform ?? 'aws';
}

export function resolvePlatform(
  tenant: TenantLike | null | undefined,
  discoveredPlatform: TenantPlatform | null | undefined,
): TenantPlatform {
  return discoveredPlatform ?? inferPlatform(tenant);
}

export function agentPlatformFor(tenantPlatform: TenantPlatform): Platform {
  return tenantPlatform === 'databricks' ? 'databricks' : 'aws_bedrock';
}

const TENANT_PLATFORM_LABEL: Record<TenantPlatform, string> = {
  aws: 'Amazon Bedrock AgentCore',
  databricks: 'Databricks',
};

export function tenantPlatformLabel(p: TenantPlatform): string {
  return TENANT_PLATFORM_LABEL[p];
}

export const PLATFORM_READONLY_HINT =
  'Set by the tenant — a tenant’s platform is fixed when it is created, so its agents inherit it.';

export function platformStepWarning(tenantPlatform: TenantPlatform): string | null {
  return isInventoryOnly(agentPlatformFor(tenantPlatform)) ? INVENTORY_ONLY_NOTE : null;
}

/** The trace delivery leg; absent on the wire means 'auto'. */
export type TraceDelivery = NonNullable<AgentCreate['trace_delivery']>;

const TRACE_AUTO_DELIVERY: Record<TenantPlatform, boolean> = {
  aws: true,
  databricks: false,
};

export function supportsAutoTraceDelivery(tenantPlatform: TenantPlatform): boolean {
  return TRACE_AUTO_DELIVERY[tenantPlatform];
}

export function platformSupportsAutoTraceDelivery(platform: Platform): boolean {
  if (platform === 'aws_bedrock') return supportsAutoTraceDelivery('aws');
  if (platform === 'databricks') return supportsAutoTraceDelivery('databricks');
  return false;
}

export function discoveryStageNames(tenant: TenantLike | null | undefined): string[] {
  return Object.keys(tenant?.stages ?? {}).sort((a, b) => a.localeCompare(b));
}

export function defaultDiscoveryStage(tenant: TenantLike | null | undefined): string | null {
  const names = discoveryStageNames(tenant);
  return names.length > 0 ? names[0] : null;
}

export type DiscoveryState =
  | { kind: 'no-tenant' }
  | { kind: 'not-permitted' }
  | { kind: 'no-stages' }
  | { kind: 'loading' }
  | { kind: 'error'; message: string }
  | { kind: 'empty' }
  | { kind: 'list'; rows: DiscoveredAgent[] };

export const DISCOVERY_EMPTY_COPY: Record<
  'no-tenant' | 'not-permitted' | 'no-stages' | 'empty',
  string
> = {
  'no-tenant': 'Select a tenant first — discovery runs against that tenant’s platform.',
  'not-permitted':
    'Listing a tenant’s running agents needs an admin. Enter the runtime details manually, or ask an admin to register it.',
  'no-stages':
    'This tenant has no environments configured, so there is nothing to discover. Add one on the tenant record.',
  empty: 'The platform answered and reports no agents on this environment.',
};

export function discoveryState(input: {
  tenant: TenantLike | null | undefined;
  canDiscover: boolean;
  stage: string | null;
  loading: boolean;
  error: string | null;
  rows: DiscoveredAgent[] | null;
}): DiscoveryState {
  if (!input.tenant) return { kind: 'no-tenant' };
  if (!input.canDiscover) return { kind: 'not-permitted' };
  if (!input.stage) return { kind: 'no-stages' };
  if (input.loading) return { kind: 'loading' };
  if (input.error) return { kind: 'error', message: input.error };
  if (input.rows === null) return { kind: 'loading' };
  if (input.rows.length === 0) return { kind: 'empty' };
  return { kind: 'list', rows: input.rows };
}

export interface DiscoveredRowState {
  disabled: boolean;
  reason: string | null;
  /** A caveat on a selectable row; never set alongside a reason. */
  warning: string | null;
}

export const ALREADY_REGISTERED_REASON = 'Already registered in AGP — it is governed here.';
export const MISSING_HANDLE_REASON =
  'The platform reported no runtime handle for this agent, so AGP cannot bind to it.';

export const SERVING_ENDPOINT_WARNING =
  'Serving endpoints register for inventory only — AGP cannot govern one yet (no identity ' +
  'binding, no grants, no invoke), so provisioning will report "failed" for it.';

export function discoveredRowState(row: DiscoveredAgent): DiscoveredRowState {
  if (row.already_registered) {
    return { disabled: true, reason: ALREADY_REGISTERED_REASON, warning: null };
  }
  if ((row.runtime_handle ?? '').trim().length === 0) {
    return { disabled: true, reason: MISSING_HANDLE_REASON, warning: null };
  }
  if (row.kind === 'serving_endpoint') {
    return { disabled: false, reason: null, warning: SERVING_ENDPOINT_WARNING };
  }
  return { disabled: false, reason: null, warning: null };
}

const DATABRICKS_APPS_HOST_SUFFIX = '.databricksapps.com';

export function isValidDatabricksAppUrl(raw: string): boolean {
  const value = (raw ?? '').trim();
  if (value.length === 0) return false;
  if (/[\s\p{Cc}]/u.test(value)) return false;
  let parsed: URL;
  try {
    parsed = new URL(value);
  } catch {
    return false;
  }
  if (parsed.protocol !== 'https:') return false;
  if (parsed.username.length > 0 || parsed.password.length > 0) return false;
  const host = parsed.hostname;
  return host.endsWith(DATABRICKS_APPS_HOST_SUFFIX);
}

const AWS_ARN_REGION_RE = /^[a-z]{2}-[a-z]+(?:-[a-z]+)?-\d$/;

export function isValidAgentArn(raw: string): boolean {
  const value = (raw ?? '').trim();
  if (value.length === 0) return false;
  const parts = value.split(':');
  return parts.length >= 4 && AWS_ARN_REGION_RE.test(parts[3]);
}

export interface ManualHandleField {
  key: 'runtime_handle' | 'agent_arn';
  label: string;
  placeholder: string;
  hint: string;
}

const MANUAL_FIELDS: Record<TenantPlatform, ManualHandleField> = {
  aws: {
    key: 'agent_arn',
    label: 'Agent ARN',
    placeholder: 'arn:aws:bedrock-agentcore:…',
    hint: 'The AgentCore runtime ARN this agent runs as, including a valid AWS region.',
  },
  databricks: {
    key: 'runtime_handle',
    label: 'App URL',
    placeholder: 'https://my-app-1234.aws.databricksapps.com',
    hint: 'The HTTPS URL of the Databricks App, on a *.databricksapps.com host.',
  },
};

function unknownTenantPlatformNote(tenantPlatform: TenantPlatform): string | null {
  if (Object.prototype.hasOwnProperty.call(MANUAL_FIELDS, tenantPlatform)) return null;
  warnOnce('[agentRegistrationWizardModel] tenant platform this frontend does not know; the agent create is refused:', tenantPlatform);
  return `This tenant reports platform "${tenantPlatform}", which this frontend does not know yet; reload once the deploy finishes.`;
}

export function manualHandleField(tenantPlatform: TenantPlatform): ManualHandleField {
  const unknownNote = unknownTenantPlatformNote(tenantPlatform);
  if (unknownNote !== null) return { key: 'agent_arn', label: 'Runtime handle', placeholder: '', hint: unknownNote };
  return MANUAL_FIELDS[tenantPlatform];
}

export function isManualHandleValid(tenantPlatform: TenantPlatform, raw: string): boolean {
  const value = (raw ?? '').trim();
  if (value.length === 0) return true;
  return tenantPlatform === 'databricks' ? isValidDatabricksAppUrl(value) : isValidAgentArn(value);
}

export function manualHandleError(tenantPlatform: TenantPlatform, raw: string): string | null {
  if (isManualHandleValid(tenantPlatform, raw)) return null;
  return tenantPlatform === 'databricks'
    ? 'Enter the HTTPS URL of the Databricks App — it must be on a *.databricksapps.com host.'
    : 'Enter a valid AgentCore runtime ARN, e.g. arn:aws:bedrock-agentcore:eu-central-1:…';
}

export type HandleSource = 'discovery' | 'manual';

export interface Draft {
  name: string;
  purpose: string;
  sponsor_email: string;
  sponsor_oid: string;
  tenant_id: string;
  business_unit: string;
  region: string;
  data_classification: '' | DataClassification;
  framework: string;
  origin: Origin;
  endpoint_url: string;
  auth_type: AgentAuthType;
  agent_arn: string;
  runtime_handle: string;
  handle_source: HandleSource;
  /** The runtime_handle of the selected discovery row, or '' when none is selected. */
  selected_handle: string;
  trace_delivery: TraceDelivery;
}

export const EMPTY_DRAFT: Draft = {
  name: '',
  purpose: '',
  sponsor_email: '',
  sponsor_oid: '',
  tenant_id: '',
  business_unit: '',
  region: '',
  data_classification: '',
  framework: '',
  origin: 'Registered',
  endpoint_url: '',
  auth_type: 'none',
  agent_arn: '',
  runtime_handle: '',
  handle_source: 'discovery',
  selected_handle: '',
  trace_delivery: 'auto',
};

export function selectedRow(
  draft: Draft,
  rows: DiscoveredAgent[] | null,
): DiscoveredAgent | null {
  if (draft.handle_source !== 'discovery') return null;
  const handle = draft.selected_handle;
  if (handle.length === 0) return null;
  return (rows ?? []).find((r) => r.runtime_handle === handle) ?? null;
}

export function runtimeKindFor(kind: string): string | undefined {
  return kind === 'app' || kind === 'serving_endpoint' ? kind : undefined;
}

export function canSubmit(draft: Draft, tenantPlatform: TenantPlatform): boolean {
  return createBlockedReason(draft, tenantPlatform) === null;
}

export function createBlockedReason(draft: Draft, tenantPlatform: TenantPlatform): string | null {
  if (draft.name.trim().length === 0) return 'A name is required.';
  if (draft.tenant_id.length === 0) return 'A tenant is required.';
  const unknownNote = unknownTenantPlatformNote(tenantPlatform);
  if (unknownNote !== null) return unknownNote;
  if (draft.handle_source === 'manual') {
    const field = manualHandleField(tenantPlatform);
    const value = draft[field.key];
    if (!isManualHandleValid(tenantPlatform, value)) return manualHandleError(tenantPlatform, value);
  }
  return null;
}

export function buildPayload(
  draft: Draft,
  tenantPlatform: TenantPlatform,
  row: DiscoveredAgent | null,
): AgentCreate {
  const trimmed = (s: string) => {
    const t = (s ?? '').trim();
    return t.length ? t : undefined;
  };
  const payload: AgentCreate = {
    name: draft.name.trim(),
    purpose: trimmed(draft.purpose),
    sponsor_email: trimmed(draft.sponsor_email),
    sponsor_oid: trimmed(draft.sponsor_oid),
    tenant_id: draft.tenant_id,
    business_unit: trimmed(draft.business_unit),
    region: trimmed(draft.region),
    data_classification: draft.data_classification || undefined,
    platform: agentPlatformFor(tenantPlatform),
    framework: trimmed(draft.framework),
    origin: draft.origin,
    endpoint_url: trimmed(draft.endpoint_url),
    auth_type: draft.auth_type,
  };

  if (supportsAutoTraceDelivery(tenantPlatform) && draft.trace_delivery === 'manual') {
    payload.trace_delivery = 'manual';
  }

  const handle =
    draft.handle_source === 'discovery'
      ? (row?.runtime_handle ?? '').trim()
      : (tenantPlatform === 'databricks' ? draft.runtime_handle : draft.agent_arn).trim();

  if (handle.length === 0) return payload;

  if (tenantPlatform === 'databricks') {
    payload.runtime_handle = handle;
    payload.runtime_kind = row ? runtimeKindFor(row.kind) : 'app';
    payload.auth_type = 'entra';
    return payload;
  }
  payload.agent_arn = handle;
  return payload;
}

export type AgentCreateRefusalKind = 'name_taken';

export const AGENT_CREATE_REFUSAL_CODES = {
  name_taken: ['agent.name_taken'],
} as const satisfies Record<AgentCreateRefusalKind, readonly ErrorCode[]>;

export function classifyAgentCreateRefusal(failure: ApiFailure): AgentCreateRefusalKind | null {
  return refusalKind(AGENT_CREATE_REFUSAL_CODES, failure);
}
