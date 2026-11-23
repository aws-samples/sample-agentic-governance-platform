import { describe, it, expect, expectTypeOf } from 'vitest';
import type { GitHubLinkStatus } from '../../api/openapi';
import {
  AGP_GITHUB_LINK_STATE_KEY,
  buildLinkRedirectUrl,
  resolveLinkCallbackParams,
  resolveCallback,
  deriveLinkCardState,
  linkCardCopy,
} from './githubLinkModel';
import type { LinkCardInput, LinkCardState } from './githubLinkModel';

const LINKED: LinkCardInput = { linked: true, status: 'linked', github_login: 'octocat' };

const ALL_STATES: LinkCardState[] = ['no-oauth-client', 'unlinked', 'linked', 'revoked'];

describe('LinkCardInput is the wire row', () => {
  it("the client's GitHubLinkStatus assigns to it, so the module imports nothing", () => {
    expectTypeOf<GitHubLinkStatus>().toExtend<LinkCardInput>();
  });
});

describe('AGP_GITHUB_LINK_STATE_KEY', () => {
  it('is a distinct key from the manifest flow: the admin handshake and the per-user flow never read each other\'s state', () => {
    expect(AGP_GITHUB_LINK_STATE_KEY).toBe('agp_github_link_state');
    expect(AGP_GITHUB_LINK_STATE_KEY).not.toBe('agp_manifest_state');
  });
});

describe('buildLinkRedirectUrl', () => {
  it('builds the callback URL from the origin', () => {
    expect(buildLinkRedirectUrl('https://agp.example')).toBe(
      'https://agp.example/ops/github-link/callback',
    );
  });

  it('works for a localhost dev origin', () => {
    expect(buildLinkRedirectUrl('http://localhost:5173')).toBe(
      'http://localhost:5173/ops/github-link/callback',
    );
  });
});

describe('resolveLinkCallbackParams — every refusal is pinned by its exact message, the function\'s only observable output', () => {
  it('returns code + state on the happy path (state echoes the stored value)', () => {
    const params = new URLSearchParams({ code: 'gh-code-123', state: 'csrf-abc' });
    expect(resolveLinkCallbackParams(params, 'csrf-abc')).toEqual({
      code: 'gh-code-123',
      state: 'csrf-abc',
    });
  });

  it('accepts a raw query string as well as a URLSearchParams', () => {
    expect(resolveLinkCallbackParams('?code=gh-code-123&state=csrf-abc', 'csrf-abc')).toEqual({
      code: 'gh-code-123',
      state: 'csrf-abc',
    });
  });

  const REFUSALS = {
    providerError: 'GitHub did not authorize the link. Nothing was connected — you can try again.',
    missingCode: 'Missing authorization code in the callback URL.',
    missingState: 'Missing state in the callback URL.',
    noStoredState: 'No pending GitHub link request — start again from your profile menu.',
    mismatch: 'State mismatch — the callback could not be verified. Please try again.',
  } as const;

  it('fails in order: provider error, missing code, missing state, no stored state, mismatch — each input satisfies every earlier guard, so swapping two changes a message', () => {
    expect(resolveLinkCallbackParams('?error=access_denied&code=c&state=s', 's')).toEqual({
      error: REFUSALS.providerError,
    });
    expect(resolveLinkCallbackParams('?state=s', 's')).toEqual({ error: REFUSALS.missingCode });
    expect(resolveLinkCallbackParams('?code=c', 's')).toEqual({ error: REFUSALS.missingState });
    expect(resolveLinkCallbackParams('?code=c&state=s', null)).toEqual({
      error: REFUSALS.noStoredState,
    });
    expect(resolveLinkCallbackParams('?code=c&state=s', 'other')).toEqual({
      error: REFUSALS.mismatch,
    });
    expect(resolveLinkCallbackParams('?code=c&state=s', 's')).toEqual({ code: 'c', state: 's' });
  });

  it('reports the MISSING CODE first when both code and state are absent: an empty callback blames the first thing GitHub owes us', () => {
    expect(resolveLinkCallbackParams('', 's')).toEqual({ error: REFUSALS.missingCode });
    expect(resolveLinkCallbackParams('?foo=bar', 's')).toEqual({ error: REFUSALS.missingCode });
    expect(resolveLinkCallbackParams(new URLSearchParams(), 's')).toEqual({
      error: REFUSALS.missingCode,
    });
  });

  it('gives every refusal a distinct message (no two guards report the same thing)', () => {
    const messages = Object.values(REFUSALS);
    expect(new Set(messages).size).toBe(messages.length);
  });

  it('refuses a GitHub-returned error param (the human declined) with the decline message, never the misleading "Missing authorization code"', () => {
    expect(resolveLinkCallbackParams('?error=access_denied', 'csrf-abc')).toEqual({
      error: REFUSALS.providerError,
    });
    expect(resolveLinkCallbackParams('?error=access_denied', 'csrf-abc')).not.toEqual(
      resolveLinkCallbackParams('?state=csrf-abc', 'csrf-abc'),
    );
  });

  it('refuses a GitHub error even when a stored state is absent', () => {
    expect(resolveLinkCallbackParams('?error=access_denied&state=csrf-abc', null)).toEqual({
      error: REFUSALS.providerError,
    });
  });

  it('reads the provider error param itself, not merely the absence of a code: a callback carrying code, state and a match is refused only by reading error', () => {
    expect(resolveLinkCallbackParams('?error=some_other_failure&code=c&state=s', 's')).toEqual({
      error: REFUSALS.providerError,
    });
  });

  it('refuses an empty stored state: the callback clears the key on read, so a second landing sees \'\' or null, and either reads as absent, never as a match', () => {
    expect(resolveLinkCallbackParams('?code=c&state=', '')).toEqual({
      error: REFUSALS.missingState,
    });
    expect(resolveLinkCallbackParams('?code=c&state=s', '')).toEqual({
      error: REFUSALS.noStoredState,
    });
  });

  it('compares the state exactly — no trimming, no case folding, no prefix match', () => {
    expect(resolveLinkCallbackParams('?code=c&state=abc', ' abc')).toEqual({
      error: REFUSALS.mismatch,
    });
    expect(resolveLinkCallbackParams('?code=c&state=abc', 'ABC')).toEqual({
      error: REFUSALS.mismatch,
    });
    expect(resolveLinkCallbackParams('?code=c&state=abc', 'abcd')).toEqual({
      error: REFUSALS.mismatch,
    });
    expect(resolveLinkCallbackParams('?code=c&state=abcd', 'abc')).toEqual({
      error: REFUSALS.mismatch,
    });
  });

  it('never echoes the code or the state back in a refusal message', () => {
    const out = resolveLinkCallbackParams('?code=secret-code&state=csrf-abc', 'csrf-other');
    expect(out).toHaveProperty('error');
    const message = 'error' in out ? out.error : '';
    expect(message).not.toContain('secret-code');
    expect(message).not.toContain('csrf-abc');
    expect(message).not.toContain('csrf-other');
  });

  it('surfaces no code on any refusal path (the code must not be usable by the caller)', () => {
    const refusals = [
      resolveLinkCallbackParams('?error=access_denied', 's'),
      resolveLinkCallbackParams('?state=s', 's'),
      resolveLinkCallbackParams('?code=c', 's'),
      resolveLinkCallbackParams('?code=c&state=s', null),
      resolveLinkCallbackParams('?code=c&state=s', 'other'),
    ];
    for (const out of refusals) {
      expect(out).not.toHaveProperty('code');
      expect(out).not.toHaveProperty('state');
    }
  });
});

describe('resolveCallback — the landing page decides nothing: the session first, then the echo, then the exchange', () => {
  const VALID = { search: '?code=gh-code-123&state=csrf-abc', storedState: 'csrf-abc' };

  it('resolves a missing token to the retryable error', () => {
    expect(resolveCallback({ token: null, ...VALID })).toEqual({
      kind: 'no-session',
      message: 'Your AGP session could not be restored, so the link was not completed.',
      retryable: true,
      clearStoredState: false,
    });
  });

  it('leaves the stored state in place when the session is missing, so a re-landing on the same URL can still verify the echo', () => {
    expect(resolveCallback({ token: '', ...VALID }).clearStoredState).toBe(false);
  });

  it('decides the session before reading the callback: with no token even a refusable callback is the retryable error', () => {
    expect(resolveCallback({ token: null, search: '?error=access_denied', storedState: null }).kind).toBe(
      'no-session',
    );
  });

  it('resolves a refused echo to a terminal error carrying the refusal message, with the stored state consumed', () => {
    expect(resolveCallback({ token: 't', search: '?code=c&state=s', storedState: 'other' })).toEqual({
      kind: 'refused',
      message: 'State mismatch — the callback could not be verified. Please try again.',
      retryable: false,
      clearStoredState: true,
    });
  });

  it("resolves GitHub's own error param to the decline message, never to the exchange", () => {
    expect(
      resolveCallback({ token: 't', search: '?error=access_denied&code=c&state=s', storedState: 's' }),
    ).toEqual({
      kind: 'refused',
      message: 'GitHub did not authorize the link. Nothing was connected — you can try again.',
      retryable: false,
      clearStoredState: true,
    });
  });

  it('resolves a verified callback to the exchange with the code and the state, with the stored state consumed', () => {
    expect(resolveCallback({ token: 't', ...VALID })).toEqual({
      kind: 'exchange',
      code: 'gh-code-123',
      state: 'csrf-abc',
      clearStoredState: true,
    });
  });

  it('carries no code on any non-exchange result', () => {
    for (const out of [
      resolveCallback({ token: null, ...VALID }),
      resolveCallback({ token: 't', search: '?code=c&state=s', storedState: null }),
      resolveCallback({ token: 't', search: '?state=s', storedState: 's' }),
    ]) {
      expect(out.kind).not.toBe('exchange');
      expect(out).not.toHaveProperty('code');
    }
  });
});

describe('deriveLinkCardState', () => {
  it('no-oauth-client wins over every other state', () => {
    expect(deriveLinkCardState(undefined, false)).toBe('no-oauth-client');
    expect(deriveLinkCardState({ ...LINKED }, false)).toBe('no-oauth-client');
  });

  it('derives unlinked / linked / revoked', () => {
    expect(deriveLinkCardState(undefined, true)).toBe('unlinked');
    expect(deriveLinkCardState({ ...LINKED, linked: true, status: 'linked' }, true)).toBe('linked');
    expect(deriveLinkCardState({ ...LINKED, linked: false, status: 'unlinked' }, true)).toBe(
      'revoked',
    );
  });

  it('treats a refreshing row as linked (the token rotation is in flight, not broken)', () => {
    expect(deriveLinkCardState({ ...LINKED, linked: true, status: 'refreshing' }, true)).toBe(
      'linked',
    );
  });
});

describe('linkCardCopy', () => {
  it('says "Linked as @login"', () => {
    expect(linkCardCopy('linked', 'octocat').headline).toBe('Linked as @octocat');
  });

  it('invites the connect on the unlinked state', () => {
    const copy = linkCardCopy('unlinked', null);
    expect(copy.headline).toBe('Connect your GitHub account');
    expect(copy.action).not.toBeNull();
  });

  it('explains a revoked authorization and offers a reconnect', () => {
    const copy = linkCardCopy('revoked', 'octocat');
    expect(copy.headline).toBe(
      'Your GitHub authorization was revoked — reconnect to continue',
    );
    expect(copy.action).not.toBeNull();
  });

  it('never claims a "via" attribution string: GitHub documents an avatar badge and an audit field, no literal "via" label', () => {
    const all = ALL_STATES.map((s) => JSON.stringify(linkCardCopy(s, 'octocat'))).join(' ');
    expect(all).not.toContain(' via ');
  });

  it('offers no action when the connection has no OAuth client: an unavailable capability renders no button, never a disabled one', () => {
    expect(linkCardCopy('no-oauth-client', null).action).toBeNull();
  });

  it('returns a non-empty headline for every state, with and without a login', () => {
    for (const state of ALL_STATES) {
      expect(linkCardCopy(state, 'octocat').headline.length).toBeGreaterThan(0);
      expect(linkCardCopy(state, null).headline.length).toBeGreaterThan(0);
    }
  });

  it('does not render a bare "@" when the login is unknown', () => {
    expect(linkCardCopy('linked', null).headline).not.toContain('@');
  });
});
