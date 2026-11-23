import { describe, it, expect, expectTypeOf } from 'vitest';
import { GITHUB_LINK_PATHS } from '../../api/client';
import { ApiFailure, type ErrorCode } from '../../api/failure';
import { AUTH_401_HALT_MESSAGE } from '../../api/authRetry';
import { failureOf } from '../../api/loadState';
import { stripComments } from '../../rawSource';
import {
  LINK_ERROR_CODES,
  LINK_RETRY_HINT,
  classifyLinkError,
  classifyLinkFailure,
  type LinkErrorKind,
} from './githubLinkModel';
import gitHubLinkRaw from './GitHubLink.tsx?raw';
import gitHubLinkCallbackRaw from './GitHubLinkCallback.tsx?raw';

describe('GITHUB_LINK_PATHS', () => {
  it('pins the five link route paths', () => {
    expect(GITHUB_LINK_PATHS.view).toBe('/api/v1/me/github-link');
    expect(GITHUB_LINK_PATHS.start).toBe('/api/v1/me/github-link/start');
    expect(GITHUB_LINK_PATHS.callback).toBe('/api/v1/me/github-link/callback');
    expect(GITHUB_LINK_PATHS.verify('c-1')).toBe('/api/v1/me/github-link/c-1/verify');
    expect(GITHUB_LINK_PATHS.unlink('c-1')).toBe('/api/v1/me/github-link/c-1');
  });

  it('scopes every path to /me — the oid comes from the principal, never from a path param, so one human can never address another\'s link', () => {
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

function apiFailure({ status, code, detail = '', message = detail }: { status: number; code: ErrorCode | null; detail?: string; message?: string }): ApiFailure {
  return new ApiFailure(status, code, detail, message);
}

const REFUSAL: Record<LinkErrorKind, ApiFailure> = {
  not_found: apiFailure({ status: 404, code: 'github_link.not_found', detail: 'GitHub link not found' }),
  bad_request: apiFailure({ status: 400, code: 'github_link.bad_request', detail: 'Invalid request' }),
  conflict: apiFailure({
    status: 409,
    code: 'github_link.conflict',
    detail: 'That GitHub account is already linked to another user',
  }),
  oauth_client_missing: apiFailure({
    status: 409,
    code: 'github_link.oauth_client_missing',
    detail: 'This org connection has no GitHub OAuth client — ask an admin to add one',
  }),
  refresh_in_progress: apiFailure({
    status: 409,
    code: 'github_link.refresh_in_progress',
    detail: 'GitHub token refresh in progress — retry',
  }),
  link_revoked: apiFailure({
    status: 409,
    code: 'github_link.link_revoked',
    detail: 'Your GitHub authorization was revoked — reconnect your account',
  }),
  provider_error: apiFailure({ status: 502, code: 'github_link.provider_error', detail: 'GitHub request failed' }),
  secret_error: apiFailure({ status: 502, code: 'github_link.secret_error', detail: 'Secret store operation failed' }),
};

describe('LINK_ERROR_CODES — typed on the generated ErrorCode', () => {
  it('satisfies Record<LinkErrorKind, readonly ErrorCode[]>, so a renamed backend code fails tsc where this table names it', () => {
    expectTypeOf(LINK_ERROR_CODES).toExtend<Record<LinkErrorKind, readonly ErrorCode[]>>();
  });

  it('names at least one code per kind and no code under two kinds', () => {
    const all = Object.values(LINK_ERROR_CODES).flat();
    for (const codes of Object.values(LINK_ERROR_CODES)) expect(codes.length).toBeGreaterThan(0);
    expect(new Set(all).size).toBe(all.length);
  });

  it('keys not_found on both codes the route answers 404 with: the link row missing and the connection missing', () => {
    expect(classifyLinkFailure(REFUSAL.not_found)).toBe('not_found');
    expect(
      classifyLinkFailure(apiFailure({ status: 404, code: 'github_link.connection_missing', detail: 'GitHub link not found' })),
    ).toBe('not_found');
  });

  it('classifies by code alone: the detail under no code is unrecognised, a reworded detail under the code is not', () => {
    expect(classifyLinkFailure(apiFailure({ status: 404, code: null, detail: 'GitHub link not found' }))).toBeNull();
    expect(classifyLinkFailure(apiFailure({ status: 409, code: 'github_link.refresh_in_progress', detail: 'reworded' }))).toBe(
      'refresh_in_progress',
    );
  });
});

describe('classifyLinkError — the route\'s eight kinds, each pinned by the code it recovers', () => {
  it('marks a token-refresh collision retryable: the other ECS task holds the claim for at most 60 s, so a terminal verdict would tell the human to give up on a link that is fine', () => {
    const out = classifyLinkError(REFUSAL.refresh_in_progress, 'fallback');
    expect(out.kind).toBe('refresh_in_progress');
    expect(out.retryable).toBe(true);
    expect(out.message).toContain(LINK_RETRY_HINT);
  });

  it('marks a Secrets Manager blip retryable', () => {
    const out = classifyLinkError(REFUSAL.secret_error, 'fallback');
    expect(out.kind).toBe('secret_error');
    expect(out.retryable).toBe(true);
    expect(out.message).toContain(LINK_RETRY_HINT);
  });

  it('marks a GitHub transport failure retryable', () => {
    const out = classifyLinkError(REFUSAL.provider_error, 'fallback');
    expect(out.kind).toBe('provider_error');
    expect(out.retryable).toBe(true);
    expect(out.message).toContain(LINK_RETRY_HINT);
  });

  it('marks a revoked authorization terminal — the remedy is reconnect, not retry: a retry re-probes a dead token and gets the same answer', () => {
    const out = classifyLinkError(REFUSAL.link_revoked, 'fallback');
    expect(out.kind).toBe('link_revoked');
    expect(out.retryable).toBe(false);
    expect(out.message).not.toContain(LINK_RETRY_HINT);
  });

  it('recognizes not-found as not-found, and terminal', () => {
    const out = classifyLinkError(REFUSAL.not_found, 'fallback');
    expect(out.kind).toBe('not_found');
    expect(out.retryable).toBe(false);
  });

  it('recognizes bad-request as bad-request, and terminal', () => {
    const out = classifyLinkError(REFUSAL.bad_request, 'fallback');
    expect(out.kind).toBe('bad_request');
    expect(out.retryable).toBe(false);
  });

  it('recognizes an account already linked elsewhere as conflict, and terminal', () => {
    const out = classifyLinkError(REFUSAL.conflict, 'fallback');
    expect(out.kind).toBe('conflict');
    expect(out.retryable).toBe(false);
  });

  it('recognizes a missing OAuth client as oauth-client-missing, and terminal', () => {
    const out = classifyLinkError(REFUSAL.oauth_client_missing, 'fallback');
    expect(out.kind).toBe('oauth_client_missing');
    expect(out.retryable).toBe(false);
  });

  it('recognizes every one of the eight backend codes and shows the detail as sent', () => {
    for (const [kind, failure] of Object.entries(REFUSAL)) {
      const out = classifyLinkError(failure, 'fallback');
      expect(out.kind, `${kind} fell through to the fallback`).toBe(kind);
      expect(out.message).toContain(failure.message);
    }
  });

  it('answers the error\'s own message, or the caller\'s text when it has none, as terminal with kind null: an unknown failure is more likely a bug than contention', () => {
    expect(classifyLinkError(new Error('Network Error'), 'Could not reach AGP.')).toEqual({
      message: 'Network Error',
      retryable: false,
      kind: null,
    });
    expect(
      classifyLinkError(apiFailure({ status: 404, code: 'connection.not_found', detail: 'Connection not found' }), 'Could not reach AGP.'),
    ).toEqual({
      message: 'Connection not found',
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

  it('shows the interceptor’s 401 copy as sent when the load state carries its codeless 401 failure', () => {
    const fallback = 'Could not load your GitHub links.';
    const state = failureOf(new ApiFailure(401, null, undefined, AUTH_401_HALT_MESSAGE), fallback);
    expect(state.failure).toMatchObject({ status: 401, code: null, message: AUTH_401_HALT_MESSAGE });
    expect(classifyLinkError(state.failure ?? state.message, fallback)).toEqual({
      message: AUTH_401_HALT_MESSAGE,
      retryable: false,
      kind: null,
    });
    expect(stripComments(gitHubLinkRaw)).toContain(
      "classifyLinkError(state.failure ?? state.message, 'Could not load your GitHub links.')",
    );
  });
});

describe('component thinness — the two .tsx files stay presentation-only, read raw so a copied string in a comment cannot seed a drift either', () => {
  const SOURCES = [
    { file: 'GitHubLink.tsx', src: gitHubLinkRaw },
    { file: 'GitHubLinkCallback.tsx', src: gitHubLinkCallbackRaw },
  ];

  it('reads both components as code, so the guards below are not vacuous', () => {
    for (const { file, src } of SOURCES) {
      expect(stripComments(src).length, file).toBeGreaterThan(1000);
    }
  });

  it('keeps all card copy in githubLinkModel.ts', () => {
    for (const { file, src } of SOURCES) {
      expect(src, file).not.toContain('Linked as');
      expect(src, file).not.toContain('Connect your GitHub account');
      expect(src, file).not.toContain('was revoked');
    }
  });

  it('keeps the status→copy derivation in githubLinkModel.ts: the one status literal a .tsx may read is refreshing, for the rotation spinner; the two derived states are not', () => {
    for (const { file, src } of SOURCES) {
      expect(src, file).not.toContain("'unlinked'");
      expect(src, file).not.toContain("'revoked'");
    }
  });

  it('never hardcodes the callback path — buildLinkRedirectUrl owns it, and the route table in routes.ts is the one unavoidable second literal', () => {
    for (const { file, src } of SOURCES) {
      expect(src, file).not.toContain('/ops/github-link/callback');
    }
  });
});
