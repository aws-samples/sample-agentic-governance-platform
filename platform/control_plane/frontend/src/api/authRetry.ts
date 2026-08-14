// authRetry — the BOUNDED 401 recovery (E36/T19, research item 10).
//
// Pure, framework-free, `.test.ts`-pinned — the `opsStatus.ts` / `cedarPosture.ts` idiom.
// `vitest.config.ts` collects `src/**/*.test.ts` under the NODE environment: no jsdom, no
// happy-dom, so there is no `sessionStorage` and no `window` in the test lane and `client.ts`
// pulls in axios besides. Every DECISION therefore lives here, which is what makes the budget
// below assertable at all, and the `window` IO stays in `client.ts`.
//
// `safeStorage` at the bottom is the ONE deliberate IO exception, and the DOM-less lane is the
// reason rather than an obstacle: its entire job is to survive `sessionStorage` being absent or
// throwing, and under NODE the ABSENT case is the lane's DEFAULT state — so that branch is
// exercised by simply calling it, with no mock and no way for the mock to be wrong. The THROWING
// case is the `vi.stubGlobal` half, on the `uiPreference.test.ts` precedent.
//
// ---------------------------------------------------------------------------
// WHY THIS MODULE EXISTS
//
// One axios instance is the SOLE egress to the API, so its response interceptor is the only
// 401 handler in the SPA — and it answered every 401 identically: clear `auth_token`, then
// `window.location.reload()`.
//
// That recovers EXPIRY, and only expiry. MSAL still holds the account, so the reload
// re-acquires a token silently and the call succeeds. But when the cause is STRUCTURAL —
// mismatched audience, roles assigned on the wrong app registration, the wrong scope baked
// into the bundle — the freshly acquired token is rejected in exactly the same way, and the
// browser loops 401 → reload → 401 for as long as the tab is open. `UserContext.tsx:32-38`
// guards ONE call site (it skips `/users/me` when there is no token); nothing guards the
// general case.
//
// The loop is also self-obscuring, which is why bounding it is worth a module: the page never
// settles long enough to render an error, so the most diagnosable auth failure in the system
// (the 401 body literally lists the audience it expected) presents as a flickering blank page.
//
// ---------------------------------------------------------------------------
// WHY THE BUDGET IS EXACTLY ONE
//
// Zero reloads would break the common case — an ordinary expiry would become a dead page, and
// the silent re-acquire is the mechanism this SPA relies on instead of refresh-and-retry. Two
// or more would still be a loop, just slower, and each iteration destroys whatever the user
// had on screen. One reload distinguishes the two causes perfectly: expiry survives it,
// structural failure does not.
//
// The counter lives in `sessionStorage` (written by `client.ts`, key below) for two reasons:
// it must survive the reload it authorises — a module-scope variable would be reset by the very
// navigation it is counting, which is the bug that makes this look like it needs no storage —
// and it must NOT outlive the tab, so a genuinely fixed tenant recovers by itself rather than
// needing an explicit reset path a user cannot be talked through.
//
// RESET ON AN AUTHENTICATED SUCCESSFUL RESPONSE: a 2xx on a request that carried a bearer is
// proof the backend accepted THIS token, which is the only evidence that actually retires the
// suspicion. It also keeps the budget per-INCIDENT rather than per-session, so a user whose
// token expires twice in one long session gets a reload both times. An UNAUTHENTICATED 2xx says
// nothing about the token and must not clear it — see `clearsAuth401Suspicion` below.
//
// A freshly issued token clears it too (`EntraProvider.tsx`, on a successful
// `acquireTokenSilent`), because otherwise a tab that never manages an authenticated 2xx — the
// tokenless-mount path — has no reset a user could reach.
// ---------------------------------------------------------------------------

/**
 * The `sessionStorage` key holding the 401 count.
 *
 * A contract BETWEEN TWO PAGE LOADS — written before the reload, read after it — so renaming
 * it silently restores the unbounded loop rather than breaking anything visibly. Namespaced
 * because this key shares `sessionStorage` with MSAL's own cache (`msalConfig.ts` sets
 * `cacheLocation: 'sessionStorage'`).
 */
export const AUTH_401_COUNT_KEY = 'agp.auth.401count';

/**
 * The first-401 message — BYTE-IDENTICAL to what this interceptor has always rejected with,
 * and quoted as such in `docs/token-propagation.md` §2. Unchanged on purpose: the reload path
 * itself is unchanged, so its copy should not move.
 */
export const AUTH_401_RELOAD_MESSAGE = 'Session expired. Please log in again.';

/**
 * The terminal message for a 401 that survived a reload.
 *
 * Three product decisions are settled in this string, so it is pinned by a test (copy has no
 * compiler — the `cedarPosture.ts` precedent):
 *
 *   1. IT DOES NOT SAY "LOG IN AGAIN". That is the expiry message's advice and it is false
 *      here: the input that produced the rejected token is baked into the bundle, so a new
 *      sign-in produces an identically rejected token. Repeating the advice would move the
 *      loop from the browser into the user's hands.
 *   2. IT NAMES THE CAUSE. The research flags the tension — an end user cannot act on
 *      "audience/scope/roles" — but they are not the only reader: this text is what gets
 *      screenshotted and forwarded, and the three causes named are exactly the ones
 *      `docs/token-propagation.md` §11 sends an operator to check. Naming them is what makes
 *      the message forwardable instead of merely final.
 *   3. IT ADDRESSES THE USER, NOT THE OPERATOR. It surfaces through `err.message` like any
 *      other API error, so it says who to ask rather than what to fix.
 *
 * It carries no token, no id and no URL — a message shown to end users must not become an
 * exfiltration surface.
 */
export const AUTH_401_HALT_MESSAGE =
  'Sign-in is not working for this deployment. Reloading did not help, so signing in ' +
  'again will not either: a new token is rejected the same way. Ask an administrator ' +
  'to check the sign-in configuration — the token audience, the requested scope, and ' +
  'the app registration holding the role assignments.';

/**
 * How many 401s this session has already answered, from the raw `sessionStorage` value.
 *
 * TOTAL BY CONSTRUCTION. The input is a string a user can edit in devtools and `getItem`
 * answers `null` for a key never written, so every unparseable, negative or non-finite value
 * reads as ZERO — "no attempt has been established" — rather than being propagated into the
 * decision. Zero is the honest reading: an absent counter genuinely means no 401 has been
 * answered yet, which is the state on the first 401 of a session and after any 2xx clears it.
 */
export function readAttempts(stored: string | null | undefined): number {
  const parsed = Number((stored ?? '').trim());
  return Number.isFinite(parsed) && parsed > 0 ? Math.floor(parsed) : 0;
}

/**
 * The decision: `'reload'` for the first 401 of a session, `'halt'` for any after it.
 *
 * TOTAL — it is called from inside an axios ERROR interceptor, where a throw would replace the
 * 401 rejection with an unrelated error and lose the diagnostic entirely. An input under one
 * (including `NaN`, which no comparison satisfies) answers `'reload'`, which is the safe
 * direction for a value we cannot trust: the reload path writes a clean integer on its way
 * out, so the very next 401 halts. Halting on a corrupt counter would strand a user with no
 * way back.
 *
 * Takes the count rather than reading storage so the budget is a pure function of state — the
 * one thing about this fix that must be verifiable without a DOM.
 */
export function decide401(attempts: number): 'reload' | 'halt' {
  return attempts >= 1 ? 'halt' : 'reload';
}

/**
 * May this successful response clear the 401 count? ONLY if its request carried a bearer token.
 *
 * A 2xx is evidence about A TOKEN, not about the server being up, so the reset has to be scoped
 * to requests that presented one. Resetting on ANY 2xx is a latent restoration of the very loop
 * this module bounds: `healthApi` in `client.ts` calls a route that takes no principal, so a
 * status indicator or an uptime poll wired onto it would clear the counter on every tick and the
 * next 401 would reload again, forever. Nothing would notice, because the reset lives in the one
 * arm the DOM-less test lane cannot import — hence a pure predicate here, pinned by a test.
 *
 * TOTAL, on `readAttempts`' reasoning: the input is whatever axios left on the request config, so
 * it is typed `unknown` and anything that is not a non-blank string reads as "no bearer" — the
 * conservative direction, since failing to clear costs one extra reload while clearing wrongly
 * costs the bound.
 */
export function clearsAuth401Suspicion(authorizationHeader: unknown): boolean {
  return typeof authorizationHeader === 'string' && authorizationHeader.trim().length > 0;
}

/**
 * `sessionStorage`, made TOTAL — the ONLY way this module's callers may touch it.
 *
 * WHY THIS EXISTS. Every `sessionStorage` access can THROW, not just fail: `SecurityError`
 * when storage is blocked for the origin (Safari "block all cookies", a sandboxed or
 * third-party iframe) and `QuotaExceededError` on a write. That is fatal in both places this
 * counter is touched, and in both cases it converts a working auth flow into a broken one:
 *
 *   • `client.ts`' SUCCESS interceptor clears the counter on an authenticated 2xx. A throw
 *     there propagates out of the interceptor, so axios rejects an otherwise-fine 2xx — and it
 *     does so for EVERY authenticated call in the SPA, on a code path that has nothing to do
 *     with the response it is failing.
 *   • `EntraProvider.tsx` clears the counter immediately after a SUCCESSFUL
 *     `acquireTokenSilent`, inside the `try` whose `catch` does `setToken(null)` +
 *     `localStorage.removeItem('auth_token')`. A throw there therefore turns a token the
 *     provider JUST acquired into a TOKENLESS MOUNT — precisely the unrecoverable state that
 *     line was added to prevent, reached by the line meant to prevent it.
 *
 * So the 401 breaker — a diagnostic that improves a failure mode — could manufacture a worse
 * failure than the one it bounds, in browsers where nothing is actually wrong with the token.
 * Wrapping is the whole fix: NOTHING here throws, so no storage condition can reach the auth
 * flow. The counter is best-effort by design, and it is the right thing to make best-effort
 * because it only ever decides between two RECOVERY strategies for an already-failing request.
 *
 * WHAT DEGRADES, STATED HONESTLY. If storage is unavailable, `read` answers `null`, which
 * `readAttempts` reads as zero, which `decide401` answers `'reload'` — so the cross-page-load
 * bound is gone and only `client.ts`' module-scope `latched401` remains (one reload per page
 * load, i.e. the old unbounded loop, slower). That is accepted rather than overlooked: MSAL's
 * own cache IS `sessionStorage` (`msalConfig.ts` sets `cacheLocation: 'sessionStorage'`) — MSAL
 * tolerates a blocked one by falling back to in-memory, but that fallback does not survive the
 * sign-in redirect — so a tab with no working `sessionStorage` still cannot complete a sign-in
 * and never holds the token whose 401 this would have counted. Halting instead would be strictly
 * worse — it would strand every such tab on "ask an administrator" for an ordinary expiry that
 * a reload would fix.
 *
 * NO LOGGING, deliberately. The success-arm call runs on EVERY authenticated 2xx, so a warn
 * here is unbounded console output on exactly the broken-storage tab that can least afford
 * noise; bounding it would mean mutable module state in the one module whose testability is
 * the point. The degradation above is the observable.
 *
 * SCOPE: `sessionStorage` only. The adjacent `localStorage` calls in both files carry the same
 * exposure, but they pre-date this counter (the app has read/written `auth_token` there since
 * long before E36) and are deliberately left as they are rather than widened into this fix.
 *
 * SHAPE: `typeof … === 'undefined'` guard PLUS try/catch on every access, which is verbatim the
 * `uiPreference.ts` idiom (same two hazards, same swallow, already pinned by
 * `uiPreference.test.ts:34`/`:63`). The guard is not redundant with the catch — it separates
 * "there is no storage here", which is the ordinary state under NODE and SSR, from "storage
 * exists and refused", which is the browser fault — and keeps this the same shape a reader has
 * already met elsewhere in the app.
 */
export const safeStorage = {
  /** The stored string, or `null` when absent OR unreadable — both mean "no counter". */
  read(key: string): string | null {
    if (typeof sessionStorage === 'undefined') return null;
    try {
      return sessionStorage.getItem(key);
    } catch {
      return null;
    }
  },

  /** Best-effort write. A failure costs the cross-load bound, never the request. */
  write(key: string, value: string): void {
    if (typeof sessionStorage === 'undefined') return;
    try {
      sessionStorage.setItem(key, value);
    } catch {
      // Storage blocked or over quota — the counter just does not persist; `latched401` in
      // `client.ts` still bounds this page load. See NO LOGGING above.
    }
  },

  /** Best-effort clear. A failure costs at most one extra reload on a later 401. */
  remove(key: string): void {
    if (typeof sessionStorage === 'undefined') return;
    try {
      sessionStorage.removeItem(key);
    } catch {
      // Storage blocked — a stale count survives, costing one extra reload at worst. It must
      // NOT cost the 2xx or the freshly acquired token this runs alongside.
    }
  },
};
