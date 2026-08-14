// accessDrift — the Access tab's platform-ACL drift decisions (E29/T13e, design §3A).
//
// WHAT DRIFT IS. On a Databricks-governed agent the Entra app-role assignment is the single
// truth for "may X invoke this agent", and the app's per-user `CAN_USE` entry is a one-way
// MIRROR of it. The mirror can stop matching in two directions: a workspace owner hand-grants
// access around AGP (`unauthorized_acl`, fails OPEN), or an AGP grant is not on the platform's
// list (`missing_acl`, fails CLOSED — a half-completed mirrored write, or an admin deleted the
// entry). Detection is automatic; repair is a human's button, because silently re-asserting
// would hide the fact an FSI operator most needs to see: someone is editing ACLs around the
// platform.
//
// WHY THIS IS A SEPARATE MODULE AND NOT `.tsx` LOGIC. Two reasons, one structural and one
// naming:
//
//  1. vitest collects `src/**/*.test.ts` in a node environment — no `.tsx`. So a decision that
//     lives inside `AccessTab.tsx` is a decision no test can execute. Every judgement the panel
//     makes is exported from here (the `platformLabels.ts` / `tenantsAdminForm.ts` idiom); the
//     component binds and renders and judges nothing.
//  2. "Reconcile" was already taken. `accessGrantsReconcile.ts` is the read-your-writes overlay
//     for Microsoft Graph's eventual consistency — a millisecond-scale rendering detail of one
//     tab. Reusing that word for a governance mismatch would put two unrelated meanings on one
//     word in one tab, which is why §3A names this feature drift and its action **Re-assert**.
//
// This module holds NO copy of its own: every string comes from `platformLabels.ts`, next to the
// binding-mode copy, so the contract's wording has exactly one home.
import type { AccessDriftEntry, AuthType, Platform } from '../../api/client';
import {
  DRIFT_CLEAN_NOTE,
  DRIFT_DIRECTION_NOTE,
  DRIFT_KIND_LABEL,
  DRIFT_PANEL_NOTE,
  DRIFT_UNAVAILABLE_NOTE,
  DRIFT_UNKNOWN_DIRECTION_NOTE,
} from './platformLabels';

/**
 * The subset of an agent the panel's gate reads. Structural rather than `Agent`, for
 * `RuntimeHandleSource`'s reason: the rule can then be exercised over hand-written shapes —
 * including ones a real record should never reach, which is where a gating bug would surface —
 * without building a 30-field fixture.
 */
export interface DriftPanelSource {
  platform?: Platform | null;
  auth_type?: AuthType | null;
  runtime_handle?: string | null;
}

/**
 * Whether this agent HAS a platform ACL that can drift.
 *
 * The exact three legs of the backend's `Agent.is_databricks_governed`, deliberately mirrored
 * rather than approximated, because a UI gate looser than the route's gate is how a surface ends
 * up calling an endpoint that answers 404/409 and showing the operator an error for a question
 * that never applied:
 *
 * - `runtime_handle` — there is actually an app. A Databricks record without one is inert
 *   metadata (like the ~18 metadata-only seed agents); no app means no access-control list.
 * - `auth_type === 'entra'` — AGP owns the identity, so there are grants to mirror. An `api_key`
 *   agent has no Entra assignments and nothing to compare an ACL against.
 * - `platform === 'databricks'` — the only platform whose door AGP asserts. A POSITIVE test, like
 *   every other gate in these modules: an ABSENT platform gets no panel, since a record whose
 *   platform nobody set is not evidence of a Databricks ACL.
 *
 * `binding_mode` is deliberately NOT read — §3A is explicit that `sp_secret` carries the same
 * asserted ACL, and the mode can legitimately be absent on a record written before it resolved.
 */
export function driftPanelApplies(agent: DriftPanelSource): boolean {
  return (
    !!agent.runtime_handle &&
    agent.auth_type === 'entra' &&
    agent.platform === 'databricks'
  );
}

/**
 * The drift READ's body → entries, or `null` when the body proves nothing.
 *
 * A 200 whose body has no `entries` array (a proxy answering `{}`, a route shape change, a
 * gateway error page served with a 200) must NOT collapse to `[]` — `[]` is the POSITIVE claim
 * "the platform matches AGP's grants", and `driftSummary`'s whole reason for a third state is
 * that absence of evidence is not evidence of a match. `Array.isArray`, not a `?? []`, also stops
 * a non-array `entries` from reaching `driftRows`' `.map` and throwing during render.
 *
 * Typed `unknown` so it is the guard even when the caller's static type says the field is there —
 * the wire is what decides, not the declaration.
 */
export function driftEntriesOf(res: unknown): AccessDriftEntry[] | null {
  if (typeof res !== 'object' || res === null) return null;
  const entries = (res as { entries?: unknown }).entries;
  return Array.isArray(entries) ? (entries as AccessDriftEntry[]) : null;
}

/**
 * How loudly one drift entry should read.
 *
 * NOT cosmetic — the two directions have opposite failure modes. `unauthorized_acl` fails OPEN:
 * someone can reach the app that AGP never authorised, which is the governance breach §3A exists
 * to make visible, so it gets amber. `missing_acl` fails CLOSED: a user AGP granted is being
 * refused at the platform's door. That is a real problem and a real annoyance, but nothing is
 * exposed by it, so it reads as information rather than a warning.
 */
export type DriftSeverity = 'warning' | 'info';

/** One rendered drift row — everything the panel needs, nothing it has to decide. */
export interface DriftRow {
  /** React key. Includes the direction: one principal can appear in BOTH directions. */
  key: string;
  /** The platform's own principal name (a Databricks username / group / SP name). */
  principal: string;
  /** 'User' | 'Group' | 'Service principal', or a neutral fallback. */
  kindLabel: string;
  /** The platform's permission word (e.g. 'CAN_USE'), passed through — AGP does not own it. */
  level: string;
  /** The contract line for this direction. */
  note: string;
  severity: DriftSeverity;
  /** Tailwind classes for the row's badge, derived from `severity`. */
  tint: string;
}

const SEVERITY_TINT: Record<DriftSeverity, string> = {
  warning: 'bg-amber-50 text-amber-700',
  info: 'bg-slate-100 text-slate-600',
};

/** The fallback kind word. Says only that this is a principal, which is all that is known. */
const UNKNOWN_KIND_LABEL = 'Principal';

/**
 * Direction → copy + severity, tolerating a value this bundle has never seen.
 *
 * `hasOwnProperty`, NOT `direction in DRIFT_DIRECTION_NOTE` — the same caught bug
 * `platformHostLabel` documents: `in` walks the prototype chain, so `'constructor'` tests TRUE
 * and the lookup then returns a non-string straight into JSX from a field the wire controls.
 *
 * An unknown direction keeps its ROW (with the neutral note) and takes the louder severity.
 * Dropping it would hide a principal from an operator; and between the two guesses, treating an
 * unrecognised mismatch as potentially-open is the safe direction.
 */
function directionView(direction: string): { note: string; severity: DriftSeverity } {
  if (!Object.prototype.hasOwnProperty.call(DRIFT_DIRECTION_NOTE, direction)) {
    return { note: DRIFT_UNKNOWN_DIRECTION_NOTE, severity: 'warning' };
  }
  return {
    note: DRIFT_DIRECTION_NOTE[direction as AccessDriftEntry['direction']],
    severity: direction === 'missing_acl' ? 'info' : 'warning',
  };
}

/** Kind → label, with the same own-property guard and the same reason. */
function kindLabel(kind: string): string {
  return Object.prototype.hasOwnProperty.call(DRIFT_KIND_LABEL, kind)
    ? DRIFT_KIND_LABEL[kind as AccessDriftEntry['kind']]
    : UNKNOWN_KIND_LABEL;
}

/**
 * The panel's rows, in the order the server sent them.
 *
 * ORDER IS THE SERVER'S, not re-sorted here. The drift read is a diff of two lists and the
 * backend is the only side that knows how it walked them; re-ordering in the UI would make two
 * consecutive reads of unchanged state look like a change.
 */
export function driftRows(entries: AccessDriftEntry[]): DriftRow[] {
  return entries.map((e, i) => {
    const { note, severity } = directionView(e.direction);
    return {
      // The index is in the key because the tuple (principal, direction) is not guaranteed
      // unique by the contract — nothing forbids two entries differing only by `level`.
      key: `${e.direction}:${e.principal}:${i}`,
      principal: e.principal,
      kindLabel: kindLabel(e.kind),
      level: e.level,
      note,
      severity,
      tint: SEVERITY_TINT[severity],
    };
  });
}

/**
 * The panel's headline state.
 *
 * THREE STATES, and keeping `unavailable` distinct from `clean` is the whole point. `null` means
 * the drift READ failed (or this deployment has no drift route yet) — that is not evidence the
 * ACL matches, and saying "permissions match" on a failed read would be the exact class of lie
 * the E29 surfaces were written to remove. `clean` is a POSITIVE claim, made only from a
 * successful read that returned nothing: it is also the post-re-assert success state, since the
 * re-assert route answers with fresh drift rather than an ack.
 */
export interface DriftSummary {
  state: 'clean' | 'drifted' | 'unavailable';
  count: number;
  note: string;
}

export function driftSummary(entries: AccessDriftEntry[] | null): DriftSummary {
  if (entries === null) return { state: 'unavailable', count: 0, note: DRIFT_UNAVAILABLE_NOTE };
  if (entries.length === 0) return { state: 'clean', count: 0, note: DRIFT_CLEAN_NOTE };
  return { state: 'drifted', count: entries.length, note: DRIFT_PANEL_NOTE };
}
