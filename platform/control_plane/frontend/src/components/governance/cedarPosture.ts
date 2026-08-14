// cedarPosture — the ENFORCE posture warning for the Cedar Policies tab (E36/T10,
// research item 15 tier 1).
//
// Pure, framework-free, `.test.ts`-pinned — the same reason `cedarPolicyForm.ts` sits
// beside it and `opsStatus.ts` sits beside the ops pills: vitest collects only
// `src/**/*.test.ts`, so `CedarPoliciesTab.tsx` is build-gated while every DECISION it
// renders is test-gated. Here the decision IS the copy.
//
// ---------------------------------------------------------------------------
// WHY THIS MODULE EXISTS
//
// An enforcing gateway is default-deny: a call with no matching allow is refused in AWS,
// and NOTHING in this platform turns that refusal into a governance message — no reason
// string, no policy id, no blocked screen. The refusal reaches the agent as a plain tool
// error inside its reasoning loop, where the model retries, apologises, or picks another
// tool. So the operator's only diagnosis is to read the posture, and until now the tab
// stated the posture (`ENFORCEMENT_COPY.enforce`, an emerald "Enforcing — default-deny")
// without stating its CONSEQUENCE. Emerald is right — the posture is the secure one and it
// is deliberate — but on its own it reads as reassurance for a state whose blast radius is
// every agent on the gateway.
//
// The research names the trap directly: this is the SAME copy decision as the ENFORCE-flip
// confirmation dialog in the component, and "written separately, the two surfaces will
// contradict each other." So the copy is a single exported constant, pinned by a test, and
// the test asserts the vocabulary the two surfaces genuinely share — "blocked" and "not
// covered by a policy" — against a transcript of the dialog, rather than leaving alignment
// to a reviewer's memory.
//
// ---------------------------------------------------------------------------
// WHY IT TAKES A BARE STRING
//
// `enforcement_mode` arrives from `GET /mcp-servers/{id}/policies` already narrowed to
// `CedarEnforcementMode` by the FE client — but the field behind it is a plain `str` on the
// backend model (`models/mcp_server.py`, tolerant by design), and the mode can also be
// ADOPTED from the live AWS engine, whose own spelling is `ENFORCE`. TypeScript's union is
// therefore an assertion about the wire, not a guarantee from it. So the narrowing here is
// tolerant — trimmed and lower-cased, the `toCicdStatus` idiom — because the harmful
// direction is asymmetric: a stray `"ENFORCE"` that fell through a strict `=== 'enforce'`
// would SILENTLY WITHHOLD the warning from the one gateway that most needs it. There is no
// corresponding harm in recognising a mode spelled loudly.
// ---------------------------------------------------------------------------

import type { CedarEnforcementMode } from '../../api/client';

/**
 * The ENFORCE posture warning, in one place.
 *
 * Every clause is a fact NO other surface on this tab states, and each is sourced from
 * `docs/cedar-tool-policies.md` so console and guide cannot drift:
 *
 *   1. the blast radius is every agent on the gateway, not just the operator's own;
 *   2. a deny is invisible here — it surfaces to the agent as a bare tool error;
 *   3. deleting the last policy does NOT lift enforcement (it leaves a gateway that
 *      denies everything).
 *
 * It must not contradict the ENFORCE-flip confirmation dialog in `CedarPoliciesTab.tsx`
 * ("only users named in policies will be able to call its tools. Agents and users not
 * covered by a policy will be blocked."), nor the enforce banner beside it ("Assigned users
 * not covered by a policy are blocked."). Same terms — "blocked", "not covered by a policy"
 * — because three surfaces in one card must not name one outcome three ways: "blocked" is
 * the word the other two already use, so this one follows rather than inventing "denied".
 * Same claim too, stated for a posture already in force rather than one about to be.
 *
 * It deliberately does NOT name the "Log only" / "Disable" buttons: that control renders
 * only for `canManage`, so a viewer must not be pointed at a control they cannot see.
 */
export const ENFORCE_POSTURE_WARNING =
  'Every tool call not covered by a policy below is blocked — for every agent connected ' +
  'to this gateway. A blocked call is not recorded here: it reaches the agent as a ' +
  'tool error with no reason and no policy id. Removing the last policy does not lift ' +
  'enforcement — only changing the mode does.';

/**
 * Mode → its persistent warning, or `null` for a posture that needs none.
 *
 * `Record<CedarEnforcementMode, …>` with no `default` branch: adding a member to the union
 * without deciding its warning here is a COMPILE error, so `tsc` is the exhaustiveness
 * test. `none` and `log_only` block nothing, so neither warns — `ENFORCEMENT_COPY` already
 * says so in the banner, and an amber panel over a gateway that denies nobody would train
 * operators to ignore the one that does.
 */
const POSTURE_WARNING: Record<CedarEnforcementMode, string | null> = {
  none: null,
  log_only: null,
  enforce: ENFORCE_POSTURE_WARNING,
};

// A `Map` for the lookup, not the object literal above — `githubLink.ts:100-102`'s reasoning
// applies unchanged (and `templatesView.ts:393-396`, `reconcileView.ts:601-604` restate it):
// the key is a SERVER-SUPPLIED string, and an object would resolve `'constructor'` to
// `Object.prototype`'s member, handing React a FUNCTION where a warning belongs. `?? null`
// cannot catch that, since a function is neither `null` nor `undefined`, and lower-casing
// only hides the ones it mangles (`toString` → `tostring`); `constructor` is already
// lower-case. DERIVED from the `Record` rather than re-listed, so the compile-time
// exhaustiveness above is still the gate on a new enforcement mode.
const POSTURE_WARNING_BY_MODE: ReadonlyMap<string, string | null> = new Map(
  Object.entries(POSTURE_WARNING),
);

/**
 * The persistent warning to render for a gateway's live engine mode — `null` when the
 * posture blocks nothing.
 *
 * Tolerant on input: whitespace and case are normalised before lookup (`" ENFORCE "` →
 * the warning), and an absent or unrecognised mode yields `null` rather than a guess.
 * Withholding the warning is only correct when we genuinely have no enforcing mode to
 * report; inventing one over an unknown string would be a claim about a gateway nobody has
 * established anything about.
 *
 * A prototype-named mode is safe here for a structural reason, not a lucky one: the lookup
 * goes through a `Map`, whose keys are its own entries and nothing else, so `"constructor"`
 * misses instead of resolving to `Object.prototype.constructor`. See the note on
 * `POSTURE_WARNING_BY_MODE` — the unit test for this failed against a plain object literal.
 */
export function postureWarning(engineMode: string | undefined): string | null {
  return POSTURE_WARNING_BY_MODE.get((engineMode ?? '').trim().toLowerCase()) ?? null;
}
