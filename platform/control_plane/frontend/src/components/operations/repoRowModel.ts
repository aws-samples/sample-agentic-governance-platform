// repoRowModel.ts — the pure derivations behind the shared repo row (E28/T10, contract C4).
//
// `RepoRow.tsx` renders; this file DECIDES. The split is not stylistic: vitest collects only
// `src/**/*.test.ts`, so any judgement made inside the `.tsx` is a judgement no test can
// reach — which is exactly how the forked status tables drifted into shipping a
// production repo in provisioning's amber. `projectRoles.ts` established this idiom; the row
// follows it.

import { isCicdInFlight, toCicdStatus, toRuntimeStatus, type RuntimeStatusKey } from './opsStatus';
import { initialsFor, NO_VALUE } from './opsLabels';

// ---------------------------------------------------------------------------
// The fields the row actually reads, as a STRUCTURAL shape rather than an import of
// `Repository`. Same reason `projectRoles.ts` declares `ProdCandidateSource`: it keeps this
// module free of the axios-importing client (so it stays unit-testable) and makes the
// contract explicit — these six fields, nothing else.
// ---------------------------------------------------------------------------
export interface RepoRowSource {
  cicd_status?: string | null;
  last_dev_image_tag?: string | null;
  last_promoted_image_tag?: string | null;
  prod_candidate_status?: string | null;
  created_by?: string | null;
}

/** Trim to a real value, or null. Blank strings are absence, not data. */
function text(value: string | null | undefined): string | null {
  const trimmed = (value ?? '').trim();
  return trimmed.length > 0 ? trimmed : null;
}

// ---------------------------------------------------------------------------
// repoAction — WHAT, if anything, a human must do about this repo.
//
// This is the row's first column and its sort key (C4: "`Action required` sorts first"),
// which makes it the highest-stakes derivation in the row: too eager and it fires on every
// row, at which point an operator learns to ignore it and a real failure is buried by the
// noise; too lax and the failure is buried by silence.
//
// The precedence, and why it is this order:
//   1. delivery-failed  — a broken build. Outranks everything: nothing else about the row is
//      actionable until it is fixed.
//   2. runtime-failed   — the thing actually serving traffic is down. This is the SECOND
//      state machine earning its place in the row: delivery can read `deployed` and have
//      nothing bad to say while the runtime is broken.
//   3. approval-pending — a merge to `main` is waiting for an owner. A request, not a fault.
//   4. drift            — dev has moved past prod. Worth knowing, nobody is blocked.
//   5. never-deployed   — nothing has ever shipped. An observation about a new repo.
//   6. status-unknown   — the delivery status is a value no writer produces, so the row
//      cannot speak for it. Surfaced rather than hidden, because a status nobody recognizes
//      on a production surface is itself the thing to look at.
//
// TWO RULES THAT LOOK LIKE OMISSIONS AND ARE NOT:
//
//   • IN FLIGHT ⇒ `none`. While a build or promotion is running the answer to "what should I
//     do?" is "wait", and a column that says "wait" is asking for an action that does not
//     exist. The status pill already reports the in-flight state honestly. This also covers
//     the mid-promotion case where a candidate is still recorded: the promotion IS the
//     approval being acted on, so re-requesting approval would be wrong.
//
//   • AN ABSENT RUNTIME ANSWER ⇒ NOT an action. `runtime === undefined` means the read failed
//     or was never made, which is the normal state anywhere the runtime route is not wired
//     up. Treating it as actionable would light up "action required" on every row in the
//     fleet simultaneously. Note this does NOT contradict the rule that absent data must
//     render as "unknown" and never as "ready": the runtime PILL still says unreachable, and
//     `toRuntimeStatus(undefined)` is still `unknown`. What this column refuses to do is
//     manufacture WORK out of a missing answer — the two are different claims, and only the
//     pill is making a claim about the runtime.
// ---------------------------------------------------------------------------
export const REPO_ACTIONS = [
  'delivery-failed',
  'runtime-failed',
  'approval-pending',
  'drift',
  'never-deployed',
  'status-unknown',
  'none',
] as const;
export type RepoActionKey = typeof REPO_ACTIONS[number];

export function repoAction(
  repo: RepoRowSource,
  runtime: RuntimeStatusKey | string | null | undefined,
): RepoActionKey {
  const cicd = toCicdStatus(repo.cicd_status);
  const runtimeStatus = toRuntimeStatus(runtime);

  // A failure is a failure regardless of anything else on the row.
  if (cicd === 'failed') return 'delivery-failed';
  if (runtimeStatus === 'failed') return 'runtime-failed';

  // Nothing is asked of a human while the pipeline is mid-flight.
  if (isCicdInFlight(cicd)) return 'none';

  // Compared to the LITERAL 'pending', matching `canPromote` / `prodCandidateView`, so this
  // column and the promote surface can never disagree about whether something is waiting.
  if (repo.prod_candidate_status === 'pending') return 'approval-pending';

  if (prodVersion(repo).drifted) return 'drift';

  // No prod image has ever been recorded. A distinct and more accurate statement than
  // "drift" — there is no prod version for dev to be ahead of.
  if (text(repo.last_promoted_image_tag) === null) return 'never-deployed';

  if (cicd === 'unknown') return 'status-unknown';

  return 'none';
}

// ---------------------------------------------------------------------------
// The sort order. Failures, then requests, then observations, then nothing — the order an
// operator scanning a fleet reads down. Distinct ranks so the sort is deterministic.
// ---------------------------------------------------------------------------
export const REPO_ACTION_RANK: Record<RepoActionKey, number> = {
  'delivery-failed': 0,
  'runtime-failed': 1,
  'approval-pending': 2,
  drift: 3,
  'never-deployed': 4,
  'status-unknown': 5,
  none: 6,
};

// ---------------------------------------------------------------------------
// The column's copy. Each label names an ACTION or states an OBSERVATION.
//
// `none` is an em dash, deliberately: "OK" or "Healthy" would be a claim about facts this
// column never checked — all it knows is that nothing it inspects is asking for a human.
//
// `approval-pending` STATES that an approval is outstanding and does not offer one. Ops
// surfaces must never grow approve/grant verbs (the shadow-governance failure mode), and C4's
// props carry no promote callback — only `onNavigate`. The approval itself lives on the
// governed promote surface.
// ---------------------------------------------------------------------------
// M-a (E28/T13): `delivery-failed` read "Build failed" while `CICD_LABEL.failed` — the SAME
// condition, one cell to the right on the SAME row — read "Delivery failed". Two words for one
// fact, side by side. It is "Delivery failed" in both places now, and `opsStatus.ts` explains why
// that is the correct half of the pair to keep: this status covers a failed materialize STEP and
// a promotion that could not START, not only a build, so "Build failed" was not merely
// inconsistent — it named the wrong stage of the pipeline for two of the three ways to reach it.
export const REPO_ACTION_LABEL: Record<RepoActionKey, string> = {
  'delivery-failed': 'Delivery failed',
  'runtime-failed': 'Runtime down',
  'approval-pending': 'Awaiting owner approval',
  drift: 'Prod behind dev',
  'never-deployed': 'Never deployed',
  'status-unknown': 'Status not reported',
  none: NO_VALUE,
};

/** Which actions are FAULTS (rose) vs REQUESTS/OBSERVATIONS (amber) vs nothing (slate). */
export const REPO_ACTION_TONE: Record<RepoActionKey, 'fault' | 'attention' | 'quiet'> = {
  'delivery-failed': 'fault',
  'runtime-failed': 'fault',
  'approval-pending': 'attention',
  drift: 'attention',
  // An observation about a new repo, not a problem — it must not wear a warning tint, or
  // every freshly created repo starts life looking broken.
  'never-deployed': 'quiet',
  'status-unknown': 'quiet',
  none: 'quiet',
};

// ---------------------------------------------------------------------------
// sortRepoRows — the row order BOTH lists use (E28/T13). C4: "`Action required` sorts first."
//
// Exported and shared for the same reason the status tables are: two independent sorts over the
// same fact drift exactly as two independent tint tables did. A fleet list that puts a failing
// repo third and a project list that puts the same repo first are two answers to one question,
// and the column's whole promise is that what needs a human is at the top.
//
// Non-mutating (`[...rows]`) — the array belongs to the caller's props, and React state must not
// be sorted in place.
//
// Ties break on NAME, not on insertion order: `Array.prototype.sort` is stable, so without a
// tiebreak the order within a rank would be whatever the API happened to return, and two lists
// reading the same partition in different orders would disagree again through the back door. It
// takes the runtime status per repo because `repoAction` does — the runtime machine can put a
// row at the top on its own (`runtime-failed`), so a sort that ignored it would rank a row
// differently from the label the row actually renders.
// ---------------------------------------------------------------------------
export function sortRepoRows<T extends RepoRowSource & { id: string; name: string }>(
  rows: readonly T[],
  runtimeFor: (repo: T) => RuntimeStatusKey | string | null | undefined = () => undefined,
): T[] {
  return [...rows].sort((a, b) => {
    const rankA = REPO_ACTION_RANK[repoAction(a, runtimeFor(a))];
    const rankB = REPO_ACTION_RANK[repoAction(b, runtimeFor(b))];
    return rankA !== rankB ? rankA - rankB : a.name.localeCompare(b.name);
  });
}

// ---------------------------------------------------------------------------
// prodVersion — what prod is running, and whether dev has moved past it.
//
// Drift requires BOTH tags: a repo with a dev image and no prod one has not drifted, it has
// never shipped, and calling that drift would put a warning on every newly created repo. The
// dev tag is returned alongside because "prod is behind" without naming what it is behind is
// not actionable.
//
// DISTINCT from the amber differing-tag warning in `ProjectRepositoriesTab`, which compares
// the PROD CANDIDATE's tag against the dev tag to answer "would approving this ship what dev
// is running?". This compares the PROMOTED tag against the dev tag to answer "is what prod is
// running still current?". Different questions, different pairs of fields; that warning is
// untouched.
// ---------------------------------------------------------------------------
export interface ProdVersion {
  /** The promoted image tag, or null when nothing has ever been promoted. */
  tag: string | null;
  /** True only when BOTH tags exist and differ. */
  drifted: boolean;
  /** The dev tag prod is behind. Null unless `drifted`. */
  devTag: string | null;
}

export function prodVersion(repo: RepoRowSource): ProdVersion {
  const tag = text(repo.last_promoted_image_tag);
  const dev = text(repo.last_dev_image_tag);
  const drifted = tag !== null && dev !== null && tag !== dev;
  return { tag, drifted, devTag: drifted ? dev : null };
}

// ---------------------------------------------------------------------------
// ownerLabel — the Owner column. `created_by` is written by the backend from the validated
// principal, so it is a UPN or an Entra oid depending on the writer; both shapes go through
// the shared `initialsFor`, which splits on dots so `jane.doe` and `jane.smith` do not
// collapse into one avatar.
//
// An absent owner reads "Unknown" rather than blank: a partial record is a fact, and an empty
// cell reads as a rendering fault.
// ---------------------------------------------------------------------------
export function ownerLabel(createdBy: string | null | undefined): { name: string; initials: string } {
  const name = text(createdBy);
  return name === null ? { name: 'Unknown', initials: '?' } : { name, initials: initialsFor(name) };
}

// ---------------------------------------------------------------------------
// THE TWO WORDINGS THIS EPIC HAD TO CORRECT (E28/T13). Both are the same defect — a caption
// asserting something the data does not establish — and both are here, as ONE constant each with
// a test, because the wrong versions existed in FOUR and TWO places respectively. Four copies of
// a string is how this epic's original bug happened.
// ---------------------------------------------------------------------------

/**
 * How the prod candidate's actor is described. "pushed by", NEVER "merged by".
 *
 * A3. The old copy read `merged by @jorge`, and NOTHING IN THE SYSTEM KNOWS THAT A MERGE
 * HAPPENED. Tracing the writer: `builds.py` registers the candidate from a validated GitHub OIDC
 * token, and `actor` / `sha` are read ONLY from that token; the template workflow triggers on
 * `push: branches: [main, dev]`, so the candidate job fires on ANY push to `main` — whether it
 * arrived by merging a pull request or by committing straight to the branch. No `merged` flag, no
 * PR number and no `event_name` is persisted anywhere, so the two cases are indistinguishable to
 * every reader of this record.
 *
 * The fix is therefore NOT to detect a direct push — that is not derivable — but to stop making
 * the claim. "pushed by" is true of both cases: a merge IS a push to `main`. "merged by" is true
 * of only one, and it is the reassuring one, because it implies a review the platform never saw
 * (AGP holds no PR or review state — design §6). Same rule as the runtime pill carrying no stage
 * caption and an actor carrying its own currency: do not caption a fact with a name the data does
 * not establish.
 */
export const CANDIDATE_ACTOR_VERB = 'pushed by';

/**
 * How the `last_promoted_*` fields are labelled. "Last deployed" / "last deployed by", NEVER
 * "Promoted" / "promoted by".
 *
 * P10/A4. A ROLLBACK writes `last_promoted_*` too (T4), so after one the row said "Promoted
 * <date> by <someone>" about a person who rolled back and an act that was not a promotion. The
 * field means "what prod is RUNNING", not "what was APPROVED" — and those diverge the moment a
 * deployment is undone.
 *
 * The wording matches the detail page and the deployments tab exactly ("Deployed by"), which is
 * the point: a third wording for this one fact would be the drift these changes exist to remove.
 */
export const LAST_DEPLOYED_LABEL = 'Last deployed';
export const LAST_DEPLOYED_ACTOR_VERB = 'last deployed by';

// ---------------------------------------------------------------------------
// promotionReadiness — the row's PASSIVE indicator, where its Promote button used to be
// (E28C/T7, D-C4d).
//
// WHY THE BUTTON WENT AWAY, because this is an inversion of the obvious fix. Both repository
// surfaces offered "Promote to prod" and only the project tab's had the reveal-then-confirm
// dialog, so the instinct was to give the detail page a dialog as well and keep both entries.
// The ruling went the other way: promotion is an APPROVAL OF SPECIFIC BYTES, and the row cannot
// show what those bytes are — what dev is currently running, what the candidate's commit and
// image are, and whether the candidate is digest-pinned or a mutable tag all live on the detail
// page. An approval offered where the evidence is not visible is the weaker act dressed as the
// stronger one, which is the defect this epic is named for. So there is now ONE promote entry
// point, at the one surface that can show its object.
//
// WHAT THE ROW KEEPS IS THE INFORMATION, which was never the problem: that this repository is
// waiting for an owner. A passive indicator states it. It carries no verb, no button styling and
// no handler — the row's props deliberately carry no promote callback (C4), and this is why they
// still don't need one.
//
// SAME PREDICATE AS THE BUTTON'S GATE. The literal `'pending'` comparison that `canPromote`,
// `promoteBlockedReason`, `prodCandidateView` and `repoAction` all make. An indicator asking its
// own question could show "waiting" on a repo whose detail page offers nothing, and two answers
// to "is an approval outstanding?" is exactly the drift the forked status tables were.
//
// NOT ROLE-GATED — the one deliberate difference from the button it replaces, and it is the same
// call `isCurrentAttempt` makes for "this is what is live". The button was withheld from a caller
// who could not use it (a role refusal renders NOTHING, so no row nags anyone about privilege).
// This is a FACT ABOUT THE REPOSITORY rather than an offer to the reader, so it renders for
// everyone; hiding "an owner is being waited on" from a viewer would withhold the very state a
// fleet list exists to surface. The function therefore takes no role, and its arity is pinned.
// ---------------------------------------------------------------------------

/**
 * The indicator's copy. A STATE, not a call to action — "Promote to prod" here would read as a
 * button on a surface that no longer has one, and the row must not appear to offer the act.
 */
export const PROMOTION_READY_LABEL = 'Ready for promotion';

/** Is an owner's approval outstanding on this repository? */
export function promotionReadiness(repo: RepoRowSource): boolean {
  return repo.prod_candidate_status === 'pending';
}

// ---------------------------------------------------------------------------
// THE DIGEST (E28B/T6, D-B3). Shared display logic, because more than one surface shows an
// artifact and a second copy of "how do we abbreviate a digest" is the drift this module exists
// to remove.
// ---------------------------------------------------------------------------

/**
 * The OCI digest grammar the backend ingests, mirrored: `sha256:` + exactly 64 LOWERCASE hex.
 *
 * Byte-identical to `builds.py`'s `_IMAGE_DIGEST_RE`, and lowercase-only for the same reason it
 * gives: registries emit lowercase, so a case-variant digest would not resolve. A mirror that were
 * looser here would abbreviate a value the API would have refused.
 */
const IMAGE_DIGEST_RE = /^sha256:[0-9a-f]{64}$/;

/** How many hex chars of the digest are shown. Seven, matching the git-short sha beside it. */
const DIGEST_HEX_SHOWN = 7;

/**
 * A digest, short enough to read: `sha256:abc1234…`.
 *
 * THE PREFIX IS KEPT, and the ellipsis with it. Both are load-bearing on a surface that also shows
 * a 7-char git sha and an image tag ending in one: a bare `abc1234` is indistinguishable from the
 * commit, and a truncation with nothing marking it reads as the whole value. `sha256:abc1234…` can
 * only be one thing, and it cannot be mistaken for something copy-pasteable.
 *
 * A NON-BLANK VALUE THAT IS NOT A DIGEST IS ECHOED VERBATIM, never truncated — the `toCicdStatus` /
 * `recordStatusLabel` rule. Truncating an unrecognized value would present the first few characters
 * of something we do not understand as though we had parsed it; passing it through says "the record
 * holds this", which is true and debuggable. (The backend validates the shape at ingest, so this
 * branch should be unreachable — which is exactly why it must not silently mangle its input.)
 *
 * Blank/absent ⇒ null. The caller renders absence; see `PROMOTION_TAG_ONLY_*` for what absence
 * MEANS here, because on the promote surface it is not merely a missing field.
 */
export function shortDigest(digest: string | null | undefined): string | null {
  const value = text(digest);
  if (value === null) return null;
  if (!IMAGE_DIGEST_RE.test(value)) return value;
  return `sha256:${value.slice('sha256:'.length, 'sha256:'.length + DIGEST_HEX_SHOWN)}…`;
}

/**
 * The marker shown where a promotable candidate has NO digest — a "tag-only" candidate.
 *
 * WHY THIS EXISTS AT ALL, because it is not a fault and must not be dressed as one.
 * `prod_candidate_digest` is OPTIONAL BY DESIGN: a materialized repo carries a COMMITTED copy of
 * its build workflow, so a repo whose `build.yml` predates this epic keeps POSTing a digest-less
 * body, and `record_prod_candidate` registers that candidate tag-only rather than refusing it.
 * Requiring the digest would have turned a missing optimization into a total outage of the deploy
 * path, so accepting both is right.
 *
 * BUT IT MAKES THE EPIC'S HEADLINE GUARANTEE CONDITIONAL ON THE DEPLOYED TEMPLATE, and until this
 * marker existed NOTHING told the operator which repos were affected. The guarantee is that an
 * approval names the exact BYTES: a digest is that by construction, whereas a tag is a mutable
 * pointer into a mutable registry over a non-reproducible build, so the bytes behind it can differ
 * between the moment a human approves and the moment prod deploys. On a tag-only candidate the
 * owner is approving a pointer, and that is a materially weaker act than the one this surface
 * otherwise promises.
 *
 * So it is rendered as a CAUTION, in the `attention` weight this file already gives a request rather
 * than a fault (`REPO_ACTION_TONE`) — never rose, never an error, and never blocking. It is a known,
 * accepted, self-healing state: the repo's next template update ships a workflow that posts a
 * digest. An operator must be able to see the gap and choose; they must not be told their repository
 * is broken.
 *
 * The remedy is named because a caution with no remedy is just an alarm.
 */
export const PROMOTION_TAG_ONLY_LABEL = 'Tag-only — mutable';
export const PROMOTION_TAG_ONLY_NOTE =
  'This candidate records no image digest, so approving it approves a mutable tag rather than exact bytes: what the tag points at can change between this approval and the deployment. The repository’s build workflow predates digest recording — updating it from the template makes later candidates digest-pinned.';
