export const LOGIN_HINT_KEY = 'agp.auth.loginHint';

export interface HintStorage {
  getItem(key: string): string | null;
  setItem(key: string, value: string): void;
  removeItem(key: string): void;
}

export function readHint(storage: HintStorage): string | undefined {
  const value = (storage.getItem(LOGIN_HINT_KEY) ?? '').trim();
  return value === '' ? undefined : value;
}

export function rememberHint(storage: HintStorage, username: string): void {
  storage.setItem(LOGIN_HINT_KEY, username);
}

export function forgetHint(storage: HintStorage): void {
  storage.removeItem(LOGIN_HINT_KEY);
}

export function ssoSilentRequest(
  scopes: string[],
  hint: string | undefined,
): { scopes: string[]; loginHint?: string } {
  return hint === undefined ? { scopes } : { scopes, loginHint: hint };
}

function withoutTrailingSlash(path: string): string {
  return path.endsWith('/') ? path.slice(0, -1) : path;
}

export function isRedirectBridgePage(pathname: string, redirectUri: string, base?: string): boolean {
  const configured = withoutTrailingSlash(new URL(redirectUri, base).pathname);
  return configured !== '' && withoutTrailingSlash(pathname) === configured;
}

export function silentFailureMessage(err: unknown): string {
  if (typeof err === 'object' && err !== null) {
    if ('errorCode' in err && typeof err.errorCode === 'string' && err.errorCode !== '') {
      return `no silent session: ${err.errorCode}`;
    }
    return `no silent session: ${err.constructor?.name ?? 'object'}`;
  }
  return `no silent session: ${String(err)}`;
}
