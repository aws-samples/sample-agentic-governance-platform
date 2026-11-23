import { describe, expect, it } from 'vitest';

import { resolveApiScope, resolveRedirectUri } from './authEnv';
import {
  type HintStorage,
  LOGIN_HINT_KEY,
  forgetHint,
  isRedirectBridgePage,
  readHint,
  rememberHint,
  silentFailureMessage,
  ssoSilentRequest,
} from './silentSignIn';

function mapStorage(initial: Record<string, string> = {}): HintStorage & { map: Map<string, string> } {
  const map = new Map(Object.entries(initial));
  return {
    map,
    getItem: (key) => map.get(key) ?? null,
    setItem: (key, value) => {
      map.set(key, value);
    },
    removeItem: (key) => {
      map.delete(key);
    },
  };
}

describe('silentSignIn', () => {
  it('readHint returns undefined for a missing or blank value and the trimmed username otherwise', () => {
    expect(readHint(mapStorage())).toBeUndefined();
    expect(readHint(mapStorage({ [LOGIN_HINT_KEY]: '' }))).toBeUndefined();
    expect(readHint(mapStorage({ [LOGIN_HINT_KEY]: '   ' }))).toBeUndefined();
    expect(readHint(mapStorage({ [LOGIN_HINT_KEY]: 'a@b' }))).toBe('a@b');
    expect(readHint(mapStorage({ [LOGIN_HINT_KEY]: '  a@b\n' }))).toBe('a@b');
  });

  it('rememberHint and forgetHint write and remove LOGIN_HINT_KEY on the storage they are handed', () => {
    const storage = mapStorage();
    rememberHint(storage, 'a@b');
    expect(storage.map.get(LOGIN_HINT_KEY)).toBe('a@b');
    expect([...storage.map.keys()]).toEqual([LOGIN_HINT_KEY]);
    forgetHint(storage);
    expect(storage.map.has(LOGIN_HINT_KEY)).toBe(false);
    expect(LOGIN_HINT_KEY).toBe('agp.auth.loginHint');
  });

  it('ssoSilentRequest carries loginHint only when a hint exists', () => {
    const scopes = ['api://agp/Access.Default'];
    const bare = ssoSilentRequest(scopes, undefined);
    expect(bare).toEqual({ scopes });
    expect('loginHint' in bare).toBe(false);
    expect(bare.scopes).toBe(scopes);
    expect(ssoSilentRequest(scopes, 'a@b')).toEqual({ scopes, loginHint: 'a@b' });
  });

  it('isRedirectBridgePage matches the configured path with or without a trailing slash', () => {
    const redirectUri = 'http://localhost:5173/auth/callback';
    expect(isRedirectBridgePage('/auth/callback', redirectUri)).toBe(true);
    expect(isRedirectBridgePage('/auth/callback/', redirectUri)).toBe(true);
    expect(isRedirectBridgePage('/', redirectUri)).toBe(false);
    expect(isRedirectBridgePage('/ops/projects', redirectUri)).toBe(false);
    expect(isRedirectBridgePage('/auth/callback', 'https://app.example.invalid/auth/callback')).toBe(true);
    expect(isRedirectBridgePage('/auth/callback', 'https://app.example.invalid/auth/callback/')).toBe(true);
    expect(isRedirectBridgePage('/auth/callback', '/auth/callback', 'http://localhost:5173')).toBe(true);
    expect(isRedirectBridgePage('/ops/projects', '/auth/callback', 'http://localhost:5173')).toBe(false);
    expect(() => isRedirectBridgePage('/auth/callback', '/auth/callback')).toThrow(TypeError);
    expect(isRedirectBridgePage('/', 'http://localhost:5173/')).toBe(false);
    expect(isRedirectBridgePage('/', 'http://localhost:5173')).toBe(false);
    expect(isRedirectBridgePage('/ops/projects', 'http://localhost:5173/')).toBe(false);
  });

  it('silentFailureMessage names the MSAL error code or the class', () => {
    expect(silentFailureMessage({ errorCode: 'interaction_required' })).toBe(
      'no silent session: interaction_required',
    );
    expect(silentFailureMessage(new TypeError('x'))).toBe('no silent session: TypeError');
    expect(silentFailureMessage('odd')).toBe('no silent session: odd');
    expect(() => silentFailureMessage(Object.create(null))).not.toThrow();
    expect(silentFailureMessage(Object.create(null))).toBe('no silent session: object');
  });
});

describe('authEnv', () => {
  it('resolveRedirectUri and resolveApiScope fall back to the code defaults on an empty or missing value', () => {
    expect(resolveRedirectUri({})).toBe('http://localhost:5173/auth/callback');
    expect(resolveRedirectUri({ VITE_ENTRA_SPA_REDIRECT_URI: '' })).toBe('http://localhost:5173/auth/callback');
    expect(resolveRedirectUri({ VITE_ENTRA_SPA_REDIRECT_URI: '   ' })).toBe('http://localhost:5173/auth/callback');
    expect(resolveRedirectUri({ VITE_ENTRA_SPA_REDIRECT_URI: ' https://app.example.invalid/auth/callback ' })).toBe(
      'https://app.example.invalid/auth/callback',
    );
    expect(resolveApiScope({})).toBe('api://agp/Access.Default');
    expect(resolveApiScope({ VITE_ENTRA_SPA_SCOPE: '' })).toBe('api://agp/Access.Default');
    expect(resolveApiScope({ VITE_ENTRA_SPA_SCOPE: ' api://other/Scope ' })).toBe('api://other/Scope');
  });
});
