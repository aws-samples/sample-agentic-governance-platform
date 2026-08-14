// deploymentsTab.ts — the pure companion behind the repository's Deployments tab and its
// rollback confirm (E28/T12, contracts C1 + C2 — D5/D7).
//
// It lives in a `.ts` because vitest collects only `src/**/*.test.ts`: a judgement made inside a
// `.tsx` is a judgement no test can reach, which is how two forked copies of the status→tint table
// shipped a repo live in production wearing provisioning's amber. `repositoryDetailTabs.ts` and
// `repoRowModel.ts` are the house pattern; `DeploymentsTab.tsx` is wiring only.
//
// NO STAGE LITERAL APPEARS IN THIS FILE, and none may (C5). Stages are free-form (D8) — a tenant's
// stage set is OPEN, so a tenant may legitimately carry a single non-conventional stage — and every
// stage here comes from a deployment row and is compared to nothing. The rollback route defaults
// its own stage server-side; the confirm sends the ATTEMPT'S own stage, so the frontend never names
// one.
//
// ---------------------------------------------------------------------------
// THE COLLAPSE IS IMPORTED, NOT RE-DERIVED
//
// `collapseByBuild` is imported from `repositoryDetailTabs.ts` and this module contains no second
// copy of it — not a partial one, not an "extended" one. That is the single most expensive drift
// available to this file, because T11 shipped the naive version of the rule and review caught it:
// the `deployment` partition is APPEND-ONLY, AGP writes the launch row when a build is requested,
// the buildspec writes the terminal row SEPARATELY, and NOTHING EVER CLOSES A LAUNCH ROW. So a
// predicate that asks whether any row is still at its launch outcome is permanently true after any
// build, and a history list that believed one would render every historical deployment as
// perpetually in flight — on the page an operator opens precisely to find out whether something is
// still running.
//
// Both the join key and the launch outcome's literal value are therefore ABSENT from this file
// entirely, and its test asserts that: a re-derivation would have to name both, so their absence is
// a checkable property rather than a promise. (Which is also why this comment describes them
// instead of spelling them out — the guard reads raw source and does not skip comments, because a
// guard that has to decide what is "only a comment" is a guard a comment-shaped hit can defeat.)
//
// The shared function also solves the mirror defect: the terminal row carries no `actor`, no
// `actor_kind` and no `source_sha` BY DESIGN (`buildspec.yml:104-106` states that reader contract),
// so reading them off the succeeded row yields null every time, forever, while the platform DOES
// know who promoted. `DeploymentAttempt` hands back `{ row, outcome, inFlight, actor, shortSha }`
// with each field taken from whichever row actually carries it. If this tab ever needs behaviour
// the shared function lacks, the fix is to EXTEND that function with tests — never to fork it.

import type { Deployment } from '../../../api/client';
import { isCicdInFlight, toCicdStatus } from '../opsStatus';
import { canDestroy, roleActionMessage, type ProjectRoleName } from '../projectRoles';
import { collapseByBuild, type DeploymentAttempt } from '../repositoryDetailTabs';

/** Trim to a real value, or null. Blank strings are absence, not data. */
function text(value: string | null | undefined): string | null {
  const trimmed = (value ?? '').trim();
  return trimmed.length > 0 ? trimmed : null;
}

// ---------------------------------------------------------------------------
// stageHistories — the history, grouped per stage.
//
// GROUPED BY STAGE rather than rendered as one flat list, because "roll this stage back" is a
// per-stage question: the route validates a tag against the succeeded rows FOR ONE STAGE, so an
// interleaved list would put two stages' targets side by side under one confirm and invite an
// operator to pick a target the route will refuse.
//
// THE AUTHORITY HERE IS THE HISTORY, NOT THE TENANT RECORD — and that is a deliberate divergence
// from `environmentRows`, which reads the tenant's stage map and renders NOTHING for a stage the
// API does not return. The two answer different questions. The environment strip asks "what is each
// CONFIGURED environment running?", where a retired stage must not resurrect a row. This asks "what
// has this repository SHIPPED?", which is a question about the past: dropping a retired stage's
// deployments would delete the evidence that a real release happened, and the append-only partition
// exists precisely so that evidence survives. Pinned in the tests so the divergence stays a
// decision rather than becoming a bug report.
//
// Stages are sorted ALPHABETICALLY, on the stage's own name. A fixed order array naming the
// conventional stages would be the same hardcode wearing a hat.
// ---------------------------------------------------------------------------

export interface StageHistory {
  /** The stage's name, verbatim from the rows. Free-form (D8). */
  stage: string;
  /** Its COLLAPSED attempts, newest first. */
  attempts: DeploymentAttempt[];
  /**
   * The image this stage is CURRENTLY serving — its newest SUCCEEDED attempt's tag, or null.
   *
   * Not its newest attempt: the newest may be a failure or still running, and naming either as
   * what the stage is serving would report an image that never took traffic. It is also what the
   * rollback confirm compares against, so that "roll back" cannot be offered for the tag already
   * live.
   */
  currentTag: string | null;
}

export function stageHistories(rows: readonly Deployment[]): StageHistory[] {
  // ONE collapse for the whole history, then split. Collapsing per stage would be the same work
  // with a chance to disagree, and `collapseByBuild` already returns newest-first.
  const attempts = collapseByBuild(rows);
  const stages = [...new Set(attempts.map((a) => a.row.stage))].sort((a, b) => a.localeCompare(b));
  return stages.map((stage) => {
    const mine = attempts.filter((a) => a.row.stage === stage);
    const current = mine.find((a) => a.outcome === 'succeeded') ?? null;
    return {
      stage,
      attempts: mine,
      currentTag: current === null ? null : text(current.row.image_tag),
    };
  });
}

// ---------------------------------------------------------------------------
// attemptRepoRef — WHOSE REPOSITORY IS THIS ROW'S? (E28C/T7, D-C5)
//
// THE FACT THIS EXISTS FOR, confirmed twice on live data: DELETING A REPOSITORY DOES NOT DELETE ITS
// DEPLOYMENT ROWS. The E23 cascade tears down five artifacts plus the registry record, and the
// `deployment` partition is deliberately not among them — ten rows survived the 2026-08-04 cascade.
//
// RETENTION IS THE DECISION, AND IT IS THE RIGHT ONE. The partition is append-only precisely so the
// evidence of what reached production outlives the thing that produced it; dropping a deleted
// repository's history would erase the record of real production deployments, which is the opposite
// of what an audit surface is for. What was wrong was that the retention was SILENT.
//
// WHERE THE DANGLING REFERENCE IS, because it is not a rendered id. Nothing displays
// `Deployment.repo_id`. The history is reachable only through the repository detail page, which asks
// `/agents/{id}/deployments` and lets the route resolve the repository server-side — so every row
// that comes back is IMPLICITLY ATTRIBUTED to the repository whose page is open, by the title and
// frame around it. THAT attribution is the reference, and it is the one that can be false: an agent
// whose repository was deleted and re-created carries rows from both, and the survivors would read
// as this repository's delivery history without a word said.
//
// So the comparison is the row's own `repo_id` against the repository being shown — the same shape
// as `prodServingState`'s join: identify by an id the row CARRIES, never by position and never by
// trusting the surrounding framing.
//
// BOTH SIDES MUST BE REAL VALUES, and the two absences are refused for the same reason. A row with
// no `repo_id` is a partial record, not evidence of a deletion; a page that does not know which
// repository it is showing would mark EVERY row deleted. "repo deleted" is a definite claim about a
// destructive act, so it is made only when both ids are present and genuinely differ — the rule the
// runtime probe follows for an absent reading, and the reason a null join key is un-collapsible.
// ---------------------------------------------------------------------------

/**
 * What a retained row says when the repository it belongs to is not the one on screen.
 *
 * A STATEMENT OF FACT, not a fault. The row is real evidence of a real deployment and is kept; it
 * simply is not this repository's. No alarm word and no imperative — nothing here is broken, and an
 * operator must not be sent to fix a partition that is working exactly as designed.
 */
export const DELETED_REPO_MARKER = 'repo deleted';

/**
 * The delete confirm's statement of what the cascade does NOT tear down.
 *
 * The OTHER half of D-C5's honesty, and it lives beside the marker on purpose: the two are one
 * decision seen from two moments. This sentence is what an operator reads BEFORE the teardown; the
 * marker is what they read AFTERWARDS, on the rows this sentence promised would still be there. Two
 * separately-worded versions of one retention policy is how a surface comes to promise one thing and
 * show another, so both come from this module and both are pinned.
 *
 * It states the retention and then the REASON, because "history is kept" alone reads like an
 * oversight on a screen whose whole subject is removing things — and the reason is what makes it
 * obviously correct rather than merely disclosed.
 */
export const DEPLOYMENT_RETENTION_NOTE =
  'Deployment history is retained. The record of what reached production outlives the repository, so past deliveries stay auditable and are marked as belonging to a deleted repository.';

export interface AttemptRepoRef {
  /** True only when both ids are known and differ. */
  orphaned: boolean;
  /** The marker to render, or null when there is nothing honest to say. */
  marker: string | null;
}

export function attemptRepoRef(
  viewingRepoId: string | null | undefined,
  row: Pick<Deployment, 'repo_id'>,
): AttemptRepoRef {
  const mine = text(viewingRepoId);
  const theirs = text(row.repo_id);
  // Either side absent ⇒ no claim. See the comment above for why each refusal is deliberate.
  if (mine === null || theirs === null || mine === theirs) {
    return { orphaned: false, marker: null };
  }
  return { orphaned: true, marker: DELETED_REPO_MARKER };
}

// ---------------------------------------------------------------------------
// isCurrentAttempt — is this attempt the one the stage is serving?
//
// A FACT, not an affordance, and therefore rendered for EVERYONE — including a caller who may not
// roll anything back. Hiding "this is what is live" behind the role gate would withhold the single
// most useful thing on the panel from every viewer, which is the opposite of what an Ops surface is
// for. Both sides must be real values: `null === null` would mark a never-deployed stage's every
// attempt as current.
// ---------------------------------------------------------------------------
export function isCurrentAttempt(
  imageTag: string | null | undefined,
  currentTag: string | null | undefined,
): boolean {
  const tag = text(imageTag);
  const current = text(currentTag);
  return tag !== null && current !== null && tag === current;
}

// ---------------------------------------------------------------------------
// mayRollback — may this caller be OFFERED a rollback at all?
//
// OWNER-or-admin, on the STRICT gate: the route calls `_require_project_role` — the plain helper,
// NOT the variant that carries the design-§3 fallback — which is the SAME helper and the SAME
// threshold promote uses. Its docstring says why in as many words: a rollback is a write to
// PRODUCTION, so anything looser here would BE a bypass of promote's gate, since whichever of a
// differently-behaving pair is looser becomes the real gate.
//
// So a project with nobody accountable is NOT rollbackable by a mere tenant member, and this module
// never reads the server's fallback bit at all — a test asserts that field name appears nowhere in
// this file, which is why the name is described rather than written. `headerActions` is the
// precedent to copy: `mayMaintainProject` keeps the fallback for Retry; `canPromote` and
// `canDestroy` do not.
//
// DELEGATED to `canDestroy` rather than re-derived, and a test asserts the two agree for every
// (role, level) pair. "Owner-or-admin with no fallback" already has a body; a fourth predicate
// spelling the same rule out again is how two gates that must match come to differ. It is named
// separately because the vocabulary at the call site is a rollback rather than a teardown, so a
// future divergence (a break-glass admin-only rollback, say) is one edit here.
// ---------------------------------------------------------------------------
export function mayRollback(held: ProjectRoleName | null, roleLevel: number): boolean {
  return canDestroy(held, roleLevel);
}

// ---------------------------------------------------------------------------
// rollbackEligibility — may THIS attempt be offered as a rollback target, and if not, why?
//
// Four refusals folded into one boolean would lose the distinction the repo already treats as
// load-bearing (see `promoteBlockedReason`): "not yet" is a precondition an operator can act on and
// deserves a sentence, whereas "not you" is a role refusal that must show NOTHING — a hint reading
// "you need Owner" on every row of a history list is both noise and an invitation to go asking for
// privilege.
//
// THE ORDER IS THE CONTRACT. `not-owner` is checked FIRST and outranks every other reason, so no
// refusal can leak a privilege hint by another route: if `in-flight` won, a viewer would read "a
// delivery is running", a sentence that implies waiting would earn them a button it never will.
//
//   • not-owner    — render nothing at all.
//   • not-a-target — the attempt is not a `succeeded` one, or carries no tag. `started` means "we
//                    asked" and `failed` means "it did not run"; neither is evidence the image ever
//                    served traffic, and the service refuses both with the same 409. A missing tag
//                    would be a 422 (the route requires one, with no default). Nothing is said,
//                    because a failed attempt already reads as failed — a "cannot roll back to a
//                    failure" note on every failed row is noise.
//   • current      — this IS what the stage is serving. Not a rollback: the confirm's own sentence
//                    would be false and the build would change nothing. The row is MARKED as
//                    current instead, which says the same thing positively.
//   • in-flight    — a delivery is running, and the service reuses promote's bounded in-flight
//                    guard, so the route answers 409. Stated, because it is the one refusal that
//                    resolves on its own.
//
// A `failed` delivery status is deliberately NOT a refusal: an incident is exactly when a rollback
// is needed, and `failed` is the likeliest status to be looking at while doing it.
// ---------------------------------------------------------------------------
export type RollbackEligibility = 'ok' | 'not-owner' | 'not-a-target' | 'current' | 'in-flight';

export interface RollbackTarget {
  held: ProjectRoleName | null;
  roleLevel: number;
  /** The repo's bare `cicd_status` — narrowed here, never compared raw. */
  cicdStatus: string | null | undefined;
  /** The collapsed ATTEMPT's outcome, not a raw row's. */
  outcome: Deployment['outcome'];
  /** The attempt's image tag. */
  imageTag: string | null | undefined;
  /** What the stage is serving, from `stageHistories`. */
  currentTag: string | null | undefined;
}

export function rollbackEligibility(target: RollbackTarget): RollbackEligibility {
  // FIRST, always — see the ordering note above.
  if (!mayRollback(target.held, target.roleLevel)) return 'not-owner';
  if (target.outcome !== 'succeeded' || text(target.imageTag) === null) return 'not-a-target';
  if (isCurrentAttempt(target.imageTag, target.currentTag)) return 'current';
  // Narrowed through the shared boundary: a raw `===` would miss ` PROMOTING ` and offer a
  // rollback the route answers with a 409.
  if (isCicdInFlight(toCicdStatus(target.cicdStatus))) return 'in-flight';
  return 'ok';
}

/**
 * The sentence shown INSTEAD of the affordance, or `null` to show nothing.
 *
 * `Record<RollbackEligibility, string | null>` with no `default` branch, so a fifth eligibility is
 * a `tsc` error naming this table (the C3 idiom). Three of the five are deliberately `null`:
 * `ok` renders the button, and `not-owner` / `not-a-target` / `current` each have a reason to stay
 * silent that is given above.
 */
export const ROLLBACK_BLOCKED_NOTE: Record<RollbackEligibility, string | null> = {
  ok: null,
  'not-owner': null,
  'not-a-target': null,
  current: null,
  'in-flight':
    'A delivery is running for this repository. Wait for it to finish — a rollback started now would be refused.',
};

// ---------------------------------------------------------------------------
// rollbackActionMessage — the rollback route's FIXED literals → a sentence.
//
// The axios interceptor surfaces the backend `detail` as `err.message`, so these are matched on the
// literals the rollback route in `api/routes/projects.py` pins (cited without line numbers, the
// idiom `resourcesTab.ts` uses — a line range drifts on the next edit to that file and then points
// at something else, which is worse than pointing at nothing):
//   403 "insufficient project role"                     — the STRICT owner gate
//   409 "no such succeeded deployment to roll back to"   — the tag was refused
//   409 "a promotion is already in flight"               — a delivery is running
//   502 "failed to start the rollback build"             — the build never STARTED
//   404 "Repository not found"                           — the row moved under us
//
// THE REJECTED TAG IS THE INTERESTING ONE, and it is why this function exists rather than reusing
// `promotionActionMessage`. The route answers 409 with a FIXED detail that NEVER ECHOES THE TAG
// BACK (deliberately — echoing caller input into a log-visible response), and it is an ORDINARY
// STATE, not a server fault: the tag was validated against the succeeded rows for this repo AND
// this stage and did not match, which in practice means the history moved between this page's read
// and the click. So its sentence describes a stale page and offers a reload, and it names no tag —
// there is none to name.
//
// The 502's sentence must distinguish "the build never started" from "the rollback failed": nothing
// reached production, so retrying is safe. That is the one thing an operator mid-incident needs to
// be told, and it is the same distinction `promotionActionMessage` draws for promote's 502.
//
// The 403 names OWNER flatly and mentions no fallback, because the design-§3 one genuinely does not
// reach this route — naming it would suggest a way in that does not exist.
//
// Everything unrecognised delegates to `roleActionMessage`, so the shared literals (409 last-owner,
// 503 unreadable roster, 400 invalid role) still have ONE mapping table and an already-curated
// backend detail passes through rather than being swallowed.
// ---------------------------------------------------------------------------
export function rollbackActionMessage(
  raw: string,
  fallback = 'The rollback could not be started.',
): string {
  const message = raw.trim();
  if (!message) return fallback;
  if (/insufficient project role/i.test(message)) {
    return 'You need the Owner role on this project to roll back — a rollback deploys to production, so it is gated exactly as promotion is. Ask an owner to run it, or to grant you Owner.';
  }
  if (/no such succeeded deployment to roll back to/i.test(message)) {
    return 'That image is no longer a valid rollback target for this stage — only a deployment that previously SUCCEEDED here can be redeployed. Reload this page to see the current history.';
  }
  if (/promotion is already in flight/i.test(message)) {
    return 'A delivery is already running for this repository, so the rollback was refused rather than allowed to race it. Wait for it to finish and try again.';
  }
  if (/failed to start the rollback build/i.test(message)) {
    return 'The rollback build couldn’t be started, so nothing changed in production. Try again in a moment.';
  }
  if (/repository not found/i.test(message)) {
    return 'This repository is no longer available — it may have been deleted. Reload the page.';
  }
  return roleActionMessage(message, fallback);
}
