import { createContext, useContext, useEffect, useState, useCallback } from 'react';
import type { ReactNode } from 'react';
import {
  MsalProvider,
  useMsal,
  useIsAuthenticated,
} from '@azure/msal-react';
import {
  type AccountInfo,
  InteractionRequiredAuthError,
  EventType,
  type EventMessage,
  type AuthenticationResult,
} from '@azure/msal-browser';
import type { AuthState } from './authShape';
import { msalInstance, apiScopes } from './msalConfig';
// The 401 breaker's storage key and its non-throwing storage wrapper (E36/T19; the wrapper added
// by the final review's F1). `authRetry.ts` pulls in no axios and no client, and the key is
// imported rather than retyped because it is a contract between two page loads: a second spelling
// of it silently restores the unbounded reload loop.
import { AUTH_401_COUNT_KEY, safeStorage } from '../api/authRetry';

// TESTS (E2-FE/T7):
// rendering tests deferred — vitest is not configured with a DOM env.
// Consider adding @testing-library/react + happy-dom in a follow-up.

const EntraAuthContext = createContext<AuthState | null>(null);

export function useEntraAuth(): AuthState {
  const ctx = useContext(EntraAuthContext);
  if (!ctx) throw new Error('useEntraAuth must be used within EntraAuthProvider');
  return ctx;
}

/**
 * Inner provider — runs *inside* MsalProvider so it can use useMsal/useIsAuthenticated.
 */
function EntraAuthInner({ children }: { children: ReactNode }) {
  const { instance, accounts } = useMsal();
  const isAuthenticated = useIsAuthenticated();

  const [token, setToken] = useState<string | null>(localStorage.getItem('auth_token'));
  const [isLoading, setIsLoading] = useState(true);

  const account: AccountInfo | null = accounts[0] ?? null;

  /**
   * Acquire an access token silently for our backend's scope. If MSAL needs
   * interaction (consent, MFA challenge), fall through to redirect.
   */
  const acquireToken = useCallback(async () => {
    if (!account) {
      setToken(null);
      localStorage.removeItem('auth_token');
      return;
    }
    try {
      const result = await instance.acquireTokenSilent({
        scopes: apiScopes,
        account,
      });
      setToken(result.accessToken);
      localStorage.setItem('auth_token', result.accessToken);
      // A FRESHLY ISSUED TOKEN RETIRES THE 401 SUSPICION (E36/T19 fix round 1, D-2), for the same
      // reason an authenticated 2xx does — a healthy sign-in is evidence, so it resets the breaker.
      // Without this the breaker had a state no user could leave: the `catch` below leaves
      // `isLoading` false with no token while MSAL still reports the account as signed in, so the
      // route tree mounts TOKENLESS and every call 401s. With the count already at one from the
      // preceding reload, a transient network blip talking to Entra would present as the terminal
      // "check the sign-in configuration" and the only way out would be closing the tab, since
      // nothing tokenless can ever produce the authenticated 2xx that clears it.
      //
      // THROUGH `safeStorage`, and this is the sharpest instance of F1 in the app: this line sits
      // inside the `try` whose `catch` below does `setToken(null)` +
      // `localStorage.removeItem('auth_token')`. A raw storage call that threw here — a
      // `SecurityError` on a blocked-storage origin, nothing wrong with the token at all — would
      // hand a token acquired ONE LINE ABOVE straight to that teardown and produce the tokenless
      // mount this line exists to rescue the user from. The wrapper cannot throw, so a successful
      // acquisition stays successful and only the counter reset is lost.
      safeStorage.remove(AUTH_401_COUNT_KEY);
    } catch (err) {
      if (err instanceof InteractionRequiredAuthError) {
        await instance.acquireTokenRedirect({ scopes: apiScopes, account });
        // redirect navigates away; nothing to do after this
      } else {
        // eslint-disable-next-line no-console
        console.error('[EntraProvider] acquireTokenSilent failed:', err);
        setToken(null);
        localStorage.removeItem('auth_token');
      }
    }
  }, [instance, account]);

  // On mount, complete any in-flight redirect and acquire a token.
  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        // MSAL v5 requires explicit initialization before any auth method is
        // called. PublicClientApplication.initialize() is idempotent — safe to
        // call on every effect run; subsequent calls are cheap no-ops.
        await instance.initialize();
        await instance.handleRedirectPromise();
      } catch (err) {
        // eslint-disable-next-line no-console
        console.error('[EntraProvider] handleRedirectPromise failed:', err);
      }
      if (!cancelled) {
        if (account) {
          await acquireToken();
        }
        setIsLoading(false);
      }
    })();
    return () => {
      cancelled = true;
    };
    // We intentionally re-run when `account` becomes available after redirect.
  }, [instance, account, acquireToken]);

  // Refresh token when MSAL emits an acquisition success event.
  // IMPORTANT: only ACQUIRE_TOKEN_SUCCESS is mirrored here, NOT LOGIN_SUCCESS.
  // LOGIN_SUCCESS's payload.accessToken is the ID token (audience = SPA client
  // ID GUID), not the access token for our backend's exposed-API audience.
  // The right access token (audience = api://agp) is produced only by
  // acquireTokenSilent in `acquireToken` above, which fires ACQUIRE_TOKEN_SUCCESS.
  useEffect(() => {
    const callbackId = instance.addEventCallback((event: EventMessage) => {
      if (
        event.eventType === EventType.ACQUIRE_TOKEN_SUCCESS &&
        event.payload
      ) {
        const payload = event.payload as AuthenticationResult;
        if (payload.accessToken) {
          setToken(payload.accessToken);
          localStorage.setItem('auth_token', payload.accessToken);
        }
      }
    });
    return () => {
      if (callbackId) instance.removeEventCallback(callbackId);
    };
  }, [instance]);

  const signInRedirect = useCallback(async () => {
    // initialize() is idempotent and required before any auth method in MSAL v5.
    // We call it here defensively so a fast button click doesn't race the
    // mount-effect's initialize.
    await instance.initialize();
    await instance.loginRedirect({ scopes: apiScopes });
  }, [instance]);

  const signOut = useCallback(() => {
    // Clear local state synchronously so callers see immediate effect.
    localStorage.removeItem('auth_token');
    setToken(null);
    // Navigation away is fire-and-forget; ensure MSAL is initialized first.
    void (async () => {
      await instance.initialize();
      instance.logoutRedirect({
        account: account ?? undefined,
        postLogoutRedirectUri: window.location.origin,
      });
    })();
  }, [instance, account]);

  const value: AuthState = {
    isAuthenticated,
    isLoading,
    user: account ? { username: account.username, name: account.name } : null,
    token,
    signInRedirect,
    signOut,
  };

  return <EntraAuthContext.Provider value={value}>{children}</EntraAuthContext.Provider>;
}

export function EntraAuthProvider({ children }: { children: ReactNode }) {
  return (
    <MsalProvider instance={msalInstance}>
      <EntraAuthInner>{children}</EntraAuthInner>
    </MsalProvider>
  );
}
