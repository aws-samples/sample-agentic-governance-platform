// Platform vocabulary for the AGENT registry surfaces — ONE map, five former copies
// (E29/T9, contract C-4 + C-6 copy).
//
// WHY THIS FILE EXISTS. Before E29 the agent `Platform` enum was spelled out FIVE times:
// `AgentDetail.tsx`, `AgentsList.tsx` (derived from its filter options), `AgentsOverview.tsx`,
// `graph/graphNodes.tsx`, and `AgentRegistrationWizard.tsx`. Four of them agreed on
// 'Amazon Bedrock AgentCore'; the graph's used a SHORTER vocabulary ('AWS') because it renders
// into a 180px node. That divergence was legitimate — and it is exactly why five independent
// literal maps was the wrong shape: adding a platform meant editing five files, and the two
// vocabularies had no name, so a reader could not tell an intentional difference from a typo.
//
// Here they are ONE entry with TWO fields. `full` is the product name a detail page or a filter
// menu shows; `host` is the compact "where is this running" tag a graph node shows. Adding a
// platform is one line and both surfaces stay complete by construction.
//
// Framework-free (no JSX, no React import) so vitest picks it up: the project's vitest config
// collects `src/**/*.test.ts` ONLY, in a node environment. Every DECISION on the surfaces this
// module serves is therefore exported from here and tested here; the components bind and render
// but judge nothing (the `tenantsAdminForm.ts` / `agentRegistrationWizardModel.ts` idiom).
import type { AccessDriftDirection, AccessDriftEntry, Platform } from '../../api/client';

// One platform, two vocabularies.
export interface PlatformLabel {
  /** Product name — detail pages, filter menus, distribution tables. */
  full: string;
  /**
   * Compact hosting tag for the governance graph's 180px nodes.
   *
   * Shorter is not merely a size concession: on a node the label answers "where does this
   * run", so the CLOUD/vendor is the useful word and the product name is noise. `full` answers
   * "what is this", which is the detail page's question.
   */
  host: string;
}

// THE map. Keyed by the backend `Platform` enum's wire values, exhaustive by type: adding a
// member to `Platform` in `client.ts` makes this a compile error until it is filled in, which is
// the property the five duplicated copies could never have.
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

/**
 * The full product label, or an em-dash for an absent platform.
 *
 * The em-dash is this surface's established "no value" glyph (`Field` rows on AgentDetail use it
 * for region, framework, business unit), so an unset platform reads as unset rather than as a
 * platform named "Other" — a real registry value that must stay distinguishable from "nobody
 * said".
 */
export function platformLabel(p?: Platform | null): string {
  return p ? PLATFORM_LABEL[p].full : '—';
}

/**
 * The full product label with a caller-chosen fallback for an absent OR UNRECOGNISED platform.
 *
 * Exists because the list and the overview each say the absence differently and both are right:
 * a table row says 'Platform not set' (it is describing THIS agent), a distribution table says
 * 'Not set' (it is labelling a bucket). Passing the word in beats either forcing one spelling on
 * both or letting each file keep a copy of the map to get its own wording.
 *
 * The fallback covers an unrecognised value too, and that is load-bearing rather than defensive
 * garnish. The `Platform` type is a promise about a stored DynamoDB record, not a guarantee: a
 * value written by a build that knew a platform this one does not is a real possibility, and the
 * overview's distribution keys arrive as raw `string`s cast at the call site. A bare
 * `PLATFORM_LABEL[p].full` would throw on `.full` of `undefined` and blank the whole page —
 * turning an unknown label into a crash. Same `hasOwnProperty` guard, same reason, as
 * `platformHostLabel`.
 */
export function platformLabelOr(p: Platform | null | undefined, fallback: string): string {
  if (!p || !Object.prototype.hasOwnProperty.call(PLATFORM_LABEL, p)) return fallback;
  return PLATFORM_LABEL[p].full;
}

/**
 * The compact hosting tag for a graph node, or `''` when the platform is absent/unknown.
 *
 * Takes a `string`, not a `Platform`, deliberately: the graph reads `data.metadata.platform` off
 * a `Record<string, unknown>` the backend fills, so the value is genuinely unvalidated at this
 * boundary and the node must survive a spelling it has never seen.
 *
 * `''` means "render no tag" — the honest outcome. A node that printed the raw wire value would
 * teach the reader a vocabulary the product does not have, and one that printed 'AWS' as a
 * default would state a hosting fact about an agent whose platform nobody recorded. That second
 * failure is the one E29 came to fix: this map is the reason a `databricks` node can no longer
 * be captioned 'AWS'.
 *
 * The lookup is `hasOwnProperty`, NOT `platform in PLATFORM_LABEL` — a caught bug, not a
 * stylistic preference. `in` walks the prototype chain, so `'constructor'` (or `'toString'`,
 * `'__proto__'`, …) tested TRUE and the function then read `.host` off `Object`'s constructor and
 * returned `undefined` — a `string`-typed function handing back a non-string, straight into JSX,
 * from an attacker-influencable-shaped field. Pinned by test.
 */
export function platformHostLabel(platform: string): string {
  return Object.prototype.hasOwnProperty.call(PLATFORM_LABEL, platform)
    ? PLATFORM_LABEL[platform as Platform].host
    : '';
}

/**
 * The compact hosting tag for an MCP-server graph node, derived from its `kind`.
 *
 * DERIVED, and correct to derive — unlike the agent case above. `gateway` and `runtime` are not
 * generic words here: they name AgentCore Gateway and AgentCore Runtime, two AWS-only Bedrock
 * constructs, so 'AWS' is a fact about what the kind IS rather than a default applied to an
 * unknown. `standard` is any MCP server reachable over HTTP, hosted wherever its owner put it —
 * 'External' says only that AGP does not host it, which is all that is known.
 *
 * When a second platform grows its own MCP-server kinds, this stops being derivable and the
 * record needs a hosting field. Recorded here rather than pre-built: an unused field is a field
 * nothing keeps honest.
 */
export function mcpHostLabel(kind: string): string {
  if (kind === 'gateway' || kind === 'runtime') return 'AWS';
  if (kind === 'standard') return 'External';
  return '';
}

/**
 * The `<option>` list for a platform filter, in the registry's canonical order.
 *
 * Derived from `PLATFORM_LABEL` rather than written out again — the second copy in
 * `AgentsList.tsx` was the one the label map there was itself derived FROM, so keeping the
 * options and dropping the map would just re-seed the duplication one level down.
 *
 * `Object.entries` over a `Record<Platform, …>` widens the key to `string`, so the cast restores
 * what the type already guarantees. Insertion order is the declaration order above, which is the
 * order every one of the five copies used.
 */
export const PLATFORM_OPTIONS: { value: Platform; label: string }[] = (
  Object.entries(PLATFORM_LABEL) as [Platform, PlatformLabel][]
).map(([value, label]) => ({ value, label: label.full }));

// ---------------------------------------------------------------------------
// The runtime handle — one row, two platforms, and no empty ARN
// ---------------------------------------------------------------------------

/**
 * The subset of an agent this decision reads. STRUCTURAL rather than `Agent` so the rule can be
 * exercised over hand-written cases (including shapes a real record should never reach, which is
 * where a labelling bug would actually surface) without constructing a 30-field fixture.
 */
export interface RuntimeHandleSource {
  platform?: Platform | null;
  agent_arn?: string | null;
  runtime_handle?: string | null;
}

/** One labelled invocation row: what to call it, what it holds, what "copy" copies. */
export interface RuntimeHandleField {
  label: string;
  /** `null` ⇒ the field APPLIES to this agent but carries no value yet ("Not set"). */
  value: string | null;
  /** Lower-case noun for the copy button's aria-label ("Copy app URL"). */
  copyLabel: string;
}

/**
 * How to render this agent's runtime handle — or `null` for "render no row at all".
 *
 * THE DISTINCTION THIS FUNCTION EXISTS FOR is `null` (no row) versus `{ value: null }` (a row
 * reading "Not set"). They are different claims and the old code could only make one of them:
 * AgentDetail rendered an unconditional `Agent ARN` row, so EVERY agent that is not an AgentCore
 * agent — a Databricks app, an Azure agent, all ~18 metadata-only seed records — displayed
 * "Agent ARN — Not set". That is not a missing value; the field does not apply. It reads as a
 * pending AWS deployment for an agent that will never have an ARN, which is precisely the
 * "ARN-less UI assumption" E29 exists to remove (design §2).
 *
 * The branch order is the contract:
 *
 * 1. **`databricks` → `App URL`, ALWAYS** (value possibly `null`). Keyed on the platform, not on
 *    the handle's presence, because the platform is what makes the field applicable: a
 *    Databricks record with no handle is a real, legitimate, inert metadata-only registration
 *    (`is_databricks_governed` requires the handle precisely so that such a record provisions
 *    nothing), and "App URL — Not set" states that accurately. Suppressing the row instead would
 *    hide the one field an operator needs to fill in to make the agent governed.
 * 2. **`agent_arn` present → `Agent ARN`.** Only ever for an agent that HAS one. `agent_arn`
 *    stays AgentCore-only across the whole stack (the delete cascade and the runtime-status
 *    probe parse it as a Bedrock ARN), so this row is never a place a Databricks handle lands.
 * 3. **`runtime_handle` on a non-Databricks platform → `Runtime handle`.** Shape-not-yet-real,
 *    kept honest rather than kept out: a third platform's records will arrive before this file
 *    learns its name, and captioning its handle `Agent ARN` would assert a Bedrock ARN that
 *    other code then tries to parse. A neutral label states only what is known.
 * 4. **Otherwise → `null`.** No handle of any kind, and no platform that implies one: there is
 *    nothing to label, so nothing renders.
 */
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

// ---------------------------------------------------------------------------
// Platform-gated tabs — hidden, never greyed
// ---------------------------------------------------------------------------

/**
 * Tab ids that must NOT render for a given platform, because the capability behind them does not
 * exist there.
 *
 * `guardrails` is the E29 case: Bedrock Guardrails are an AWS service, so a Databricks agent has
 * no guardrail surface at all. The design is explicit that such a tab is **hidden, not
 * mock-greyed** (§4 stretch) — a greyed tab promises a feature that is coming, and nothing is
 * coming, because guardrails on Databricks would be a different product built on Databricks' own
 * primitives.
 *
 * ⚠ HONEST NOTE ON TODAY'S STATE, so a reader is not misled by the presence of this table:
 * `agentDetailTabs.ALL_TABS` currently has NO `guardrails` entry. Guardrails is a
 * platform-level destination (`/secure/guardrails`), never a per-agent tab, so there is at
 * present no guardrails tab to hide on any agent and the requirement holds vacuously. This gate
 * is kept anyway, and it is not decoration: it makes the platform rule the thing a future
 * per-agent guardrails tab must pass, so adding that tab cannot silently light it up on a
 * Databricks agent. Any tab id listed here is filtered whether or not it exists yet.
 */
export const HIDDEN_TAB_IDS_BY_PLATFORM: Partial<Record<Platform, readonly string[]>> = {
  databricks: ['guardrails'],
};

/**
 * Whether a tab may render for an agent on this platform.
 *
 * An ABSENT platform hides nothing. A record whose platform nobody set is not evidence that a
 * capability is missing, and the failure directions are not symmetric: wrongly hiding a tab
 * deletes a working surface with no error and no clue, while wrongly showing one is visible and
 * self-correcting. So the gate suppresses only on a POSITIVE platform match.
 */
export function isTabVisibleForPlatform(tabId: string, platform?: Platform | null): boolean {
  if (!platform) return true;
  return !(HIDDEN_TAB_IDS_BY_PLATFORM[platform] ?? []).includes(tabId);
}

// ---------------------------------------------------------------------------
// Binding mode — the badge, and the cost it must state
// ---------------------------------------------------------------------------

/**
 * The agent-detail binding-mode badge, or `null` when there is nothing to badge.
 *
 * Copy is contract C-6, VERBATIM, and it is deliberately NOT a second literal: the label and the
 * hint are re-exported from `admin/tenantsAdminForm` (T4's landed home for them), because C-6
 * pins the SAME two words and the SAME sentence on the tenant surface and this one. Two literals
 * spelling one contract is the drift C-6 was written to prevent — the badge here and the badge
 * on the tenant list cannot disagree if there is only one string.
 *
 * `null` for `''` (an AWS tenant's mode), for `undefined` (a record written before its mode was
 * resolved), and for any value this build does not recognise. An unprobed agent has no mode to
 * claim, and a badge rendering a raw wire value teaches a vocabulary the product does not have.
 *
 * `tint` rides along because the three modes are not peers. Federation is the intended path
 * (emerald). `invoke_unavailable` (E29/T14) is a REFUSAL — the tenant could not federate, so invoke
 * is declined while inventory still works — and takes the app's rejected-red. `sp_secret` works and
 * is supported, but it costs per-caller attribution in the Databricks audit log, so it keeps amber
 * (noteworthy). Colour alone never carries the meaning — the `hint` is on the badge's `title` for
 * every mode, which is why it is required here rather than optional.
 */
export interface BindingModeBadge {
  label: string;
  /**
   * What this mode means for the caller: verbatim C-6 for `sp_secret`, the actionable
   * "what federation needs" line for `invoke_unavailable`. Always present — never `undefined`,
   * which the pre-T14 cast could produce.
   */
  hint: string;
  tint: string;
}

// One tint per mode, EXHAUSTIVELY — a `Record` over `BadgedBindingMode`, the same union that keys
// `BINDING_MODE_LABEL` and `BINDING_MODE_HINT`. Label, hint and tint are therefore one triple over
// one union: adding a fourth mode is a compile error in all three maps, not a silently untinted or
// unlabelled badge. (This claim used to be only half-true — the label came from a hand-written
// if-chain that was compile-linked to neither map, so a new branch there produced a null badge.)
//
// Federation keeps emerald (the intended path). `invoke_unavailable` takes the app's existing
// REFUSED idiom, `bg-red-50 text-red-700` — the same pair `lifecycleBadge` gives `rejected` — for
// two reasons: it is neither of the tints already in use on this badge, and the fact it reports is
// a refusal (invoke does not work) rather than a cost. Amber stays reserved for `sp_secret`, which
// works and charges for it. Colour never carries the meaning alone; the label says "Invoke
// unavailable" in words and the `hint` rides on `title` plus an sr-only span.
const BINDING_MODE_TINT: Record<BadgedBindingMode, string> = {
  federation: 'bg-emerald-50 text-emerald-700',
  invoke_unavailable: 'bg-red-50 text-red-700',
  sp_secret: 'bg-amber-50 text-amber-700',
};

export function bindingModeBadge(mode?: string | null): BindingModeBadge | null {
  // `?? undefined` because `bindingModeLabel` (T4's, on the tenant surface) takes
  // `string | undefined` while an agent's `binding_mode` arrives as `string | null` — the wire
  // spells absence as null. Both answer null, so the normalisation is lossless.
  const label = bindingModeLabel(mode ?? undefined);
  if (label === null) return null;
  // NO CAST. This used to read `mode as 'federation' | 'sp_secret'`, which was a lie the moment a
  // third mode existed (E29/T14): the cast type-checked, then `BINDING_MODE_HINT[key]` returned
  // `undefined` for a REQUIRED field and every consumer reading `hint.length` or rendering it into
  // a `title` got `undefined` instead of a sentence. A lookup plus a real null check cannot do
  // that — and because `bindingModeLabel` is the gate above, an unrecognised mode never reaches
  // here in the first place. The guard is belt-and-braces for the case where the label map and the
  // hint map disagree, which is precisely the drift worth failing loudly on.
  const hint = BINDING_MODE_HINT[mode as BadgedBindingMode];
  const tint = BINDING_MODE_TINT[mode as BadgedBindingMode];
  if (hint === undefined || tint === undefined) return null;
  return { label, hint, tint };
}

// Re-exported so a reader of this module sees where the strings come from, and so a future
// consumer has one import rather than two. `bindingModeLabel` is the C-6 label pair
// (`Federation` / `SP secret`) plus T14's `Invoke unavailable — federation required`;
// `BINDING_MODE_HINT` holds the three consequence sentences, including the sp_secret line the
// design insists the UI state rather than bury.
// `isValidWorkspaceUrl` joins them for the same reason (E29/T11): the observability pointers
// below turn a tenant's stored `workspace_url` into an `href`, and the read side must apply the
// EXACT rule the write side validated with — a second, laxer expression of "is this a workspace
// origin" is how a `javascript:` URL written by an older build reaches an anchor.
export { bindingModeLabel, BINDING_MODE_HINT } from './admin/tenantsAdminForm';
// `isDatabricksStage` joins for the third time on the same principle: it is T4's runtime
// narrowing for the stage union, and `tenantWorkspaceUrl` below needs exactly that test. A second
// `typeof c.workspace_url === 'string'` written here would be a duplicate of a discriminator.
import {
  bindingModeLabel,
  BINDING_MODE_HINT,
  isValidWorkspaceUrl,
  isDatabricksStage,
  type BadgedBindingMode,
} from './admin/tenantsAdminForm';

// ---------------------------------------------------------------------------
// Platform-access drift — the words for a mirror that stopped matching (E29/T13, §3A)
// ---------------------------------------------------------------------------

/**
 * The copy for the Access tab's drift panel. Lives HERE, next to the binding-mode copy, for
 * that copy's reason: the strings are the contract, so they belong in the framework-free module
 * vitest can execute, not inside a `.tsx` no test can reach.
 *
 * WHY THE WORD IS "DRIFT" AND THE ACTION IS "RE-ASSERT". The tab already owns the word
 * *reconcile* for something else entirely — `accessGrantsReconcile.ts` is the read-your-writes
 * overlay that absorbs Microsoft Graph's eventual consistency. That is a millisecond-scale
 * self-healing detail of one tab's rendering. THIS is a governance fact: someone edited the
 * platform's ACL around AGP, or a mirrored write half-completed. Spelling both "reconcile"
 * would put two unrelated meanings on one word in one file, which is why §3A names the action
 * Re-assert explicitly.
 *
 * The two direction lines state the DIRECTION of the mismatch, never a cause. AGP cannot tell a
 * hand-granted entry from one left by a failed revoke, and guessing would put a false story in
 * front of an operator whose next move depends on which it was.
 */
export const DRIFT_PANEL_TITLE = 'Platform access drift';

/** What the panel says under its title — the fact, and what the button will do about it. */
export const DRIFT_PANEL_NOTE =
  'The app’s platform permissions no longer match this agent’s AGP grants.';

/** The action. Verb + object, matching the tab's other buttons ("Add principal"). */
export const DRIFT_REASSERT_LABEL = 'Re-assert access';

/**
 * The confirm text, and it names the DESTRUCTIVE half deliberately.
 *
 * Re-assert is a PUT: it replaces the app's access-control list with AGP's grants, so any
 * hand-granted platform access disappears. That is the intended behaviour (§3A: AGP owns what
 * the door's list says) and it is exactly why the repair is a human's button rather than
 * automatic — an operator who has not been told this could destroy access someone else
 * deliberately granted.
 */
export const DRIFT_REASSERT_CONFIRM =
  'Re-asserting rewrites the app’s platform permissions to match AGP’s grants. ' +
  'Platform access granted by hand will be removed. Continue?';

/** What the panel says when AGP and the platform agree. Stated, so silence is not the only proof. */
export const DRIFT_CLEAN_NOTE = 'Platform permissions match this agent’s AGP grants.';

/**
 * What the panel says when the drift READ failed.
 *
 * Quiet and non-blocking on purpose: drift is a secondary read on a tab whose primary job is
 * the grant list, so a Databricks outage (or a route this deployment does not have yet) must
 * cost the operator the indicator and nothing else. It also does not claim the ACL is fine —
 * "could not be checked" is the whole of what is known.
 */
export const DRIFT_UNAVAILABLE_NOTE = 'Platform access could not be checked right now.';

/** VERBATIM contract copy, per direction. Keyed by the wire value. */
export const DRIFT_DIRECTION_NOTE: Record<AccessDriftDirection, string> = {
  unauthorized_acl: 'Has platform access without an AGP grant',
  missing_acl: 'AGP grant not enforced on the platform',
};

/**
 * The line for a direction this build does not recognise.
 *
 * Not defensive garnish: `direction` arrives off the wire from a backend that may be newer than
 * this bundle, and the alternatives are both worse than a vague sentence. Dropping the row would
 * hide a principal the operator needs to see; printing the raw wire value would teach a
 * vocabulary the product does not have. This says only what every direction has in common.
 */
export const DRIFT_UNKNOWN_DIRECTION_NOTE = 'Differs from this agent’s AGP grants';

/** The principal-kind words. Databricks' three ACL key kinds, in the tab's Title-case idiom. */
export const DRIFT_KIND_LABEL: Record<AccessDriftEntry['kind'], string> = {
  user: 'User',
  group: 'Group',
  service_principal: 'Service principal',
};

// ---------------------------------------------------------------------------
// The Access tab — which surface, and the bug that kept the grants table hidden
// ---------------------------------------------------------------------------

/**
 * The subset of an agent the Access-tab decision reads. Structural, for the same reason
 * `RuntimeHandleSource` is.
 */
export interface AccessSurfaceSource {
  platform?: Platform | null;
  identity_status: 'none' | 'pending' | 'provisioned' | 'failed';
  entra_sp_id?: string | null;
  agent_arn?: string | null;
  runtime_handle?: string | null;
}

/**
 * What the Access tab shows. A closed union so every way of NOT showing grants stays a distinct,
 * named case — the previous code could only distinguish four of these five, which is how the bug
 * below survived.
 */
export type AccessSurface =
  /** No Entra identity was ever minted (metadata-only agent). Nothing to assign roles against. */
  | { kind: 'no-identity' }
  /** Provisioning failed. Grants unavailable; Re-provision is the recovery path. */
  | { kind: 'failed' }
  /** Identity minted, runtime not wired yet — AgentCore-shaped only. Static, by design. */
  | { kind: 'awaiting-runtime' }
  /** Mid-provision: no SP yet. Spinner; Re-provision once it looks stranded. */
  | { kind: 'provisioning' }
  /** Render the grants table. `notReadyNote` is set when the identity exists but is not
   *  `provisioned` yet — the table is visible and READ-ONLY, and the caveat says why. */
  | { kind: 'grants'; notReadyNote: string | null };

/**
 * The note shown above the grants table when the identity exists but `identity_status` has not
 * reached `provisioned`.
 *
 * THE COPY IS CONSTRAINED BY THE BACKEND, and getting this wrong was a live defect in the first
 * cut of this module. `grants.py::_is_provisioned` is
 * `identity_status == "provisioned" and bool(entra_sp_id)` — BOTH legs. So while an agent sits at
 * `pending` with an SP:
 *
 * - `GET /agents/{id}/grants` returns `[]` (deliberately, not a 409 — see its docstring), and
 * - `POST /agents/{id}/grants` answers **409 "agent identity is not provisioned"**.
 *
 * An earlier draft of this note said "these assignments are live". That would have been a lie in
 * both directions: the list is empty because the server declines to read it, not because nobody
 * has access, and an operator acting on the encouragement would have hit a 409. So the note states
 * the actual constraint, and `AccessTab` hides the Add affordance while it is set — a visible
 * button that always 409s is a worse surface than the banner this replaced.
 *
 * The table is still rendered rather than suppressed, which is the point of the fix: an operator
 * can see WHERE the access list will be and that nothing is broken, instead of a banner making a
 * false claim about a runtime.
 */
export const GRANTS_PENDING_NOTE =
  'Identity provisioning has not finished, so access grants cannot be read or changed yet. ' +
  'This list becomes live once provisioning completes.';

/**
 * Which Access-tab surface an agent gets.
 *
 * ## THE BUG THIS FIXES
 *
 * The old rule was two nested conditions in `AccessTab.tsx`: the grants UI rendered only when
 * `identity_status === 'provisioned'`, and inside the not-provisioned branch, an agent with
 * `entra_sp_id && !agent_arn` got the static "Identity ready — awaiting runtime" banner.
 *
 * For an AgentCore agent that reads correctly. `identity_status` only flips to `provisioned` once
 * `provision_runtime` has configured the runtime's inbound JWT authorizer, which needs the runtime
 * to exist — so a pre-registered AgentCore agent legitimately sits at `pending` with an SP and no
 * ARN, and its grants genuinely cannot be exercised yet.
 *
 * For a **Databricks** agent the same rule is a permanent lie. `agent_arn` is AgentCore-only and a
 * Databricks agent will NEVER have one, so `entra_sp_id && !agent_arn` is true forever — the
 * grants table was suppressed for the entire life of the record, with a banner claiming a runtime
 * had not been deployed while the app was running and serving traffic. That is the headline case
 * of the design's "every ARN-less UI/logic assumption gets fixed" (§2).
 *
 * ## THE RULE NOW
 *
 * Grants render whenever `entra_sp_id` exists AND no runtime wiring is still in flight. Everything
 * else follows from that:
 *
 * - `identity_status === 'none'` and `'failed'` are checked FIRST and still win over an
 *   `entra_sp_id`. `failed` outranks a present SP deliberately: provisioning writes the SP id
 *   before the later steps that can fail, so a failed agent may well carry one, and its Access
 *   tab must offer recovery rather than a table whose grants may not be enforceable.
 * - A `provisioned` agent gets the table with no note (today's behaviour, unchanged).
 * - `pending` with NO SP → `provisioning`. Nothing to assign against yet.
 * - **`pending` WITH an SP and an `agent_arn` → `provisioning`, on every platform.** See the
 *   stranded-state note below — this is the case a first cut of this function got wrong.
 * - `pending` with an SP, no ARN, and `aws_bedrock` → `awaiting-runtime`. A positive platform
 *   test, never `!agent_arn`, so no future platform inherits the banner by lacking a field it was
 *   never going to have.
 * - `pending` with an SP, no ARN, any other platform → the table WITH `GRANTS_PENDING_NOTE`.
 *   This is the actual E29 fix: it is the only branch whose behaviour changes.
 *
 * ## THE STRANDED STATE — why a present ARN outranks the grants table
 *
 * A first cut of this function returned `grants` for `aws_bedrock` + `pending` + SP + **ARN
 * present**, which silently deleted the only recovery path for the NORMAL mid-provision failure.
 * `provision_identity` persists `entra_sp_id` with status `pending` BEFORE steps 2-3
 * (`agent_identity_service.py` ~:343 — deliberately, so a later failure cannot orphan the app
 * registration), and `is_agentcore` requires `agent_arn`, so an ECS task death mid-provision
 * leaves exactly `pending` + SP + ARN. That is precisely the shape `AccessTab`'s
 * `STALE_PENDING_MS` / `isPendingStranded` machinery exists to rescue: after ~2.5 minutes the
 * spinner offers **Re-provision**, the only way out. A read-only grants table offers nothing.
 *
 * So a present `agent_arn` on a `pending` agent means "runtime wiring has not finished" — which is
 * in-flight work with a recovery affordance, not a governance surface. Checked BEFORE the platform
 * arms and independently of platform: it is a statement about the record's runtime, and a
 * non-AgentCore record carrying an ARN is already anomalous enough that offering recovery is the
 * safer read.
 *
 * The AWS branch is a FENCE: for every AgentCore agent this returns exactly what the old nested
 * conditions in `AccessTab` returned, and the same holds for any platform presenting the SP+ARN
 * shape. Only the SP-without-ARN, non-AgentCore case changes.
 */
export function accessSurface(agent: AccessSurfaceSource): AccessSurface {
  if (agent.identity_status === 'none') return { kind: 'no-identity' };
  if (agent.identity_status === 'failed') return { kind: 'failed' };
  if (agent.identity_status === 'provisioned') return { kind: 'grants', notReadyNote: null };
  // Not provisioned, not failed, not none ⇒ 'pending'.
  if (!agent.entra_sp_id) return { kind: 'provisioning' };
  // An ARN on a still-pending agent ⇒ runtime authorizer wiring is unfinished or died mid-flight.
  // Spinner + (once stale) Re-provision — NEVER a read-only table with no way out.
  if (agent.agent_arn) return { kind: 'provisioning' };
  if (agent.platform === 'aws_bedrock') return { kind: 'awaiting-runtime' };
  return { kind: 'grants', notReadyNote: GRANTS_PENDING_NOTE };
}

// ---------------------------------------------------------------------------
// MCP grants on Databricks — recorded, and NOT delivered (E29/T11, contract C-6)
// ---------------------------------------------------------------------------

/**
 * The MCP-tab note for a platform with no env-delivery path. Contract C-6, VERBATIM.
 *
 * THE FACT BEING SURFACED is mechanical, not a roadmap hedge. An agent→MCP grant is two
 * things: an Entra app-role assignment on the MCP's service principal (the authorization),
 * and the runtime actually being told which MCP servers it may reach (the delivery). AGP
 * delivers the second half by writing environment variables through AgentCore's
 * `UpdateAgentRuntime` — a Bedrock control-plane call. A Databricks App has no counterpart:
 * AGP does not own its deployment, so there is nowhere for AGP to put the value.
 *
 * So the grant on a Databricks agent is REAL as governance (the assignment exists, it is
 * auditable in Entra, and revoking it is a real revocation) and INERT as configuration (the
 * app will not learn about it from AGP). Both halves are true, and stating only the first is
 * the failure this note exists to prevent — an operator who grants MCP access and sees a green
 * row would reasonably conclude the app can now reach that server.
 *
 * WHY THE RECORDING UI STAYS ENABLED, which is the non-obvious half. Disabling the grant
 * affordance would be the tidier-looking choice and it would be wrong twice over: the
 * assignment is a governance record with independent value (it is the intent, and it is what a
 * future delivery path would read), and a disabled control teaches "not supported" where the
 * truth is "recorded here, wired up by hand over there". Telling the truth beats removing the
 * capability.
 */
export const MCP_NOT_DELIVERED_NOTE =
  'Recorded in the registry — not delivered to the runtime on this platform yet.';

/**
 * The delivery caveat for this agent's MCP grants, or `null` when grants really are delivered.
 *
 * A POSITIVE platform test, like every other gate in this module: an ABSENT platform gets no
 * note. A record whose platform nobody set is not evidence that delivery is missing, and the
 * failure directions are not symmetric — a spurious caveat on an AgentCore agent would
 * undermine a mechanism that demonstrably works, which is worse than saying nothing.
 *
 * Keyed on the platform rather than on `runtime_kind` or on the presence of a handle, because
 * the missing thing is the PLATFORM's control-plane API, not anything about this particular
 * record. A Databricks agent with a handle, an identity and a live app still has no
 * `UpdateAgentRuntime` to call.
 */
export function mcpDeliveryNote(platform?: Platform | null): string | null {
  return platform === 'databricks' ? MCP_NOT_DELIVERED_NOTE : null;
}

// ---------------------------------------------------------------------------
// Observability — what AGP knows, and what only the workspace knows (E29/T11)
// ---------------------------------------------------------------------------

/**
 * The two things an operator asks about a Databricks agent's observability, answered by two
 * DIFFERENT systems — which is the whole reason this is one shaped object instead of a link list.
 *
 * `langfuse` is AGP's own answer and it is READ STRAIGHT OFF THE RECORD. Langfuse project
 * provisioning already fires platform-neutrally at registration (`provision_langfuse_best_effort`
 * in `agents.py`), so the join fields are on the envelope for a Databricks agent exactly as they
 * are for an AgentCore one. Nothing is fetched to answer this: the presence of
 * `langfuse_project_id` IS the answer.
 *
 * `workspace` is the workspace's answer and AGP does not have it. Per-request logging for a
 * Databricks-served model lives in that workspace's own inference tables, and who-called-what
 * lives in its system tables / audit log — both inside the customer's Unity Catalog, reachable
 * only by a query AGP is not authorised to run and would not want to. So this half is POINTERS
 * ONLY: a link into the workspace and a sentence naming where the data is.
 *
 * ⛔ WHAT THIS DELIBERATELY IS NOT. No MLflow bridging (design §4 is explicit), no Databricks
 * REST call of any kind, and no claim about whether a trace has actually been emitted. AGP knows
 * a project exists; only Langfuse knows whether anything landed in it, and the Traces tab is
 * where that question is already asked and answered against the real data.
 */
export interface ObservabilityPointers {
  /** AGP's own trace destination for this agent. */
  langfuse: {
    /** Whether a Langfuse project was provisioned for this agent. */
    provisioned: boolean;
    /** What the surface says about it. */
    note: string;
  };
  /**
   * The workspace's own telemetry — pointers only. `null` when no workspace URL is known for
   * this agent's tenant/stage, because a link cannot be built from nothing and a broken link is
   * worse than an absent one.
   */
  workspace: WorkspaceObservability | null;
}

export interface WorkspaceObservability {
  /** The workspace origin the links are built from (display + link base). */
  workspaceUrl: string;
  /** Deep link to the workspace's system-tables / audit area. */
  auditUrl: string;
  /** The label for `auditUrl`. */
  auditLabel: string;
  /** Where per-request logging actually lives. A statement, not a link. */
  inferenceNote: string;
}

/** What the surface says when the agent HAS a Langfuse project. */
export const LANGFUSE_PROJECT_PRESENT_NOTE =
  'A Langfuse project is provisioned for this agent — invocations that reach it appear on the Traces tab.';

/**
 * What the surface says when it does NOT.
 *
 * Careful about WHY, because there are two reasons and they are not the same: Langfuse may be
 * switched off for this deployment entirely, or provisioning may have been skipped/failed for
 * this one agent (it is deliberately best-effort — a Langfuse outage must not fail a
 * registration). The record cannot tell those apart, so the copy does not guess; it states the
 * observable fact and leaves the cause to the platform's own observability settings.
 */
export const LANGFUSE_PROJECT_ABSENT_NOTE =
  'No Langfuse project is provisioned for this agent, so AGP is not collecting its traces.';

/**
 * Where per-request logging lives on Databricks. Stated rather than linked: the tables are in
 * the customer's Unity Catalog under names AGP does not know, so naming the FEATURE is the
 * honest precision available — a guessed table path would be a fabricated one.
 */
export const DATABRICKS_INFERENCE_TABLES_NOTE =
  'Per-request prompts and responses are logged by Databricks itself, in the workspace’s inference tables.';

/** The audit link's label. Names the destination, not the action. */
export const DATABRICKS_AUDIT_LINK_LABEL = 'Workspace system tables & audit logs';

/**
 * The workspace-relative path to the system-tables/audit area.
 *
 * ONE path, appended to the workspace origin — Databricks' workspace UI is served from the
 * workspace host, so `{origin}{path}` is the whole construction. `explore/data/system` is the
 * Catalog Explorer entry for the `system` catalog, which is where `system.access.audit` (and the
 * rest of the system tables) live once an account admin has enabled the schemas.
 *
 * The link may land on a "not enabled" page in a workspace where system tables were never turned
 * on, and that is the RIGHT failure: Databricks says so in its own words, in the place where an
 * operator can fix it. The alternative — AGP deciding whether to show the link by probing — is
 * the Databricks read this task is explicitly forbidden to make.
 */
const DATABRICKS_AUDIT_PATH = '/explore/data/system';

/**
 * A workspace origin, or `null` if the value cannot safely become a link.
 *
 * VALIDATED, NOT TRIMMED-AND-TRUSTED, and the reason is a real attack rather than tidiness: this
 * value comes off a tenant record and lands in an `href`. `javascript:` and `data:` URLs in an
 * `href` execute on click, so a stored tenant field would be a stored-XSS sink if this function
 * merely checked for emptiness. `isValidWorkspaceUrl` is the SAME anchored `^https://…$` regex
 * the tenant form validates writes with (T4's, executed over hostile inputs in its own tests) —
 * reused rather than re-expressed, so the read side cannot be laxer than the write side, which
 * is exactly how a record written by an older/looser build slips through.
 *
 * AND THERE IS NO NORMALIZATION STEP AT ALL — no trim, no trailing-slash strip. A first cut had
 * both, and its own test caught why they were wrong: `.trim()` made a value ending in a NEWLINE
 * validate, when the write side rejects exactly that (the one hole an unanchored `$` leaves, and
 * the case the backend's own regex comment warns about). Every normalization here is a way for the
 * read side to accept something the write side refused, which is the drift this function was
 * written to prevent — so the raw value goes to the raw rule, and a record carrying stray
 * whitespace or a trailing slash simply renders no link. That is the safe direction: the Langfuse
 * half of the card still renders, and an operator sees a missing link rather than a live one built
 * out of a value nothing validated.
 */
function workspaceOrigin(url: string | null | undefined): string | null {
  if (!url || !isValidWorkspaceUrl(url)) return null;
  return url;
}

/**
 * The workspace's observability pointers for one workspace URL, or `null` when there is no
 * usable URL.
 *
 * Split out from `agentObservability` so the URL rule is testable on its own over hostile
 * inputs, and so a second surface that has a workspace URL in hand (a tenant detail page, say)
 * can render the same two pointers without going through an agent.
 */
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

/**
 * The ONE workspace URL a tenant's Databricks stages agree on, or `null`.
 *
 * THE PROBLEM THIS SOLVES is that an agent record names no stage. `workspace_url` lives on the
 * TENANT's per-stage config, a tenant has an OPEN set of stages (E28/D8 — and C5 forbids a
 * hardcoded stage name anywhere in `frontend/`), and nothing on the agent says which one hosts
 * it. So "this agent's workspace" is not a lookup; it is a question the data may not answer.
 *
 * IT ANSWERS ONLY WHEN THE ANSWER IS UNAMBIGUOUS. Every Databricks stage pointing at the same
 * workspace ⇒ that workspace is this agent's, whichever stage it runs in, and the link is safe.
 * Two stages on DIFFERENT workspaces ⇒ `null`, because picking one would be a coin flip
 * presented as a fact: an operator following a link to the wrong workspace's audit log and
 * finding no record of their agent would draw exactly the wrong conclusion. A missing link is
 * recoverable; a confidently wrong one is not.
 *
 * Iterating the tenant's own map is also the only C5-clean way to do this — the stage KEYS are
 * never read, only their configs, so no stage name appears here or can.
 *
 * Callers pass `stages` straight off a `UserTenant`/`TenantInfo`; an AWS tenant's stages carry no
 * `workspace_url`, `isDatabricksStage` skips them, and the result is `null`.
 *
 * EVERY VALUE IN THE MAP IS TREATED AS UNVALIDATED, and fix round 1 is why that is stated rather
 * than assumed. A first cut called `isDatabricksStage(config as never)` and then read the property
 * off a second cast; a review executed `{dev: null}` and got a `TypeError` that blanked the whole
 * AgentDetail page — for EVERY platform, since this runs before the Databricks check. `null` is an
 * ordinary shape here: `stages` is JSON off `/users/me`, and a key with a null value round-trips
 * exactly like that.
 *
 * The fix went into the PREDICATE (it now takes `unknown` and guards truthiness), which is what
 * let both casts disappear from this function. That ordering is the lesson worth keeping: the
 * `as never` was not incidental to the crash, it was the cause — it told `tsc` to stop checking at
 * the one boundary where unvalidated data enters, so the null had no way to be reported.
 */
export function tenantWorkspaceUrl(
  stages: Record<string, unknown> | null | undefined,
): string | null {
  if (!stages) return null;
  const urls = new Set<string>();
  for (const config of Object.values(stages)) {
    // T4's narrowing, now safe on `unknown` — so `config` needs no cast and the property read is
    // the predicate's own guarantee rather than a second assertion about the same value.
    if (isDatabricksStage(config)) urls.add(config.workspace_url);
  }
  if (urls.size !== 1) return null;
  const [only] = urls;
  return only;
}

/** The subset of an agent this decision reads. Structural, for `RuntimeHandleSource`'s reason. */
export interface ObservabilitySource {
  platform?: Platform | null;
  langfuse_project_id?: string | null;
}

/**
 * This agent's observability pointers, or `null` for "render no observability card".
 *
 * `null` FOR EVERY NON-DATABRICKS PLATFORM, deliberately, and this is a scope statement rather
 * than a claim that AgentCore agents have nothing to show. They have MORE: an AgentCore agent's
 * observability is already a first-class surface (the Traces and Cost tabs, plus the platform
 * Observability page), so a card repeating "a Langfuse project exists" above those tabs would be
 * noise. The card exists because a DATABRICKS agent's telemetry is split across two systems and
 * one of them is not AGP's — which is a fact that surface has to state, and currently states
 * nowhere.
 *
 * The `workspace` half is `null` whenever no usable workspace URL was passed. That is the common
 * case for a caller that could not resolve the agent's tenant (a viewer outside the tenant
 * directory), and the card still renders — the Langfuse half is knowable from the record alone,
 * and dropping the whole card because one half is unresolvable would hide the half AGP does know.
 */
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
