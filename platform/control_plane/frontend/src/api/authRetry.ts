// Rationale: docs/token-propagation.md §2 (401 budget)

export const AUTH_401_COUNT_KEY = 'agp.auth.401count';

export const AUTH_401_RELOAD_MESSAGE = 'Session expired. Please log in again.';

export const AUTH_401_HALT_MESSAGE =
  'Sign-in is not working for this deployment. Reloading did not help, so signing in ' +
  'again will not either: a new token is rejected the same way. Ask an administrator ' +
  'to check the sign-in configuration — the token audience, the requested scope, and ' +
  'the app registration holding the role assignments.';

export function readAttempts(stored: string | null | undefined): number {
  const parsed = Number((stored ?? '').trim());
  return Number.isFinite(parsed) && parsed > 0 ? Math.floor(parsed) : 0;
}

export function decide401(attempts: number): 'reload' | 'halt' {
  return attempts >= 1 ? 'halt' : 'reload';
}

export function coalesceAsync<T>(fn: () => Promise<T>): () => Promise<T> {
  let inFlight: Promise<T> | null = null;
  return () => {
    if (!inFlight) {
      inFlight = fn().finally(() => {
        inFlight = null;
      });
    }
    return inFlight;
  };
}

export function clearsAuth401Suspicion(authorizationHeader: unknown): boolean {
  return typeof authorizationHeader === 'string' && authorizationHeader.trim().length > 0;
}

export const safeStorage = {
  read(key: string): string | null {
    if (typeof sessionStorage === 'undefined') return null;
    try {
      return sessionStorage.getItem(key);
    } catch {
      return null;
    }
  },

  write(key: string, value: string): void {
    if (typeof sessionStorage === 'undefined') return;
    try {
      sessionStorage.setItem(key, value);
    } catch {
      // storage blocked or over quota
    }
  },

  remove(key: string): void {
    if (typeof sessionStorage === 'undefined') return;
    try {
      sessionStorage.removeItem(key);
    } catch {
      // storage blocked
    }
  },
};
