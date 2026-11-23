import { describe, expect, expectTypeOf, it } from 'vitest';

import type { PullRequestView } from '../../../api/client';
import { ApiFailure, type ErrorCode } from '../../../api/failure';
import {
  PULL_REQUEST_REFUSAL_CODES,
  authorDisplay,
  classifyPullRequestFailure,
  createPullRequestBody,
  prProbeFrom,
  prReadOnlyNote,
  prRowActions,
  prStateBadgeKey,
  prStateLabel,
  prTabVisibility,
  pullRequestActionMessage,
  shortHeadSha,
  sortPullRequests,
  withLocalMerges,
  type PullRequestRefusalKind,
  type PullRequestRow,
} from './pullRequestsTabModel';
import pureRaw from './pullRequestsTabModel.ts?raw';
import ownRaw from './pullRequestsTabModel.test.ts?raw';
import tabRaw from './PullRequestsTab.tsx?raw';
import { stripComments } from '../../../rawSource';

const pureSrc = stripComments(pureRaw);
const ownSrc = stripComments(ownRaw);
const tabSrc = stripComments(tabRaw);

const CAPABILITY_MESSAGE = 'pull requests are not enabled for this organization';

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

describe('PullRequestRow — the structural subset of client.ts PullRequestView this tab reads', () => {
  it('accepts every PullRequestView as a row, so the wire type and the tab model cannot drift apart; tsc -b holds the assignment', () => {
    const view: PullRequestView = {
      number: 7,
      title: 'Raise the claim-triage threshold',
      state: 'open',
      author: 'jorge',
      head_sha: '3f9a1c2b4d5e6f',
      url: 'https://github.com/acme/claims-triage/pull/7',
      can_approve: true,
      approve_blocked_reason: null,
      mergeable: null,
      merge_commit_sha: null,
    };
    const asRow: PullRequestRow = view;
    expect(asRow).toBe(view);
  });
});

describe('prTabVisibility — hidden, never broken, when the org lacks the grant', () => {
  it('is VISIBLE only on a definite success', () => {
    expect(prTabVisibility({ status: 'ok' })).toBe('visible');
  });

  it('HIDES the tab when the org lacks `pull_requests: write`', () => {
    expect(prTabVisibility({ status: 'error', message: CAPABILITY_MESSAGE })).toBe('hidden');
  });

  it('hides on an UNRECOGNIZED failure too — the probe failing is itself a reason to hide', () => {
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
    expect(prTabVisibility({ status: 'loading' })).toBe('pending');
  });

  it('never answers `visible` for anything but a success', () => {
    const probes = [
      { status: 'loading' } as const,
      { status: 'error', message: CAPABILITY_MESSAGE } as const,
      { status: 'error', message: null } as const,
    ];
    for (const p of probes) expect(prTabVisibility(p), p.status).not.toBe('visible');
  });
});

describe('prProbeFrom — the probe the tab decision reads off the page’s load state', () => {
  it('the first fetch with no carried data is loading, so the tab is pending until the probe answers', () => {
    const probe = prProbeFrom({ kind: 'loading', data: null });
    expect(probe).toEqual({ status: 'loading' });
    expect(prTabVisibility(probe)).toBe('pending');
  });

  it('carried rows during a refetch are ok, so the tab stays mounted through the refetch', () => {
    const probe = prProbeFrom({ kind: 'loading', data: [row()] });
    expect(probe).toEqual({ status: 'ok' });
    expect(prTabVisibility(probe)).toBe('visible');
    expect(prProbeFrom({ kind: 'ready', data: [] })).toEqual({ status: 'ok' });
  });

  it('an error is error with the backend’s message, so the tab hides whatever the cause', () => {
    const probe = prProbeFrom({ kind: 'error', message: CAPABILITY_MESSAGE, failure: null, data: null });
    expect(probe).toEqual({ status: 'error', message: CAPABILITY_MESSAGE });
    expect(prTabVisibility(probe)).toBe('hidden');
    expect(prTabVisibility(prProbeFrom({ kind: 'error', message: 'boom', failure: null, data: [row()] }))).toBe('hidden');
  });
});

describe('createPullRequestBody — the create request, built once from the form', () => {
  it('omits base when it is blank: the backend distinguishes an absent base from an empty branch name', () => {
    const body = createPullRequestBody('Raise the threshold', 'feature/threshold', '   ');
    expect('base' in body).toBe(false);
    expect(createPullRequestBody('t', 'h', ' main ')).toEqual({ title: 't', head: 'h', base: 'main' });
  });

  it('trims title and head so a title of spaces cannot pass the button and 422', () => {
    expect(createPullRequestBody('  Raise the threshold ', ' feature/threshold ', '')).toEqual({
      title: 'Raise the threshold',
      head: 'feature/threshold',
    });
    expect(createPullRequestBody('   ', 'h', '').title).toBe('');
  });
});

function apiFailure({ status, code, detail = '', message = detail }: { status: number; code: ErrorCode | null; detail?: string; message?: string }): ApiFailure {
  return new ApiFailure(status, code, detail, message);
}

const REFUSAL: Record<PullRequestRefusalKind, ApiFailure> = {
  self_approval: apiFailure({ status: 409, code: 'github_pr.self_approval', detail: 'you cannot approve your own pull request' }),
  not_approvable: apiFailure({ status: 409, code: 'github_pr.not_approvable', detail: 'this pull request cannot be approved' }),
  not_mergeable: apiFailure({ status: 409, code: 'github_pr.not_mergeable', detail: 'this pull request cannot be merged yet' }),
  not_linked: apiFailure({
    status: 409,
    code: 'github_link.not_linked',
    detail: 'connect your GitHub account to act on pull requests',
  }),
  capability_missing: apiFailure({ status: 403, code: 'github_pr.capability_missing', detail: CAPABILITY_MESSAGE }),
  no_commits: apiFailure({ status: 409, code: 'github_pr.no_commits', detail: 'there are no commits between these branches' }),
  conflict: apiFailure({ status: 409, code: 'github_pr.conflict', detail: 'GitHub declined the request for this pull request' }),
  not_found: apiFailure({ status: 404, code: 'github_pr.not_found', detail: 'pull request not found' }),
  insufficient: apiFailure({ status: 403, code: 'project_role.insufficient', detail: 'insufficient project role' }),
  provider_error: apiFailure({ status: 502, code: 'github_pr.provider_error', detail: 'GitHub request failed' }),
};

describe('prRowActions — approve is the SERVER’s answer, never re-derived', () => {
  it('offers approve to a maintainer when the server says it is available', () => {
    const may = prRowActions(row(), 'maintainer', 0, false);
    expect(may.approve).toBe(true);
    expect(may.blockedReason).toBeNull();
  });

  it('SUPPRESSES approve and states the server’s reason when `can_approve` is false', () => {
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
    const may = prRowActions(row({ author: 'anyone-at-all' }), 'maintainer', 0, false);
    expect(may.approve).toBe(true);
  });

  it('a refusal with no reason still suppresses the button', () => {
    const may = prRowActions(row({ can_approve: false }), 'maintainer', 0, false);
    expect(may.approve).toBe(false);
    expect(may.blockedReason).toBeNull();
  });

  it('offers neither verb on a closed or merged pull request', () => {
    for (const state of ['closed', 'merged']) {
      const may = prRowActions(row({ state }), 'maintainer', 0, false);
      expect(may.approve, state).toBe(false);
      expect(may.merge, state).toBe(false);
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
    for (const mergeable of [null, undefined]) {
      expect(prRowActions(row({ mergeable }), 'maintainer', 0, false).merge, String(mergeable)).toBe(
        true,
      );
    }
  });

  it('reports NOTHING per-row about an absent mergeability', () => {
    for (const mergeable of [null, undefined, true, false]) {
      expect(Object.keys(prRowActions(row({ mergeable }), 'maintainer', 0, false)).sort()).toEqual([
        'approve',
        'blockedReason',
        'merge',
      ]);
    }
  });
});

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

describe('withLocalMerges — a merge from this tab shows at once', () => {
  const base = { title: 't', author: 'a', head_sha: 'abc', url: 'u', can_approve: true };
  const rows: PullRequestRow[] = [
    { ...base, number: 1, state: 'open' },
    { ...base, number: 2, state: 'open' },
    { ...base, number: 3, state: 'closed' },
  ];

  it('flips an open row merged here to merged, so its Merge button is gone', () => {
    const out = withLocalMerges(rows, new Set([1]));
    expect(out.map((r) => r.state)).toEqual(['merged', 'open', 'closed']);
    expect(prRowActions(out[0], 'maintainer', 0, false).merge).toBe(false);
    expect(prRowActions(out[1], 'maintainer', 0, false).merge).toBe(true);
  });

  it('never overrides a non-open server state, and does not mutate its input', () => {
    const out = withLocalMerges(rows, new Set([3]));
    expect(out[2].state).toBe('closed');
    expect(rows[0].state).toBe('open');
  });
});

describe('authorDisplay — a GitHub login, marked as one', () => {
  it('prefixes `@`, the marker for the PROVIDER’s currency', () => {
    expect(authorDisplay('jorge')).toBe('@jorge');
  });

  it('does not double-prefix an already-prefixed login', () => {
    expect(authorDisplay('@jorge')).toBe('@jorge');
  });

  it('renders an ABSENT author as absent, never as "unknown user"', () => {
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

describe('prStateBadgeKey — a SEMANTIC key, never a class string', () => {
  it('maps the three real states', () => {
    expect(prStateBadgeKey('open')).toBe('pending');
    expect(prStateBadgeKey('merged')).toBe('ok');
    expect(prStateBadgeKey('closed')).toBe('unknown');
  });

  it('does not render a closed pull request as a FAILURE', () => {
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
    expect(prStateLabel('draft')).toBe('draft');
    expect(prStateLabel(null)).toBe('Unknown');
  });
});

describe('PULL_REQUEST_REFUSAL_CODES — typed on the generated ErrorCode', () => {
  it('satisfies Record<PullRequestRefusalKind, readonly ErrorCode[]>, so a renamed backend code fails tsc where this table names it', () => {
    expectTypeOf(PULL_REQUEST_REFUSAL_CODES).toExtend<Record<PullRequestRefusalKind, readonly ErrorCode[]>>();
  });

  it('names at least one code per kind and no code under two kinds', () => {
    const all = Object.values(PULL_REQUEST_REFUSAL_CODES).flat();
    for (const codes of Object.values(PULL_REQUEST_REFUSAL_CODES)) expect(codes.length).toBeGreaterThan(0);
    expect(new Set(all).size).toBe(all.length);
  });

  it('keys provider_error on every code whose detail is "GitHub request failed": the PR service’s own, its connection fault, and the link service’s', () => {
    for (const code of ['github_pr.provider_error', 'github_pr.connection_unavailable', 'github_link.provider_error'] as const) {
      expect(classifyPullRequestFailure(apiFailure({ status: 502, code, detail: 'GitHub request failed' })), code).toBe('provider_error');
    }
  });

  it('classifies by code alone: the detail under no code is unrecognised, a reworded detail under the code is not', () => {
    expect(classifyPullRequestFailure(apiFailure({ status: 409, code: null, detail: 'you cannot approve your own pull request' }))).toBeNull();
    expect(classifyPullRequestFailure(apiFailure({ status: 409, code: 'github_pr.self_approval', detail: 'reworded' }))).toBe('self_approval');
  });
});

describe('pullRequestActionMessage — every refusal the route answers with gets a sentence with a remedy', () => {
  it('states the SELF-APPROVAL refusal as a state, not a failure', () => {
    const message = pullRequestActionMessage(REFUSAL.self_approval, 'fallback');
    expect(message).toMatch(/you opened this pull request/i);
    expect(message).toMatch(/someone else/i);
    expect(message).not.toMatch(/failed/i);
  });

  it('covers each of the route’s other refusals', () => {
    const cases: [PullRequestRefusalKind, RegExp][] = [
      ['not_mergeable', /what is blocking the merge/i],
      ['not_approvable', /closed or merged/i],
      ['not_linked', /settings/i],
      ['capability_missing', /org admin/i],
      ['no_commits', /push a commit/i],
      ['conflict', /check the pull request on github/i],
      ['not_found', /no longer exists/i],
      ['insufficient', /maintainer role/i],
      ['provider_error', /nothing was changed/i],
    ];
    for (const [kind, expected] of cases) {
      expect(pullRequestActionMessage(REFUSAL[kind], 'fallback'), kind).toMatch(expected);
    }
  });

  it('every refusal resolves to something OTHER than the fallback, and other than its raw detail', () => {
    for (const failure of Object.values(REFUSAL)) {
      const message = pullRequestActionMessage(failure, 'SENTINEL');
      expect(message, failure.message).not.toBe('SENTINEL');
      expect(message, failure.message).not.toBe(failure.message);
    }
  });

  it('names the no-commits cause instead of inventing two others', () => {
    const message = pullRequestActionMessage(REFUSAL.no_commits, 'fallback');
    expect(message).toMatch(/nothing to open a pull request for/i);
    expect(message).toMatch(/push a commit/i);
    expect(message).not.toMatch(/declined/i);
  });

  it('the generic decline ASSERTS NO CAUSE, because it cannot know one', () => {
    const message = pullRequestActionMessage(REFUSAL.conflict, 'fallback');
    for (const invented of [/already exist/i, /may have moved/i, /no commits/i, /conflict/i]) {
      expect(message, String(invented)).not.toMatch(invented);
    }
    expect(message).toMatch(/nothing was changed/i);
  });

  it('the merge refusal ASSERTS NO CAUSE either, because a 405 does not establish one', () => {
    const message = pullRequestActionMessage(REFUSAL.not_mergeable, 'fallback');
    for (const invented of [/check/i, /branch protection/i, /conflict/i, /review/i]) {
      expect(message, String(invented)).not.toMatch(invented);
    }
    expect(message).toMatch(/refused to merge/i);
    expect(message).toMatch(/on github/i);
  });

  it('falls back rather than ECHOING an unrecognized upstream string, with or without a code', () => {
    const raw = 'Head branch was modified by octocat';
    const fallback = 'The action could not be completed.';
    expect(pullRequestActionMessage(new Error(raw), fallback)).toBe(fallback);
    expect(pullRequestActionMessage(apiFailure({ status: 502, code: null, detail: raw }), fallback)).toBe(fallback);
    expect(
      pullRequestActionMessage(apiFailure({ status: 404, code: 'connection.not_found', detail: 'Connection not found' }), fallback),
    ).toBe(fallback);
    expect(pullRequestActionMessage(new Error(raw), fallback)).not.toContain('octocat');
  });

  it('handles an empty or absent message', () => {
    expect(pullRequestActionMessage(new Error(''), 'fallback')).toBe('fallback');
    expect(pullRequestActionMessage(null, 'fallback')).toBe('fallback');
  });
});

describe('PullRequestsTab.tsx obeys the rules a .ts cannot enforce for it', () => {
  it('parses both sources (guards every assertion below against an empty read)', () => {
    expect(tabSrc.length).toBeGreaterThan(500);
    expect(pureSrc.length).toBeGreaterThan(500);
    expect(tabSrc).toContain('pullRequestsApi');
  });

  it('owns the three WRITE verbs and not the list read, which is the page’s own capability probe', () => {
    for (const verb of ['create', 'approve', 'merge']) {
      expect(new RegExp(`pullRequestsApi\\s*\\.\\s*${verb}\\s*\\(`).test(tabSrc), verb).toBe(true);
    }
    expect(/pullRequestsApi\s*\.\s*list\s*\(/.test(tabSrc)).toBe(false);
    expect(tabSrc).not.toContain('axios');
    expect(/(?<![\w$])fetch\s*\(/.test(tabSrc)).toBe(false);
  });

  it('that four-call guard can SEE a chained call (so it cannot regress to strict-substring)', () => {
    const pattern = (verb: string) => new RegExp(`pullRequestsApi\\s*\\.\\s*${verb}\\s*\\(`);
    expect(pattern('list').test('pullRequestsApi.list(repoId)')).toBe(true);
    expect(pattern('list').test('pullRequestsApi\n      .list(repoId)')).toBe(true);
    expect(pattern('list').test("import { pullRequestsApi } from '../../../api/client';")).toBe(
      false,
    );
    expect(pattern('merge').test('// merge is handled below')).toBe(false);
    const bareFetch = /(?<![\w$])fetch\s*\(/;
    expect(bareFetch.test('await fetch(url)')).toBe(true);
    expect(bareFetch.test('refetch();')).toBe(false);
  });

  it('builds the create request and the button gate from the same trimmed draft, so what the button accepts is what is sent', () => {
    expect(tabSrc).toMatch(/const draft = createPullRequestBody\(title, head, base\)/);
    expect(tabSrc).toMatch(/draft\.title\.length > 0 && draft\.head\.length > 0/);
    expect(tabSrc).not.toMatch(/base:\s*base\.trim\(\)/);
  });

  it('imports its companion with an EXPLICIT extension', () => {
    expect(tabSrc).toContain("from './pullRequestsTabModel.ts'");
  });

  it('decides NOTHING itself — every judgement comes from the companion', () => {
    for (const decision of ['prRowActions', 'prStateBadgeKey', 'prStateLabel', 'authorDisplay']) {
      expect(tabSrc, decision).toContain(decision);
    }
    expect(tabSrc).not.toMatch(/author\s*===/);
    expect(tabSrc).not.toMatch(/toLowerCase\(\)\s*===/);
  });

  it('renders the refusal REASON rather than implying it, and hands the failure itself to the companion, never its message', () => {
    expect(tabSrc).toContain('blockedReason');
    expect(tabSrc).toContain('pullRequestActionMessage');
    expect(tabSrc).not.toMatch(/err instanceof Error \? err\.message/);
  });

  it('speculates about mergeability NOWHERE', () => {
    expect(tabSrc).not.toContain('has not finished checking');
    expect(tabSrc).not.toMatch(/mergeab\w*Unknown/);
    expect(pureSrc).not.toMatch(/mergeab\w*Unknown/);
    expect(pureSrc).toContain('row.mergeable !== false');
  });

  it('conditionally RENDERS the write affordances rather than disabling them', () => {
    expect(tabSrc).toMatch(/actions\.approve\s*&&/);
    expect(tabSrc).toMatch(/actions\.merge\s*&&/);
  });

  it('offers NO governance verb — the shadow-governance failure mode', () => {
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

  it('contains NO stage literal anywhere — quoted OR as a property access', () => {
    const stageLiteral =
      /(['"`])(dev|prod)\1|[.[]\s*['"`]?(dev|prod)['"`]?(?![\w$])|[{,;(]\s*(dev|prod)\s*[:,}]/;
    for (const [name, src] of [
      ['PullRequestsTab.tsx', tabSrc],
      ['pullRequestsTabModel.ts', pureSrc],
      ['pullRequestsTabModel.test.ts', ownSrc],
    ] as const) {
      expect(stageLiteral.test(src), `${name} contains a stage literal`).toBe(false);
    }
  });

  it('the stage guard can SEE a property-access read (so it cannot regress to quoted-only)', () => {
    const stageLiteral =
      /(['"`])(dev|prod)\1|[.[]\s*['"`]?(dev|prod)['"`]?(?![\w$])|[{,;(]\s*(dev|prod)\s*[:,}]/;
    const stage = 'd' + 'ev';
    expect(stageLiteral.test(`pr.base === '${stage}'`)).toBe(true);
    expect(stageLiteral.test(`t.stages.${stage}.region`)).toBe(true);
    expect(stageLiteral.test(`const { ${stage} } = t.stages;`)).toBe(true);
    expect(stageLiteral.test(`the ${stage} branch is the base`)).toBe(false);
    expect(stageLiteral.test(`a.${stage}elopment.x`)).toBe(false);
  });

  it('every clickable element is keyboard-reachable', () => {
    const clickableNonInteractive = /<(tr|div|span|td|li)\b[^>]*\sonClick/;
    expect(clickableNonInteractive.test(tabSrc)).toBe(false);
  });

  it('opens external provider links safely', () => {
    expect(tabSrc).toContain('rel="noreferrer"');
    expect(tabSrc).toContain('target="_blank"');
  });

  it('contains no AWS account id (a hard project rule)', () => {
    const twelveDigits = /(?<!\d)\d{12}(?!\d)/;
    for (const [name, src] of [
      ['PullRequestsTab.tsx', tabSrc],
      ['pullRequestsTabModel.ts', pureSrc],
    ] as const) {
      expect(twelveDigits.test(src), `${name} contains a 12-digit literal`).toBe(false);
    }
  });
});

describe('PullRequestsTab.tsx offers a MANUAL refresh, and calls the honest prop for it', () => {
  it('takes an `onRefresh` prop distinct from `onChanged`', () => {
    expect(/onRefresh\s*:\s*\(\s*\)\s*=>\s*void/.test(tabSrc)).toBe(true);
    expect(/onChanged\s*:\s*\(\s*\)\s*=>\s*void/.test(tabSrc)).toBe(true);
    expect(/^\s*onRefresh,\s*$/m.test(tabSrc)).toBe(true);
    expect(/^\s*onChanged,\s*$/m.test(tabSrc)).toBe(true);
  });

  it('wires the refresh BUTTON to `onRefresh` — not to `onChanged`, not to a local read', () => {
    expect(/onClick=\{onRefresh\}/.test(tabSrc)).toBe(true);
    expect(/onClick=\{onChanged\}/.test(tabSrc)).toBe(false);
  });

  it('those two click-binding patterns can tell the props apart (they cannot match prose)', () => {
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
    const sig = /onRefresh\s*:\s*\(\s*\)\s*=>\s*void/;
    expect(sig.test('  onRefresh: () => void;')).toBe(true);
    expect(sig.test('  /** Re-read the list. */ // onRefresh')).toBe(false);
  });

  it('the button LABELS its in-flight state from the page’s `loading`', () => {
    expect(/loading\s*:\s*boolean/.test(tabSrc)).toBe(true);
    expect(/^\s*loading,\s*$/m.test(tabSrc)).toBe(true);
    expect(/\{loading \? 'Refreshing…' : 'Refresh'\}/.test(tabSrc)).toBe(true);
    expect(/disabled=\{loading\}/.test(tabSrc)).toBe(true);
  });

  it('that label pattern is the rendered expression, not a description of one', () => {
    const label = /\{loading \? 'Refreshing…' : 'Refresh'\}/;
    expect(label.test("              {loading ? 'Refreshing…' : 'Refresh'}")).toBe(true);
    expect(label.test('// the label reads Refreshing… while loading, else Refresh')).toBe(false);
    expect(label.test("{loading ? 'Refresh' : 'Refreshing…'}")).toBe(false);
  });

  it('introduces NO second read path and NO nonce of its own', () => {
    expect(/useEffect\s*\(/.test(tabSrc)).toBe(false);
    expect(/setInterval|setTimeout/.test(tabSrc)).toBe(false);
    expect(/[Nn]once/.test(tabSrc)).toBe(false);
    expect(/setPullRequests/.test(tabSrc)).toBe(false);
  });

  it('those no-second-path patterns can each SEE their target', () => {
    expect(/useEffect\s*\(/.test('useEffect(() => {')).toBe(true);
    expect(/useEffect\s*\(/.test('// no useEffect here, deliberately')).toBe(false);
    expect(/setInterval|setTimeout/.test('const t = setInterval(load, 5000);')).toBe(true);
    expect(/[Nn]once/.test('const [nonce, setNonce] = useState(0);')).toBe(true);
    expect(/setPullRequests/.test('setPullRequests(rows);')).toBe(true);
  });

  it('does NOT bolt a timestamp or an auto-refresh onto the control', () => {
    for (const forbidden of [/last updated/i, /auto[- ]?refresh/i, /every \d+ ?s/i]) {
      expect(tabSrc, String(forbidden)).not.toMatch(forbidden);
    }
  });
});
