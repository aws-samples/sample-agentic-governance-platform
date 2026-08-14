import { describe, it, expect } from 'vitest';
import {
  AGP_GITHUB_LINK_STATE_KEY,
  buildLinkRedirectUrl,
  resolveLinkCallbackParams,
  deriveLinkCardState,
  linkCardCopy,
} from './githubLink';
import type { LinkCardInput, LinkCardState } from './githubLink';

// A linked row, spread-and-overridden per case (the brief's LINKED fixture).
const LINKED: LinkCardInput = { linked: true, status: 'linked', github_login: 'octocat' };

const ALL_STATES: LinkCardState[] = ['no-oauth-client', 'unlinked', 'linked', 'revoked'];

describe('AGP_GITHUB_LINK_STATE_KEY', () => {
  it('is a distinct key from the manifest flow (the two CSRF states must never collide)', () => {
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

describe('resolveLinkCallbackParams', () => {
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

  // The EXACT message of each refusal, asserted positively. `toHaveProperty('error')` alone is
  // not enough: `?error=access_denied` carries no `code`, so with the provider-error guard deleted
  // the `!code` branch fires and an `error` property still comes back — the assertion would pass
  // for the wrong reason and the guard's deletion would be invisible. The message is this
  // function's only observable output, so it is what pins the guards and their order.
  const REFUSALS = {
    providerError: 'GitHub did not authorize the link. Nothing was connected — you can try again.',
    missingCode: 'Missing authorization code in the callback URL.',
    missingState: 'Missing state in the callback URL.',
    noStoredState: 'No pending GitHub link request — start again from your profile menu.',
    mismatch: 'State mismatch — the callback could not be verified. Please try again.',
  } as const;

  it('fails in order: provider error, missing code, missing state, no stored state, mismatch', () => {
    // Each input satisfies every EARLIER guard, so only the named one can produce the message —
    // swapping any two guards changes at least one of these five strings.
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

  it('reports the MISSING CODE first when both code and state are absent', () => {
    // The one input that can distinguish guard 2 from guard 3: with only one param missing each
    // ordering yields the same message, so a swap would be invisible. An empty callback (and a
    // GitHub decline, which carries neither) must blame the code — the first thing GitHub owes us.
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

  it('refuses a GitHub-returned error param (the human declined the authorization)', () => {
    // The decline callback is `?error=access_denied` with NO code. Reporting "Missing
    // authorization code" here would be true but actively misleading — that misreport is the
    // entire reason this guard exists, so the message is what must be asserted.
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

  it('reads the provider error param itself, not merely the absence of a code', () => {
    // A callback carrying a code, a state and a matching stored state satisfies all four
    // inherited guards — only reading `error` can refuse it.
    expect(resolveLinkCallbackParams('?error=some_other_failure&code=c&state=s', 's')).toEqual({
      error: REFUSALS.providerError,
    });
  });

  it('refuses an empty stored state (a consumed state reads as absent, never as a match)', () => {
    // The component clears the localStorage key unconditionally on read, so a second landing
    // sees '' or null — either must refuse, not re-submit the single-use code.
    expect(resolveLinkCallbackParams('?code=c&state=', '')).toEqual({
      error: REFUSALS.missingState,
    });
    // An empty stored state must read as ABSENT, never be compared as a value.
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

  it('never claims a "via" attribution string', () => {
    // GitHub documents an avatar + identicon badge and an audit field — no literal "via" label.
    const all = ALL_STATES.map((s) => JSON.stringify(linkCardCopy(s, 'octocat'))).join(' ');
    expect(all).not.toContain(' via ');
  });

  it('offers no action when the connection has no OAuth client', () => {
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
