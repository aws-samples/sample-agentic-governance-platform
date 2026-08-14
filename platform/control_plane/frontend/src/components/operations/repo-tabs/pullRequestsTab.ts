// pullRequestsTab.ts — the pure companion behind the repository's Pull requests tab
// (E28/T14, contract C2 — D14+D15).
//
// It lives in a `.ts` because vitest collects only `src/**/*.test.ts`: a judgement made inside a
// `.tsx` is a judgement no test can reach, which is how two forked copies of the status→tint table
// shipped a repo live in production wearing provisioning's amber. `repositoryDetailTabs.ts` and
// `deploymentsTab.ts` are the house pattern; `PullRequestsTab.tsx` is wiring only.
//
// ---------------------------------------------------------------------------
// THE TAB'S VISIBILITY IS A RUNTIME FACT, AND `ready` IS NOT IT
//
// The registry's `ready` flag answers "does this body exist in the shipped app?" — a BUILD-TIME
// question, and after this task the answer is permanently yes. Whether this org can serve pull
// requests at all is a different question with a different lifetime: the App's `pull_requests`
// permission is a MANUAL per-org grant, and GitHub does not retro-apply a manifest permission
// change to an already-created App. So an org onboarded before this feature answers 403 forever
// until an admin grants it and re-approves.
//
// Overloading `ready` with that would make the registry lie about what this epic built — and it
// would lie per-org, on a value that is global to the build. Hence `prTabVisibility` below: a
// separate, runtime mechanism, decided here where a test reaches it.
//
// EVERY FAILURE RESOLVES TO HIDDEN, not just the capability one. That is deliberate and it is the
// requirement: a tab that renders and then cannot answer is worse than an absent tab, because the
// tablist announces it and hands the keyboard user somewhere broken. A missing grant, a GitHub
// outage, a 502 and an unrecognized error are all "this surface cannot serve pull requests right
// now", and the honest rendering of that is nothing at all — the rest of the page is unaffected
// and still answers every question it can. Note which direction that fails in: the risk of
// hiding-on-error is a tab an operator expected, and the risk of showing-on-error is a tab whose
// every action fails. Only the first is recoverable by reloading.
//
// ---------------------------------------------------------------------------
// A REFUSAL IS A STATE, NOT AN ERROR (D15)
//
// `can_approve === false` is the backend telling us the linked human may not approve THIS pull
// request — most importantly because they opened it. That is rendered calmly, with the reason
// stated beside the missing button, never as an error banner: the platform is working exactly as
// designed when it refuses a self-approval.
//
// The affordance is CONDITIONALLY RENDERED rather than `disabled` (the epic's FE constraint), so a
// caller who cannot approve is never shown a button whose every click is refused. `disabled` is
// reserved for an in-flight request.
//
// ---------------------------------------------------------------------------
// TWO CURRENCIES, NEVER MERGED
//
// A PR author is a GITHUB LOGIN, proven by GitHub. An Entra oid is proven by Entra. AGP holds no
// mapping between them (E27A §6), so `authorDisplay` prefixes `@` — the marker for the provider's
// currency — and nothing here joins an author to an AGP principal or guesses one.
//
// NO STAGE LITERAL APPEARS IN THIS FILE, and none may (C5). A pull request's base branch is
// free-form; it is echoed, never compared to a name.

import { mayMaintainProject, type ProjectRoleName } from '../projectRoles';

/** The em-dash the Ops surface uses for "no value" — re-derived nowhere. */
export { NO_VALUE } from '../opsLabels';

/**
 * The wire shape of one pull request — `client.ts`'s `PullRequestView` (C2), structurally.
 *
 * Declared here rather than imported so this module stays pure (the client pulls in axios, and
 * vitest only collects `.ts`). Structural typing means the client's own declaration assigns to
 * this one without either side importing the other — the same reason `opsStatus.ts` declares
 * `RuntimeStatus` locally. The BACKEND counterpart is `PullRequestView` in
 * `backend/src/models/repository.py`; whoever changes one changes all three.
 */
export interface PullRequestRow {
  number: number;
  title: string;
  /** "open" | "closed" | "merged" — derived server-side, because GitHub's own state has no
   *  `merged` member and a shipped PR is the interesting row on this surface. */
  state: string;
  /** A GITHUB login. Not an AGP principal, and never joined to an Entra oid. */
  author: string;
  head_sha: string;
  url: string;
  can_approve: boolean;
  approve_blocked_reason?: string | null;
  /**
   * `false` ⇒ the provider says this cannot be merged. Anything else means NOTHING WAS
   * ESTABLISHED, and this surface does not distinguish the reasons: the LIST endpoint
   * (`GET /repos/{o}/{r}/pulls`, which is where these rows come from) omits the key entirely,
   * while the single-PR reads behind create/approve/merge do return it. The backend folds an
   * omitted key and GitHub's own `null` into one value, so "not asked" and "still computing" are
   * not tellable apart here — which is why nothing renders a hint off this. Only `false` is
   * acted on.
   */
  mergeable?: boolean | null;
}

// ---------------------------------------------------------------------------
// prTabVisibility (A3) — may this tab RENDER AT ALL?
// ---------------------------------------------------------------------------

/**
 * The outcome of the capability probe — which is the list read itself, not a second endpoint.
 *
 * Reusing the read is deliberate: a dedicated capability endpoint would be a second thing that
 * can disagree with the read the tab actually depends on, and the read already answers the
 * question definitively (it is the call the missing grant refuses).
 */
export type PrProbe =
  | { status: 'loading' }
  | { status: 'ok' }
  | { status: 'error'; message?: string | null };

/**
 * `pending` while the probe is in flight, `visible` only on a definite success.
 *
 * `pending` is distinct from `hidden` so the strip does not FLICKER the tab in and out on every
 * load: a tab that appears, vanishes and reappears is a worse artefact than a tab that appears
 * once the answer is known. The caller renders neither the tab nor an error while pending.
 */
export type PrTabVisibility = 'pending' | 'visible' | 'hidden';

/**
 * The backend's FIXED capability literal (`projects.py`'s `_PR_ERROR['capability_missing']`).
 *
 * Matched here so the reason can be LOGGED and explained rather than guessed at — but note the
 * visibility decision below does NOT depend on recognizing it. That is the point of A3's "the
 * capability check must degrade to hidden, never to a crash": if this literal is ever reworded,
 * the tab still hides, because the fallback for an UNRECOGNIZED error is also hidden. A guard that
 * only works when it recognizes the message is a guard a rewording defeats.
 */
export const PR_CAPABILITY_LITERAL = 'pull requests are not enabled for this organization';

/**
 * Is this specific failure the missing-grant one? For the EXPLANATION only, never for the
 * visibility decision (see above).
 */
export function isCapabilityRefusal(message: string | null | undefined): boolean {
  return /not enabled for this organization/i.test(message ?? '');
}

export function prTabVisibility(probe: PrProbe): PrTabVisibility {
  if (probe.status === 'loading') return 'pending';
  if (probe.status === 'ok') return 'visible';
  // EVERY error — the missing grant, an outage, a 502, an unrecognized message, an absent one.
  // Hidden is the honest rendering of "this surface cannot serve pull requests right now", and it
  // is the only outcome that cannot present a broken tab.
  return 'hidden';
}

// ---------------------------------------------------------------------------
// Ordering — outstanding work first
// ---------------------------------------------------------------------------

/**
 * OPEN first, then everything else; newest number first within each group.
 *
 * An operator opens this tab to see what is OUTSTANDING, so a merged PR from last month must not
 * sit above an open one waiting on review. Number descending is a proxy for recency that needs no
 * timestamp — GitHub's numbers are monotonic per repository — which matters because the
 * projection deliberately does not carry a created/updated date it would then have to keep in
 * step with the provider's.
 *
 * Pure and non-mutating: the caller's array is React state.
 */
export function sortPullRequests(rows: readonly PullRequestRow[]): PullRequestRow[] {
  return [...rows].sort((a, b) => {
    const aOpen = a.state === 'open' ? 0 : 1;
    const bOpen = b.state === 'open' ? 0 : 1;
    return aOpen === bOpen ? b.number - a.number : aOpen - bOpen;
  });
}

// ---------------------------------------------------------------------------
// Per-row affordances — WHICH verbs may this caller be OFFERED on this row?
// ---------------------------------------------------------------------------

/**
 * The two write verbs and the reason each is unavailable.
 *
 * `blockedReason` is set ONLY when the caller has the standing to act but the pull request itself
 * refuses — i.e. it explains a SUPPRESSED button to someone who would otherwise see one. A caller
 * without the role is told nothing per-row, because listing capabilities they do not hold on every
 * row is noise, and the tab's own header already says the read is view-only for them.
 */
export interface PrRowActions {
  approve: boolean;
  merge: boolean;
  /** Why approve is suppressed, from the SERVER's reason — never re-derived here. */
  blockedReason: string | null;
}

/**
 * Gated on `mayMaintainProject` — the SAME predicate `retry` uses, because the PR routes carry the
 * same MAINTAINER threshold and the same design-§3 ungoverned fallback.
 *
 * Deliberately NOT `canPromote`'s strict owner gate: a merge deploys nothing on its own (it
 * registers a prod CANDIDATE that still needs an OWNER's promote), so gating a pull request behind
 * the production gate would block ordinary development without protecting production. And
 * deliberately not the plain role either — gating on that alone would hide these verbs on every
 * ungoverned project, exactly the way it would have hidden retry.
 *
 * APPROVE ALSO REQUIRES `can_approve`, which is the SERVER's answer (D15). This function never
 * re-derives it from the author: the frontend does not know which GitHub account the caller's link
 * names — that is a provider currency resolved backend-side — so guessing here would be the
 * two-currency merge this design forbids, and would offer the self-approval the backend then
 * refuses.
 *
 * MERGE requires the PR to be OPEN and not KNOWN-unmergeable — only an explicit `false` suppresses
 * it. That asymmetry is load-bearing rather than lenient: the rows on this tab come from GitHub's
 * LIST endpoint, which omits `mergeable` altogether, so absent is the NORMAL case here and gating
 * on it would hide the button on every row of every repository. Nothing is reported per-row about
 * the absence either (T7): the surface cannot tell "never computed" from "still computing", and a
 * hint that cannot be true or false is a hint that only ever misleads. The real gate is the
 * provider's own refusal at merge time, which arrives as the backend's `not_mergeable` copy.
 */
export function prRowActions(
  row: PullRequestRow,
  held: ProjectRoleName | null,
  roleLevel: number,
  ungoverned: boolean | null | undefined,
): PrRowActions {
  const mayWrite = mayMaintainProject(held, roleLevel, ungoverned);
  const open = row.state === 'open';
  return {
    approve: mayWrite && open && row.can_approve,
    merge: mayWrite && open && row.mergeable !== false,
    // Stated only to a caller who could otherwise act, and only when the PR is open — "you
    // cannot approve a merged pull request" is noise beside a row that says `Merged`.
    blockedReason:
      mayWrite && open && !row.can_approve ? (row.approve_blocked_reason ?? null) : null,
  };
}

// ---------------------------------------------------------------------------
// Display
// ---------------------------------------------------------------------------

/**
 * `@login`, or the em-dash when the PR has no author.
 *
 * The `@` marks the PROVIDER's currency specifically — the same convention
 * `repositoryDetailTabs.actorOf` uses for a `github` actor, and the reason an Entra oid never
 * gets one. A PR whose GitHub account was deleted has no author at all, and absent is the honest
 * rendering: "unknown user" would assert that a human is recorded and we lost their name.
 *
 * An already-prefixed login is not double-prefixed.
 */
export function authorDisplay(author: string | null | undefined): string {
  const trimmed = (author ?? '').trim();
  if (!trimmed) return '—';
  return `@${trimmed.replace(/^@/, '')}`;
}

/** The head commit, git-short (7). Empty when the row carries none — never a fabricated sha. */
export function shortHeadSha(sha: string | null | undefined): string {
  return (sha ?? '').trim().slice(0, 7);
}

/**
 * The state → `OPS_BADGE` key. A SEMANTIC key, never a Tailwind class string (C3's rule), so this
 * surface keeps owning its own styling and a different visual treatment elsewhere is a styling
 * change rather than a reason to fork the table.
 *
 * `open` is amber (attention: it is waiting on somebody), `merged` emerald (it landed), and
 * anything else — including a closed-unmerged PR and a state no writer produces — is the NEUTRAL
 * slate. Closed-unmerged is deliberately not rose: abandoning a pull request is an ordinary
 * decision, not a failure, and rose is reserved for things that broke.
 */
export function prStateBadgeKey(state: string | null | undefined): 'pending' | 'ok' | 'unknown' {
  const value = (state ?? '').trim().toLowerCase();
  if (value === 'open') return 'pending';
  if (value === 'merged') return 'ok';
  return 'unknown';
}

/** The state's own label — sentence case, so the caller applies no `capitalize`. */
export function prStateLabel(state: string | null | undefined): string {
  const value = (state ?? '').trim().toLowerCase();
  if (value === 'open') return 'Open';
  if (value === 'merged') return 'Merged';
  if (value === 'closed') return 'Closed';
  // An unrecognized state is echoed rather than mapped or blanked — the raw value is the
  // evidence, and defaulting it to a known state would claim something the wire never said.
  return (state ?? '').trim() || 'Unknown';
}

// ---------------------------------------------------------------------------
// pullRequestActionMessage — the backend's FIXED literals → a sentence with a remedy
// ---------------------------------------------------------------------------

/**
 * The route pins every one of these (`projects.py`'s `_PR_ERROR`), and each maps to a sentence
 * that says what happened AND what to do — the `promotionActionMessage` idiom.
 *
 * Matched on the RAW message with `test()`, so a longer wrapped message still resolves. The
 * self-approval case is first because it is the one an operator is most likely to meet and the
 * one whose default wording ("failed") would be most misleading: nothing failed.
 *
 * An unrecognized message falls back to the caller's own sentence rather than being echoed. The
 * literals are safe by construction, but echoing an unrecognized upstream string is how provider
 * text starts reaching the UI.
 */
export function pullRequestActionMessage(raw: string, fallback: string): string {
  const message = (raw ?? '').trim();
  if (/cannot approve your own pull request/i.test(message)) {
    return 'You opened this pull request, so GitHub will not accept your own review. Someone else has to approve it.';
  }
  if (/cannot be approved/i.test(message)) {
    return 'This pull request cannot be approved — it may have been closed or merged since this page loaded.';
  }
  if (/cannot be merged yet/i.test(message)) {
    // ASSERTS NOTHING ABOUT THE CAUSE, for the same reason as the `declined the request` branch
    // below. This literal is GitHub's 405 on the merge, which covers a merge CONFLICT as much as
    // an unsatisfied check or a protected branch — so the previous copy, which named checks and
    // branch protection, sent an operator to look at checks that were green on a conflicted pull
    // request. And every open pull request now offers merge, so this refusal is the ordinary
    // outcome rather than a rare one.
    return 'GitHub refused to merge this pull request — open it on GitHub to see what is blocking the merge.';
  }
  if (/connect your GitHub account/i.test(message)) {
    return 'Connect your GitHub account in Settings to act on pull requests as yourself.';
  }
  if (isCapabilityRefusal(message)) {
    return 'Pull requests are not enabled for this organization. An org admin has to grant the AGP app pull-request access.';
  }
  // BEFORE the generic `declined the request` branch below, which would otherwise swallow this
  // and restate it as two causes it is not.
  if (/no commits between/i.test(message)) {
    return 'There is nothing to open a pull request for — this branch has no commits the target branch does not already have. Push a commit first.';
  }
  if (/declined the request/i.test(message)) {
    // ASSERTS NOTHING ABOUT THE CAUSE. This literal covers a 409 (the branch moved, a
    // conflicting state) AND a 422 AGP could not classify further, and the provider body is
    // deliberately discarded before it reaches here — so a sentence naming a cause would be
    // naming one of several, which is what the previous copy did: it told an operator a PR may
    // already exist and the branch may have moved, on a repository where neither was true.
    return 'GitHub declined the request and nothing was changed. Check the pull request on GitHub for the reason.';
  }
  if (/pull request not found/i.test(message)) {
    return 'That pull request no longer exists.';
  }
  if (/insufficient project role/i.test(message)) {
    return 'You need the Maintainer role on this project to act on its pull requests.';
  }
  if (/GitHub request failed/i.test(message)) {
    return 'GitHub could not be reached. Nothing was changed — try again in a moment.';
  }
  return fallback;
}

/**
 * The header note explaining a view-only tab, or `null` when the caller may act.
 *
 * Stated once at the top rather than per row: a caller without the role sees no buttons anywhere,
 * and repeating the reason on every row would bury the pull requests themselves.
 */
export function prReadOnlyNote(
  held: ProjectRoleName | null,
  roleLevel: number,
  ungoverned: boolean | null | undefined,
): string | null {
  return mayMaintainProject(held, roleLevel, ungoverned)
    ? null
    : 'You can read this repository’s pull requests. Opening, approving and merging need the Maintainer role on this project.';
}
