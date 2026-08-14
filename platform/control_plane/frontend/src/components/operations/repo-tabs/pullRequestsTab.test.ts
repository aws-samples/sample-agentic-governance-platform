// pullRequestsTab.test.ts — the pure companion behind the repository's Pull requests tab
// (E28/T14, contract C2 — D14+D15).
//
// Everything this tab DECIDES lives in `pullRequestsTab.ts` because vitest collects only
// `src/**/*.test.ts`: a judgement made inside a `.tsx` is a judgement no test can reach. Same
// split `repositoryDetailTabs.ts` and `deploymentsTab.ts` use.
//
// The four judgements pinned here, and why each is load-bearing:
//
//   • TAB VISIBILITY (A3) — the tab must be HIDDEN, never broken, when the org lacks the
//     `pull_requests` grant. `ready` is a BUILD-TIME flag and cannot express a per-org runtime
//     fact, so this is a separate mechanism — and the probe's OWN failure must resolve to hidden
//     too, including for a message nobody recognizes.
//   • THE APPROVE REFUSAL (D15) — `can_approve === false` is the SERVER's answer and is rendered
//     calmly with its reason. The frontend never re-derives it, because it does not know which
//     GitHub account the caller's link names.
//   • THE ROLE GATE — the same MAINTAINER predicate `retry` uses, with the design-§3 ungoverned
//     fallback, and deliberately not promote's strict owner gate.
//   • THE TWO CURRENCIES — a PR author is a GitHub login and wears `@`; nothing joins it to an
//     Entra identity.
//
// Plus source-as-text guards over `PullRequestsTab.tsx`, for the rules a `.ts` cannot enforce for
// it (the `repositoryDetailTabs.test.ts:825` idiom): no stage literal, no governance verb, no
// re-derived refusal, and the explicit-extension import convention.

import { describe, expect, it } from 'vitest';

import {
  PR_CAPABILITY_LITERAL,
  authorDisplay,
  isCapabilityRefusal,
  prReadOnlyNote,
  prRowActions,
  prStateBadgeKey,
  prStateLabel,
  prTabVisibility,
  pullRequestActionMessage,
  shortHeadSha,
  sortPullRequests,
  type PullRequestRow,
} from './pullRequestsTab';
import pureSrc from './pullRequestsTab.ts?raw';
// This file's own source. The stage-literal guard is applied to it too — a rule that exempts the
// file enforcing it is a rule with a hole in it.
import ownSrc from './pullRequestsTab.test.ts?raw';
import tabSrc from './PullRequestsTab.tsx?raw';
// Vite's `?raw` rather than `node:fs`: `tsconfig.app.json` sets `types: ["vite/client"]` (no
// `node`), so `node:fs` has no declarations here. The explicit `.tsx` is load-bearing on a
// case-insensitive filesystem, where a name differing from a sibling only in casing resolves to
// the sibling — which is exactly the `.ts` companion this file also imports.

function row(over: Partial<PullRequestRow> = {}): PullRequestRow {
  return {
    number: 7,
    title: 'Raise the claim-triage threshold',
    state: 'open',
    author: 'jorge',
    head_sha: '3f9a1c2b4d5e6f',
    url: 'https://github.com/acme/claims-triage/pull/7',
    can_approve: true,
    ...over,
  };
}

// ---------------------------------------------------------------------------
// Tab visibility (A3) — the requirement this task is shaped around
// ---------------------------------------------------------------------------

describe('prTabVisibility — hidden, never broken, when the org lacks the grant', () => {
  it('is VISIBLE only on a definite success', () => {
    expect(prTabVisibility({ status: 'ok' })).toBe('visible');
  });

  it('HIDES the tab when the org lacks `pull_requests: write`', () => {
    // THE requirement. The grant is manual per org and GitHub does not retro-apply a manifest
    // permission change, so an org onboarded before this feature answers 403 forever until an
    // admin grants it. The tab must be absent, not present-and-failing.
    expect(prTabVisibility({ status: 'error', message: PR_CAPABILITY_LITERAL })).toBe('hidden');
  });

  it('hides on an UNRECOGNIZED failure too — the probe failing is itself a reason to hide', () => {
    // "The capability check must degrade to hidden, never to a crash." A guard that hides only
    // when it RECOGNIZES the message is a guard a rewording defeats — and the rewording would
    // produce a visible, broken tab, which is the outcome the requirement forbids.
    for (const message of [
      'GitHub request failed',
      'Repository not found',
      'Session expired. Please log in again.',
      'Network Error',
      'something nobody has ever seen',
      '',
      null,
      undefined,
    ]) {
      expect(prTabVisibility({ status: 'error', message }), String(message)).toBe('hidden');
    }
  });

  it('is PENDING while the probe is in flight — never a flicker', () => {
    // Distinct from hidden so the strip does not show the tab, remove it and show it again on
    // every load. A tab that appears and vanishes is a worse artefact than one that appears once.
    expect(prTabVisibility({ status: 'loading' })).toBe('pending');
  });

  it('never answers `visible` for anything but a success', () => {
    // The property that actually matters, stated once so no future branch can widen it: only
    // `ok` produces a rendered tab.
    const probes = [
      { status: 'loading' } as const,
      { status: 'error', message: PR_CAPABILITY_LITERAL } as const,
      { status: 'error', message: null } as const,
    ];
    for (const p of probes) expect(prTabVisibility(p), p.status).not.toBe('visible');
  });
});

describe('isCapabilityRefusal — for the EXPLANATION, not for the decision', () => {
  it('recognizes the backend’s fixed literal', () => {
    expect(isCapabilityRefusal(PR_CAPABILITY_LITERAL)).toBe(true);
  });

  it('does not claim an unrelated failure is a missing grant', () => {
    for (const message of ['GitHub request failed', 'pull request not found', '', null]) {
      expect(isCapabilityRefusal(message), String(message)).toBe(false);
    }
  });

  it('the literal it matches is the one the backend actually pins', () => {
    // Guards against this pair drifting apart: the sentence here must be the route's
    // `_PR_ERROR['capability_missing']` detail, and the matcher must accept it.
    expect(PR_CAPABILITY_LITERAL).toBe('pull requests are not enabled for this organization');
    expect(isCapabilityRefusal(`Request failed: ${PR_CAPABILITY_LITERAL}`)).toBe(true);
  });
});

// ---------------------------------------------------------------------------
// The approve refusal (D15)
// ---------------------------------------------------------------------------

describe('prRowActions — approve is the SERVER’s answer, never re-derived', () => {
  it('offers approve to a maintainer when the server says it is available', () => {
    const may = prRowActions(row(), 'maintainer', 0, false);
    expect(may.approve).toBe(true);
    expect(may.blockedReason).toBeNull();
  });

  it('SUPPRESSES approve and states the server’s reason when `can_approve` is false', () => {
    // D15's refusal, rendered calmly: the button is absent (conditionally rendered, not
    // `disabled` — the epic's FE constraint) and the reason is stated beside it.
    const may = prRowActions(
      row({ can_approve: false, approve_blocked_reason: 'You opened this pull request.' }),
      'maintainer',
      0,
      false,
    );
    expect(may.approve).toBe(false);
    expect(may.blockedReason).toBe('You opened this pull request.');
  });

  it('does NOT re-derive the refusal from the author', () => {
    // The frontend cannot know which GitHub account the caller's E27B link names — that is a
    // provider currency resolved backend-side. Guessing here would merge two currencies and would
    // offer the self-approval the backend then refuses. So a row the server marked approvable
    // stays approvable whatever its author says.
    const may = prRowActions(row({ author: 'anyone-at-all' }), 'maintainer', 0, false);
    expect(may.approve).toBe(true);
  });

  it('a refusal with no reason still suppresses the button', () => {
    // Missing copy must never fail OPEN into an offered button whose click is refused.
    const may = prRowActions(row({ can_approve: false }), 'maintainer', 0, false);
    expect(may.approve).toBe(false);
    expect(may.blockedReason).toBeNull();
  });

  it('offers neither verb on a closed or merged pull request', () => {
    for (const state of ['closed', 'merged']) {
      const may = prRowActions(row({ state }), 'maintainer', 0, false);
      expect(may.approve, state).toBe(false);
      expect(may.merge, state).toBe(false);
      // …and says nothing about it: "you cannot approve a merged PR" is noise beside a row
      // already labelled Merged.
      expect(may.blockedReason, state).toBeNull();
    }
  });
});

describe('prRowActions — the merge verb and three-valued mergeability', () => {
  it('offers merge when the provider says the PR is mergeable', () => {
    expect(prRowActions(row({ mergeable: true }), 'maintainer', 0, false).merge).toBe(true);
  });

  it('suppresses merge only when the provider says FALSE', () => {
    expect(prRowActions(row({ mergeable: false }), 'maintainer', 0, false).merge).toBe(false);
  });

  it('STILL offers merge when the provider said nothing — only FALSE suppresses', () => {
    // And absent is the NORMAL case on this tab, not an edge one: these rows come from GitHub's
    // LIST endpoint, which omits `mergeable` entirely (only the single-PR read computes it). So
    // gating on absence would hide the merge button on every row of every repository, forever.
    for (const mergeable of [null, undefined]) {
      expect(prRowActions(row({ mergeable }), 'maintainer', 0, false).merge, String(mergeable)).toBe(
        true,
      );
    }
  });

  it('reports NOTHING per-row about an absent mergeability (T7)', () => {
    // The removed `mergeabilityUnknown` rendered "GitHub has not finished checking whether this
    // can merge" on every row, forever — because the list endpoint never computes the field, so
    // the "not finished yet" it described was never started. The surface cannot tell "never
    // asked" from "still computing" (the backend folds an omitted key and GitHub's `null` into
    // one value), so it states neither. Pinned as an exact shape: re-adding a per-row hint here
    // fails this, which is the point.
    for (const mergeable of [null, undefined, true, false]) {
      expect(Object.keys(prRowActions(row({ mergeable }), 'maintainer', 0, false)).sort()).toEqual([
        'approve',
        'blockedReason',
        'merge',
      ]);
    }
  });
});

// ---------------------------------------------------------------------------
// The role gate
// ---------------------------------------------------------------------------

describe('prRowActions — gated on the SAME predicate the routes carry', () => {
  it('offers a viewer neither verb', () => {
    const may = prRowActions(row({ mergeable: true }), 'viewer', 0, false);
    expect(may).toEqual({
      approve: false,
      merge: false,
      blockedReason: null,
    });
  });

  it('gets the design-§3 ungoverned fallback, exactly as `retry` does', () => {
    // The PR routes gate through `_require_project_role_or_ungoverned`, so a role-less caller on
    // an ungoverned project must still be able to act. Gating on the plain role would hide these
    // verbs on every pre-migration project — the same defect that would have hidden retry.
    const may = prRowActions(row({ mergeable: true }), null, 0, true);
    expect(may.approve).toBe(true);
    expect(may.merge).toBe(true);
  });

  it('does NOT extend the fallback to a governed project', () => {
    const may = prRowActions(row({ mergeable: true }), null, 0, false);
    expect(may.approve).toBe(false);
    expect(may.merge).toBe(false);
  });

  it('a platform admin holds both verbs without a project role row', () => {
    const may = prRowActions(row({ mergeable: true }), null, 2, false);
    expect(may.approve).toBe(true);
    expect(may.merge).toBe(true);
  });

  it('an owner holds them too — the threshold is MAINTAINER, not exactly-maintainer', () => {
    expect(prRowActions(row({ mergeable: true }), 'owner', 0, false).merge).toBe(true);
  });
});

describe('prReadOnlyNote — stated once, not per row', () => {
  it('explains a view-only tab to a caller who cannot act', () => {
    const note = prReadOnlyNote('viewer', 0, false);
    expect(note).toBeTruthy();
    expect(note).toMatch(/maintainer/i);
  });

  it('says nothing to a caller who can act', () => {
    expect(prReadOnlyNote('maintainer', 0, false)).toBeNull();
    expect(prReadOnlyNote(null, 2, false)).toBeNull();
    expect(prReadOnlyNote(null, 0, true)).toBeNull();
  });
});

// ---------------------------------------------------------------------------
// Ordering
// ---------------------------------------------------------------------------

describe('sortPullRequests — outstanding work first', () => {
  it('puts OPEN pull requests above everything else', () => {
    const sorted = sortPullRequests([
      row({ number: 9, state: 'merged' }),
      row({ number: 3, state: 'open' }),
      row({ number: 8, state: 'closed' }),
    ]);
    expect(sorted.map((r) => r.number)).toEqual([3, 9, 8]);
  });

  it('orders newest-first within each group', () => {
    const sorted = sortPullRequests([
      row({ number: 1, state: 'open' }),
      row({ number: 5, state: 'open' }),
      row({ number: 3, state: 'open' }),
    ]);
    expect(sorted.map((r) => r.number)).toEqual([5, 3, 1]);
  });

  it('does not mutate the caller’s array (it is React state)', () => {
    const rows = [row({ number: 1 }), row({ number: 2 })];
    sortPullRequests(rows);
    expect(rows.map((r) => r.number)).toEqual([1, 2]);
  });

  it('handles an empty list', () => {
    expect(sortPullRequests([])).toEqual([]);
  });
});

// ---------------------------------------------------------------------------
// Display — and the two currencies
// ---------------------------------------------------------------------------

describe('authorDisplay — a GitHub login, marked as one', () => {
  it('prefixes `@`, the marker for the PROVIDER’s currency', () => {
    // The same convention `repositoryDetailTabs.actorOf` uses for a `github` actor — and the
    // reason an Entra oid never gets one. AGP holds no mapping between the two (E27A §6).
    expect(authorDisplay('jorge')).toBe('@jorge');
  });

  it('does not double-prefix an already-prefixed login', () => {
    expect(authorDisplay('@jorge')).toBe('@jorge');
  });

  it('renders an ABSENT author as absent, never as "unknown user"', () => {
    // A deleted GitHub account leaves no author. "Unknown user" would assert that a human is
    // recorded and their name was lost.
    for (const value of ['', '   ', null, undefined]) {
      expect(authorDisplay(value), String(value)).toBe('—');
    }
  });
});

describe('shortHeadSha', () => {
  it('git-shortens to 7', () => {
    expect(shortHeadSha('3f9a1c2b4d5e6f')).toBe('3f9a1c2');
  });

  it('is empty when the row carries no sha — never a fabricated one', () => {
    expect(shortHeadSha('')).toBe('');
    expect(shortHeadSha(null)).toBe('');
  });
});

describe('prStateBadgeKey — a SEMANTIC key, never a class string (C3)', () => {
  it('maps the three real states', () => {
    expect(prStateBadgeKey('open')).toBe('pending');
    expect(prStateBadgeKey('merged')).toBe('ok');
    expect(prStateBadgeKey('closed')).toBe('unknown');
  });

  it('does not render a closed pull request as a FAILURE', () => {
    // Abandoning a pull request is an ordinary decision. Rose is reserved for things that broke,
    // and spending it here would teach an operator to ignore it where it matters.
    expect(prStateBadgeKey('closed')).not.toBe('failed');
  });

  it('is total and case/whitespace tolerant', () => {
    expect(prStateBadgeKey(' OPEN ')).toBe('pending');
    expect(prStateBadgeKey('something-new')).toBe('unknown');
    expect(prStateBadgeKey(null)).toBe('unknown');
  });

  it('returns only OPS_BADGE keys — no Tailwind class ever crosses this boundary', () => {
    for (const state of ['open', 'merged', 'closed', 'nonsense', null]) {
      expect(['pending', 'ok', 'unknown']).toContain(prStateBadgeKey(state));
    }
  });
});

describe('prStateLabel', () => {
  it('is sentence case, so no caller applies `capitalize`', () => {
    expect(prStateLabel('open')).toBe('Open');
    expect(prStateLabel('merged')).toBe('Merged');
    expect(prStateLabel('closed')).toBe('Closed');
  });

  it('ECHOES an unrecognized state rather than mapping it to a known one', () => {
    // The raw value is the evidence. Defaulting it to a known state would claim something the
    // wire never said — the failure mode `opsStatus` exists to prevent.
    expect(prStateLabel('draft')).toBe('draft');
    expect(prStateLabel(null)).toBe('Unknown');
  });
});

// ---------------------------------------------------------------------------
// Error copy — the backend's FIXED literals
// ---------------------------------------------------------------------------

describe('pullRequestActionMessage — every pinned literal gets a sentence with a remedy', () => {
  it('states the SELF-APPROVAL refusal as a state, not a failure', () => {
    const message = pullRequestActionMessage(
      'you cannot approve your own pull request',
      'fallback',
    );
    expect(message).toMatch(/you opened this pull request/i);
    expect(message).toMatch(/someone else/i);
    // Not the word "failed": nothing failed. The platform worked exactly as designed.
    expect(message).not.toMatch(/failed/i);
  });

  it('covers each of the route’s other fixed literals', () => {
    const cases: [string, RegExp][] = [
      ['this pull request cannot be merged yet', /what is blocking the merge/i],
      ['this pull request cannot be approved', /closed or merged/i],
      ['connect your GitHub account to act on pull requests', /settings/i],
      ['pull requests are not enabled for this organization', /org admin/i],
      ['there are no commits between these branches', /push a commit/i],
      ['GitHub declined the request for this pull request', /check the pull request on github/i],
      ['pull request not found', /no longer exists/i],
      ['insufficient project role', /maintainer role/i],
      ['GitHub request failed', /nothing was changed/i],
    ];
    for (const [literal, expected] of cases) {
      expect(pullRequestActionMessage(literal, 'fallback'), literal).toMatch(expected);
    }
  });

  it('every literal resolves to something OTHER than the fallback', () => {
    // Guards the table against a vacuous pass: a regex that stopped matching would silently
    // return the generic sentence, and the assertions above would still read plausibly.
    for (const literal of [
      'you cannot approve your own pull request',
      'this pull request cannot be merged yet',
      'this pull request cannot be approved',
      'connect your GitHub account to act on pull requests',
      'pull requests are not enabled for this organization',
      'there are no commits between these branches',
      'GitHub declined the request for this pull request',
      'pull request not found',
      'insufficient project role',
      'GitHub request failed',
    ]) {
      expect(pullRequestActionMessage(literal, 'SENTINEL'), literal).not.toBe('SENTINEL');
    }
  });

  it('names the no-commits cause instead of inventing two others', () => {
    // FOUND LIVE. Opening a PR from a branch with nothing ahead answered the generic `declined
    // the request` copy, which told the operator a pull request may already exist and the branch
    // may have moved. The repo had ZERO pull requests and the branch had not moved — the surface
    // asserted two causes it had never established, over GitHub's actual answer ("No commits
    // between main and dev").
    const message = pullRequestActionMessage(
      'there are no commits between these branches',
      'fallback',
    );
    expect(message).toMatch(/nothing to open a pull request for/i);
    expect(message).toMatch(/push a commit/i);
    // It must NOT reach the generic branch below it — that ordering is the fix.
    expect(message).not.toMatch(/declined/i);
  });

  it('the generic decline ASSERTS NO CAUSE, because it cannot know one', () => {
    // `GitHub declined the request for this pull request` covers a 409 AND a 422 the backend
    // could not classify further, and the provider body is discarded before it gets here. So any
    // named cause is one of several guesses. This is the assertion the live test's finding #4 is
    // about: the copy may say what happened and where to look, never why.
    const message = pullRequestActionMessage(
      'GitHub declined the request for this pull request',
      'fallback',
    );
    for (const invented of [/already exist/i, /may have moved/i, /no commits/i, /conflict/i]) {
      expect(message, String(invented)).not.toMatch(invented);
    }
    // …and it is still a usable sentence rather than a shrug.
    expect(message).toMatch(/nothing was changed/i);
  });

  it('the merge refusal ASSERTS NO CAUSE either, because a 405 does not establish one', () => {
    // `this pull request cannot be merged yet` is GitHub's 405 on `PUT /pulls/{n}/merge`, which
    // covers a merge CONFLICT (`dirty`, `behind`) as much as it covers `blocked`. Naming checks
    // or branch protection is therefore a guess, and on a conflicted pull request with green
    // checks it is a wrong one that sends the operator to the wrong place. Same defect class as
    // the generic decline above, and list rows now offer merge on every open PR, so this is the
    // ordinary refusal rather than a rare one.
    const message = pullRequestActionMessage('this pull request cannot be merged yet', 'fallback');
    for (const invented of [/check/i, /branch protection/i, /conflict/i, /review/i]) {
      expect(message, String(invented)).not.toMatch(invented);
    }
    // …and it still says what happened and where the reason actually lives.
    expect(message).toMatch(/refused to merge/i);
    expect(message).toMatch(/on github/i);
  });

  it('falls back rather than ECHOING an unrecognized upstream string', () => {
    // The literals are safe by construction, but echoing an unrecognized one is how provider
    // text starts reaching the UI.
    const raw = 'Head branch was modified by 111122223333';
    const message = pullRequestActionMessage(raw, 'The action could not be completed.');
    expect(message).toBe('The action could not be completed.');
    expect(message).not.toContain('111122223333');
  });

  it('handles an empty message', () => {
    expect(pullRequestActionMessage('', 'fallback')).toBe('fallback');
  });
});

// ---------------------------------------------------------------------------
// Source-as-text guards over the `.tsx` wiring
// ---------------------------------------------------------------------------

describe('PullRequestsTab.tsx obeys the rules a .ts cannot enforce for it', () => {
  it('parses both sources (guards every assertion below against an empty read)', () => {
    expect(tabSrc.length).toBeGreaterThan(500);
    expect(pureSrc.length).toBeGreaterThan(500);
    expect(tabSrc).toContain('pullRequestsApi');
  });

  it('owns the three WRITE verbs — and deliberately NOT the list read', () => {
    // The four calls live in `client.ts` (T14 may not edit that file), so the tab consumes them
    // rather than reaching for axios. A hand-rolled fetch here would bypass the auth interceptor
    // AND the 401 handling.
    //
    // THE LIST READ IS THE PAGE'S, and that split is a real ordering constraint rather than a
    // preference: the same read is the capability probe (A3) whose answer decides whether this tab
    // appears in the strip at all — and a tab body only mounts once its tab is selectable, so a tab
    // that fetched its own visibility could never become visible. `RepositoryDetail` therefore reads
    // the list and passes the rows down; a second read HERE would also be a second copy that could
    // disagree with the one the tab's own visibility was decided from.
    for (const verb of ['create', 'approve', 'merge']) {
      expect(new RegExp(`pullRequestsApi\\s*\\.\\s*${verb}\\s*\\(`).test(tabSrc), verb).toBe(true);
    }
    expect(/pullRequestsApi\s*\.\s*list\s*\(/.test(tabSrc)).toBe(false);
    expect(tabSrc).not.toContain('axios');
    // A WORD-BOUNDARY match on a bare `fetch` call. The naive `'fetch('` substring hits the
    // house `refetch()` helper — a false positive that would have to be silenced, and silencing
    // it is how the real guard gets deleted.
    expect(/(?<![\w$])fetch\s*\(/.test(tabSrc)).toBe(false);
  });

  it('that four-call guard can SEE a chained call (so it cannot regress to strict-substring)', () => {
    // A guard that cannot fail on the real formatting is worse than no guard. Asserts the
    // pattern's discriminating power directly, the `repositoryDetailTabs.test.ts:895` idiom.
    const pattern = (verb: string) => new RegExp(`pullRequestsApi\\s*\\.\\s*${verb}\\s*\\(`);
    expect(pattern('list').test('pullRequestsApi.list(repoId)')).toBe(true);
    expect(pattern('list').test('pullRequestsApi\n      .list(repoId)')).toBe(true);
    // …and it must not match a mere mention of the verb, or the import line alone.
    expect(pattern('list').test("import { pullRequestsApi } from '../../../api/client';")).toBe(
      false,
    );
    expect(pattern('merge').test('// merge is handled below')).toBe(false);
    // The bare-`fetch` guard likewise: it must see a real call and not the `refetch` helper.
    const bareFetch = /(?<![\w$])fetch\s*\(/;
    expect(bareFetch.test('await fetch(url)')).toBe(true);
    expect(bareFetch.test('refetch();')).toBe(false);
  });

  it('imports its companion and the sibling tab with EXPLICIT extensions', () => {
    // Not cosmetic. This module and its `.tsx` differ only in casing, and on a case-insensitive
    // filesystem an extensionless specifier resolves to the OTHER one — binding an import to a
    // module with no default export. `tsc` catches it (TS1149), and T8/T9/T11/T12 each hit it.
    expect(tabSrc).toContain("from './pullRequestsTab.ts'");
  });

  it('decides NOTHING itself — every judgement comes from the companion', () => {
    // The whole reason the companion exists. A predicate re-derived here is a predicate no test
    // can reach, which is how two forked status tables shipped.
    for (const decision of ['prRowActions', 'prStateBadgeKey', 'prStateLabel', 'authorDisplay']) {
      expect(tabSrc, decision).toContain(decision);
    }
    // …and specifically not a local re-derivation of the approve gate. `can_approve` is the
    // SERVER's answer; comparing an author to anything here would merge two currencies.
    expect(tabSrc).not.toMatch(/author\s*===/);
    expect(tabSrc).not.toMatch(/toLowerCase\(\)\s*===/);
  });

  it('renders the refusal REASON rather than implying it', () => {
    // A suppressed button with no explanation reads as a bug. The reason is the server's.
    expect(tabSrc).toContain('blockedReason');
    expect(tabSrc).toContain('pullRequestActionMessage');
  });

  it('speculates about mergeability NOWHERE (T7)', () => {
    // The row used to state "GitHub has not finished checking whether this can merge" on every
    // row of every repository, permanently — GitHub's LIST endpoint omits `mergeable`, so there
    // was no check in flight to finish. A `.tsx`-only render is a claim no unit test reaches,
    // which is why this is asserted over the source.
    expect(tabSrc).not.toContain('has not finished checking');
    expect(tabSrc).not.toMatch(/mergeab\w*Unknown/);
    // Not merely renamed into the companion either.
    expect(pureSrc).not.toMatch(/mergeab\w*Unknown/);
    // …while the merge affordance itself is still offered — the fix drops the hint, not the verb.
    expect(pureSrc).toContain('row.mergeable !== false');
  });

  it('conditionally RENDERS the write affordances rather than disabling them', () => {
    // The epic's FE constraint: `disabled` is reserved for an in-flight request, so a caller
    // without the standing is never shown a button whose every click is refused.
    expect(tabSrc).toMatch(/actions\.approve\s*&&/);
    expect(tabSrc).toMatch(/actions\.merge\s*&&/);
  });

  it('offers NO governance verb — the shadow-governance failure mode', () => {
    // Approving a PULL REQUEST is a repo-provider act and is in scope (D15). Approving an AGENT
    // is governance and is forbidden on an Operations surface. The two must not blur.
    for (const forbidden of [
      'agentsApi.transition',
      'agentsApi.submit',
      'grantsApi.add',
      'grantsApi.remove',
      'projectRolesApi.grant',
      'projectRolesApi.revoke',
      'projectsApi.promoteRepo',
      'lifecycle_state',
    ]) {
      expect(tabSrc, forbidden).not.toContain(forbidden);
    }
  });

  it('contains NO stage literal anywhere (C5) — quoted OR as a property access', () => {
    // The `repositoryDetailTabs.test.ts:873` pattern verbatim, applied to the raw source with no
    // lowercasing, trimming or comment stripping: a normalization step is how a guard silently
    // stops seeing the thing it guards. A pull request's base branch is free-form and is echoed,
    // never compared to a name.
    const stageLiteral =
      /(['"`])(dev|prod)\1|[.[]\s*['"`]?(dev|prod)['"`]?(?![\w$])|[{,;(]\s*(dev|prod)\s*[:,}]/;
    for (const [name, src] of [
      ['PullRequestsTab.tsx', tabSrc],
      ['pullRequestsTab.ts', pureSrc],
      // …and THIS file, so the guard covers the file that defines it.
      ['pullRequestsTab.test.ts', ownSrc],
    ] as const) {
      expect(stageLiteral.test(src), `${name} contains a stage literal`).toBe(false);
    }
  });

  it('the stage guard can SEE a property-access read (so it cannot regress to quoted-only)', () => {
    // A guard that cannot fail is worse than no guard. Asserts the pattern's discriminating
    // power directly, the way `repositoryDetailTabs.test.ts:895` does.
    const stageLiteral =
      /(['"`])(dev|prod)\1|[.[]\s*['"`]?(dev|prod)['"`]?(?![\w$])|[{,;(]\s*(dev|prod)\s*[:,}]/;
    const stage = 'd' + 'ev';
    expect(stageLiteral.test(`pr.base === '${stage}'`)).toBe(true);
    expect(stageLiteral.test(`t.stages.${stage}.region`)).toBe(true);
    expect(stageLiteral.test(`const { ${stage} } = t.stages;`)).toBe(true);
    // …and prose or a longer identifier must not match, or the guard becomes unusable.
    expect(stageLiteral.test(`the ${stage} branch is the base`)).toBe(false);
    expect(stageLiteral.test(`a.${stage}elopment.x`)).toBe(false);
  });

  it('every clickable element is keyboard-reachable (M-e, not inherited)', () => {
    // T10's row shipped a clickable `<tr>` with no role/tabIndex/key handler. Nothing here may
    // repeat it: real `<button>`/`<a>` elements only.
    const clickableNonInteractive = /<(tr|div|span|td|li)\b[^>]*\sonClick/;
    expect(clickableNonInteractive.test(tabSrc)).toBe(false);
  });

  it('opens external provider links safely', () => {
    // A PR link leaves the app, so it must not hand the opened page a `window.opener` handle.
    expect(tabSrc).toContain('rel="noreferrer"');
    expect(tabSrc).toContain('target="_blank"');
  });

  it('contains no AWS account id (a hard project rule)', () => {
    const twelveDigits = /(?<!\d)\d{12}(?!\d)/;
    for (const [name, src] of [
      ['PullRequestsTab.tsx', tabSrc],
      ['pullRequestsTab.ts', pureSrc],
    ] as const) {
      expect(twelveDigits.test(src), `${name} contains a 12-digit literal`).toBe(false);
    }
  });
});

// ---------------------------------------------------------------------------
// The manual refresh affordance (T9, finding #8)
//
// A pull request's life happens on github.com: someone merges it, someone closes it, and this tab
// keeps showing the row it read on mount until a full page navigation. Polling was rejected
// outright, so the operator gets an explicit control instead — and the control is honest about
// which of the page's two callbacks it is using.
//
// Source-as-text again, for the same reason as the block above: the button is `.tsx`-only markup,
// so a unit test cannot click it. Every pattern here is paired with a discriminating-power
// assertion, because E28+E28A had FIVE guards defeated by their own explanatory comment quoting
// the string they forbade.
// ---------------------------------------------------------------------------

describe('PullRequestsTab.tsx offers a MANUAL refresh, and calls the honest prop for it', () => {
  it('takes an `onRefresh` prop distinct from `onChanged`', () => {
    // Two props rather than one overloaded one. `onChanged` is documented as "a write LANDED" and
    // is what an approve/merge/create reports; a refresh mutated NOTHING. Collapsing them would
    // save a prop and cost a future reader the ability to tell a read from a write.
    expect(/onRefresh\s*:\s*\(\s*\)\s*=>\s*void/.test(tabSrc)).toBe(true);
    expect(/onChanged\s*:\s*\(\s*\)\s*=>\s*void/.test(tabSrc)).toBe(true);
    // …and both are actually DESTRUCTURED, not merely declared in the interface.
    expect(/^\s*onRefresh,\s*$/m.test(tabSrc)).toBe(true);
    expect(/^\s*onChanged,\s*$/m.test(tabSrc)).toBe(true);
  });

  it('wires the refresh BUTTON to `onRefresh` — not to `onChanged`, not to a local read', () => {
    // The whole point of the separate prop. A button bound to `onChanged` would work identically
    // at runtime and lie in the source, which is the regression this pins.
    expect(/onClick=\{onRefresh\}/.test(tabSrc)).toBe(true);
    // `onChanged` must remain bound to the three write paths ONLY — it is called, never handed to
    // a click handler directly.
    expect(/onClick=\{onChanged\}/.test(tabSrc)).toBe(false);
  });

  it('those two click-binding patterns can tell the props apart (they cannot match prose)', () => {
    // Discriminating power, asserted directly. Both patterns are built from JSX syntax that
    // cannot occur in a sentence, so a comment mentioning either prop name cannot satisfy or
    // defeat them.
    expect(/onClick=\{onRefresh\}/.test('onClick={onRefresh}')).toBe(true);
    expect(/onClick=\{onChanged\}/.test('onClick={onChanged}')).toBe(true);
    for (const prose of [
      '// the button calls onRefresh so the page re-reads',
      '// onClick onRefresh — reported upward',
      '* A write LANDED — onChanged. A refresh is not a write.',
      'onRefresh();',
    ]) {
      expect(/onClick=\{onRefresh\}/.test(prose), prose).toBe(false);
      expect(/onClick=\{onChanged\}/.test(prose), prose).toBe(false);
    }
    // The interface pattern likewise sees a real signature and not a mention of the name.
    const sig = /onRefresh\s*:\s*\(\s*\)\s*=>\s*void/;
    expect(sig.test('  onRefresh: () => void;')).toBe(true);
    expect(sig.test('  /** Re-read the list. */ // onRefresh')).toBe(false);
  });

  it('the button LABELS its in-flight state from the page’s `loading`', () => {
    // Without this the click has no feedback: `refetch` re-runs the page load, which takes as long
    // as a page load, and a control that looks identical before and after being pressed reads as
    // broken. `DeploymentsTab` already takes `loading: boolean` for exactly this, and the label
    // idiom is `Templates.tsx`/`ConnectionsAdmin`'s.
    expect(/loading\s*:\s*boolean/.test(tabSrc)).toBe(true);
    expect(/^\s*loading,\s*$/m.test(tabSrc)).toBe(true);
    // The two-state label, and the disable that stops a second overlapping load.
    expect(/\{loading \? 'Refreshing…' : 'Refresh'\}/.test(tabSrc)).toBe(true);
    expect(/disabled=\{loading\}/.test(tabSrc)).toBe(true);
  });

  it('that label pattern is the rendered expression, not a description of one', () => {
    // Same anti-prose proof. The pattern requires the JSX braces and the ternary, so the sentence
    // "the label reads Refreshing… while in flight" cannot satisfy it.
    const label = /\{loading \? 'Refreshing…' : 'Refresh'\}/;
    expect(label.test("              {loading ? 'Refreshing…' : 'Refresh'}")).toBe(true);
    expect(label.test('// the label reads Refreshing… while loading, else Refresh')).toBe(false);
    expect(label.test("{loading ? 'Refresh' : 'Refreshing…'}")).toBe(false);
  });

  it('introduces NO second read path and NO nonce of its own', () => {
    // The refresh is the PAGE's existing `[id, reloadNonce]` effect, re-run. A local read here
    // would be the very thing the tab is forbidden from owning (the list read is also the
    // capability probe — see the block above), and a local nonce would be a second source of
    // truth for when to reload. The two guards above (`pullRequestsApi.list(`, bare `fetch(`)
    // already ban the read; these ban the machinery that would need one.
    expect(/useEffect\s*\(/.test(tabSrc)).toBe(false);
    expect(/setInterval|setTimeout/.test(tabSrc)).toBe(false);
    expect(/[Nn]once/.test(tabSrc)).toBe(false);
    // And the tab still holds no copy of the list — `pullRequests` arrives as a prop and is
    // never fed into a setter.
    expect(/setPullRequests/.test(tabSrc)).toBe(false);
  });

  it('those no-second-path patterns can each SEE their target', () => {
    // A guard that cannot fail is worse than no guard.
    expect(/useEffect\s*\(/.test('useEffect(() => {')).toBe(true);
    expect(/useEffect\s*\(/.test('// no useEffect here, deliberately')).toBe(false);
    expect(/setInterval|setTimeout/.test('const t = setInterval(load, 5000);')).toBe(true);
    expect(/[Nn]once/.test('const [nonce, setNonce] = useState(0);')).toBe(true);
    expect(/setPullRequests/.test('setPullRequests(rows);')).toBe(true);
  });

  it('does NOT bolt a timestamp or an auto-refresh onto the control', () => {
    // Explicitly out of scope. A "last updated" line would need a clock the tab does not have and
    // would go stale the moment the page refetches for another reason; an interval is the polling
    // the finding rejected by name.
    for (const forbidden of [/last updated/i, /auto[- ]?refresh/i, /every \d+ ?s/i]) {
      expect(tabSrc, String(forbidden)).not.toMatch(forbidden);
    }
  });
});
