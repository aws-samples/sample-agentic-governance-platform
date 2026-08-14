// E29/T8 — the registration wizard's decisions, tested as pure functions.
//
// Two rules this file holds itself to, both inherited from the epic's Global Constraints:
//
//   • The app-URL guard is a SECURITY BOUNDARY on a governed record, so every hostile input is
//     EXECUTED against the real function. Nothing is asserted from reading the pattern — that is
//     how a regex that looks right and is not gets shipped.
//   • Nothing here renders. The wizard binds and renders; every decision lives in the model, and
//     that is what makes this file able to test the decisions at all.
import { describe, expect, it } from 'vitest';
import type { DiscoveredAgent } from '../../api/client';
import {
  ALREADY_REGISTERED_REASON,
  DISCOVERY_EMPTY_COPY,
  EMPTY_DRAFT,
  MISSING_HANDLE_REASON,
  SERVING_ENDPOINT_WARNING,
  agentPlatformFor,
  buildPayload,
  canSubmit,
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
  resolvePlatform,
  runtimeKindFor,
  selectedRow,
  shouldAcceptDiscovery,
  tenantPlatformLabel,
  type Draft,
} from './agentRegistrationWizardModel';

// A workspace-shaped fake host, per the epic's fakes rule (obvious test values only).
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

// ---------------------------------------------------------------------------
// Platform inference
// ---------------------------------------------------------------------------

describe('inferPlatform', () => {
  it('reads the platform off the tenant', () => {
    expect(inferPlatform({ id: 't', name: 'T', platform: 'databricks' })).toBe('databricks');
    expect(inferPlatform({ id: 't', name: 'T', platform: 'aws' })).toBe('aws');
  });

  // The backend's own zero-migration rule (`hydrate_tenant_item` defaults an absent platform to
  // "aws"), and the shape a non-admin's `UserTenant` membership actually has — it carries no
  // platform key at all.
  it('defaults an absent platform to aws, and never to databricks', () => {
    expect(inferPlatform({ id: 't', name: 'T' })).toBe('aws');
    expect(inferPlatform(null)).toBe('aws');
    expect(inferPlatform(undefined)).toBe('aws');
  });
});

describe('resolvePlatform — the discovery response outranks the directory', () => {
  // THE CRITICAL. `canDiscover` is `isAdmin`, but `useTenantDirectory` is fail-silent and returns
  // null while loading / after a failed fetch — so an admin can be looking at real Databricks
  // rows while the tenant record in hand is a `/users/me` membership carrying no platform key.
  // Inferring `aws` there posted the app URL into `agent_arn` with `platform: 'aws_bedrock'`,
  // which `agent_identity_service` later feeds to `rsplit('/', 1)`.
  it('trusts the response platform over a tenant whose platform is ABSENT', () => {
    const membership = { id: 't-1', name: 'T', stages: { s: {} as never } }; // no `platform`
    expect(inferPlatform(membership)).toBe('aws'); // the weaker source, for contrast
    expect(resolvePlatform(membership, 'databricks')).toBe('databricks');
  });

  // The response read the tenant RECORD; the directory copy can be stale in either direction.
  it('trusts the response platform over a STALE tenant platform, both ways', () => {
    const staleAws = { id: 't', name: 'T', platform: 'aws' as const };
    const staleDb = { id: 't', name: 'T', platform: 'databricks' as const };
    expect(resolvePlatform(staleAws, 'databricks')).toBe('databricks');
    expect(resolvePlatform(staleDb, 'aws')).toBe('aws');
  });

  // Manual-entry-only flows (no discovery response: non-admin, or a failed//not-yet-run fetch)
  // fall back to the inference, which keeps its safe AWS default.
  it('falls back to the tenant inference when there is no discovery response', () => {
    expect(resolvePlatform({ id: 't', name: 'T', platform: 'databricks' }, null)).toBe('databricks');
    expect(resolvePlatform({ id: 't', name: 'T' }, null)).toBe('aws');
    expect(resolvePlatform({ id: 't', name: 'T' }, undefined)).toBe('aws');
    expect(resolvePlatform(null, null)).toBe('aws');
  });

  // End-to-end on the payload: this is the shape the CRITICAL actually broke.
  it('builds a Databricks body from a discovered platform even when the tenant record lacks one', () => {
    const membership = { id: 't-1', name: 'T', stages: { s: {} as never } };
    const platform = resolvePlatform(membership, 'databricks');
    const body = buildPayload(draft({ selected_handle: APP_URL }), platform, row());
    expect(body.platform).toBe('databricks');
    expect(body.runtime_handle).toBe(APP_URL);
    // The regression: a Databricks app URL must never land in the ARN-parsed field.
    expect(body.agent_arn).toBeUndefined();
  });
});

describe('shouldAcceptDiscovery — stale responses are discarded', () => {
  it('accepts a response whose scope is still the one on screen', () => {
    expect(
      shouldAcceptDiscovery({ tenantId: 't-1', stage: 'dev' }, { tenantId: 't-1', stage: 'dev' }),
    ).toBe(true);
  });

  // THE RACE. Request A (Databricks tenant) is in flight, the operator switches to tenant B,
  // B resolves first, then A resolves — unguarded, A's rows and platform paint over B's form and
  // `buildPayload` posts B's tenant id with A's handle.
  it('rejects a response for a tenant the operator has already left', () => {
    expect(
      shouldAcceptDiscovery({ tenantId: 't-A', stage: 'dev' }, { tenantId: 't-B', stage: 'dev' }),
    ).toBe(false);
  });

  // The quieter half of the same race, within ONE tenant: right platform, wrong workspace's
  // handles, and nothing on screen looks wrong.
  it('rejects a response for a stage the operator has already left', () => {
    expect(
      shouldAcceptDiscovery({ tenantId: 't-1', stage: 'dev' }, { tenantId: 't-1', stage: 'prod' }),
    ).toBe(false);
  });

  // A stageless tenant makes no request, so `null` is never a snapshot — but it IS reachable as
  // the CURRENT scope (the reset effect sets it before the in-flight promise settles).
  it('rejects when the form no longer has a stage at all', () => {
    expect(
      shouldAcceptDiscovery({ tenantId: 't-1', stage: 'dev' }, { tenantId: 't-1', stage: null }),
    ).toBe(false);
  });

  // Identity, not a counter: navigating away and back to the same scope must still accept the
  // answer, because the answer is about that scope. A generation counter would discard it.
  it('accepts a response when the operator returned to the same scope', () => {
    const snapshot = { tenantId: 't-1', stage: 'dev' };
    expect(shouldAcceptDiscovery(snapshot, { tenantId: 't-2', stage: 'dev' })).toBe(false);
    expect(shouldAcceptDiscovery(snapshot, { tenantId: 't-1', stage: 'dev' })).toBe(true);
  });

  it('requires BOTH halves to match', () => {
    const snapshot = { tenantId: 't-1', stage: 'dev' };
    expect(shouldAcceptDiscovery(snapshot, { tenantId: 't-2', stage: 'prod' })).toBe(false);
  });
});

describe('agentPlatformFor', () => {
  // The two vocabularies. `aws` is NOT a valid agent platform, and `aws_bedrock` specifically is
  // what `Agent.is_agentcore` requires — any other spelling produces an agent the AgentCore
  // provisioning gate silently skips.
  it('maps the tenant platform onto the agent registry enum', () => {
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

// ---------------------------------------------------------------------------
// Stage selection — no hardcoded stage names (C5)
// ---------------------------------------------------------------------------

describe('discoveryStageNames', () => {
  it('returns the tenant’s own stage names, sorted', () => {
    const t = {
      id: 't',
      name: 'T',
      stages: { prod: {} as never, dev: {} as never, canary: {} as never },
    };
    expect(discoveryStageNames(t)).toEqual(['canary', 'dev', 'prod']);
  });

  // A single-stage tenant is a legitimate record (E28/T6 removed the dev/prod guarantee), and a
  // tenant with none must not be papered over with an invented name.
  it('handles a one-stage tenant and a stageless one', () => {
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

  // null means "do not make the request". The route makes `stage` required with no default for
  // exactly this reason: an invented stage either 400s or silently discovers the wrong workspace.
  it('is null when there is no stage to discover on', () => {
    expect(defaultDiscoveryStage({ id: 't', name: 'T', stages: {} })).toBeNull();
    expect(defaultDiscoveryStage(null)).toBeNull();
  });
});

// ---------------------------------------------------------------------------
// Discovery state — the honest-empty rule
// ---------------------------------------------------------------------------

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

  // Discovery is ADMIN-gated; agent creation is OPERATOR-gated. A non-admin operator is an
  // ordinary case, not a failure — and must never be shown a discovery error for it.
  it('reports a caller who may not discover as not-permitted, never as an error', () => {
    expect(discoveryState({ ...base, canDiscover: false })).toEqual({ kind: 'not-permitted' });
    // Even with a stage and rows in hand, permission outranks them: no request should have been
    // made at all.
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

  // THE POINT OF THE WHOLE UNION: a 502 and a genuinely-empty platform both arrive as no rows.
  it('keeps a failure distinct from an empty platform', () => {
    expect(
      discoveryState({ ...base, error: 'platform discovery failed (workspace_unreachable)' }),
    ).toEqual({ kind: 'error', message: 'platform discovery failed (workspace_unreachable)' });
    expect(discoveryState(base)).toEqual({ kind: 'empty' });
  });

  // Not-yet-asked is not an answer. A null row list with no error and nothing in flight must not
  // render as "the platform reports no agents" — nobody has looked yet.
  it('treats a request that has not been made as loading, never as empty', () => {
    expect(discoveryState({ ...base, rows: null })).toEqual({ kind: 'loading' });
  });

  it('returns the rows when there are some', () => {
    const rows = [row()];
    expect(discoveryState({ ...base, rows })).toEqual({ kind: 'list', rows });
  });

  // An error outranks rows: stale rows from a previous stage must not be presented as this
  // stage's answer.
  it('prefers an error over rows left from an earlier fetch', () => {
    expect(discoveryState({ ...base, error: 'boom', rows: [row()] }).kind).toBe('error');
  });

  it('says what each empty state means, in words that do not claim a failure', () => {
    expect(Object.keys(DISCOVERY_EMPTY_COPY).sort()).toEqual([
      'empty',
      'no-stages',
      'no-tenant',
      'not-permitted',
    ]);
    // The empty copy must state that the platform ANSWERED — that is the whole distinction.
    expect(DISCOVERY_EMPTY_COPY.empty).toMatch(/answered/);
    // The permission copy must offer the way forward (manual entry), not merely refuse.
    expect(DISCOVERY_EMPTY_COPY['not-permitted']).toMatch(/manually/);
  });
});

// ---------------------------------------------------------------------------
// Row selectability
// ---------------------------------------------------------------------------

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

  // `runtime_handle` IS the binding pin — a blank one would produce an agent no invoke path can
  // reach. Disabled with a reason rather than filtered out, so a real ungoverned agent is never
  // hidden from the operator who came looking for it.
  it('disables a row the platform reported with no handle', () => {
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

  // FINAL REVIEW (item 4). Registering a serving endpoint was permitted while provisioning
  // refuses it (`databricks_identity_service` raises for any `runtime_kind != "app"`), so the
  // operator's FIRST signal was a failed provision on a record they had already created.
  describe('a serving endpoint is forewarned, not refused', () => {
    it('warns at selection time', () => {
      expect(discoveredRowState(row({ kind: 'serving_endpoint' }))).toEqual({
        disabled: false,
        reason: null,
        warning: SERVING_ENDPOINT_WARNING,
      });
    });

    // NOT disabled: the epic's scope boundary says serving-endpoint agents "discover + register
    // only", so registering one for inventory is a SUPPORTED act. Blocking it would refuse a
    // legitimate use; the warning is what makes it an informed one.
    it('stays selectable', () => {
      expect(discoveredRowState(row({ kind: 'serving_endpoint' })).disabled).toBe(false);
    });

    // The warning says what is missing, so the operator is not merely told "unsupported".
    it('names what a registered serving endpoint does not get', () => {
      expect(SERVING_ENDPOINT_WARNING).toMatch(/inventory/);
      expect(SERVING_ENDPOINT_WARNING).toMatch(/failed/);
    });

    // A REFUSAL still wins over a caveat: an already-registered serving endpoint is not
    // selectable, and a row that cannot be picked must not advertise advice about picking it.
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

// ---------------------------------------------------------------------------
// The app-URL guard — HOSTILE INPUTS, EXECUTED
// ---------------------------------------------------------------------------

describe('isValidDatabricksAppUrl — accepted', () => {
  it('accepts a real Databricks Apps URL', () => {
    expect(isValidDatabricksAppUrl(APP_URL)).toBe(true);
  });

  // Both are accepted by the BACKEND validator (it lowercases the host before the suffix test,
  // and never looks at the port), so accepting them here is agreement, not laxity: a client-side
  // check stricter than the server's refuses registrations the platform would happily serve.
  it('accepts an uppercase host (the URL parser normalises it)', () => {
    expect(isValidDatabricksAppUrl('https://Claims-Triage.AWS.DatabricksApps.COM')).toBe(true);
  });

  it('accepts an explicit port', () => {
    expect(isValidDatabricksAppUrl('https://claims.aws.databricksapps.com:443')).toBe(true);
  });

  it('accepts a path and a query string', () => {
    expect(isValidDatabricksAppUrl('https://claims.aws.databricksapps.com/api/v1/agent?x=1')).toBe(
      true,
    );
  });

  // Surrounding whitespace is STRIPPED, matching the backend's `.strip()` — the two parsers must
  // agree on what the input is before they can agree on whether it is allowed.
  it('accepts a value with surrounding whitespace, including a trailing newline', () => {
    expect(isValidDatabricksAppUrl(`  ${APP_URL}  `)).toBe(true);
    expect(isValidDatabricksAppUrl(`${APP_URL}\n`)).toBe(true);
    expect(isValidDatabricksAppUrl(`\t${APP_URL}\r\n`)).toBe(true);
  });
});

describe('isValidDatabricksAppUrl — REFUSED (each one is a real attack)', () => {
  it('refuses http:// — a token on the wire in clear', () => {
    expect(isValidDatabricksAppUrl('http://claims-triage-1234.aws.databricksapps.com')).toBe(false);
  });

  // THE SUFFIX LOOKALIKE. `endsWith('databricksapps.com')` accepts this; the leading dot is what
  // refuses it. Registering it would attach a live Databricks bearer token to a host the customer
  // does not own.
  it('refuses the suffix lookalike evil-databricksapps.com', () => {
    expect(isValidDatabricksAppUrl('https://evil-databricksapps.com')).toBe(false);
    expect(isValidDatabricksAppUrl('https://notdatabricksapps.com')).toBe(false);
    expect(isValidDatabricksAppUrl('https://x.evil-databricksapps.com/app')).toBe(false);
  });

  it('refuses the bare apex databricksapps.com', () => {
    expect(isValidDatabricksAppUrl('https://databricksapps.com')).toBe(false);
  });

  // The classic allowlist bypass: the trusted-looking part is a USERNAME; the hostname is
  // attacker.example.
  it('refuses userinfo', () => {
    expect(
      isValidDatabricksAppUrl('https://claims.aws.databricksapps.com@attacker.example'),
    ).toBe(false);
    expect(
      isValidDatabricksAppUrl('https://user:pw@claims.aws.databricksapps.com'),
    ).toBe(false);
  });

  // A trailing dot is a DISTINCT DNS name that does not end in the dotted suffix.
  it('refuses a trailing dot on the host', () => {
    expect(isValidDatabricksAppUrl('https://claims.aws.databricksapps.com.')).toBe(false);
    expect(isValidDatabricksAppUrl('https://claims.aws.databricksapps.com./app')).toBe(false);
  });

  // Interior whitespace / control characters: the injection rides in the PATH while the host
  // passes every other check.
  it('refuses embedded spaces and embedded newlines', () => {
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

  // The suffix must be a real host suffix, not a substring anywhere in the URL.
  it('refuses the suffix appearing outside the host', () => {
    expect(isValidDatabricksAppUrl('https://attacker.example/.databricksapps.com')).toBe(false);
    expect(isValidDatabricksAppUrl('https://attacker.example?x=.databricksapps.com')).toBe(false);
    expect(isValidDatabricksAppUrl('https://attacker.example#.databricksapps.com')).toBe(false);
  });
});

// ---------------------------------------------------------------------------
// FE/BE PARITY — measured, and pinned only where measurement supports it
//
// The property WORTH having is directional: this validator should not be STRICTER than the
// backend's `_validate_databricks_app_url`, because a stricter client refuses registrations the
// platform would happily serve and the operator has no way around it. A looser one merely defers
// an exotic refusal to the enforcement point, which still refuses, before any token is minted.
//
// THAT PROPERTY IS NOT ABSOLUTE, and this file says so rather than asserting a tidier claim than
// the measurements support (an earlier version claimed "always looser, never stricter" — falsified
// below by `STRICTER_THAN_BACKEND`). Every verdict here was produced by EXECUTING both
// implementations over these exact inputs — the backend's as `urlparse` + the `.host` access that
// forces httpx's IDNA encode. Re-measure if either side changes; do not reason from the source.
// ---------------------------------------------------------------------------

// Inputs BOTH sides accept. A failure here means this validator has become stricter than the
// platform's on a value that should register cleanly.
//
// The two CJK-dot rows are accepted by the backend for a reason worth stating exactly, because the
// obvious explanation is wrong: `urlparse` does NOT normalise U+FF0E/U+3002 — it returns the host
// VERBATIM (measured: `'claims．aws.databricksapps.com'`), which does not end in the ASCII dotted
// suffix. They survive the backend's suffix check only because... they don't: the suffix check sees
// the raw host and the LAST label still reads `.databricksapps.com` in ASCII, since only the dot
// BEFORE `aws` is fullwidth. It is httpx's downstream IDNA encode that maps the fullwidth dot to
// '.' — which is why the request that finally goes out is aimed at the normalised host. FE reaches
// the same verdict earlier, via WHATWG `URL`'s own normalisation.
const ACCEPTED_BY_BACKEND_TOO = [
  'https://claims-triage-1234.aws.databricksapps.com',
  'https://Claims-Triage.AWS.DatabricksApps.COM', // host normalises; BE lowercases
  'https://claims.aws.databricksapps.com:443', // BE never inspects the port
  'https://claims.aws.databricksapps.com/api/v1/agent?x=1',
  'https://claims.ß.aws.databricksapps.com', // a legitimate IDN label, encodes fine
  'https://claims．aws.databricksapps.com', // U+FF0E before `aws`; ASCII suffix intact
  'https://claims。aws.databricksapps.com', // U+3002, same shape
];

// Inputs BOTH sides refuse — the security boundary proper, agreed on by two implementations.
const REFUSED_BY_BOTH = [
  'http://claims.aws.databricksapps.com',
  'https://evil-databricksapps.com',
  'https://databricksapps.com',
  'https://claims.aws.databricksapps.com@attacker.example',
  'https://claims.aws.databricksapps.com.',
  'https://claims.aws.databricksapps.com\njavascript:alert(1)',
  'https://xn--.aws.databricksapps.com', // empty A-label: even WHATWG URL throws
];

// LOOSER than the backend: accepted here, REFUSED there. Five, from TWO distinct root causes.
//
// Cause 1 — httpx's IDNA encode, which WHATWG `URL` does not run (the first four). Cause 2 — the
// two sides check DIFFERENT STRINGS: the backend tests its suffix against the raw `urlparse` host,
// while FE tests the WHATWG-NORMALISED one. So a host whose `.databricksapps.com` labels are
// themselves written with fullwidth dots is `evil.example．databricksapps．com` verbatim to the
// backend (suffix check FAILS — measured) and `evil.example.databricksapps.com` after WHATWG
// normalisation (suffix check passes here). Note the direction: this is a host the backend REFUSES
// and FE accepts, so nothing hostile gets through the enforcement point.
//
// Listed rather than fixed, deliberately: matching exactly would mean reimplementing IDNA in the
// browser (or a new dependency, which the epic forbids), and a hand-rolled approximation is how a
// client check silently becomes STRICTER than the server on some legitimate international host.
// Asserting them as `true` means a future tightening BREAKS THIS TEST — which is the point:
// closing one becomes a deliberate act with a visible diff, not a silent behaviour change.
const KNOWN_LOOSER_THAN_BACKEND = [
  'https://xn--evil.aws.databricksapps.com', // invalid punycode A-label
  'https://claims­.aws.databricksapps.com', // U+00AD soft hyphen — invisible, URL strips it
  'https://cla​ims.aws.databricksapps.com', // U+200B zero-width space — likewise
  'https:///claims.aws.databricksapps.com', // triple slash — empty authority
  'https://evil.example．databricksapps．com', // cause 2: raw vs normalised host (see above)
];

// STRICTER than the backend — the falsifying case, kept as a first-class set rather than a caveat
// in prose. FE refuses it; the backend ACCEPTS it (measured `BE=True`) and httpx percent-encodes
// the space when it builds the request (`/pa th` → `/pa%20th`).
//
// It is a real, if small, usability cost: an operator who pastes an app URL with a space in the
// PATH is refused here on a value the platform would have served. Accepted knowingly — the same
// interior-whitespace rule is what refuses CR/LF and the `\njavascript:` injection above, and
// loosening it to allow only U+0020-in-path would trade a clear rule for a subtle one on the
// field that decides where a bearer token is sent. Pinned so the trade-off stays visible.
const STRICTER_THAN_BACKEND = [
  'https://claims.aws.databricksapps.com/pa th', // space in the PATH; BE accepts, we refuse
];

describe('isValidDatabricksAppUrl — FE/BE parity, as measured', () => {
  it('accepts everything the backend accepts, except the documented stricter case', () => {
    for (const url of ACCEPTED_BY_BACKEND_TOO) {
      expect(isValidDatabricksAppUrl(url), url).toBe(true);
    }
  });

  it('refuses everything the backend refuses, for the whole security boundary', () => {
    for (const url of REFUSED_BY_BOTH) {
      expect(isValidDatabricksAppUrl(url), url).toBe(false);
    }
  });

  it('documents the five looser cases, so tightening one is deliberate', () => {
    for (const url of KNOWN_LOOSER_THAN_BACKEND) {
      expect(isValidDatabricksAppUrl(url), url).toBe(true);
    }
    // Pinned as a set: a sixth divergence must be MEASURED and added here rather than discovered
    // in production. The count is a claim about what has been measured, not about what exists —
    // the earlier version of this pin asserted exhaustiveness and a fifth case falsified it.
    expect(KNOWN_LOOSER_THAN_BACKEND).toHaveLength(5);
  });

  it('is STRICTER on exactly one measured input, and says so', () => {
    for (const url of STRICTER_THAN_BACKEND) {
      expect(isValidDatabricksAppUrl(url), url).toBe(false);
    }
    expect(STRICTER_THAN_BACKEND).toHaveLength(1);
  });

  // The looser cases are only acceptable because the backend still refuses them. That is a fact
  // about the BACKEND, so it is stated here as the assumption this whole trade-off rests on —
  // `test_invoke_route.py` is where it is enforced.
  it('relies on the backend refusing the looser inputs at invoke time', () => {
    // No set may claim an input twice: a value cannot be both agreed-on and divergent, and an
    // overlap would mean one of these tables is stale.
    const all = [
      ...ACCEPTED_BY_BACKEND_TOO,
      ...REFUSED_BY_BOTH,
      ...KNOWN_LOOSER_THAN_BACKEND,
      ...STRICTER_THAN_BACKEND,
    ];
    expect(new Set(all).size).toBe(all.length);
  });
});

// ---------------------------------------------------------------------------
// Manual entry
// ---------------------------------------------------------------------------

describe('isValidAgentArn — the AWS fence', () => {
  // Byte-identical to the pre-E29 behaviour of the wizard's Agent ARN field: free text,
  // non-empty. Tightening it inside the Databricks change would put an unrelated risk in this
  // diff.
  it('accepts any non-empty value and refuses a blank one', () => {
    expect(isValidAgentArn(ARN)).toBe(true);
    expect(isValidAgentArn('anything')).toBe(true);
    expect(isValidAgentArn('')).toBe(false);
    expect(isValidAgentArn('   ')).toBe(false);
  });
});

describe('manualHandleField', () => {
  it('gives an AWS tenant today’s ARN field', () => {
    const f = manualHandleField('aws');
    expect(f.key).toBe('agent_arn');
    expect(f.label).toBe('Agent ARN');
  });

  it('gives a Databricks tenant an app-URL field', () => {
    const f = manualHandleField('databricks');
    expect(f.key).toBe('runtime_handle');
    expect(f.label).toBe('App URL');
    // The hint must state the rule the validator enforces, not a vaguer one.
    expect(f.hint).toMatch(/databricksapps\.com/);
  });
});

describe('isManualHandleValid / manualHandleError', () => {
  // A metadata-only registration is a real supported record (~18 exist), so an empty handle must
  // not block submit on either branch.
  it('treats an empty handle as valid on both platforms', () => {
    expect(isManualHandleValid('aws', '')).toBe(true);
    expect(isManualHandleValid('databricks', '  ')).toBe(true);
    expect(manualHandleError('databricks', '')).toBeNull();
  });

  it('applies the app-URL rule only on the Databricks branch', () => {
    expect(isManualHandleValid('databricks', 'https://evil-databricksapps.com')).toBe(false);
    expect(isManualHandleValid('databricks', APP_URL)).toBe(true);
    // The same hostile string on an AWS tenant is just free text in an ARN field — the AWS
    // branch's rule is unchanged from pre-E29.
    expect(isManualHandleValid('aws', 'https://evil-databricksapps.com')).toBe(true);
  });

  it('names the rule in the error message', () => {
    expect(manualHandleError('databricks', 'http://x.databricksapps.com')).toMatch(
      /databricksapps\.com/,
    );
    expect(manualHandleError('aws', '')).toBeNull();
  });
});

// ---------------------------------------------------------------------------
// Selection + submit gate
// ---------------------------------------------------------------------------

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
  it('carries only C-4’s two Databricks kinds', () => {
    expect(runtimeKindFor('app')).toBe('app');
    expect(runtimeKindFor('serving_endpoint')).toBe('serving_endpoint');
  });

  // `runtime_kind` is a two-value Databricks field. Writing `agentcore_runtime` into it would
  // make `is_databricks_governed` reason about a record that is not one.
  it('drops agentcore_runtime and anything unrecognised', () => {
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
    // Empty is fine — a metadata-only registration.
    expect(canSubmit({ ...bad, runtime_handle: '' }, 'databricks')).toBe(true);
  });

  it('does not gate the discovery path on a manual field', () => {
    // A leftover hostile value in the manual box must not block a discovery selection, and must
    // not travel either (see buildPayload).
    const d = draft({ handle_source: 'discovery', runtime_handle: 'https://evil-databricksapps.com' });
    expect(canSubmit(d, 'databricks')).toBe(true);
  });
});

// ---------------------------------------------------------------------------
// buildPayload — the mapping this module exists for
// ---------------------------------------------------------------------------

describe('buildPayload — Databricks', () => {
  it('maps a selected discovery row onto the C-4 create body', () => {
    const d = draft({ selected_handle: APP_URL });
    const body = buildPayload(d, 'databricks', row());
    expect(body.platform).toBe('databricks');
    expect(body.runtime_handle).toBe(APP_URL);
    expect(body.runtime_kind).toBe('app');
    // FORCED, not defaulted: `is_databricks_governed` requires ENTRA before any identity work
    // happens, so `none` here is a record that looks governed and provisions nothing.
    expect(body.auth_type).toBe('entra');
    // `agent_arn` is parsed as a Bedrock ARN by the delete cascade and the status probe — a URL
    // must never land there.
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

  it('uses the manual app URL when discovery was not used', () => {
    const body = buildPayload(
      draft({ handle_source: 'manual', runtime_handle: APP_URL }),
      'databricks',
      null,
    );
    expect(body.runtime_handle).toBe(APP_URL);
    // Manual entry asks for an App URL by name, so `app` is the only kind it can mean.
    expect(body.runtime_kind).toBe('app');
    expect(body.auth_type).toBe('entra');
  });

  // A stale manual value from before the operator switched to the discovery list must not travel.
  it('never sends the manual value when a discovery row is selected', () => {
    const body = buildPayload(
      draft({ selected_handle: APP_URL, runtime_handle: 'https://evil-databricksapps.com' }),
      'databricks',
      row(),
    );
    expect(body.runtime_handle).toBe(APP_URL);
  });

  it('sends no handle at all when nothing was selected or typed', () => {
    const body = buildPayload(draft(), 'databricks', null);
    expect(body.runtime_handle).toBeUndefined();
    expect(body.runtime_kind).toBeUndefined();
    // Nothing to govern yet, so the auth type is left as the operator set it.
    expect(body.auth_type).toBe('none');
  });

  // These four are the tenant probe's / the provisioning service's to write. The invoke path
  // re-reads `binding_mode` from the TENANT precisely because the agent's copy is client-settable
  // — sending one here would let a caller pick the weaker credential path on a federation tenant.
  it('never sends a service-written or probe-computed field', () => {
    // Through `unknown`, because the point of the assertion is to look for keys the type does
    // NOT declare — a direct cast is a type error precisely because `AgentCreate` has no index
    // signature, which is the property being relied on here.
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
  it('maps a manual ARN onto the pre-E29 shape', () => {
    const body = buildPayload(
      draft({ handle_source: 'manual', agent_arn: ARN, auth_type: 'entra' }),
      'aws',
      null,
    );
    expect(body.platform).toBe('aws_bedrock');
    expect(body.agent_arn).toBe(ARN);
    // `auth_type` stays the operator's choice on this branch — an AgentCore agent legitimately
    // has three auth shapes and `is_agentcore` gates on Entra having been picked deliberately.
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
  it('omits blank optionals so the backend applies its own defaults', () => {
    const body = buildPayload(draft(), 'aws', null);
    expect(body.purpose).toBeUndefined();
    expect(body.sponsor_email).toBeUndefined();
    expect(body.sponsor_oid).toBeUndefined();
    expect(body.business_unit).toBeUndefined();
    expect(body.region).toBeUndefined();
    expect(body.data_classification).toBeUndefined();
    expect(body.framework).toBeUndefined();
    expect(body.endpoint_url).toBeUndefined();
    // Always sent.
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

  // The platform is the tenant's, always — there is no draft key that can contradict it.
  it('takes the platform from the tenant on every path', () => {
    expect(buildPayload(draft(), 'databricks', null).platform).toBe('databricks');
    expect(buildPayload(draft(), 'aws', null).platform).toBe('aws_bedrock');
  });
});
