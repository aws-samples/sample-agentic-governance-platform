import { afterEach, describe, expect, it, vi } from 'vitest';

import {
  AUTH_401_COUNT_KEY,
  AUTH_401_HALT_MESSAGE,
  AUTH_401_RELOAD_MESSAGE,
  clearsAuth401Suspicion,
  coalesceAsync,
  decide401,
  readAttempts,
  safeStorage,
} from './authRetry';
import { stripComments } from '../rawSource';
import clientRaw from './client.ts?raw';
import entraProviderRaw from '../auth/EntraProvider.tsx?raw';
import tokenBrokerRaw from '../auth/tokenBroker.ts?raw';

const clientSrc = stripComments(clientRaw);
const entraProviderSrc = stripComments(entraProviderRaw);
const tokenBrokerSrc = stripComments(tokenBrokerRaw);

describe('decide401 — one reload per session, then stop', () => {
  it('reloads on the first 401 of the session', () => {
    expect(decide401(0)).toBe('reload');
  });

  it('halts on the second', () => {
    expect(decide401(1)).toBe('halt');
  });

  it.each([2, 3, 10, 1000])('stays halted at %i attempts', (attempts) => {
    expect(decide401(attempts)).toBe('halt');
  });

  it('the reload budget is exactly one per session', () => {
    const first = readAttempts(null);
    expect(decide401(first)).toBe('reload');
    const second = readAttempts(String(first + 1));
    expect(decide401(second)).toBe('halt');
    expect(decide401(readAttempts(String(second + 1)))).toBe('halt');
  });
});

describe('decide401 — total, because its input is an editable string', () => {
  it.each([-1, -0.5, 0.5, 0.999, Number.NaN])(
    'answers reload for %o (under one attempt, or no attempt count at all): the safe direction, since the reload path writes a clean integer',
    (attempts) => {
      expect(decide401(attempts)).toBe('reload');
    },
  );

  it.each([1.5, Number.POSITIVE_INFINITY])('answers halt for %o (at or past one)', (attempts) => {
    expect(decide401(attempts)).toBe('halt');
  });
});

describe('readAttempts — normalising what came out of sessionStorage', () => {
  it('reads a written counter', () => {
    expect(readAttempts('0')).toBe(0);
    expect(readAttempts('1')).toBe(1);
    expect(readAttempts('7')).toBe(7);
  });

  it('treats an absent key as no attempts', () => {
    expect(readAttempts(null)).toBe(0);
    expect(readAttempts(undefined)).toBe(0);
    expect(readAttempts('')).toBe(0);
    expect(readAttempts('   ')).toBe(0);
  });

  it.each(['abc', 'NaN', 'Infinity', '-1', '-99', '1,2'])(
    'readAttempts is total on garbage input: %o reads as no attempts rather than guessing',
    (stored) => {
      expect(readAttempts(stored)).toBe(0);
    },
  );

  it('floors a fractional value instead of propagating it', () => {
    expect(readAttempts('1.7')).toBe(1);
  });

  it('tolerates padding', () => {
    expect(readAttempts('  2  ')).toBe(2);
  });

  it('composes with decide401 into the documented behaviour', () => {
    expect(decide401(readAttempts(null))).toBe('reload');
    expect(decide401(readAttempts('1'))).toBe('halt');
  });
});

describe('coalesceAsync — concurrent callers share ONE in-flight invocation', () => {
  it('calls fn exactly once for a burst of concurrent callers, and all get its answer', async () => {
    let calls = 0;
    const shared = coalesceAsync(async () => {
      calls += 1;
      return 'token-a';
    });
    const results = await Promise.all([shared(), shared(), shared()]);
    expect(calls).toBe(1);
    expect(results).toEqual(['token-a', 'token-a', 'token-a']);
  });

  it('calls fn again for a caller arriving AFTER the in-flight call has settled', async () => {
    let calls = 0;
    const shared = coalesceAsync(async () => {
      calls += 1;
      return `token-${calls}`;
    });
    expect(await shared()).toBe('token-1');
    expect(await shared()).toBe('token-2');
    expect(calls).toBe(2);
  });

  it('clears the slot on a REJECTION too, so one failure does not poison every later caller', async () => {
    let calls = 0;
    const shared = coalesceAsync(async () => {
      calls += 1;
      if (calls === 1) throw new Error('transient');
      return 'token-ok';
    });
    await expect(shared()).rejects.toThrow('transient');
    expect(await shared()).toBe('token-ok');
    expect(calls).toBe(2);
  });

  it('shares the SAME rejection across a burst that fails together', async () => {
    const err = new Error('structural');
    const shared = coalesceAsync(async () => {
      throw err;
    });
    const burst = [shared(), shared(), shared()];
    await Promise.allSettled(burst);
    for (const p of burst) await expect(p).rejects.toBe(err);
  });
});

describe('the storage key', () => {
  it('is the pinned literal: a contract between two page loads, namespaced apart from the MSAL cache it shares sessionStorage with', () => {
    expect(AUTH_401_COUNT_KEY).toBe('agp.auth.401count');
  });
});

describe('the two messages', () => {
  it('keeps the expiry message byte-identical to the one the docs quote', () => {
    expect(AUTH_401_RELOAD_MESSAGE).toBe('Session expired. Please log in again.');
  });

  it('is the reviewed halt copy, verbatim', () => {
    expect(AUTH_401_HALT_MESSAGE).toBe(
      'Sign-in is not working for this deployment. Reloading did not help, so signing in ' +
        'again will not either: a new token is rejected the same way. Ask an administrator ' +
        'to check the sign-in configuration — the token audience, the requested scope, and ' +
        'the app registration holding the role assignments.',
    );
  });

  it('does not tell the user to do the thing that cannot work', () => {
    expect(AUTH_401_HALT_MESSAGE).not.toContain('Please log in again');
    expect(AUTH_401_HALT_MESSAGE).not.toBe(AUTH_401_RELOAD_MESSAGE);
  });

  it('says the cause is configuration, not the session: the three causes docs/token-propagation.md §11 sends an operator to check', () => {
    expect(AUTH_401_HALT_MESSAGE).toContain('audience');
    expect(AUTH_401_HALT_MESSAGE).toContain('scope');
    expect(AUTH_401_HALT_MESSAGE).toContain('role assignments');
  });

  it('leaks no token, id or URL', () => {
    expect(AUTH_401_HALT_MESSAGE).not.toMatch(/Bearer|eyJ|http/i);
  });
});

describe('clearsAuth401Suspicion — only an authenticated 2xx retires the count', () => {
  it('accepts a bearer header', () => {
    expect(clearsAuth401Suspicion('Bearer abc.def.ghi')).toBe(true);
  });

  it('an unauthenticated 2xx does not clear the suspicion: a request that carried no Authorization header is refused', () => {
    expect(clearsAuth401Suspicion(undefined)).toBe(false);
    expect(clearsAuth401Suspicion(null)).toBe(false);
    expect(clearsAuth401Suspicion('')).toBe(false);
    expect(clearsAuth401Suspicion('   ')).toBe(false);
  });

  it.each([0, 1, true, false, {}, [], ['Bearer x']])(
    'refuses the non-string %o axios could leave on a config: failing to clear costs one reload, clearing wrongly costs the bound',
    (header) => {
      expect(clearsAuth401Suspicion(header)).toBe(false);
    },
  );
});

describe('client.ts success arm — resets the counter, and only on an authenticated 2xx', () => {
  it('clears the key on the success path', () => {
    expect(clientSrc).toMatch(
      /if \(clearsAuth401Suspicion\(response\.config\.headers\?\.Authorization\)\) \{\s*safeStorage\.remove\(AUTH_401_COUNT_KEY\);/,
    );
  });

  it('asks the shared predicate rather than re-deciding in the arm', () => {
    expect(clientSrc.match(/clearsAuth401Suspicion\(/g)).toHaveLength(1);
    expect(clientSrc).toMatch(/clearsAuth401Suspicion,/);
  });
});

describe('client.ts 401 arm — one decision per page load, and halt never reloads', () => {
  it('latches the decision in module scope, which the reload resets, so the sessionStorage counter carries the bound across it', () => {
    expect(clientSrc).toMatch(/let latched401: 'reload' \| 'halt' \| null = null;/);
  });

  it('reads the counter and decides once behind the latch, so a burst of concurrent 401s cannot halt on the write of a sibling request', () => {
    expect(clientSrc).toMatch(
      /if \(latched401 === null\) \{\s*const attempts = readAttempts\(safeStorage\.read\(AUTH_401_COUNT_KEY\)\);\s*latched401 = decide401\(attempts\);/,
    );
    expect(clientSrc.match(/readAttempts\(safeStorage\.read/g)).toHaveLength(1);
    expect(clientSrc.match(/decide401\(attempts\)/g)).toHaveLength(1);
  });

  it('writes the counter BEFORE the navigation it authorises, in the reload branch only', () => {
    expect(clientSrc).toMatch(
      /if \(latched401 === 'reload'\) \{[^}]*safeStorage\.write\(AUTH_401_COUNT_KEY, String\(attempts \+ 1\)\);[^}]*window\.location\.reload\(\);/,
    );
  });

  it('reloads in exactly one place, and it is not the halt branch', () => {
    expect(clientSrc.match(/window\.location\.reload/g)).toHaveLength(1);
    expect(clientSrc).toMatch(
      /if \(latched401 === 'halt'\) \{[^}]*return Promise\.reject\(new ApiFailure\(401, null, undefined, AUTH_401_HALT_MESSAGE\)\);/,
    );
  });

  it('the halt branch only rejects: auth_token stays', () => {
    expect(clientSrc).toMatch(
      /if \(latched401 === 'halt'\) \{\s*return Promise\.reject\(new ApiFailure\(401, null, undefined, AUTH_401_HALT_MESSAGE\)\);\s*\}/,
    );
  });
});

describe('client.ts 401 arm — one force-refreshed retry BEFORE the reload/halt breaker', () => {
  it('attempts the retry inside the 401 branch, ahead of the latch block', () => {
    const branch = clientSrc.indexOf('if (error.response?.status === 401) {');
    const retry = clientSrc.indexOf('_agpForceRefreshRetried');
    const latch = clientSrc.indexOf('if (latched401 === null) {');
    expect(branch).toBeGreaterThan(-1);
    expect(retry).toBeGreaterThan(branch);
    expect(retry).toBeLessThan(latch);
  });

  it('caps the retry at exactly one per request via a flag on the request config, so a second 401 falls through to the breaker', () => {
    expect(clientSrc).toMatch(/if \(config && !config\._agpForceRefreshRetried\) \{/);
    expect(clientSrc.match(/_agpForceRefreshRetried/g)?.length).toBeGreaterThanOrEqual(2);
  });

  it('retries through the SAME client instance, carrying the refreshed bearer', () => {
    expect(clientSrc).toMatch(/config\.headers\.Authorization = `Bearer \$\{refreshed\}`;/);
    expect(clientSrc).toMatch(/return client\(config\);/);
  });

  it('reaches MSAL only through a DYNAMIC import, so the DOM-less lane can still import this file', () => {
    expect(clientSrc).toMatch(/await import\('\.\.\/auth\/tokenBroker'\)/);
    expect(clientSrc).not.toMatch(/^import .* from '\.\.\/auth\/tokenBroker';/m);
  });

  it('reads the legacy localStorage mirror only when MSAL answered null, never as the source of truth', () => {
    expect(clientSrc).toMatch(/const fallbackToken = token \?\? safeLocalStorage\.getItem\('auth_token'\);/);
  });
});

describe('tokenBroker — one coalesced slot per refresh kind', () => {
  it('keeps acquire(false) and acquire(true) in two coalesceAsync slots, so a forced refresh never joins an in-flight cached call and a cached read never waits on a forced one', () => {
    expect(tokenBrokerSrc.match(/coalesceAsync\(\(\) => acquire\(false\)\)/g)).toHaveLength(1);
    expect(tokenBrokerSrc.match(/coalesceAsync\(\(\) => acquire\(true\)\)/g)).toHaveLength(1);
    expect(tokenBrokerSrc).toMatch(/opts\?\.forceRefresh \? acquireForced\(\) : acquireCached\(\)/);
  });

  it('never redirects from the request path: the provider owns interaction', () => {
    expect(tokenBrokerSrc).not.toMatch(/acquireTokenRedirect|loginRedirect/);
  });
});

describe('EntraProvider — a freshly issued token retires the suspicion', () => {
  it('clears the count right after storing a successfully acquired token, inside the try and before its catch', () => {
    const stored = entraProviderSrc.indexOf("localStorage.setItem('auth_token', result.accessToken);");
    const cleared = entraProviderSrc.indexOf('safeStorage.remove(AUTH_401_COUNT_KEY);');
    const caught = entraProviderSrc.indexOf('} catch (err) {');
    expect(stored).toBeGreaterThan(-1);
    expect(cleared).toBeGreaterThan(stored);
    expect(cleared).toBeLessThan(caught);
  });

  it('imports the key and the wrapper instead of retyping either', () => {
    expect(entraProviderSrc).toMatch(
      /import \{ AUTH_401_COUNT_KEY, safeStorage \} from '\.\.\/api\/authRetry';/,
    );
    expect(entraProviderSrc).not.toMatch(/'agp\.auth\.401count'/);
  });
});

describe('safeStorage — no storage condition may reach the auth flow', () => {
  afterEach(() => vi.unstubAllGlobals());

  function installThrowingStorage() {
    vi.stubGlobal('sessionStorage', {
      getItem: () => {
        throw new Error('SecurityError');
      },
      setItem: () => {
        throw new Error('QuotaExceededError');
      },
      removeItem: () => {
        throw new Error('SecurityError');
      },
    });
  }

  function installWorkingStorage(initial: Record<string, string> = {}) {
    const store = new Map(Object.entries(initial));
    vi.stubGlobal('sessionStorage', {
      getItem: (k: string) => (store.has(k) ? store.get(k)! : null),
      setItem: (k: string, v: string) => void store.set(k, v),
      removeItem: (k: string) => void store.delete(k),
    });
    return store;
  }

  it('round-trips through a working storage — the wrapper is not a stub', () => {
    const store = installWorkingStorage();
    safeStorage.write(AUTH_401_COUNT_KEY, '1');
    expect(store.get(AUTH_401_COUNT_KEY)).toBe('1');
    expect(safeStorage.read(AUTH_401_COUNT_KEY)).toBe('1');
    safeStorage.remove(AUTH_401_COUNT_KEY);
    expect(store.has(AUTH_401_COUNT_KEY)).toBe(false);
    expect(safeStorage.read(AUTH_401_COUNT_KEY)).toBeNull();
  });

  it('never throws when storage throws, so a blocked storage can neither reject a 2xx nor void a freshly acquired token', () => {
    installThrowingStorage();
    expect(() => safeStorage.read(AUTH_401_COUNT_KEY)).not.toThrow();
    expect(() => safeStorage.write(AUTH_401_COUNT_KEY, '1')).not.toThrow();
    expect(() => safeStorage.remove(AUTH_401_COUNT_KEY)).not.toThrow();
  });

  it('never throws when there is no storage global at all, the default state of the node lane', () => {
    expect(() => safeStorage.read(AUTH_401_COUNT_KEY)).not.toThrow();
    expect(() => safeStorage.write(AUTH_401_COUNT_KEY, '1')).not.toThrow();
    expect(() => safeStorage.remove(AUTH_401_COUNT_KEY)).not.toThrow();
  });

  it.each<[string, () => void]>([
    ['throwing', installThrowingStorage],
    ['absent', () => {}],
  ])('reads as null when storage is %s, so the breaker degrades to reload', (_label, install) => {
    install();
    const stored = safeStorage.read(AUTH_401_COUNT_KEY);
    expect(stored).toBeNull();
    expect(decide401(readAttempts(stored))).toBe('reload');
  });
});

describe('no storage access in either guarded file is raw: the wrapper is the only spelling', () => {
  it.each<[string, string]>([
    ['client.ts', clientSrc],
    ['EntraProvider.tsx', entraProviderSrc],
  ])('%s makes no direct sessionStorage call', (_name, src) => {
    expect(src).not.toMatch(/sessionStorage\s*[.[]/);
  });

  it('routes all four call sites through the wrapper: three in client.ts, one in EntraProvider.tsx', () => {
    expect(clientSrc.match(/safeStorage\.(read|write|remove)\(/g)).toHaveLength(3);
    expect(entraProviderSrc.match(/safeStorage\.(read|write|remove)\(/g)).toHaveLength(1);
  });
});
