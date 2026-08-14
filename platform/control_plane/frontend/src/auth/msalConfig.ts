import { PublicClientApplication, type Configuration, LogLevel } from '@azure/msal-browser';

import { resolveAuthConfig } from './authConfigGuard';

// FAIL FAST AT MODULE LOAD — unconditionally, whatever `VITE_AUTH_PROVIDER` says (E36/T19).
// The decision, the unusable-value predicate and the message all live in `authConfigGuard.ts`,
// which is where they are testable: this module builds a `PublicClientApplication` and reads
// `window.location.origin` at module scope, so it cannot be imported by the DOM-less vitest
// lane. This line is the whole consumer.
//
// The old guard threw only inside `if (import.meta.env.VITE_AUTH_PROVIDER === 'entra')` and
// otherwise fell back to `clientId: ''` against the `/common` multi-tenant authority — a
// sign-in that completes against the wrong directory and then 401s on every API call. Both
// fallbacks are gone with the gate; see `authConfigGuard.ts` for why the variable no longer
// participates at all.
const { clientId, authority } = resolveAuthConfig(import.meta.env);

// These two keep their defaults, deliberately: both are real, working values (the Vite dev
// server's own callback and the documented scope), and neither can authenticate against the
// wrong directory — which is the specific harm the guard above exists to prevent.
const redirectUri = import.meta.env.VITE_ENTRA_SPA_REDIRECT_URI || 'http://localhost:5173/auth/callback';
const scope = import.meta.env.VITE_ENTRA_SPA_SCOPE || 'api://agp/Access.Default';

export const msalConfig: Configuration = {
  auth: {
    clientId,
    authority,
    redirectUri,
    postLogoutRedirectUri: window.location.origin,
    // Note: `navigateToLoginRequestUrl` and `storeAuthStateInCookie` were moved
    // out of Configuration in @azure/msal-browser v5 (they are now per-request
    // options on RedirectRequest / HandleRedirectPromiseOptions). MSAL v5
    // navigates to the login-request URL by default, which matches what the
    // v3 `navigateToLoginRequestUrl: true` would have requested.
  },
  cache: {
    // sessionStorage = cleared on tab close; localStorage = persists across tabs.
    // We use sessionStorage because the prototype is single-tab usage and it
    // reduces the blast radius of any XSS. The auth_token mirror in localStorage
    // is what the Axios interceptor reads — we manage that ourselves.
    cacheLocation: 'sessionStorage',
  },
  system: {
    loggerOptions: {
      loggerCallback: (level, message) => {
        if (level === LogLevel.Error) {
          // eslint-disable-next-line no-console
          console.error('[MSAL]', message);
        } else if (level === LogLevel.Warning) {
          // eslint-disable-next-line no-console
          console.warn('[MSAL]', message);
        }
      },
      logLevel: LogLevel.Warning,
      piiLoggingEnabled: false,
    },
  },
};

/** Scopes requested when calling our backend. */
export const apiScopes: string[] = [scope];

/** The single MSAL client used for the lifetime of this Vite tab. */
export const msalInstance = new PublicClientApplication(msalConfig);
