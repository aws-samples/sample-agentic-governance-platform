// authRetry.test.ts — the BOUNDED 401 recovery (E36/T19, research item 10).
//
// WHY THIS FILE IS THE CONTRACT. The old interceptor answered every 401 the same way —
// clear the token, `window.location.reload()` — which recovers EXPIRY and nothing else. On a
// STRUCTURAL 401 (wrong audience, roles on the wrong app registration, the wrong scope baked
// into the build) the fresh token is rejected identically, so the browser loops 401 → reload
// → 401 for as long as the tab is open. `vitest` has no DOM here, so the decision is
// extracted and the `sessionStorage`/`window` calls stay in `client.ts`; what this file pins
// is therefore everything that is not IO:
//
//   • the BUDGET is exactly one reload per session — `decide401` is the only thing that says
//     so, and off-by-one in either direction is the whole defect (0 reloads = expiry never
//     recovers; 2+ = still a loop, just slower);
//   • `decide401` is TOTAL. Its input comes back off `sessionStorage`, which is a string a
//     user can edit, so a garbage counter must resolve to a decision rather than throw
//     inside an error interceptor;
//   • the two MESSAGES, verbatim. `Session expired. Please log in again.` is quoted in
//     `docs/token-propagation.md` §2 and is the string this interceptor has always rejected
//     with, so it must not drift; the halt message is new copy that has to stop a user
//     retrying something that cannot work. Copy has no compiler — the `cedarPosture.ts`
//     precedent — so it is pinned here;
//   • the storage KEY, verbatim. It is a cross-page contract (the value is written before a
//     reload and read after it), so renaming it silently restores the unbounded loop.

//   • since fix round 1, the SHAPE of the two IO arms as well (D-4). `client.ts` cannot be
//     imported here (it pulls in axios, and the arms touch `sessionStorage`/`window`), so
//     "the success path resets the counter" and "the halt path does not reload" were verified by
//     reading alone. They are now read MECHANICALLY, on the `repoRow.test.ts:593` /
//     `repositoryDetailTabs.test.ts:2420` idiom: the source as text, raw — no lowercasing, no
//     comment stripping, because normalizing is how a guard stops seeing what it guards. The
//     comments in the guarded files are written not to quote the strings forbidden below.

//   • since the final review's F1, that no storage access in either guarded file is RAW. The
//     wrapper only helps if it is the ONLY spelling: one surviving `sessionStorage.` call is the
//     whole defect back, and it would be back in the one file this lane cannot import. So the
//     guard is stated negatively (no raw call anywhere) as well as positively (the wrapper at
//     each of the four sites) — a positive-only guard passes happily while a second, unwrapped
//     call sits three lines below the wrapped one.

import { afterEach, describe, expect, it, vi } from 'vitest';

import {
  AUTH_401_COUNT_KEY,
  AUTH_401_HALT_MESSAGE,
  AUTH_401_RELOAD_MESSAGE,
  clearsAuth401Suspicion,
  decide401,
  readAttempts,
  safeStorage,
} from './authRetry';
// Vite's `?raw` rather than `node:fs`: `tsconfig.app.json` sets `types: ["vite/client"]` (no
// `node`), so `node:fs` has no declarations here while `vite/client` declares `*?raw`. The
// explicit extension is load-bearing on a case-insensitive filesystem.
import clientSrc from './client.ts?raw';
import entraProviderSrc from '../auth/EntraProvider.tsx?raw';

describe('decide401 — one reload per session, then stop', () => {
  it('reloads on the first 401 of the session', () => {
    // Expiry is the common case and reloading genuinely fixes it: MSAL still holds the
    // account, so the provider re-acquires silently. Refusing to reload here would turn
    // every ordinary token expiry into a dead page.
    expect(decide401(0)).toBe('reload');
  });

  it('halts on the second', () => {
    // A 401 that survives a reload is not expiry. Reloading again cannot change the outcome,
    // because the input that produced the rejected token is baked into the bundle.
    expect(decide401(1)).toBe('halt');
  });

  it.each([2, 3, 10, 1000])('stays halted at %i attempts', (attempts) => {
    expect(decide401(attempts)).toBe('halt');
  });
});

describe('decide401 — total, because its input is an editable string', () => {
  // It is called from inside an axios ERROR interceptor: a throw here would replace the 401
  // rejection with a different error and lose the diagnostic. Every case below resolves.
  it.each([-1, -0.5, 0.5, 0.999, Number.NaN])(
    'answers reload for %o (under one attempt, or no attempt count at all)',
    (attempts) => {
      // `reload` is the SAFE direction for a value we cannot trust: the reload path WRITES a
      // clean integer on its way out, so the very next 401 halts. Choosing `halt` here would
      // strand a user on a corrupt counter with no way back.
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
    // `sessionStorage.getItem` answers `null` for a key that was never written — the first
    // 401 of a session, and the state after any successful response clears it.
    expect(readAttempts(null)).toBe(0);
    expect(readAttempts(undefined)).toBe(0);
    expect(readAttempts('')).toBe(0);
    expect(readAttempts('   ')).toBe(0);
  });

  it.each(['abc', 'NaN', 'Infinity', '-1', '-99', '1,2'])(
    'treats garbage (%o) as no attempts rather than guessing',
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
    // The two calls the interceptor makes, in order, for the two states that matter.
    expect(decide401(readAttempts(null))).toBe('reload');
    expect(decide401(readAttempts('1'))).toBe('halt');
  });
});

describe('the storage key', () => {
  it('is the pinned literal', () => {
    // Written before a reload and read after it, so this is a contract between two page
    // loads, not a local detail. It is also namespaced (`agp.`) because it shares
    // `sessionStorage` with MSAL's own cache (`msalConfig.ts` sets `cacheLocation`).
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
    // The whole point of halting is that "log in again" is false advice here. If the halt
    // copy ever inherits the expiry copy's instruction, the loop is back — in the user's
    // hands instead of the browser's.
    expect(AUTH_401_HALT_MESSAGE).not.toContain('Please log in again');
    expect(AUTH_401_HALT_MESSAGE).not.toBe(AUTH_401_RELOAD_MESSAGE);
  });

  it('says the cause is configuration, not the session', () => {
    // `docs/token-propagation.md` §11 sends an operator looking for exactly these three
    // causes; naming them is what makes the message forwardable to someone who can act.
    expect(AUTH_401_HALT_MESSAGE).toContain('audience');
    expect(AUTH_401_HALT_MESSAGE).toContain('scope');
    expect(AUTH_401_HALT_MESSAGE).toContain('role assignments');
  });

  it('leaks no token, id or URL', () => {
    // It is rendered to an end user through `err.message` like any other API error.
    expect(AUTH_401_HALT_MESSAGE).not.toMatch(/Bearer|eyJ|http/i);
  });
});

describe('clearsAuth401Suspicion — only an AUTHENTICATED 2xx retires the count (D-3)', () => {
  it('accepts a bearer header', () => {
    expect(clearsAuth401Suspicion('Bearer abc.def.ghi')).toBe(true);
  });

  it('refuses a request that carried no Authorization header', () => {
    // The case that matters: an unauthenticated 2xx is evidence the SERVER is up, never that
    // THIS TOKEN is accepted. `healthApi`'s route takes no principal, so a status poll on it
    // would otherwise clear the counter on every tick and un-bound the loop.
    expect(clearsAuth401Suspicion(undefined)).toBe(false);
    expect(clearsAuth401Suspicion(null)).toBe(false);
    expect(clearsAuth401Suspicion('')).toBe(false);
    expect(clearsAuth401Suspicion('   ')).toBe(false);
  });

  it.each([0, 1, true, false, {}, [], ['Bearer x']])(
    'refuses the non-string %o axios could leave on a config',
    (header) => {
      // TOTAL, on `readAttempts`' reasoning — it reads whatever is on the request config, and
      // "not a bearer" is the conservative answer: failing to clear costs one extra reload,
      // clearing wrongly costs the bound.
      expect(clearsAuth401Suspicion(header)).toBe(false);
    },
  );
});

// ---------------------------------------------------------------------------
// The two IO arms, read as source (D-4). Both behaviours are `client.ts`-only and this lane
// cannot import that file, so these guards are the ONLY thing standing between either arm and a
// silent regression — including regressions of D-1 and D-3, which is what makes them worth their
// fragility.
// ---------------------------------------------------------------------------
describe('client.ts success arm — resets the counter, and only on an authenticated 2xx', () => {
  it('clears the key on the success path', () => {
    // Previously the identity function: nothing cleared the count, so the budget was
    // per-session rather than per-incident.
    expect(clientSrc).toMatch(
      /if \(clearsAuth401Suspicion\(response\.config\.headers\?\.Authorization\)\) \{\s*safeStorage\.remove\(AUTH_401_COUNT_KEY\);/,
    );
  });

  it('asks the shared predicate rather than re-deciding in the arm', () => {
    // A hand-rolled truthiness check here would be a second spelling of the rule, in the one
    // place no test can reach — which is exactly how D-3 became invisible.
    expect(clientSrc.match(/clearsAuth401Suspicion\(/g)).toHaveLength(1);
    expect(clientSrc).toMatch(/clearsAuth401Suspicion,/);
  });
});

describe('client.ts 401 arm — one decision per page load, and halt never reloads', () => {
  it('latches the decision in module scope (D-1)', () => {
    // The reload discards module state, which is what makes the latch safe AND what makes it
    // insufficient on its own — hence both this and the sessionStorage counter.
    expect(clientSrc).toMatch(/let latched401: 'reload' \| 'halt' \| null = null;/);
  });

  it('reads the counter and decides ONCE, behind the latch', () => {
    // The burst case: `Home.tsx` fires two list calls in a `Promise.all`, so a per-request
    // decision let the second request read the 1 its sibling had just written and halt during
    // the FIRST incident — a false "check the sign-in configuration" on an ordinary expiry.
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
    // THE defect this whole task exists to bound: a halt that still reloaded would be the
    // unbounded loop with extra steps. Both halves are asserted because either alone leaves it
    // live — a single call site could still be the wrong one, and a guarded call site says
    // nothing about a second unguarded one.
    expect(clientSrc.match(/window\.location\.reload/g)).toHaveLength(1);
    expect(clientSrc).toMatch(
      /if \(latched401 === 'halt'\) \{[^}]*return Promise\.reject\(new Error\(AUTH_401_HALT_MESSAGE\)\);/,
    );
  });
});

describe('EntraProvider — a freshly issued token retires the suspicion (D-2)', () => {
  it('clears the count right after storing a successfully acquired token', () => {
    // Ordering, not mere presence: the reset belongs to the SUCCESS path. In the `catch` it
    // would forgive the tokenless mount it is meant to rescue the user from.
    const stored = entraProviderSrc.indexOf("localStorage.setItem('auth_token', result.accessToken);");
    const cleared = entraProviderSrc.indexOf('safeStorage.remove(AUTH_401_COUNT_KEY);');
    const caught = entraProviderSrc.indexOf('} catch (err) {');
    expect(stored).toBeGreaterThan(-1);
    expect(cleared).toBeGreaterThan(stored);
    expect(cleared).toBeLessThan(caught);
  });

  it('imports the key and the wrapper instead of retyping either', () => {
    // A second spelling of a cross-page-load contract restores the loop silently.
    expect(entraProviderSrc).toMatch(
      /import \{ AUTH_401_COUNT_KEY, safeStorage \} from '\.\.\/api\/authRetry';/,
    );
    expect(entraProviderSrc).not.toMatch(/'agp\.auth\.401count'/);
  });
});

// ---------------------------------------------------------------------------
// safeStorage — the F1 fix. Storage may THROW, and both call sites are on a SUCCESS path.
// ---------------------------------------------------------------------------
describe('safeStorage — no storage condition may reach the auth flow (F1 / D-5)', () => {
  afterEach(() => vi.unstubAllGlobals());

  // A hostile storage: exists, and refuses. This is Safari with "block all cookies", a
  // storage-blocked iframe, or a full quota — `SecurityError`/`QuotaExceededError`, thrown
  // from the accessor itself. `uiPreference.test.ts:34` is the precedent for the shape.
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
    // Asserted first and deliberately: a wrapper that silently did nothing would satisfy every
    // "never throws" test below while quietly deleting the bound it exists to preserve.
    const store = installWorkingStorage();
    safeStorage.write(AUTH_401_COUNT_KEY, '1');
    expect(store.get(AUTH_401_COUNT_KEY)).toBe('1');
    expect(safeStorage.read(AUTH_401_COUNT_KEY)).toBe('1');
    safeStorage.remove(AUTH_401_COUNT_KEY);
    expect(store.has(AUTH_401_COUNT_KEY)).toBe(false);
    expect(safeStorage.read(AUTH_401_COUNT_KEY)).toBeNull();
  });

  it('never throws when storage THROWS', () => {
    // THE finding. `EntraProvider`'s call sits inside the `try` whose `catch` does
    // `setToken(null)` + `localStorage.removeItem('auth_token')`, so a throw here converts a
    // token acquired one line earlier into a tokenless mount; `client.ts`' call sits in the
    // SUCCESS interceptor, so a throw there rejects an otherwise-2xx response app-wide.
    installThrowingStorage();
    expect(() => safeStorage.read(AUTH_401_COUNT_KEY)).not.toThrow();
    expect(() => safeStorage.write(AUTH_401_COUNT_KEY, '1')).not.toThrow();
    expect(() => safeStorage.remove(AUTH_401_COUNT_KEY)).not.toThrow();
  });

  it('never throws when there is no storage global at all', () => {
    // No stub installed — this lane runs under NODE, so the absent case is simply the default
    // state here and needs no mock to reach. It is also the SSR/prerender case.
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
    // The documented degradation, spelled out as the composition the interceptor performs: with
    // no readable counter the cross-page-load bound is gone and only `latched401` remains. That
    // is the deliberate direction — halting instead would strand every blocked-storage tab on
    // "ask an administrator" for an ordinary expiry a reload fixes.
    expect(decide401(readAttempts(stored))).toBe('reload');
  });
});

describe('no storage access in either guarded file is RAW (F1)', () => {
  // The wrapper only helps if it is the ONLY spelling. A single surviving `sessionStorage.`
  // call re-arms the finding, in the one file this DOM-less lane cannot import — so this is
  // stated as an absence, which is the only form that catches a NEW unwrapped call. The
  // comments in both guarded files are written not to spell a bare `sessionStorage.` access.
  it.each<[string, string]>([
    ['client.ts', clientSrc],
    ['EntraProvider.tsx', entraProviderSrc],
  ])('%s makes no direct sessionStorage call', (_name, src) => {
    // Property access in either spelling — `sessionStorage.getItem` and `sessionStorage['x']`.
    expect(src).not.toMatch(/sessionStorage\s*[.[]/);
  });

  it('routes all four call sites through the wrapper', () => {
    // Counted, not merely present: three in `client.ts` (success-arm clear, 401-arm read,
    // reload-branch write) and one in `EntraProvider.tsx` (fresh-token clear).
    expect(clientSrc.match(/safeStorage\.(read|write|remove)\(/g)).toHaveLength(3);
    expect(entraProviderSrc.match(/safeStorage\.(read|write|remove)\(/g)).toHaveLength(1);
  });
});
