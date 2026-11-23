// Rationale: docs/token-propagation.md §1–§2 (token homes, 401 budget)
// Decision: docs/adr/frontend-silent-sign-in-through-the-redirect-bridge.md
import { useEffect, useState, useCallback } from 'react';
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
import { AUTH_401_COUNT_KEY, safeStorage } from '../api/authRetry';
import { forgetHint, readHint, rememberHint, silentFailureMessage, ssoSilentRequest } from './silentSignIn';
import { AuthStateContext } from './useAuth';

function EntraAuthInner({ children }: { children: ReactNode }) {
  const { instance, accounts } = useMsal();
  const isAuthenticated = useIsAuthenticated();

  const [token, setToken] = useState<string | null>(localStorage.getItem('auth_token'));
  const [isLoading, setIsLoading] = useState(true);

  const account: AccountInfo | null = accounts[0] ?? null;

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
      safeStorage.remove(AUTH_401_COUNT_KEY);
    } catch (err) {
      if (err instanceof InteractionRequiredAuthError) {
        await instance.acquireTokenRedirect({ scopes: apiScopes, account });
      } else {
        console.error('[EntraProvider] acquireTokenSilent failed:', err);
        setToken(null);
        localStorage.removeItem('auth_token');
      }
    }
  }, [instance, account]);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        // MSAL v5 requires initialize() before any other call; it is idempotent, so every
        // caller invokes it instead of trusting a component-tree ordering.
        await instance.initialize();
        await instance.handleRedirectPromise();
      } catch (err) {
        console.error('[EntraProvider] handleRedirectPromise failed:', err);
      }
      if (cancelled) return;
      if (instance.getAllAccounts().length === 0) {
        try {
          const result = await instance.ssoSilent(ssoSilentRequest(apiScopes, readHint(localStorage)));
          instance.setActiveAccount(result.account);
        } catch (err) {
          console.info('[EntraProvider]', silentFailureMessage(err));
        }
      }
      if (!cancelled) {
        if (account) {
          rememberHint(localStorage, account.username);
          await acquireToken();
        }
        setIsLoading(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [instance, account, acquireToken]);

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
    await instance.initialize();
    await instance.loginRedirect({ scopes: apiScopes });
  }, [instance]);

  const signOut = useCallback(() => {
    localStorage.removeItem('auth_token');
    forgetHint(localStorage);
    setToken(null);
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
    arm: 'entra',
    signInRedirect,
    signOut,
  };

  return <AuthStateContext.Provider value={value}>{children}</AuthStateContext.Provider>;
}

export function EntraAuthProvider({ children }: { children: ReactNode }) {
  return (
    <MsalProvider instance={msalInstance}>
      <EntraAuthInner>{children}</EntraAuthInner>
    </MsalProvider>
  );
}
