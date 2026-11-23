import { describe, it, expect } from 'vitest';
import { resolveCallbackParams } from './connectionCallbackParams';

describe('resolveCallbackParams — the CSRF echo decides whether the single-use code may be converted', () => {
  it('returns code + state on the happy path (state echoes the stored session value)', () => {
    const params = new URLSearchParams({ code: 'gh-code-123', state: 'csrf-abc' });
    expect(resolveCallbackParams(params, 'csrf-abc')).toEqual({
      code: 'gh-code-123',
      state: 'csrf-abc',
    });
  });

  it('accepts a raw query string as well as a URLSearchParams', () => {
    expect(resolveCallbackParams('?code=gh-code-123&state=csrf-abc', 'csrf-abc')).toEqual({
      code: 'gh-code-123',
      state: 'csrf-abc',
    });
  });

  it('errors when code is missing, naming the callback URL', () => {
    const params = new URLSearchParams({ state: 'csrf-abc' });
    expect(resolveCallbackParams(params, 'csrf-abc')).toEqual({
      error: 'Missing authorization code in the callback URL.',
    });
  });

  it('errors when state is missing, naming the callback URL', () => {
    const params = new URLSearchParams({ code: 'gh-code-123' });
    expect(resolveCallbackParams(params, 'csrf-abc')).toEqual({ error: 'Missing state in the callback URL.' });
  });

  it('errors when the session state is missing (nothing was stored), sending the operator back to Org Connections', () => {
    const params = new URLSearchParams({ code: 'gh-code-123', state: 'csrf-abc' });
    expect(resolveCallbackParams(params, null)).toEqual({
      error: 'No pending GitHub App request — start the connection from Org Connections.',
    });
  });

  it('errors on a CSRF state mismatch and never returns the code, so the caller cannot convert it', () => {
    const params = new URLSearchParams({ code: 'gh-code-123', state: 'csrf-abc' });
    const out = resolveCallbackParams(params, 'csrf-different');
    expect(out).toEqual({ error: 'State mismatch — the callback could not be verified. Please try again.' });
    expect('code' in out).toBe(false);
  });

  it('reports the first failure only, in the order code, state, stored state, match', () => {
    expect(resolveCallbackParams(new URLSearchParams(), null)).toEqual({
      error: 'Missing authorization code in the callback URL.',
    });
    expect(resolveCallbackParams(new URLSearchParams({ code: 'c' }), null)).toEqual({
      error: 'Missing state in the callback URL.',
    });
    expect(resolveCallbackParams(new URLSearchParams({ code: 'c', state: 's' }), null)).toEqual({
      error: 'No pending GitHub App request — start the connection from Org Connections.',
    });
  });
});
