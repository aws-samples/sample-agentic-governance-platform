// opsStatus.ts — the CLOSED-UNION boundary for the two independent Operations status
// machines (E28/T10, contract C3). Pure, framework-free, `.test.ts`-pinned: only
// `src/**/*.test.ts` is collected by vitest, so every status DECISION lives here and the
// `.tsx` files that render a pill are wiring only. The `projectRoles.ts` / `opsUi.ts` pair
// is the precedent.
//
// ---------------------------------------------------------------------------
// WHY THIS MODULE EXISTS
//
// `cicd_status` is a bare string on the wire with FIVE writers — the create, retry and
// finalize paths in `project_service`, the promote route, and a best-effort shell helper
// in a CodeBuild buildspec (`_st() { _u cicd_status "$1"; }`). There is no enum behind it
// anywhere, which `projectRoles.ts` already stated as a known hazard: "With no enum behind
// `cicd_status` no backend test can catch that."
//
// It was right. A module-private copy of the status→tint table was forked across two
// pages, and when `promoting` / `deployed` were added only one copy was extended. On the
// other page BOTH new statuses fell through to that table's amber fall-through, so A REPO LIVE
// IN PRODUCTION RENDERED IN THE SAME AMBER AS ONE STILL PROVISIONING — wrong, and reassuring,
// on the one row whose question is "what is in production right now?".
//
// The remedy shipped with that fix was a comment asking the next author not to re-fork the
// table. That is an honour system, and it fails the moment a second page legitimately needs
// a different visual treatment — which is exactly what this epic builds. So the guard here
// is mechanical instead:
//
//   1. The wire string is NARROWED ONCE, at this boundary, into a closed union.
//   2. Every lookup is a `Record<CicdStatus, X>` / `Record<RuntimeStatusKey, X>` with NO
//      `default` branch and no fall-through. Adding a member to a union without adding its
//      table entry IS A COMPILE ERROR — `tsc` is the exhaustiveness test.
//   3. The tables return a SEMANTIC KEY (an `OPS_BADGE` key), never a Tailwind class
//      string. Each surface keeps owning its own styling, which is what makes a
//      legitimately different treatment a styling change rather than a reason to fork the
//      table again.
//
// ---------------------------------------------------------------------------
// WHY THERE ARE TWO MACHINES AND NOT ONE PILL
//
// `cicd_status` describes the DELIVERY pipeline: it is written by the buildspec through a
// targeted DynamoDB `update-item` as a build progresses. The runtime's health is a
// different fact from a different producer — a boto3 describe against the AgentCore
// control plane, read through `GET /agents/{id}/runtime`.
//
// Merging them into one pill would force a lie in both directions, and the harmful
// direction is easy to reach: an agent whose last build deployed successfully but whose
// runtime is now unreachable would read GREEN, because the delivery machine has nothing
// bad to say. So the two stay separate, both unions carry `unknown`, and `unknown` is
// visually distinct from `failed` — an unreachable control plane is not a broken runtime.
// ---------------------------------------------------------------------------

import type { OPS_BADGE } from './opsUi';

/** An `OPS_BADGE` key — a SEMANTIC tint name, never a class string. */
type BadgeKey = keyof typeof OPS_BADGE;

// ---------------------------------------------------------------------------
// The unions. Pinned by contract C3 and coded against by T11/T13, so the names, the
// members and their order are a cross-task seam — not a local detail.
// ---------------------------------------------------------------------------

// AMENDED (E28 fix round 1). The union is now EXACTLY the set of values a real writer
// produces, verified by grep over `backend/src/` and the buildspec rather than assumed:
//
//   provisioning — the initial materialize row, and `retry_repo`'s reset
//   ready        — `_finalize_repo` (`project_service.py:1670`), materialize's terminal success
//   promoting    — the promote route, and a rollback, while the prod build runs
//   deployed     — the buildspec's terminal `_st deployed` (`buildspec.yml:391`)
//   failed       — several paths: a failed materialize step, `_mark_failed`, a promotion that
//                  could not start, and the buildspec's `_st failed`
//   unknown      — this module's fallback for anything none of the above wrote
//
// `building` and `pending` were in the first draft of the pin and are DELETED: nothing
// anywhere writes either one. Inventing union members is the same error as inventing the
// `ok`/`passing`/`success` aliases the old table carried — a member no writer produces is a
// branch no reality reaches, and it makes the exhaustiveness guard vouch for fiction.
export const CICD_STATUSES = [
  'provisioning', 'ready', 'promoting',
  'deployed', 'failed', 'unknown',
] as const;
export type CicdStatus = typeof CICD_STATUSES[number];

// The backend counterpart is `RUNTIME_STATUSES` in `backend/src/models/agent.py` (~line 372),
// beside the `RuntimeStatus` model that `GET /agents/{agent_id}/runtime` returns. The two lists
// are identical member-for-member and in order, verified against T5's implementation; the
// backend's own docstring records that it keeps `status` a plain `str` because "the frontend
// owns the exhaustiveness check", which is the `Record<>` tables below. Change one, change both.
export const RUNTIME_STATUSES = [
  'ready', 'creating', 'updating', 'failed', 'not_deployed', 'unknown',
] as const;
export type RuntimeStatusKey = typeof RUNTIME_STATUSES[number];

// ---------------------------------------------------------------------------
// RuntimeStatus — the C2 wire shape of `GET /agents/{agent_id}/runtime`.
//
// The BACKEND COUNTERPART is the `RuntimeStatus` pydantic model in
// `backend/src/models/agent.py` (~line 382); the route lives in `backend/src/api/routes/agents.py`
// and the only producer is `AgentIdentityService.runtime_status`. Field shapes were checked
// against it field-for-field. Whoever changes either side must change the other.
//
// Declared HERE rather than imported from `api/client.ts` on purpose: this module must
// stay pure (vitest only collects `.ts`, and the client pulls in axios), and the client is
// another task's file this epic. Structural typing means the client's own declaration
// assigns to this one without either side importing the other — the same reason
// `projectRoles.ts` declares `ProdCandidateSource` instead of importing `Repository`.
//
// `status` is deliberately typed `string`, not `RuntimeStatusKey`: it arrives as a bare
// wire value and must pass through `toRuntimeStatus` to be trusted. Typing it as the union
// here would ASSERT the narrowing rather than perform it — the mistake this whole module
// exists to stop.
// ---------------------------------------------------------------------------
export interface RuntimeStatus {
  agent_id: string;
  /**
   * THE STAGE THIS READING DESCRIBES — or the unattributable sentinel when it describes no
   * particular one. Never captioned onto a pill WITHOUT going through the two functions below.
   *
   * The history matters, because the field's meaning changed under it. T5 wrote it when the
   * agent envelope held a single `agent_arn` that whichever stage deployed last overwrote, so
   * the probe could not attribute what it read and the route always answered the sentinel; the
   * rule was flatly "do not caption a pill with this". Since E28A an agent owns one runtime PER
   * STAGE and the record names them, so the route reports the stage it really probed whenever
   * it can — and a LEGACY scalar-only record still answers the sentinel, because its one runtime
   * genuinely cannot be assigned to a stage.
   *
   * So the rule is now conditional, and the condition is NOT evaluated at a render site:
   * `runtimeScope` decides whether a reading is attributable at all, and `stageRuntimeCell`
   * decides whether it may sit on a particular stage's row (both in `repositoryDetailTabs.ts`,
   * both selector-tested). A `.tsx` that reads this field and forms a caption has re-derived
   * that judgement — which is the fabrication those two functions exist to make unreachable.
   */
  stage: string;
  status: string;
  runtime_arn?: string | null;
  image_tag?: string | null;
  checked_at: string;
  /** A SAFE short hint only — never an ARN, a token or a response body. */
  detail?: string | null;
}

// ---------------------------------------------------------------------------
// toCicdStatus — the ONE narrowing point for `Repository.cicd_status`.
//
// Total by construction: every input, including `null` / `undefined` / blank / garbage,
// returns a union member. Two shaping decisions worth their lines:
//
//   • CASE- AND WHITESPACE-TOLERANT, because one writer is a best-effort shell helper.
//     The value is compared, not trusted.
//
//   • NO TRANSLATION. Every writer value is a union member, so this function narrows and
//     nothing more. An earlier draft aliased `ready → pending`, because the first version of
//     the pinned union had no `ready` member even though `_finalize_repo` writes one — a
//     workaround for a bad pin. With the pin corrected the alias would preserve a fiction:
//     it would report a state the backend never wrote, which is exactly the class of
//     invention this module exists to eliminate. Translating a wire value is only ever
//     honest when the wire value is genuinely not one of ours, and then it is `unknown`.
//
//   • Everything unrecognized → `unknown`, and the old defensive aliases
//     (`ok` / `passing` / `success` / `error` / `running`) are GONE. No writer in this
//     codebase emits them; they were guesses. Under a closed union a guess is precisely
//     the failure mode — a value nobody established must not be rendered as a plausible
//     neighbour, because "Provisioning" in amber is a confident sentence about a repo we
//     know nothing about.
// ---------------------------------------------------------------------------
export function toCicdStatus(raw: string | null | undefined): CicdStatus {
  const s = (raw ?? '').trim().toLowerCase();
  return (CICD_STATUSES as readonly string[]).includes(s) ? (s as CicdStatus) : 'unknown';
}

// ---------------------------------------------------------------------------
// toRuntimeStatus — the same narrowing for the runtime machine.
//
// The rule that governs every caller: ABSENT DATA IS NOT GOOD NEWS. `undefined` is "we did
// not get an answer" — a request that failed, or one never made — and it narrows to
// `unknown`, never to `ready`. A green pill over an agent nobody could reach is the single
// most damaging thing the repo row could say, and it is the reachable direction: the
// delivery machine can legitimately read `deployed` at the same moment the runtime describe
// returns nothing.
//
// T5 maps the boto3 statuses onto the C2 enum server-side, so well-formed responses are
// already union members. The lowercasing here is what keeps a raw provider shape
// (`NOT_DEPLOYED`) from being treated as unrecognized, without inventing mappings for
// provider states the route does not promise.
// ---------------------------------------------------------------------------
export function toRuntimeStatus(raw: string | null | undefined): RuntimeStatusKey {
  const s = (raw ?? '').trim().toLowerCase();
  return (RUNTIME_STATUSES as readonly string[]).includes(s) ? (s as RuntimeStatusKey) : 'unknown';
}

// ---------------------------------------------------------------------------
// CICD_BADGE_KEY — delivery status → its `OPS_BADGE` tint key.
//
// `Record<CicdStatus, BadgeKey>` with no `default`: a new union member without an entry
// here does not compile. That type annotation IS the anti-drift mechanism.
//
//   provisioning / promoting → amber, in-flight. `promoting` is deliberately NOT emerald:
//       emerald is reserved for a TERMINAL good state, and an emerald "promoting" would read
//       as "already in prod" — the one misreading this row must never invite.
//   ready                    → amber. Materialize finished, so the repo and agent EXIST, but
//       nothing has been built or promoted. Emerald here (what the old table gave it) claimed
//       a terminal success over a repo that had never shipped anything — the amber-prod bug in
//       the other direction.
//   failed                   → rose, the only failure tint.
//   deployed                 → emerald. The buildspec's terminal success write, i.e. A DELIVERY
//       SUCCEEDED — and that is ALL it says. It does NOT say production is serving this image:
//       the buildspec runs that write for EVERY stage with no branch, so the value carries no
//       stage at all. This comment used to claim the production reading, and the ungoverned-in-prod
//       banner was built on that claim — it then fired on any successful non-production build and
//       announced "Serving production" over a repository that had promoted nothing (E28A, #5). The
//       emerald tint is still right for what the value means, and the label ("Deployed") is a
//       delivery word; the false premise was only ever in the prose. Whether production is serving
//       is a JOIN, not a status read — see `prodServingState` in `repositoryDetailTabs.ts`, which
//       is now the only thing permitted to answer it. The ONLY emerald member.
//   unknown                  → slate. Not a failure and not a success; see the `unknown` note
//       on `OPS_BADGE`.
//
// `ready` reuses the palette's `pending` amber rather than `provisioning`'s. They are the same
// two colors today, but they mean different things — `provisioning` is "work is happening now"
// and `pending` is "nothing is happening, waiting on a trigger" — so if the palette ever
// separates them (a pulse on in-flight, say) `ready` must not inherit an activity cue for a
// repo where nothing is running.
// ---------------------------------------------------------------------------
export const CICD_BADGE_KEY: Record<CicdStatus, BadgeKey> = {
  provisioning: 'provisioning',
  promoting: 'provisioning',
  ready: 'pending',
  failed: 'failed',
  deployed: 'ready',
  unknown: 'unknown',
};

// ---------------------------------------------------------------------------
// CICD_LABEL — delivery status → the sentence-case label its pill shows.
//
// Sentence-case, and never equal to the raw key: the pill used to render the bare
// lowercase wire value, so E27's new statuses read as `promoting` / `deployed` beside
// sentence-case copy. Callers must therefore NOT apply Tailwind `capitalize` over these —
// that would produce "Promoting To Prod…".
//
// `unknown`'s copy says the state was not established rather than naming a state. "No
// status reported" is a fact about our read; "Provisioning" would be a claim about the
// repo.
//
// WHY `ready` DOES NOT READ "Ready" (fix round 1). The wire value stays `ready` — this is a
// label decision only — but the row now shows Runtime beside Delivery, and the runtime machine
// has its OWN `ready` meaning "the agent is up and serving". Two adjacent pills both reading
// "Ready" for unrelated facts is the ambiguity this task exists to remove; worse, delivery's
// `ready` is the WEAKER state of the two, so the stronger-sounding word would sit on the
// column that has less to say.
//
// The label is "Not built yet", which is what the value actually denotes. Tracing the
// writers: `_finalize_repo` sets `ready` when materialize completes, and the ONLY things that
// move it afterwards are the buildspec's terminal `_st deployed` / `_st failed` — there is no
// intermediate "building" write anywhere. So a repo sits at `ready` from the moment it is
// scaffolded until its first build succeeds or fails, and "not built yet" is precisely that
// span. "Built" (the other candidate) would be false — nothing has been built yet — and
// "Awaiting promotion" would skip the build step that has to happen first.
//
// SHORTENED in E28/T13 (M-f) from "Awaiting first build" (20 chars → 13). The old wording was
// both the LONGEST pill in the table AND the default state of every freshly scaffolded repo, so
// a new project's fleet view was a column of the widest label in the palette — the column's
// width was set by its least interesting state. The shorter wording says the same thing: the
// repo exists and no build has landed. It is deliberately still distinct from `deployed`, and
// from the action column's "Never deployed" (a different machine answering a different
// question — one is "has it built?", the other "has anything reached prod?").
// ---------------------------------------------------------------------------
// The `failed` labels are SELF-DESCRIBING on both machines ("Delivery failed" /
// "Runtime failed") rather than a bare "Failed". A table gives each pill a column header to
// disambiguate it, but these pills are also rendered as a PAIR without one (the repo detail
// header shows the two independent pills side by side), and there "Failed" next to "Failed"
// forces the operator to guess which half of the system broke. "Delivery" is used rather than
// "Build" because this status covers a failed materialize step and a promotion that could not
// start, not only a build.
// ---------------------------------------------------------------------------
export const CICD_LABEL: Record<CicdStatus, string> = {
  provisioning: 'Provisioning',
  ready: 'Not built yet',
  promoting: 'Promoting to prod…',
  deployed: 'Deployed',
  failed: 'Delivery failed',
  unknown: 'No status reported',
};

// ---------------------------------------------------------------------------
// RUNTIME_BADGE_KEY — runtime health → its tint key. Same `Record` guard.
//
//   ready                → emerald, and the ONLY emerald member: the runtime is serving.
//   creating / updating  → amber, in-flight.
//   failed               → rose.
//   not_deployed         → slate. NEITHER a failure nor a success: "never deployed" is a
//       normal state for a freshly scaffolded repo. Rose would accuse it of breaking;
//       emerald would claim it is serving traffic.
//   unknown              → slate. An unreachable control plane is not a broken runtime
//       (C2, verbatim) — so it must not be rose; and it is not in-flight either, so it
//       must not be amber.
//
// `not_deployed` and `unknown` share the neutral tint because both are "nothing is
// running here", but their LABELS differ sharply: one is a known absence, the other is an
// unanswered question, and only the label can carry that.
// ---------------------------------------------------------------------------
export const RUNTIME_BADGE_KEY: Record<RuntimeStatusKey, BadgeKey> = {
  ready: 'ready',
  creating: 'provisioning',
  updating: 'provisioning',
  failed: 'failed',
  not_deployed: 'unknown',
  unknown: 'unknown',
};

// `failed` is "Runtime failed" for the reason given above `CICD_LABEL`: the two pills are also
// rendered as an uncaptioned pair, and two bare "Failed"s make the operator guess which half of
// the system broke. `ready` keeps the plain "Ready" — the runtime machine is the one where
// "ready" means what an operator expects it to mean (the agent is up and serving), so it gets
// the unqualified word and delivery's weaker `ready` yields.
//
// WHY `unknown` NO LONGER READS "Unreachable" (E28 final review, FR-3). That word ASSERTS that a
// probe was made and did not come back — a claim about the runtime's reachability. But `unknown`
// is reached by three routes and only one of them involves a probe at all:
//
//   • the probe ran and came back AMBIGUOUS. `AgentIdentityService.runtime_status` maps every
//     non-ResourceNotFound `ClientError` here — including AccessDenied, which a LIVE runtime
//     behind an IAM/SCP/wrong-region misconfig also returns — precisely BECAUSE the platform
//     cannot conclude the runtime is broken or gone. "Unreachable" concluded it anyway.
//   • the `/runtime` request itself failed, or was never sent.
//   • NO RUNTIME WAS PASSED AT ALL. Both repository lists render `RepoRow` without a `runtime`
//     prop — deliberately and permanently, because the route is per-agent and a fleet list would
//     need one request per row. So every row of both lists resolved to `unknown`, and every one of
//     them claimed a failed probe that had never been attempted.
//
// The replacement is a fact about OUR READ rather than about the thing, which is the phrasing
// `CICD_LABEL.unknown` ("No status reported") already uses for the same situation on the other
// machine. It must not be confused with `failed` ("Runtime failed", a real verdict) nor with
// `not_deployed` ("Not deployed", a known absence), which is why it names the runtime and says the
// state is unknown rather than saying nothing.
//
// ONE label serves BOTH a probed and an unprobed surface, and that is deliberate rather than a
// compromise: in every case above the platform holds NO runtime answer, which is the whole content
// of the word. A second union member ("not_probed") was considered and REFUSED — `RUNTIME_STATUSES`
// is mirrored member-for-member in `backend/src/models/agent.py` and nothing on either side would
// ever write it, and a member no writer produces is a branch no reality reaches (the same reason
// `building` / `pending` were deleted from the delivery union in fix round 1).
//
// LENGTH IS PART OF THE DECISION (the M-f lesson). This is the commonest runtime pill on both
// lists — today it is every row — so a long label would set the column's width from its least
// informative state. At 15 characters it is one wider than `failed`'s "Runtime failed", which the
// column already has to fit, so the rendered width does not move.
// ---------------------------------------------------------------------------
export const RUNTIME_LABEL: Record<RuntimeStatusKey, string> = {
  ready: 'Ready',
  creating: 'Creating…',
  updating: 'Updating…',
  failed: 'Runtime failed',
  not_deployed: 'Not deployed',
  unknown: 'Runtime unknown',
};

// ---------------------------------------------------------------------------
// isCicdInFlight — is something RUNNING for this repo right now?
//
// Two behaviours hang off this: the row's in-flight treatment (a spinner instead of a
// dot), and suppressing a second Promote — the route answers one with a 409, so there is
// nothing to offer while a build is running.
//
// Takes the NARROWED union, not a raw string, so it cannot be called on an un-narrowed
// wire value. Kept in step with the tint by construction and pinned in the tests: every
// in-flight member wears the in-flight amber.
// ---------------------------------------------------------------------------
export function isCicdInFlight(status: CicdStatus): boolean {
  // `building` was removed from the union in fix round 1 (nothing writes it), so the in-flight
  // members are exactly the two states a writer parks a repo in WHILE work is running:
  // materialize (`provisioning`) and a prod build (`promoting`). Note `ready` is NOT in flight
  // — it is a resting state waiting on a trigger, which is why it wears the `pending` amber
  // rather than `provisioning`'s.
  return status === 'provisioning' || status === 'promoting';
}
