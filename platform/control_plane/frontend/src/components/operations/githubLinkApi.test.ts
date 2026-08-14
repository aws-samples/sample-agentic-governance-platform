// Task-10 tests for the per-user GitHub link SURFACE (Epic 26B): the api-client's
// route paths, the error-copy classifier the cards read, and a source assertion that
// the two `.tsx` files stayed presentation-only.
//
// Why the classifier's tests live HERE and not in `githubLink.test.ts`: this file is
// Task 10's, `githubLink.test.ts` is Task 5's (reviewed and pinned). The function it
// covers is exported from `githubLink.ts` because that is the cohesive home for link
// logic — the module, not the test file, is what the components import.
//
// Importing `client.ts` runs no network: the module body only constructs the axios
// instance and its interceptors.

import { describe, it, expect } from 'vitest';
import { GITHUB_LINK_PATHS } from '../../api/client';
import { classifyLinkError, LINK_RETRY_HINT } from './githubLink';
// Vite's `?raw` rather than `node:fs`: `tsconfig.app.json` sets `types: ["vite/client"]`
// only, so Node's typings are not in this project — and `vite/client` is exactly what
// declares `*?raw`. The explicit `.tsx` is load-bearing: `./GitHubLink` resolves to
// `GitHubLink.ts` first, which on a case-insensitive filesystem is `githubLink.ts`.
import gitHubLinkSrc from './GitHubLink.tsx?raw';
import gitHubLinkCallbackSrc from './GitHubLinkCallback.tsx?raw';

describe('GITHUB_LINK_PATHS', () => {
  it('pins the five link route paths', () => {
    expect(GITHUB_LINK_PATHS.view).toBe('/api/v1/me/github-link');
    expect(GITHUB_LINK_PATHS.start).toBe('/api/v1/me/github-link/start');
    expect(GITHUB_LINK_PATHS.callback).toBe('/api/v1/me/github-link/callback');
    expect(GITHUB_LINK_PATHS.verify('c-1')).toBe('/api/v1/me/github-link/c-1/verify');
    expect(GITHUB_LINK_PATHS.unlink('c-1')).toBe('/api/v1/me/github-link/c-1');
  });

  it('scopes every path to /me — the oid comes from the principal, never from a path param', () => {
    // A link route that took an oid in the path would let one human address another's
    // link. The whole router is mounted under /me for that reason; a drift away from it
    // is the one shape of this API that would be a privacy bug rather than a 404.
    for (const path of [
      GITHUB_LINK_PATHS.view,
      GITHUB_LINK_PATHS.start,
      GITHUB_LINK_PATHS.callback,
      GITHUB_LINK_PATHS.verify('c-1'),
      GITHUB_LINK_PATHS.unlink('c-1'),
    ]) {
      expect(path.startsWith('/api/v1/me/github-link')).toBe(true);
    }
  });
});

// The api-client's response interceptor replaces the AxiosError with
// `new Error(response.data.detail)` — the STATUS is gone by the time a component sees
// it, so the route's FIXED detail literal is the only thing left to classify on. These
// strings are therefore a contract, copied verbatim from the brief's `_ERROR_DETAIL`.
//
// This object is the mutation barrier for all EIGHT literals. The classifier returns the
// `.kind` it recognized (`null` on a fall-through), so `expect(kind).toBe('<kind>')` fails
// the moment a literal is removed from or reworded in `DETAIL_KINDS`. Before that, five of
// the eight were unpinned: the terminal branch and the fall-through both answer
// `{message: raw, retryable: false}`, so deleting a terminal literal changed nothing any
// assertion could see.
const DETAIL = {
  not_found: 'GitHub link not found',
  bad_request: 'Invalid request',
  conflict: 'That GitHub account is already linked to another user',
  oauth_client_missing:
    'This org connection has no GitHub OAuth client — ask an admin to add one',
  refresh_in_progress: 'GitHub token refresh in progress — retry',
  link_revoked: 'Your GitHub authorization was revoked — reconnect your account',
  provider_error: 'GitHub request failed',
  secret_error: 'Secret store operation failed',
} as const;

describe('classifyLinkError', () => {
  it('marks a token-refresh collision retryable (two ECS tasks contend on the same row)', () => {
    // 409 refresh_in_progress happens under NORMAL operation — the other task holds the
    // claim for at most 60s. Presenting it as a terminal failure would tell the human to
    // give up on a link that is fine.
    const out = classifyLinkError(new Error(DETAIL.refresh_in_progress), 'fallback');
    expect(out.kind).toBe('refresh_in_progress');
    expect(out.retryable).toBe(true);
    expect(out.message).toContain(LINK_RETRY_HINT);
  });

  it('marks a Secrets Manager blip retryable', () => {
    const out = classifyLinkError(new Error(DETAIL.secret_error), 'fallback');
    expect(out.kind).toBe('secret_error');
    expect(out.retryable).toBe(true);
    expect(out.message).toContain(LINK_RETRY_HINT);
  });

  it('marks a GitHub transport failure retryable', () => {
    const out = classifyLinkError(new Error(DETAIL.provider_error), 'fallback');
    expect(out.kind).toBe('provider_error');
    expect(out.retryable).toBe(true);
    expect(out.message).toContain(LINK_RETRY_HINT);
  });

  it('marks a revoked authorization terminal — the remedy is reconnect, not retry', () => {
    // Retrying a revoked grant re-probes a dead token and returns the same answer. The
    // card flips to `revoked` and offers Reconnect; a "try again" hint here would send
    // the human round a loop that cannot succeed.
    const out = classifyLinkError(new Error(DETAIL.link_revoked), 'fallback');
    expect(out.kind).toBe('link_revoked');
    expect(out.retryable).toBe(false);
    expect(out.message).not.toContain(LINK_RETRY_HINT);
  });

  it('recognizes not-found as not-found, and terminal', () => {
    const out = classifyLinkError(new Error(DETAIL.not_found), 'fallback');
    expect(out.kind).toBe('not_found');
    expect(out.retryable).toBe(false);
  });

  it('recognizes bad-request as bad-request, and terminal', () => {
    const out = classifyLinkError(new Error(DETAIL.bad_request), 'fallback');
    expect(out.kind).toBe('bad_request');
    expect(out.retryable).toBe(false);
  });

  it('recognizes an account already linked elsewhere as conflict, and terminal', () => {
    const out = classifyLinkError(new Error(DETAIL.conflict), 'fallback');
    expect(out.kind).toBe('conflict');
    expect(out.retryable).toBe(false);
  });

  it('recognizes a missing OAuth client as oauth-client-missing, and terminal', () => {
    const out = classifyLinkError(new Error(DETAIL.oauth_client_missing), 'fallback');
    expect(out.kind).toBe('oauth_client_missing');
    expect(out.retryable).toBe(false);
  });

  it('recognizes every one of the eight backend literals — none may drift unnoticed', () => {
    // The whole-table sweep. Each `.kind` here is asserted on its own above too, but this
    // is the one assertion that fails if the backend GAINS a kind the frontend never
    // learned about: `DETAIL` is copied from `_ERROR_DETAIL`, so an entry with no match
    // classifies as `null`.
    for (const [kind, detail] of Object.entries(DETAIL)) {
      const out = classifyLinkError(new Error(detail), 'fallback');
      expect(out.kind, `${kind} fell through to the fallback`).toBe(kind);
      expect(out.message).toContain(detail);
    }
  });

  it('falls back on an unrecognized error, and treats it as terminal', () => {
    // An unknown message is more likely a bug than contention; inviting a retry on it
    // would be a guess presented as advice. `kind: null` is what distinguishes this from
    // a recognized terminal detail — the two are otherwise byte-identical, which is why
    // the terminal literals were previously unpinned.
    expect(classifyLinkError(new Error('Network Error'), 'Could not reach AGP.')).toEqual({
      message: 'Network Error',
      retryable: false,
      kind: null,
    });
    expect(classifyLinkError(undefined, 'Could not reach AGP.')).toEqual({
      message: 'Could not reach AGP.',
      retryable: false,
      kind: null,
    });
    expect(classifyLinkError({ detail: 'nope' }, 'Could not reach AGP.')).toEqual({
      message: 'Could not reach AGP.',
      retryable: false,
      kind: null,
    });
    expect(classifyLinkError(new Error(''), 'Could not reach AGP.')).toEqual({
      message: 'Could not reach AGP.',
      retryable: false,
      kind: null,
    });
  });

  it('never inherits a prototype key as a kind', () => {
    // `DETAIL_KINDS` is a plain object indexed by an attacker-influenced-ish string (the
    // response body), so `'toString'` or `'constructor'` must classify as unknown rather
    // than returning `Object.prototype`'s member.
    for (const key of ['toString', 'constructor', 'hasOwnProperty', '__proto__']) {
      expect(classifyLinkError(new Error(key), 'fallback').kind).toBe(null);
    }
  });
});

describe('component thinness', () => {
  // `.tsx` is never rendered under vitest (no jsdom, and only `src/**/*.test.ts` is
  // collected), so a source assertion is the only available check that the components
  // stayed presentation-only — Task 5's module owns every card string and every
  // status→copy decision.
  const SOURCES = [
    { file: 'GitHubLink.tsx', src: gitHubLinkSrc },
    { file: 'GitHubLinkCallback.tsx', src: gitHubLinkCallbackSrc },
  ];

  it('keeps all card copy in githubLink.ts', () => {
    for (const { file, src } of SOURCES) {
      expect(src, file).not.toContain('Linked as');
      expect(src, file).not.toContain('Connect your GitHub account');
      expect(src, file).not.toContain('was revoked');
    }
  });

  it('keeps the status→copy derivation in githubLink.ts', () => {
    for (const { file, src } of SOURCES) {
      // A `.tsx` that branches on the wire status string is re-deriving what
      // `deriveLinkCardState` already decided, untestably. The ONE legitimate read of
      // `status` is the rotation spinner (`status === 'refreshing'`), which
      // `LinkCardState` deliberately folds into `linked` — so that literal is allowed
      // and the two that name a derived state are not.
      expect(src, file).not.toContain("'unlinked'");
      expect(src, file).not.toContain("'revoked'");
    }
  });

  it('never hardcodes the callback path — buildLinkRedirectUrl owns it', () => {
    // Four tasks depend on `/ops/github-link/callback` byte-for-byte; a second literal
    // is how it drifts. The route registration in App.tsx is the one place the string
    // is unavoidable, and it is not one of these files.
    for (const { file, src } of SOURCES) {
      expect(src, file).not.toContain('/ops/github-link/callback');
    }
  });
});
