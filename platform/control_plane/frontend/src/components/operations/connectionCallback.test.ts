import { describe, it, expect } from 'vitest';
import { resolveCallbackParams } from './connectionCallbackParams';

describe('resolveCallbackParams', () => {
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

  it('errors when code is missing', () => {
    const params = new URLSearchParams({ state: 'csrf-abc' });
    const out = resolveCallbackParams(params, 'csrf-abc');
    expect('error' in out).toBe(true);
  });

  it('errors when state is missing', () => {
    const params = new URLSearchParams({ code: 'gh-code-123' });
    const out = resolveCallbackParams(params, 'csrf-abc');
    expect('error' in out).toBe(true);
  });

  it('errors when the session state is missing (nothing was stored)', () => {
    const params = new URLSearchParams({ code: 'gh-code-123', state: 'csrf-abc' });
    const out = resolveCallbackParams(params, null);
    expect('error' in out).toBe(true);
  });

  it('errors on a CSRF state mismatch', () => {
    const params = new URLSearchParams({ code: 'gh-code-123', state: 'csrf-abc' });
    const out = resolveCallbackParams(params, 'csrf-different');
    expect('error' in out).toBe(true);
  });
});
