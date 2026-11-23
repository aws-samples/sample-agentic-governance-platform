import { PublicClientApplication, type Configuration, LogLevel } from '@azure/msal-browser';

import { resolveAuthConfig } from './authConfigGuard';
import { resolveApiScope, resolveRedirectUri } from './authEnv';

const { clientId, authority } = resolveAuthConfig(import.meta.env);

const redirectUri = resolveRedirectUri(import.meta.env);
const scope = resolveApiScope(import.meta.env);

export const msalConfig: Configuration = {
  auth: {
    clientId,
    authority,
    redirectUri,
    postLogoutRedirectUri: window.location.origin,
  },
  cache: {
    // Rationale: docs/token-propagation.md §1 (two token homes)
    cacheLocation: 'sessionStorage',
  },
  system: {
    loggerOptions: {
      loggerCallback: (level, message) => {
        if (level === LogLevel.Error) {
          console.error('[MSAL]', message);
        } else if (level === LogLevel.Warning) {
          console.warn('[MSAL]', message);
        }
      },
      logLevel: LogLevel.Warning,
      piiLoggingEnabled: false,
    },
  },
};

export const apiScopes: string[] = [scope];

export const msalInstance = new PublicClientApplication(msalConfig);
