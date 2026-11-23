import { afterEach, describe, expect, expectTypeOf, it, vi } from 'vitest';
import { ApiFailure, type ErrorCode } from '../../api/failure';
import type { DiscoveredAgent } from '../../api/client';
import { stripComments } from '../../rawSource';
import {
  AGENT_CREATE_REFUSAL_CODES,
  ALREADY_REGISTERED_REASON,
  DISCOVERY_EMPTY_COPY,
  EMPTY_DRAFT,
  MISSING_HANDLE_REASON,
  PLATFORM_READONLY_HINT,
  SERVING_ENDPOINT_WARNING,
  agentPlatformFor,
  buildPayload,
  canSubmit,
  classifyAgentCreateRefusal,
  createBlockedReason,
  defaultDiscoveryStage,
  discoveredRowState,
  discoveryStageNames,
  discoveryState,
  inferPlatform,
  isManualHandleValid,
  isValidAgentArn,
  isValidDatabricksAppUrl,
  manualHandleError,
  manualHandleField,
  platformStepWarning,
  platformSupportsAutoTraceDelivery,
  resolvePlatform,
  runtimeKindFor,
  selectedRow,
  supportsAutoTraceDelivery,
  tenantPlatformLabel,
  type Draft,
} from './agentRegistrationWizardModel';
import { INVENTORY_ONLY_NOTE, isInventoryOnly } from './platformLabels';
import wizardRaw from './AgentRegistrationWizard.tsx?raw';

const wizardSrc = stripComments(wizardRaw);

const APP_URL = 'https://claims-triage-1234.aws.databricksapps.com';
const ARN = 'arn:aws:bedrock-agentcore:eu-central-1:0:runtime/claims-triage';

function row(over: Partial<DiscoveredAgent> = {}): DiscoveredAgent {
  return {
    name: 'claims-triage',
    runtime_handle: APP_URL,
    kind: 'app',
    state: 'RUNNING',
    created_by: 'lars@example.com',
    already_registered: false,
    ...over,
  };
}

function draft(over: Partial<Draft> = {}): Draft {
  return { ...EMPTY_DRAFT, name: 'claims-triage', tenant_id: 't-1', ...over };
}

afterEach(() => vi.restoreAllMocks());

describe('inferPlatform', () => {
  it('reads the platform off the tenant', () => {
    expect(inferPlatform({ id: 't', name: 'T', platform: 'databricks' })).toBe('databricks');
    expect(inferPlatform({ id: 't', name: 'T', platform: 'aws' })).toBe('aws');
  });

  it('defaults an absent platform to aws, never to databricks: the backend hydrates a platform-less record as aws, and a non-admin membership off /users/me carries no platform key', () => {
    expect(inferPlatform({ id: 't', name: 'T' })).toBe('aws');
    expect(inferPlatform(null)).toBe('aws');
    expect(inferPlatform(undefined)).toBe('aws');
  });
});

describe('resolvePlatform — the discovery response outranks the directory', () => {
  it('trusts the response platform over a tenant whose platform is absent: the directory is fail-silent, so an admin can be discovering while holding a /users/me membership with no platform key', () => {
    const membership = { id: 't-1', name: 'T', stages: { s: {} as never } };
    expect(inferPlatform(membership)).toBe('aws');
    expect(resolvePlatform(membership, 'databricks')).toBe('databricks');
  });

  it('trusts the response platform over a STALE tenant platform, both ways', () => {
    const staleAws = { id: 't', name: 'T', platform: 'aws' as const };
    const staleDb = { id: 't', name: 'T', platform: 'databricks' as const };
    expect(resolvePlatform(staleAws, 'databricks')).toBe('databricks');
    expect(resolvePlatform(staleDb, 'aws')).toBe('aws');
  });

  it('falls back to the tenant inference when there is no discovery response', () => {
    expect(resolvePlatform({ id: 't', name: 'T', platform: 'databricks' }, null)).toBe('databricks');
    expect(resolvePlatform({ id: 't', name: 'T' }, null)).toBe('aws');
    expect(resolvePlatform({ id: 't', name: 'T' }, undefined)).toBe('aws');
    expect(resolvePlatform(null, null)).toBe('aws');
  });

  it('builds a Databricks body from a discovered platform even when the tenant record lacks one, so an app URL never lands in the ARN-parsed agent_arn', () => {
    const membership = { id: 't-1', name: 'T', stages: { s: {} as never } };
    const platform = resolvePlatform(membership, 'databricks');
    const body = buildPayload(draft({ selected_handle: APP_URL }), platform, row());
    expect(body.platform).toBe('databricks');
    expect(body.runtime_handle).toBe(APP_URL);
    expect(body.agent_arn).toBeUndefined();
  });
});

describe('agentPlatformFor', () => {
  it('maps the tenant platform onto the agent registry enum: aws becomes aws_bedrock, the value is_agentcore requires', () => {
    expect(agentPlatformFor('aws')).toBe('aws_bedrock');
    expect(agentPlatformFor('databricks')).toBe('databricks');
  });
});

describe('tenantPlatformLabel', () => {
  it('labels both platforms', () => {
    expect(tenantPlatformLabel('aws')).toBe('Amazon Bedrock AgentCore');
    expect(tenantPlatformLabel('databricks')).toBe('Databricks');
  });
});

describe('PLATFORM_READONLY_HINT — why the platform field cannot be edited', () => {
  it('says the platform is set by the tenant and inherited by its agents', () => {
    expect(PLATFORM_READONLY_HINT).toBe(
      'Set by the tenant — a tenant’s platform is fixed when it is created, so its agents inherit it.',
    );
  });
});

describe('discoveryStageNames', () => {
  it('returns the tenant’s own stage names, sorted', () => {
    const t = {
      id: 't',
      name: 'T',
      stages: { prod: {} as never, dev: {} as never, canary: {} as never },
    };
    expect(discoveryStageNames(t)).toEqual(['canary', 'dev', 'prod']);
  });

  it('handles a one-stage tenant and a stageless one without inventing a stage name', () => {
    expect(discoveryStageNames({ id: 't', name: 'T', stages: { only: {} as never } })).toEqual([
      'only',
    ]);
    expect(discoveryStageNames({ id: 't', name: 'T', stages: {} })).toEqual([]);
    expect(discoveryStageNames({ id: 't', name: 'T' })).toEqual([]);
    expect(discoveryStageNames(null)).toEqual([]);
  });
});

describe('defaultDiscoveryStage', () => {
  it('picks the first stage the tenant actually carries', () => {
    const t = { id: 't', name: 'T', stages: { prod: {} as never, aaa: {} as never } };
    expect(defaultDiscoveryStage(t)).toBe('aaa');
  });

  it('is null when there is no stage to discover on: null means make no request, since an invented stage would 400 or discover the wrong workspace', () => {
    expect(defaultDiscoveryStage({ id: 't', name: 'T', stages: {} })).toBeNull();
    expect(defaultDiscoveryStage(null)).toBeNull();
  });
});

describe('discoveryState', () => {
  const tenant = { id: 't', name: 'T', platform: 'databricks' as const, stages: { s: {} as never } };
  const base = {
    tenant,
    canDiscover: true,
    stage: 's',
    loading: false,
    error: null,
    rows: [] as DiscoveredAgent[],
  };

  it('asks for a tenant before anything else', () => {
    expect(discoveryState({ ...base, tenant: null })).toEqual({ kind: 'no-tenant' });
  });

  it('reports a caller who may not discover as not-permitted, never as an error', () => {
    expect(discoveryState({ ...base, canDiscover: false })).toEqual({ kind: 'not-permitted' });
    expect(
      discoveryState({ ...base, canDiscover: false, rows: [row()], loading: true }).kind,
    ).toBe('not-permitted');
  });

  it('reports a stageless tenant as a configuration gap, not a platform failure', () => {
    expect(discoveryState({ ...base, stage: null })).toEqual({ kind: 'no-stages' });
  });

  it('reports loading while a request is in flight', () => {
    expect(discoveryState({ ...base, loading: true })).toEqual({ kind: 'loading' });
  });

  it('keeps a failure distinct from an empty platform: a 502 and a genuinely empty platform both arrive as no rows', () => {
    expect(
      discoveryState({ ...base, error: 'platform discovery failed (workspace_unreachable)' }),
    ).toEqual({ kind: 'error', message: 'platform discovery failed (workspace_unreachable)' });
    expect(discoveryState(base)).toEqual({ kind: 'empty' });
  });

  it('treats a request that has not been made as loading, never as empty', () => {
    expect(discoveryState({ ...base, rows: null })).toEqual({ kind: 'loading' });
  });

  it('returns the rows when there are some', () => {
    const rows = [row()];
    expect(discoveryState({ ...base, rows })).toEqual({ kind: 'list', rows });
  });

  it('prefers an error over rows left from an earlier fetch', () => {
    expect(discoveryState({ ...base, error: 'boom', rows: [row()] }).kind).toBe('error');
  });

  it('says what each empty state means: the empty copy states the platform answered, the permission copy offers manual entry', () => {
    expect(Object.keys(DISCOVERY_EMPTY_COPY).sort()).toEqual([
      'empty',
      'no-stages',
      'no-tenant',
      'not-permitted',
    ]);
    expect(DISCOVERY_EMPTY_COPY.empty).toMatch(/answered/);
    expect(DISCOVERY_EMPTY_COPY['not-permitted']).toMatch(/manually/);
  });
});

describe('discoveredRowState', () => {
  it('enables an ungoverned row with a handle', () => {
    expect(discoveredRowState(row())).toEqual({
      disabled: false,
      reason: null,
      warning: null,
    });
  });

  it('disables an already-registered row WITH a reason', () => {
    expect(discoveredRowState(row({ already_registered: true }))).toEqual({
      disabled: true,
      reason: ALREADY_REGISTERED_REASON,
      warning: null,
    });
  });

  it('disables a row the platform reported with no handle, with a reason rather than hiding it: runtime_handle is the binding pin', () => {
    for (const handle of ['', '   ']) {
      expect(discoveredRowState(row({ runtime_handle: handle }))).toEqual({
        disabled: true,
        reason: MISSING_HANDLE_REASON,
        warning: null,
      });
    }
  });

  it('never disables without a reason', () => {
    for (const r of [row(), row({ already_registered: true }), row({ runtime_handle: '' })]) {
      const state = discoveredRowState(r);
      expect(state.disabled).toBe(state.reason !== null);
    }
  });

  describe('a serving endpoint is forewarned, not refused: provisioning refuses any runtime_kind other than app, so the warning lands before the record exists', () => {
    it('warns at selection time', () => {
      expect(discoveredRowState(row({ kind: 'serving_endpoint' }))).toEqual({
        disabled: false,
        reason: null,
        warning: SERVING_ENDPOINT_WARNING,
      });
    });

    it('stays selectable: registering one for inventory is a supported act', () => {
      expect(discoveredRowState(row({ kind: 'serving_endpoint' })).disabled).toBe(false);
    });

    it('names what a registered serving endpoint does not get', () => {
      expect(SERVING_ENDPOINT_WARNING).toMatch(/inventory/);
      expect(SERVING_ENDPOINT_WARNING).toMatch(/failed/);
    });

    it('yields to a reason when the row is also disabled', () => {
      expect(
        discoveredRowState(row({ kind: 'serving_endpoint', already_registered: true })),
      ).toEqual({ disabled: true, reason: ALREADY_REGISTERED_REASON, warning: null });
    });

    it('leaves an app row and an unknown kind unwarned', () => {
      for (const kind of ['app', 'agentcore_runtime', '']) {
        expect(discoveredRowState(row({ kind })).warning).toBeNull();
      }
    });
  });
});

describe('isValidDatabricksAppUrl — accepted', () => {
  it('accepts a real Databricks Apps URL', () => {
    expect(isValidDatabricksAppUrl(APP_URL)).toBe(true);
  });

  it('accepts an uppercase host: the URL parser normalises it, as the backend lowercases before its suffix test', () => {
    expect(isValidDatabricksAppUrl('https://Claims-Triage.AWS.DatabricksApps.COM')).toBe(true);
  });

  it('accepts an explicit port, which the backend never inspects', () => {
    expect(isValidDatabricksAppUrl('https://claims.aws.databricksapps.com:443')).toBe(true);
  });

  it('accepts a path and a query string', () => {
    expect(isValidDatabricksAppUrl('https://claims.aws.databricksapps.com/api/v1/agent?x=1')).toBe(
      true,
    );
  });

  it('accepts a value with surrounding whitespace, including a trailing newline: stripped first, as the backend strips', () => {
    expect(isValidDatabricksAppUrl(`  ${APP_URL}  `)).toBe(true);
    expect(isValidDatabricksAppUrl(`${APP_URL}\n`)).toBe(true);
    expect(isValidDatabricksAppUrl(`\t${APP_URL}\r\n`)).toBe(true);
  });
});

describe('isValidDatabricksAppUrl — REFUSED (each one is a real attack)', () => {
  it('refuses http:// — a token on the wire in clear', () => {
    expect(isValidDatabricksAppUrl('http://claims-triage-1234.aws.databricksapps.com')).toBe(false);
  });

  it('refuses the suffix lookalike evil-databricksapps.com, which a plain endsWith("databricksapps.com") would accept and hand a bearer token to', () => {
    expect(isValidDatabricksAppUrl('https://evil-databricksapps.com')).toBe(false);
    expect(isValidDatabricksAppUrl('https://notdatabricksapps.com')).toBe(false);
    expect(isValidDatabricksAppUrl('https://x.evil-databricksapps.com/app')).toBe(false);
  });

  it('refuses the bare apex databricksapps.com', () => {
    expect(isValidDatabricksAppUrl('https://databricksapps.com')).toBe(false);
  });

  it('refuses userinfo: the trusted-looking part is a username and the hostname is attacker.example', () => {
    expect(
      isValidDatabricksAppUrl('https://claims.aws.databricksapps.com@attacker.example'),
    ).toBe(false);
    expect(
      isValidDatabricksAppUrl('https://user:pw@claims.aws.databricksapps.com'),
    ).toBe(false);
  });

  it('refuses a trailing dot on the host, a distinct DNS name outside the dotted suffix', () => {
    expect(isValidDatabricksAppUrl('https://claims.aws.databricksapps.com.')).toBe(false);
    expect(isValidDatabricksAppUrl('https://claims.aws.databricksapps.com./app')).toBe(false);
  });

  it('refuses embedded spaces and embedded newlines: the injection rides in the path while the host passes every other check', () => {
    expect(isValidDatabricksAppUrl('https://claims.aws.databricksapps.com/a b')).toBe(false);
    expect(
      isValidDatabricksAppUrl('https://claims.aws.databricksapps.com\njavascript:alert(1)'),
    ).toBe(false);
    expect(isValidDatabricksAppUrl('https://claims.aws.databricksapps.com/x\r\nX-Evil: 1')).toBe(
      false,
    );
    expect(isValidDatabricksAppUrl('https://claims aws.databricksapps.com')).toBe(false);
  });

  it('refuses a blank value, a non-URL, and a non-https scheme', () => {
    for (const bad of [
      '',
      '   ',
      'claims.aws.databricksapps.com',
      '//claims.aws.databricksapps.com',
      'javascript:alert(1)',
      'ftp://claims.aws.databricksapps.com',
      'data:text/html,<script>',
    ]) {
      expect(isValidDatabricksAppUrl(bad)).toBe(false);
    }
  });

  it('refuses the suffix appearing outside the host', () => {
    expect(isValidDatabricksAppUrl('https://attacker.example/.databricksapps.com')).toBe(false);
    expect(isValidDatabricksAppUrl('https://attacker.example?x=.databricksapps.com')).toBe(false);
    expect(isValidDatabricksAppUrl('https://attacker.example#.databricksapps.com')).toBe(false);
  });
});

const ACCEPTED_BY_BACKEND_TOO = [
  'https://claims-triage-1234.aws.databricksapps.com',
  'https://Claims-Triage.AWS.DatabricksApps.COM',
  'https://claims.aws.databricksapps.com:443',
  'https://claims.aws.databricksapps.com/api/v1/agent?x=1',
  'https://claims.ß.aws.databricksapps.com',
  'https://claims．aws.databricksapps.com',
  'https://claims。aws.databricksapps.com',
];

const REFUSED_BY_BOTH = [
  'http://claims.aws.databricksapps.com',
  'https://evil-databricksapps.com',
  'https://databricksapps.com',
  'https://claims.aws.databricksapps.com@attacker.example',
  'https://claims.aws.databricksapps.com.',
  'https://claims.aws.databricksapps.com\njavascript:alert(1)',
  'https://xn--.aws.databricksapps.com',
];

const KNOWN_LOOSER_THAN_BACKEND = [
  'https://xn--evil.aws.databricksapps.com',
  'https://claims­.aws.databricksapps.com',
  'https://cla​ims.aws.databricksapps.com',
  'https:///claims.aws.databricksapps.com',
  'https://evil.example．databricksapps．com',
];

const STRICTER_THAN_BACKEND = [
  'https://claims.aws.databricksapps.com/pa th',
];

describe('isValidDatabricksAppUrl — FE/BE parity, measured by executing both implementations; looser than the backend is tolerated, stricter only where pinned', () => {
  it('accepts everything the backend accepts: an uppercase host, a port, a path and query, an IDN label, and a fullwidth dot before "aws" that leaves the ASCII suffix intact', () => {
    for (const url of ACCEPTED_BY_BACKEND_TOO) {
      expect(isValidDatabricksAppUrl(url), url).toBe(true);
    }
  });

  it('refuses everything the backend refuses, for the whole security boundary', () => {
    for (const url of REFUSED_BY_BOTH) {
      expect(isValidDatabricksAppUrl(url), url).toBe(false);
    }
  });

  it('documents the five looser cases (an invalid punycode A-label, a soft hyphen and a zero-width space the URL parser strips, an empty authority, fullwidth dots inside the suffix labels), so tightening one is deliberate', () => {
    for (const url of KNOWN_LOOSER_THAN_BACKEND) {
      expect(isValidDatabricksAppUrl(url), url).toBe(true);
    }
    expect(KNOWN_LOOSER_THAN_BACKEND).toHaveLength(5);
  });

  it('is stricter on exactly one measured input, a space in the path the backend would percent-encode, and says so', () => {
    for (const url of STRICTER_THAN_BACKEND) {
      expect(isValidDatabricksAppUrl(url), url).toBe(false);
    }
    expect(STRICTER_THAN_BACKEND).toHaveLength(1);
  });

  it('relies on the backend refusing the looser inputs at invoke time; no input appears in two tables', () => {
    const all = [
      ...ACCEPTED_BY_BACKEND_TOO,
      ...REFUSED_BY_BOTH,
      ...KNOWN_LOOSER_THAN_BACKEND,
      ...STRICTER_THAN_BACKEND,
    ];
    expect(new Set(all).size).toBe(all.length);
  });
});

describe('isValidAgentArn — the AWS fence', () => {
  it('accepts a well-formed ARN and refuses blank, free-text, or a bad region: the backend’s write-time region check, mirrored so a 422 is caught before submit', () => {
    expect(isValidAgentArn(ARN)).toBe(true);
    expect(isValidAgentArn('')).toBe(false);
    expect(isValidAgentArn('   ')).toBe(false);
    expect(isValidAgentArn('anything')).toBe(false);
    expect(isValidAgentArn('arn:aws:bedrock-agentcore:not-a-region:0:runtime/x')).toBe(false);
  });
});

describe('manualHandleField', () => {
  it('gives an AWS tenant today’s ARN field', () => {
    const f = manualHandleField('aws');
    expect(f.key).toBe('agent_arn');
    expect(f.label).toBe('Agent ARN');
  });

  it('gives a Databricks tenant an app-URL field whose hint names the databricksapps.com rule the validator enforces', () => {
    const f = manualHandleField('databricks');
    expect(f.key).toBe('runtime_handle');
    expect(f.label).toBe('App URL');
    expect(f.hint).toMatch(/databricksapps\.com/);
  });

  it('renders the raw word and refuses the create on either path before submit for a tenant platform outside the union, without throwing', () => {
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => undefined);
    expect(manualHandleField('azure' as never).hint).toContain('"azure"');
    const manual = draft({ handle_source: 'manual', agent_arn: ARN });
    expect(createBlockedReason(manual, 'azure' as never)).toContain('"azure"');
    expect(canSubmit(manual, 'azure' as never)).toBe(false);
    const discovery = draft({ handle_source: 'discovery', selected_handle: ARN });
    expect(createBlockedReason(discovery, 'azure' as never)).toBe(createBlockedReason(manual, 'azure' as never));
    expect(canSubmit(discovery, 'azure' as never)).toBe(false);
    expect(warn).toHaveBeenCalledTimes(1);
  });
});

describe('isManualHandleValid / manualHandleError', () => {
  it('treats an empty handle as valid on both platforms: a metadata-only registration is a supported record', () => {
    expect(isManualHandleValid('aws', '')).toBe(true);
    expect(isManualHandleValid('databricks', '  ')).toBe(true);
    expect(manualHandleError('databricks', '')).toBeNull();
  });

  it('applies the app-URL rule only on the Databricks branch', () => {
    expect(isManualHandleValid('databricks', 'https://evil-databricksapps.com')).toBe(false);
    expect(isManualHandleValid('databricks', APP_URL)).toBe(true);
    expect(isManualHandleValid('aws', 'https://evil-databricksapps.com')).toBe(false);
    expect(isManualHandleValid('aws', ARN)).toBe(true);
  });

  it('names the rule in the error message', () => {
    expect(manualHandleError('databricks', 'http://x.databricksapps.com')).toMatch(
      /databricksapps\.com/,
    );
    expect(manualHandleError('aws', '')).toBeNull();
  });
});

describe('selectedRow', () => {
  const rows = [row({ name: 'a', runtime_handle: 'https://a.aws.databricksapps.com' }), row()];

  it('matches on the handle, not an index', () => {
    expect(selectedRow(draft({ selected_handle: APP_URL }), rows)?.runtime_handle).toBe(APP_URL);
  });

  it('is null when nothing is selected, when the handle is stale, or in manual mode', () => {
    expect(selectedRow(draft(), rows)).toBeNull();
    expect(selectedRow(draft({ selected_handle: 'https://gone.aws.databricksapps.com' }), rows)).toBeNull();
    expect(
      selectedRow(draft({ handle_source: 'manual', selected_handle: APP_URL }), rows),
    ).toBeNull();
    expect(selectedRow(draft({ selected_handle: APP_URL }), null)).toBeNull();
  });
});

describe('runtimeKindFor', () => {
  it('carries only the two Databricks kinds, app and serving_endpoint', () => {
    expect(runtimeKindFor('app')).toBe('app');
    expect(runtimeKindFor('serving_endpoint')).toBe('serving_endpoint');
  });

  it('drops agentcore_runtime and anything unrecognised: runtime_kind is a two-value Databricks field', () => {
    expect(runtimeKindFor('agentcore_runtime')).toBeUndefined();
    expect(runtimeKindFor('')).toBeUndefined();
    expect(runtimeKindFor('something_new')).toBeUndefined();
  });
});

describe('canSubmit', () => {
  it('requires a name and a tenant', () => {
    expect(canSubmit(draft(), 'aws')).toBe(true);
    expect(canSubmit(draft({ name: '   ' }), 'aws')).toBe(false);
    expect(canSubmit(draft({ tenant_id: '' }), 'aws')).toBe(false);
  });

  it('blocks a manual app URL that fails the rule, on the Databricks branch only', () => {
    const bad = draft({ handle_source: 'manual', runtime_handle: 'https://evil-databricksapps.com' });
    expect(canSubmit(bad, 'databricks')).toBe(false);
    expect(canSubmit({ ...bad, runtime_handle: APP_URL }, 'databricks')).toBe(true);
    expect(canSubmit({ ...bad, runtime_handle: '' }, 'databricks')).toBe(true);
  });

  it('does not gate the discovery path on a leftover manual value, which must not travel either', () => {
    const d = draft({ handle_source: 'discovery', runtime_handle: 'https://evil-databricksapps.com' });
    expect(canSubmit(d, 'databricks')).toBe(true);
  });
});

describe('createBlockedReason — why canSubmit answers false, so Create is never silently disabled', () => {
  it('is null exactly when canSubmit is true', () => {
    expect(createBlockedReason(draft(), 'aws')).toBeNull();
    expect(canSubmit(draft(), 'aws')).toBe(true);
  });

  it('names the missing name/tenant, checked in canSubmit\'s own order', () => {
    expect(createBlockedReason(draft({ name: '   ' }), 'aws')).toBe('A name is required.');
    expect(createBlockedReason(draft({ tenant_id: '' }), 'aws')).toBe('A tenant is required.');
  });

  it('names the malformed manual ARN with the same message the field itself shows', () => {
    const bad = draft({ handle_source: 'manual', agent_arn: 'not-an-arn' });
    expect(createBlockedReason(bad, 'aws')).toBe(manualHandleError('aws', 'not-an-arn'));
    expect(canSubmit(bad, 'aws')).toBe(false);
  });

  it('names the malformed manual app URL on the Databricks branch', () => {
    const bad = draft({ handle_source: 'manual', runtime_handle: 'https://evil-databricksapps.com' });
    expect(createBlockedReason(bad, 'databricks')).toBe(
      manualHandleError('databricks', 'https://evil-databricksapps.com'),
    );
  });
});

describe('buildPayload — Databricks', () => {
  it('maps a selected discovery row onto the create body: runtime_handle and kind from the row, auth_type forced to entra, no agent_arn', () => {
    const d = draft({ selected_handle: APP_URL });
    const body = buildPayload(d, 'databricks', row());
    expect(body.platform).toBe('databricks');
    expect(body.runtime_handle).toBe(APP_URL);
    expect(body.runtime_kind).toBe('app');
    expect(body.auth_type).toBe('entra');
    expect(body.agent_arn).toBeUndefined();
  });

  it('carries a serving-endpoint row’s kind through', () => {
    const body = buildPayload(
      draft({ selected_handle: 'https://ws.cloud.databricks.example/serving/x' }),
      'databricks',
      row({ kind: 'serving_endpoint', runtime_handle: 'https://ws.cloud.databricks.example/serving/x' }),
    );
    expect(body.runtime_kind).toBe('serving_endpoint');
  });

  it('uses the manual app URL when discovery was not used, with kind app, the only kind manual entry can mean', () => {
    const body = buildPayload(
      draft({ handle_source: 'manual', runtime_handle: APP_URL }),
      'databricks',
      null,
    );
    expect(body.runtime_handle).toBe(APP_URL);
    expect(body.runtime_kind).toBe('app');
    expect(body.auth_type).toBe('entra');
  });

  it('never sends the manual value when a discovery row is selected', () => {
    const body = buildPayload(
      draft({ selected_handle: APP_URL, runtime_handle: 'https://evil-databricksapps.com' }),
      'databricks',
      row(),
    );
    expect(body.runtime_handle).toBe(APP_URL);
  });

  it('sends no handle at all when nothing was selected or typed, and leaves auth_type as the operator set it', () => {
    const body = buildPayload(draft(), 'databricks', null);
    expect(body.runtime_handle).toBeUndefined();
    expect(body.runtime_kind).toBeUndefined();
    expect(body.auth_type).toBe('none');
  });

  it('never sends a service-written or probe-computed field: binding_mode, databricks_sp_id, databricks_sp_secret_arn, oauth2_app_client_id', () => {
    const body = buildPayload(draft({ selected_handle: APP_URL }), 'databricks', row()) as unknown as Record<
      string,
      unknown
    >;
    for (const key of [
      'binding_mode',
      'databricks_sp_id',
      'databricks_sp_secret_arn',
      'oauth2_app_client_id',
    ]) {
      expect(body[key]).toBeUndefined();
    }
  });
});

describe('buildPayload — AgentCore (the AWS fence)', () => {
  it('maps a manual ARN onto the AgentCore shape: platform aws_bedrock, agent_arn, auth_type as the operator chose, no runtime fields', () => {
    const body = buildPayload(
      draft({ handle_source: 'manual', agent_arn: ARN, auth_type: 'entra' }),
      'aws',
      null,
    );
    expect(body.platform).toBe('aws_bedrock');
    expect(body.agent_arn).toBe(ARN);
    expect(body.auth_type).toBe('entra');
    expect(body.runtime_handle).toBeUndefined();
    expect(body.runtime_kind).toBeUndefined();
  });

  it('does not force entra on an AgentCore registration', () => {
    const body = buildPayload(draft({ handle_source: 'manual', agent_arn: ARN }), 'aws', null);
    expect(body.auth_type).toBe('none');
  });

  it('maps a discovered AgentCore runtime onto agent_arn, never runtime_handle', () => {
    const r = row({ kind: 'agentcore_runtime', runtime_handle: ARN });
    const body = buildPayload(draft({ selected_handle: ARN }), 'aws', r);
    expect(body.agent_arn).toBe(ARN);
    expect(body.runtime_handle).toBeUndefined();
    expect(body.runtime_kind).toBeUndefined();
  });
});

describe('buildPayload — shared field handling', () => {
  it('omits blank optionals so the backend applies its own defaults; tenant_id and origin always travel', () => {
    const body = buildPayload(draft(), 'aws', null);
    expect(body.purpose).toBeUndefined();
    expect(body.sponsor_email).toBeUndefined();
    expect(body.sponsor_oid).toBeUndefined();
    expect(body.business_unit).toBeUndefined();
    expect(body.region).toBeUndefined();
    expect(body.data_classification).toBeUndefined();
    expect(body.framework).toBeUndefined();
    expect(body.endpoint_url).toBeUndefined();
    expect(body.tenant_id).toBe('t-1');
    expect(body.origin).toBe('Registered');
  });

  it('trims what it does send', () => {
    const body = buildPayload(
      draft({ name: '  claims  ', purpose: '  does things  ', business_unit: ' Claims ' }),
      'aws',
      null,
    );
    expect(body.name).toBe('claims');
    expect(body.purpose).toBe('does things');
    expect(body.business_unit).toBe('Claims');
  });

  it('takes the platform from the tenant on every path', () => {
    expect(buildPayload(draft(), 'databricks', null).platform).toBe('databricks');
    expect(buildPayload(draft(), 'aws', null).platform).toBe('aws_bedrock');
  });
});

describe('supportsAutoTraceDelivery — the mirror of the backend’s trace_auto_delivery declaration, exhaustive over both tenant platforms', () => {
  it('is true for aws, false for databricks', () => {
    expect(supportsAutoTraceDelivery('aws')).toBe(true);
    expect(supportsAutoTraceDelivery('databricks')).toBe(false);
  });
});

describe('platformSupportsAutoTraceDelivery — reached from the wider Platform enum', () => {
  it('is true only for aws_bedrock', () => {
    expect(platformSupportsAutoTraceDelivery('aws_bedrock')).toBe(true);
    expect(platformSupportsAutoTraceDelivery('databricks')).toBe(false);
  });

  it('is false for every inventory-only platform', () => {
    for (const p of ['azure', 'salesforce', 'sap', 'google', 'on_prem', 'other'] as const) {
      expect(platformSupportsAutoTraceDelivery(p)).toBe(false);
    }
  });
});

describe('buildPayload — trace_delivery', () => {
  it('ships auto as the draft default, the value the backend resolves an absent key to', () => {
    expect(EMPTY_DRAFT.trace_delivery).toBe('auto');
  });

  it('omits the key on the default auto choice (aws): absent means auto on the wire', () => {
    expect(buildPayload(draft({ trace_delivery: 'auto' }), 'aws', null).trace_delivery).toBeUndefined();
  });

  it('sends "manual" only when the operator opted out on an auto-capable platform (aws)', () => {
    expect(buildPayload(draft({ trace_delivery: 'manual' }), 'aws', null).trace_delivery).toBe('manual');
  });

  it('never sends the key on a non-auto-capable platform, even when set to manual', () => {
    expect(
      buildPayload(draft({ trace_delivery: 'manual' }), 'databricks', row()).trace_delivery,
    ).toBeUndefined();
  });
});

describe('platformStepWarning', () => {
  it('warns on neither tenant platform today, because both map onto governable labels; the gate is what a third tenant platform has to pass', () => {
    expect(platformStepWarning('aws')).toBeNull();
    expect(platformStepWarning('databricks')).toBeNull();
  });

  it('is compile-linked to the one governance predicate, not to a second literal list', () => {
    for (const tp of ['aws', 'databricks'] as const) {
      const expected = isInventoryOnly(agentPlatformFor(tp)) ? INVENTORY_ONLY_NOTE : null;
      expect(platformStepWarning(tp), tp).toBe(expected);
    }
  });

  it('would carry the contract copy verbatim, as a warning distinct from a refusal', () => {
    expect(INVENTORY_ONLY_NOTE).toBe(
      'Registry entry only — AGP cannot govern or invoke agents on this platform',
    );
    expect(SERVING_ENDPOINT_WARNING).not.toBe(INVENTORY_ONLY_NOTE);
  });
});

describe('classifyAgentCreateRefusal: the wizard returns to the name step for the registry\'s name_taken code alone', () => {
  function failure(status: number, code: ErrorCode | null = null, detail?: string): ApiFailure {
    return new ApiFailure(status, code, detail, detail ?? `Request failed with status code ${status}`);
  }

  it('a 409 agent.name_taken is name_taken', () => {
    expect(classifyAgentCreateRefusal(failure(409, 'agent.name_taken', 'An agent named x already exists'))).toBe('name_taken');
  });

  it('a 409 with another code is not: eval.running ("already running"), the MCP registry\'s name_taken, a tenant\'s, or a route-decided 409 with no code', () => {
    expect(classifyAgentCreateRefusal(failure(409, 'eval.running', 'An evaluation is already running for this agent'))).toBeNull();
    expect(classifyAgentCreateRefusal(failure(409, 'mcp_server.name_taken', 'An MCP server named x already exists'))).toBeNull();
    expect(classifyAgentCreateRefusal(failure(409, 'tenant.name_taken', 'tenant name already exists'))).toBeNull();
    expect(classifyAgentCreateRefusal(failure(409))).toBeNull();
  });

  it('a registry 503 whose message says "exists", a tenant 403 and a network failure are not', () => {
    expect(classifyAgentCreateRefusal(failure(503, 'registry.not_configured', 'No registry exists for this tenant'))).toBeNull();
    expect(classifyAgentCreateRefusal(failure(403, null, 'tenant not permitted'))).toBeNull();
    expect(classifyAgentCreateRefusal(failure(0))).toBeNull();
  });

  it('the sentence alone classifies nothing: a 500 reading "already taken" is a plain error', () => {
    expect(classifyAgentCreateRefusal(failure(500, null, 'That name is already taken'))).toBeNull();
  });

  it('the table is typed on the generated codes, so a renamed kind fails tsc where the table names it', () => {
    expectTypeOf(AGENT_CREATE_REFUSAL_CODES).toExtend<Record<'name_taken', readonly ErrorCode[]>>();
  });
});

describe('the wizard\'s create handler', () => {
  it('returns to the name step on the code and shows every other refusal where it stands', () => {
    expect(wizardSrc).toMatch(/isApiFailure\(err\) && classifyAgentCreateRefusal\(err\) === 'name_taken'/);
    expect(wizardSrc).not.toMatch(/already\|exist\|taken/);
  });
});
