# Frontend sign-in is silent first, through MSAL v5's redirect bridge at the existing callback path

## Status

Accepted 2026-10-27 (Epic 53A)

## Context

`platform/control_plane/frontend/src/auth/msalConfig.ts` caches in `sessionStorage`, so every browser tab starts with
no account, and `platform/control_plane/frontend/src/auth/EntraProvider.tsx` used to call only
`handleRedirectPromise()` on load and `loginRedirect()` from the button. A new tab therefore showed the sign-in screen
while the browser held a valid Microsoft session: every human clicked once per tab, and the live-test harness clicked
for the agent. MSAL 5 also moved where silent responses land: `ssoSilent`, the popup flows and `acquireTokenSilent`
after the refresh token expires all deliver their result through a page at the redirect URI that calls
`broadcastResponseToMainFrame()` from `@azure/msal-browser/redirect-bridge`. The app had no such page, so the
expired-refresh-token path was silently broken and silent sign-in could not be added without one. And `msalConfig.ts`
constructs the `PublicClientApplication` at module load, so any module importing it, directly or through `App`, would
build a client inside the bridge iframe too.

## Decision

On load, after `initialize()` and `handleRedirectPromise()`, when MSAL holds no account `EntraProvider.tsx` calls
`instance.ssoSilent` with the request `ssoSilentRequest` builds: the API scopes and, when `readHint(localStorage)`
finds one, the username `rememberHint` stores under `LOGIN_HINT_KEY` (`agp.auth.loginHint`) whenever an account is
present and `forgetHint` removes on sign-out. On success the result's account becomes the active account and the
existing acquire path mirrors the token. On any failure the provider logs `silentFailureMessage(err)` at
`console.info`, never `console.error` (the harness journals every error as a finding, and no session is not one), ends
`isLoading`, and `AuthGate` shows the button as before. The attempt runs once per mount.

The bridge is served at the existing callback path. `platform/control_plane/frontend/src/main.tsx` calls `bootAuth()`
from `platform/control_plane/frontend/src/auth/authBoot.ts` before React mounts; when `isRedirectBridgePage` matches
`window.location.pathname` against the path of `resolveRedirectUri(import.meta.env)`, `bootAuth` imports the bridge,
awaits `broadcastResponseToMainFrame()` and resolves `'bridged'`, and `main.tsx` mounts nothing; a visit with no
response in the URL throws inside the bridge and `bootAuth` navigates to `/` only in the top frame — in any frame it
still resolves `'bridged'` and loads nothing, so MSAL's parent times out and shows the button. Everywhere else it
resolves `'mount'` and `main.tsx` imports `App` dynamically, so `msalConfig.ts` is never evaluated in the iframe. The
redirect URI and the scope moved into `platform/control_plane/frontend/src/auth/authEnv.ts`, the one module both
`msalConfig.ts` and `authBoot.ts` read, so the callback path has one owner; the registered redirect URIs are unchanged.

Two alternatives were rejected. `cacheLocation: 'localStorage'` is one line and needs no iframe, but it still shows
the button once per browser launch (MSAL 4+ encrypts that cache with a session cookie unless "Keep me signed in" was
ticked), it moves refresh artifacts into storage shared across tabs, and it does not meet "the screen shows only when
no session exists". A dedicated bridge page at a new URL is MSAL's first recommendation but costs a redirect URI per
environment and a CloudFront change for the same behaviour.

The pure pieces are pinned in `platform/control_plane/frontend/src/auth/silentSignIn.test.ts` under
`ssoSilentRequest carries loginHint only when a hint exists`, `isRedirectBridgePage matches the configured path with
or without a trailing slash` and `silentFailureMessage names the MSAL error code or the class`; the `ssoSilent` call
and the bridge are proven live, since the node-only suite has no DOM.

## Consequences

- `AuthGate` keeps its loading state until the attempt settles; MSAL's default iframe timeout bounds the worst case at
  about ten seconds, and `system.iframeBridgeTimeout` in `msalConfig.ts` is the one-line shortening if a human minds.
- A browser that blocks third-party cookies fails the silent step and sees the button: the former default, now the
  exception, and the harness's one click still covers it.
- The redirect flow changed shape — the bridge caches the response and navigates back instead of the SPA processing
  the hash in place — so the callback path, the redirect URI default and the hint key change in `authEnv.ts` or
  `silentSignIn.ts`, in their test and in `docs/token-propagation.md` §1 in the same commit.
