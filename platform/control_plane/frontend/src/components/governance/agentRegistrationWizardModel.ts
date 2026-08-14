// Pure decision logic for the agent registration wizard (E29/T8, contracts C-3 + C-4).
// Framework-free so vitest (src/**/*.test.ts only) can unit-test it;
// AgentRegistrationWizard.tsx binds and renders, and judges NOTHING. The
// tenantsAdminForm.ts split is the exemplar this follows deliberately.
//
// WHY THE WIZARD NEEDED A MODEL AT ALL. Pre-E29 it asked the operator to type an agent's
// platform and its ARN — two free-text claims about a runtime AGP could have simply looked up.
// That was survivable while one platform existed; with two it is not, because the platform now
// decides WHICH create body a registration becomes, and an operator who picks the wrong one
// registers an agent the invoke path can never reach. So the platform stops being an input:
// it is INFERRED from the tenant (which is platform-typed and immutable, E29/T1), and the
// runtime handle comes from discovery (C-3) — read from the platform, never constructed.
//
// Manual entry survives as a documented FALLBACK, not a parallel path: discovery needs a
// reachable platform and an admin-gated route, and an operator registering an agent AGP cannot
// currently see must still be able to. What manual entry does NOT get is a free choice of
// platform — that stays inferred, so the fallback cannot produce a record the discovery path
// could not have produced.
import type {
  AgentCreate,
  AuthType,
  DataClassification,
  DiscoveredAgent,
  Origin,
  Platform,
  StageConfig,
  TenantPlatform,
} from '../../api/client';

// ---------------------------------------------------------------------------
// Platform inference — the tenant decides, the operator does not
// ---------------------------------------------------------------------------

// The minimum a tenant-ish record must carry for this module to reason about it. Deliberately
// STRUCTURAL rather than `TenantInfo`/`UserTenant`, because the two sources disagree about what
// they carry and both are legitimate callers:
//
//   • The ADMIN directory (`TenantInfo[]`, `useTenantDirectory`) carries `platform`.
//   • The caller's own memberships (`UserTenant`, from `/users/me`) do NOT — `UserTenant` is
//     `Pick<TenantInfo, 'id'|'name'|'line_of_business'|'stages'>`, so a non-admin operator has
//     a tenant record with no platform key at all.
//
// Accepting both and defaulting an absent platform is therefore the honest shape, not laziness
// — see `inferPlatform` for what the default costs and why it is the right one.
export interface TenantLike {
  id: string;
  name: string;
  platform?: TenantPlatform;
  stages?: Record<string, StageConfig>;
}

// The tenant's platform, defaulting an absent one to AWS.
//
// This mirrors the BACKEND's own rule: `hydrate_tenant_item` defaults a record written before
// platform typing to "aws", because every pre-E29 tenant was an AWS tenant. The default is
// therefore not a guess about this tenant — it is the same zero-migration reading the store
// already applies.
//
// It also fails in the SAFE direction for the `UserTenant` case above. A non-admin whose
// membership carries no platform reads as AWS, which is exactly the pre-E29 behaviour of this
// wizard — so the worst case is "no Databricks affordances offered", never "a Databricks
// registration built from a tenant we could not confirm is one". Inventing `databricks` from an
// absent key would post `platform=databricks` + `auth_type=entra` on a record whose tenant may
// have no workspace at all, which is a governance claim from a missing field.
//
// ⚠ THIS IS THE WEAKER OF THE TWO SOURCES. When a discovery response is in hand, use
// `resolvePlatform` instead — see the CRITICAL it closes.
export function inferPlatform(tenant: TenantLike | null | undefined): TenantPlatform {
  return tenant?.platform ?? 'aws';
}

// The platform to BUILD A PAYLOAD FROM: the discovery response's when there is one, else the
// tenant-derived inference.
//
// C-3's envelope carries `platform` "rather than leaving the client to re-derive it from the
// tenant" (the route's own docstring), and it is AUTHORITATIVE because the route read it straight
// off the tenant RECORD — the same record the invoke path will later read. The directory-derived
// inference is a second-hand copy that can be stale or, for a `UserTenant`, absent entirely.
//
// THE CRITICAL THIS CLOSES, executed. `canDiscover` is `isAdmin`, but `useTenantDirectory` is
// FAIL-SILENT and returns `null` while loading or after a failed fetch — so an admin can
// legitimately be discovering (real Databricks rows on screen, each with an app URL) while
// `tenantOptions` has degraded to `/users/me` memberships, which carry no `platform` key.
// `inferPlatform` then answers `'aws'`, and the app URL is posted as `agent_arn` with
// `platform: 'aws_bedrock'` — a Databricks URL in the field that
// `agent_identity_service` (:432) feeds to `rsplit('/', 1)`. The rows the operator picked from
// PROVED the tenant is Databricks; discarding that proof in favour of an absent key was the bug.
//
// Precedence is one-directional and needs no tie-break rules: a response platform always wins,
// because the only way to have one is to have asked the tenant record itself.
export function resolvePlatform(
  tenant: TenantLike | null | undefined,
  discoveredPlatform: TenantPlatform | null | undefined,
): TenantPlatform {
  return discoveredPlatform ?? inferPlatform(tenant);
}

// ---------------------------------------------------------------------------
// Stale-response rejection — the same defect as the platform CRITICAL, through a different door
// ---------------------------------------------------------------------------

// WHAT A DISCOVERY REQUEST WAS ASKED ABOUT. Captured before the await, compared after it.
export interface DiscoveryScope {
  tenantId: string;
  stage: string;
}

// Whether a resolved discovery response still describes what the form is currently showing.
//
// THE RACE, executed. Nothing about a fetch guarantees the order its promises settle in, and this
// component fires one per (tenant, stage). Interleave:
//
//   1. operator on Databricks tenant A → request A in flight
//   2. operator switches to AWS tenant B → the reset effect clears state, request B fires
//   3. B resolves first: rows = B's, platform = 'aws'
//   4. A resolves second and, unguarded, overwrites both: rows = A's, platform = 'databricks'
//
// The form now shows `draft.tenant_id === B` with A's rows and A's platform, and `buildPayload`
// posts B's tenant id carrying A's `runtime_handle` — an agent registered into the wrong tenant,
// pointing at a runtime that tenant does not own. It is the SAME class of defect as the discarded
// `res.platform` (a payload built from two disagreeing readings), which is why the guard lives
// beside `resolvePlatform` rather than being treated as an unrelated bug.
//
// It ALSO fires within a single tenant on a fast stage toggle: right platform, wrong workspace's
// handles — quieter, and worse for it, because nothing on screen looks wrong.
//
// A SCOPE SNAPSHOT, NOT A COUNTER. Comparing (tenantId, stage) rather than a monotonic generation
// number is the stronger check for the same cost: it rejects on IDENTITY, so a response is
// accepted only if it describes the tenant and stage still selected — including the case where the
// operator navigates away and back to the same scope while a request is in flight, where a counter
// would discard an answer that is in fact correct. `stage` is compared as a plain string because
// `null` never reaches here: a stageless tenant makes no request at all.
export function shouldAcceptDiscovery(
  snapshot: DiscoveryScope,
  current: { tenantId: string; stage: string | null },
): boolean {
  return snapshot.tenantId === current.tenantId && snapshot.stage === current.stage;
}

// A TENANT platform → the AGENT `platform` enum value it registers as.
//
// TWO VOCABULARIES, and conflating them is the bug this function exists to make impossible.
// `TenantPlatform` is `aws | databricks` (which runtime family a tenant's agents live on);
// `Platform` on the agent registry is the older, wider `aws_bedrock | azure | … | databricks`
// list. They overlap on the spelling of exactly one value and differ on the other, so the
// mapping is written once, here, and tested — rather than being re-derived at each call site
// where `'aws'` would silently become an invalid agent platform the backend enum rejects.
//
// `aws` → `aws_bedrock` specifically (not a generic "aws"): that is the value
// `Agent.is_agentcore` requires, so any other spelling produces an agent the AgentCore
// provisioning gate silently skips.
export function agentPlatformFor(tenantPlatform: TenantPlatform): Platform {
  return tenantPlatform === 'databricks' ? 'databricks' : 'aws_bedrock';
}

// How a platform reads on the read-only Platform step. Short, product-vocabulary labels —
// `platformLabel` on the tenant admin form is the sibling for the tenant surface.
const TENANT_PLATFORM_LABEL: Record<TenantPlatform, string> = {
  aws: 'Amazon Bedrock AgentCore',
  databricks: 'Databricks',
};

export function tenantPlatformLabel(p: TenantPlatform): string {
  return TENANT_PLATFORM_LABEL[p] ?? p;
}

// The one sentence the read-only platform field says about WHY it is read-only. An operator
// who can no longer pick a platform is owed the reason, or the missing control reads as a bug.
export const PLATFORM_READONLY_HINT =
  'Set by the tenant — a tenant’s platform is fixed when it is created, so its agents inherit it.';

// ---------------------------------------------------------------------------
// Stage selection — discovery is per-stage, and AGP names no stages
// ---------------------------------------------------------------------------

// The stage names a tenant carries, alphabetically. NEVER a hardcoded stage name (contract C5,
// E28/T11): stages are an open map on the tenant record, a single-stage tenant is a legitimate
// record, and a stage the API does not return must not render. Mirrors
// `repositoryDetailTabs.sortedStageNames` — the same rule, spelled the same way, on a surface
// that module does not own.
export function discoveryStageNames(tenant: TenantLike | null | undefined): string[] {
  return Object.keys(tenant?.stages ?? {}).sort((a, b) => a.localeCompare(b));
}

// The stage discovery starts on: the first name alphabetically, or `null` when the tenant
// carries none.
//
// `null` rather than a fallback literal is the whole point. The discovery route makes `stage`
// REQUIRED with no default for exactly this reason, and a client that invented one ("dev")
// would either 400 or — worse, on a tenant that happens to have that stage — discover the
// wrong workspace silently. A null here means the caller must NOT make the request, and the UI
// says so instead (see `discoveryState`'s `no-stages`).
export function defaultDiscoveryStage(tenant: TenantLike | null | undefined): string | null {
  const names = discoveryStageNames(tenant);
  return names.length > 0 ? names[0] : null;
}

// ---------------------------------------------------------------------------
// Discovery presentation — four states, and none of them is a blank list
// ---------------------------------------------------------------------------

// What the discovery panel is showing. A CLOSED union of five kinds, because the failure this
// models is precisely the one the backend route's docstring warns about: 200-with-no-agents,
// 502-platform-unreachable and "still loading" all produce an empty array, and a UI that
// renders an empty array the same way in all three sends an operator hunting a credential that
// is fine — or worse, tells them their platform has no agents when nobody could look.
//
//   • `no-tenant`     — no tenant picked yet. Nothing has been asked, so nothing is claimed.
//   • `not-permitted` — the caller may register an agent but may not discover. The discovery
//                       route is ADMIN-gated while agent creation is OPERATOR-gated, so this is
//                       an ordinary state for a real operator, NOT an error: no request is made,
//                       and manual entry is offered instead. Rendering a 403 as "discovery
//                       failed" would send an operator to debug a platform that is fine.
//   • `no-stages`     — the tenant carries no stages, so there is nothing to discover ON. A
//                       configuration gap on the tenant record, not a platform failure.
//   • `loading`       — a request is in flight.
//   • `error`         — the request failed. Carries the message so the operator sees WHICH
//                       failure (a 400 unknown-stage and a 502 unreachable-platform are
//                       different problems with different fixes).
//   • `empty`         — the platform WAS reached and reports no agents. An ordinary answer (a
//                       Databricks SP with no app grants sees exactly this).
//   • `list`          — rows to choose from.
export type DiscoveryState =
  | { kind: 'no-tenant' }
  | { kind: 'not-permitted' }
  | { kind: 'no-stages' }
  | { kind: 'loading' }
  | { kind: 'error'; message: string }
  | { kind: 'empty' }
  | { kind: 'list'; rows: DiscoveredAgent[] };

// What each non-list, non-loading state SAYS. Copy lives here so it is asserted by test rather
// than eyeballed in a render, and so the honest-state rule cannot be quietly softened in a JSX
// edit.
export const DISCOVERY_EMPTY_COPY: Record<
  'no-tenant' | 'not-permitted' | 'no-stages' | 'empty',
  string
> = {
  'no-tenant': 'Select a tenant first — discovery runs against that tenant’s platform.',
  // Says what IS true (you may register, an admin may list) rather than implying a fault.
  'not-permitted':
    'Listing a tenant’s running agents needs an admin. Enter the runtime details manually, or ask an admin to register it.',
  'no-stages':
    'This tenant has no environments configured, so there is nothing to discover. Add one on the tenant record.',
  // Says what WAS established (the platform answered) before what was not found. The inverse
  // wording — "no agents found" alone — is indistinguishable from a failed lookup.
  empty: 'The platform answered and reports no agents on this environment.',
};

// The inputs a caller has in hand, resolved into exactly one state. Written as a function over
// plain values (not a hook) so the precedence is testable: tenant before permission, permission
// before stage, stage before loading, loading before error, error before the list — because each
// earlier condition makes the later ones meaningless rather than merely less important. (A
// caller who may not discover is not waiting on a stage; a stageless tenant is not waiting on a
// request that will never be sent.)
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
  // `null` rows with no error and no in-flight request means no request has been made yet —
  // treated as loading rather than as "empty", so a not-yet-asked panel never claims an answer.
  if (input.rows === null) return { kind: 'loading' };
  if (input.rows.length === 0) return { kind: 'empty' };
  return { kind: 'list', rows: input.rows };
}

// One discovery row's selectability. `reason` is non-null exactly when `disabled` is true, so a
// disabled row can never render without an explanation — a greyed-out row with no reason is the
// UI equivalent of a swallowed error.
export interface DiscoveredRowState {
  disabled: boolean;
  reason: string | null;
  /**
   * A caveat about a row that IS selectable. Distinct from `reason`, which explains a row that
   * is NOT — the two must not share a field, because collapsing them would force every caveat
   * to also be a refusal (or every refusal to read as advice).
   */
  warning: string | null;
}

export const ALREADY_REGISTERED_REASON = 'Already registered in AGP — it is governed here.';
// A row the platform reported with no handle cannot be registered: `runtime_handle` IS the
// binding pin (C-3), and a blank one would produce an agent no invoke path can reach. The
// adapters skip malformed records defensively, so this is belt-and-braces on the LAST place the
// value is still recoverable — and it stays a reason rather than a filter, because hiding the
// row would hide a real ungoverned agent from the operator who came here to find it.
export const MISSING_HANDLE_REASON =
  'The platform reported no runtime handle for this agent, so AGP cannot bind to it.';

// A serving endpoint CAN be registered and provisioning WILL refuse it: only a Databricks App
// exposes the permissions surface the CAN_USE grant needs, so `provision_databricks_runtime`
// raises and the record lands at `identity_status: "failed"`. Warned here rather than at that
// point, because otherwise the operator's FIRST signal is a failed provision on a record they
// have already created — a dead end they had no way to see coming.
//
// A WARNING, NOT A REFUSAL, and the epic's scope boundary is why: serving-endpoint agents
// "discover + register only" by design (§ scope boundary), so registering one for inventory is
// a legitimate act. Disabling the row would block a supported use; saying nothing would sell an
// unsupported one. So the row stays selectable and states what it does and does not get.
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

// ---------------------------------------------------------------------------
// Manual entry — the fallback field, and the guard on it
// ---------------------------------------------------------------------------

// A Databricks Apps URL, validated on the same four CHECKS as the backend's
// `_validate_databricks_app_url` (routes/agents.py): https exactly, no userinfo, and a host ending
// in the DOTTED suffix. The checks are the same; the two implementations' VERDICTS differ on six
// measured edge inputs, enumerated further down — do not read "same" as "identical".
//
// THE LEADING DOT IS THE WHOLE POINT. `.databricksapps.com` is a real subdomain test: it refuses
// both the bare apex (`databricksapps.com`) and the suffix lookalike
// (`evil-databricksapps.com`), which a plain `endsWith('databricksapps.com')` accepts. That
// matters because this value becomes `runtime_handle`, and `runtime_handle` is the host AGP
// later attaches a live Databricks bearer token to — so a lookalike is a registration field
// that exfiltrates a credential to a host the customer does not own.
//
// THIS IS A PRE-CHECK, NOT THE ENFORCEMENT POINT, and saying so precisely matters. The backend
// re-validates at invoke time with two parsers (`urlparse` for the allowlist, `httpx.URL` for
// what will actually build the request), which is where the security boundary lives. What this
// buys is that the operator learns at the point of typing rather than at the first invoke.
//
// IT IS THE SAME RULE IN SHAPE AND DIVERGES AT THE EDGES, IN BOTH DIRECTIONS. An earlier version
// of this comment claimed the rules were identical; a later one claimed the divergence was
// one-directional. Both were false. Everything below was MEASURED by running the two
// implementations over the same inputs — never by reading either one.
//
// FIVE inputs are accepted here and REFUSED by the backend, from TWO distinct causes:
//
//   • an invalid punycode A-label       — `https://xn--evil.aws.databricksapps.com`
//   • a U+00AD SOFT HYPHEN in the host  — invisible, and WHATWG `URL` silently strips it
//   • a U+200B ZERO WIDTH SPACE ditto   — same mechanism, same result
//   • an empty authority (triple slash) — `https:///claims.aws.databricksapps.com`
//        ^ those four: `httpx.URL(...).host` runs an IDNA encode that WHATWG `URL` does not.
//   • a host whose SUFFIX labels use fullwidth dots — the two sides check DIFFERENT STRINGS.
//        The backend tests its suffix against the RAW `urlparse` host; this function tests the
//        WHATWG-NORMALISED one. So `evil.example．databricksapps．com` stays verbatim for the
//        backend (suffix check fails — measured) and normalises to `…databricksapps.com` here.
//
// AND ONE input is REFUSED here that the backend ACCEPTS — a space in the PATH
// (`…databricksapps.com/pa th`; httpx percent-encodes it to `/pa%20th` when it builds the
// request). That is a real usability cost, accepted knowingly: the interior-whitespace rule below
// is what refuses CR/LF and the `\njavascript:` injection, and narrowing it to permit
// U+0020-in-path only would trade a rule that is obvious for one that is subtle, on the field that
// decides where a bearer token gets sent.
//
// WHAT IS NOT TRUE, and was asserted here before: that `urlparse` normalises U+FF0E/U+3002. It
// does NOT — it returns the host verbatim. A fullwidth dot BEFORE `aws` leaves the ASCII
// `.databricksapps.com` suffix intact, which is why such a host passes the backend's check at all;
// httpx's IDNA encode is what maps it to '.' before the request goes out.
//
// The two invisible codepoints are NAMED rather than shown: pasting them into a comment makes the
// comment itself unreadable (and trips `no-irregular-whitespace`). Every literal lives in the
// test's parity tables, which is where they are executed.
//
// THE LOOSER DIRECTION IS THE SAFE ONE, and that is why it is tolerated rather than closed: a
// looser client defers five exotic refusals to the enforcement point, which still refuses them
// before any token is minted — the cost is the error's TIMING (the record reaches the registry
// and fails at first invoke with `invalid_runtime_handle`), not safety.
//
// NOT closed here on purpose. Matching the backend exactly would mean reimplementing IDNA in the
// browser (or shipping a dependency for it — forbidden by the epic's no-new-deps rule), and a
// hand-rolled approximation is how a client-side check silently becomes STRICTER than the server
// on some legitimate internationalized host. `isFrontendNoStricterThanBackend` in the test pins
// the property that actually matters, and the four inputs above are listed there so a future
// tightening is a deliberate act rather than an accident.
const DATABRICKS_APPS_HOST_SUFFIX = '.databricksapps.com';

export function isValidDatabricksAppUrl(raw: string): boolean {
  // Trimmed FIRST, matching the backend's `(raw or "").strip()`. Surrounding whitespace — a
  // trailing newline included — is therefore STRIPPED rather than refused, and the trimmed value
  // is what both sides validate and what `buildPayload` stores. That agreement is the property
  // worth having: two parsers that disagree about what the input even IS cannot meaningfully
  // agree about whether it is allowed.
  const value = (raw ?? '').trim();
  if (value.length === 0) return false;
  // INTERIOR whitespace and control characters are refused BEFORE parsing — a DIFFERENT rule from
  // the trim above, not a redundant one. `https://host.databricksapps.com\njavascript:…` has a
  // host that passes every check below, because the injection rides in the PATH; and a CR/LF or a
  // raw control character in a value that later builds an outbound request is header-injection
  // shaped. The backend reaches the same verdict through its second parser (`httpx.URL`, which
  // raises `InvalidURL` for a non-printable character); this states the rule where the operator
  // can still fix it. EXECUTED over hostile inputs in the test, never asserted from reading it.
  // eslint-disable-next-line no-control-regex
  if (/[\s\u0000-\u001f\u007f]/.test(value)) return false;
  let parsed: URL;
  try {
    parsed = new URL(value);
  } catch {
    return false;
  }
  // `protocol` includes the colon. Exactly https: `http:` puts the token on the wire in clear,
  // and `javascript:` / scheme-relative forms are refused by the same equality.
  if (parsed.protocol !== 'https:') return false;
  // Any userinfo is refused on the SHAPE rather than by trusting the parse, because the parse is
  // exactly what the trick targets: `https://app.aws.databricksapps.com@attacker.example` has a
  // HOSTNAME of `attacker.example` — the trusted-looking part is a username.
  if (parsed.username.length > 0 || parsed.password.length > 0) return false;
  // `hostname` is already lowercased and IDNA-normalised by `URL`, so an uppercase host is
  // ACCEPTED (it normalises), matching the backend, which lowercases before the suffix test.
  const host = parsed.hostname;
  // A trailing dot (`host.databricksapps.com.`) is a distinct DNS name that does not end in the
  // dotted suffix, so the plain check below already refuses it — pinned by test so a future
  // "tolerant" normalisation cannot silently start accepting it.
  return host.endsWith(DATABRICKS_APPS_HOST_SUFFIX);
}

// A manual AgentCore handle. Non-empty is ALL that is required, and that is deliberate rather
// than an oversight: this is byte-identical to the pre-E29 behaviour of the wizard's Agent ARN
// field (a free-text optional input), and tightening it inside the Databricks change would
// couple an unrelated risk to this diff. The AWS branch is a FENCE — an ARN this wizard
// accepted before E29 it still accepts.
export function isValidAgentArn(raw: string): boolean {
  return (raw ?? '').trim().length > 0;
}

// The manual-entry field for a platform: which draft key it binds, and how it reads. One
// definition so the label, the placeholder and the VALIDATOR cannot drift apart — a field
// labelled "App URL" validated as an ARN is the exact mismatch this replaces.
export interface ManualHandleField {
  /** The `Draft` key this input binds. */
  key: 'runtime_handle' | 'agent_arn';
  label: string;
  placeholder: string;
  /** The one-line rule, shown under the input and in the invalid message. */
  hint: string;
}

const MANUAL_FIELDS: Record<TenantPlatform, ManualHandleField> = {
  aws: {
    key: 'agent_arn',
    label: 'Agent ARN',
    placeholder: 'arn:aws:bedrock-agentcore:…',
    hint: 'The AgentCore runtime ARN this agent runs as.',
  },
  databricks: {
    key: 'runtime_handle',
    label: 'App URL',
    placeholder: 'https://my-app-1234.aws.databricksapps.com',
    // States the rule the validator actually enforces. "Must be a valid URL" would be a lie
    // about a check that refuses a valid URL on a different host.
    hint: 'The HTTPS URL of the Databricks App, on a *.databricksapps.com host.',
  },
};

export function manualHandleField(tenantPlatform: TenantPlatform): ManualHandleField {
  return MANUAL_FIELDS[tenantPlatform] ?? MANUAL_FIELDS.aws;
}

// Whether a typed manual handle is acceptable for the platform. An EMPTY value is valid: the
// handle is optional on both branches (a metadata-only registration is a real, supported record
// — ~18 of them exist), so emptiness must not block submit. Only a non-empty value that fails
// its platform's rule is refused.
export function isManualHandleValid(tenantPlatform: TenantPlatform, raw: string): boolean {
  const value = (raw ?? '').trim();
  if (value.length === 0) return true;
  return tenantPlatform === 'databricks' ? isValidDatabricksAppUrl(value) : isValidAgentArn(value);
}

// The message shown when it is not. Names the rule, not just the verdict.
export function manualHandleError(tenantPlatform: TenantPlatform, raw: string): string | null {
  if (isManualHandleValid(tenantPlatform, raw)) return null;
  return tenantPlatform === 'databricks'
    ? 'Enter the HTTPS URL of the Databricks App — it must be on a *.databricksapps.com host.'
    : 'Enter the AgentCore runtime ARN.';
}

// ---------------------------------------------------------------------------
// The draft, and the payload it becomes
// ---------------------------------------------------------------------------

// How the operator is naming the runtime. Not a boolean, because the third state is real: on a
// tenant whose platform could not be discovered the panel has nothing to offer and the operator
// has typed nothing either, which is a legitimate metadata-only registration.
export type HandleSource = 'discovery' | 'manual';

// Every editable field, all string-typed (a form always binds a string, even an empty one) —
// the pre-E29 shape plus what E29 adds. `platform` is GONE from the draft on purpose: it is
// inferred from the tenant and can no longer be typed, so keeping a draft key for it would keep
// a way to contradict the tenant.
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
  auth_type: AuthType;
  // Manual-entry values, one per platform branch. Held SIDE BY SIDE (the `tenantsAdminForm`
  // idiom) rather than as one field switched by platform, so switching tenants mid-wizard never
  // silently reinterprets an ARN as an app URL.
  agent_arn: string;
  runtime_handle: string;
  // How the handle is being supplied, and which discovery row was picked.
  handle_source: HandleSource;
  /** The `runtime_handle` of the selected discovery row, or '' when none is selected. */
  selected_handle: string;
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
};

// The selected discovery row, or null. Matches on `runtime_handle` rather than on an index,
// because the rows are re-fetched when the stage or tenant changes and an index would then
// point at a different agent than the one the operator clicked.
export function selectedRow(
  draft: Draft,
  rows: DiscoveredAgent[] | null,
): DiscoveredAgent | null {
  if (draft.handle_source !== 'discovery') return null;
  const handle = draft.selected_handle;
  if (handle.length === 0) return null;
  return (rows ?? []).find((r) => r.runtime_handle === handle) ?? null;
}

// A discovery row's `kind` → the agent record's `runtime_kind`.
//
// Only the two DATABRICKS kinds are carried. `agentcore_runtime` is deliberately dropped: C-4
// defines `runtime_kind` as `"app" | "serving_endpoint"` — it is a Databricks-shape field, and
// an AgentCore agent records its runtime in `agent_arn` instead. Writing `agentcore_runtime`
// into `runtime_kind` would put a third value into a two-value field AND make
// `is_databricks_governed` reason about a record that is not one.
export function runtimeKindFor(kind: string): string | undefined {
  return kind === 'app' || kind === 'serving_endpoint' ? kind : undefined;
}

// Whether the wizard can submit. Name and tenant are the backend's two requirements (E24), and
// a manual handle that fails its platform's rule blocks — so an invalid app URL is caught at
// the Confirm step rather than becoming a record the invoke path refuses.
export function canSubmit(draft: Draft, tenantPlatform: TenantPlatform): boolean {
  if (draft.name.trim().length === 0) return false;
  if (draft.tenant_id.length === 0) return false;
  if (draft.handle_source === 'manual') {
    const field = manualHandleField(tenantPlatform);
    return isManualHandleValid(tenantPlatform, draft[field.key]);
  }
  return true;
}

// The POST body. THE function this module exists for: one place that decides what a
// registration IS, given a draft, the tenant's platform, and (when discovery was used) the row
// the operator picked.
//
// Empty optional strings are OMITTED rather than sent as "", so the backend applies its own
// defaults (a blank sponsor defaults to the creator). `origin` and `auth_type` always travel.
//
// THE TWO PLATFORM BRANCHES, and why each field is where it is:
//
//   • DATABRICKS — `runtime_handle` + `runtime_kind` + `platform: 'databricks'` +
//     `auth_type: 'entra'`. The auth type is FORCED, not defaulted: `is_databricks_governed`
//     requires `auth_type == ENTRA` before any identity work happens, so a Databricks
//     registration left at `auth_type: 'none'` is a record that looks governed, provisions
//     nothing, and fails at the first invoke with no visible cause. There is exactly one
//     supported way to authenticate to a Databricks-hosted agent under this epic, so offering
//     the operator a choice would only offer them a way to be wrong. `agent_arn` is NEVER set
//     on this branch — the delete cascade and the runtime-status probe parse it as a Bedrock
//     ARN, so a URL there would be fed to ARN-splitting code.
//   • AGENTCORE — `agent_arn` + `platform: 'aws_bedrock'`. `auth_type` stays the operator's
//     choice, exactly as pre-E29: an AgentCore agent legitimately has three auth shapes, and
//     `is_agentcore` gates provisioning on the operator having picked Entra deliberately.
//
// NOT SENT, on either branch: `binding_mode`, `databricks_sp_id`, `databricks_sp_secret_arn`,
// `oauth2_app_client_id`. See the note on `AgentCreate` in client.ts — `binding_mode` is the
// tenant probe's computed verdict and the invoke path re-reads it from the TENANT precisely
// because the agent's copy is client-settable; the other three are service-written.
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
    // Always the tenant's platform, never a draft field — the inference IS the contract.
    platform: agentPlatformFor(tenantPlatform),
    framework: trimmed(draft.framework),
    origin: draft.origin,
    endpoint_url: trimmed(draft.endpoint_url),
    auth_type: draft.auth_type,
  };

  // The handle: the discovery row's when one is selected, else what was typed. `row` is passed
  // in already-resolved (see `selectedRow`) so this function stays a pure mapping and the
  // "which row" lookup is tested separately.
  const handle =
    draft.handle_source === 'discovery'
      ? (row?.runtime_handle ?? '').trim()
      : (tenantPlatform === 'databricks' ? draft.runtime_handle : draft.agent_arn).trim();

  if (handle.length === 0) return payload;

  if (tenantPlatform === 'databricks') {
    payload.runtime_handle = handle;
    // From the row when discovery supplied it; `'app'` is the only kind manual entry can mean,
    // since the manual field asks for an App URL by name and serving endpoints are not
    // registrable by hand under this epic (they are served from the workspace host, which this
    // field's rule does not accept).
    payload.runtime_kind = row ? runtimeKindFor(row.kind) : 'app';
    // Forced — see the branch note above.
    payload.auth_type = 'entra';
    return payload;
  }
  payload.agent_arn = handle;
  return payload;
}
