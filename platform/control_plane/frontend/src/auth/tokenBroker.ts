// Rationale: docs/token-propagation.md §1–§2 (per-request token)
import { InteractionRequiredAuthError } from '@azure/msal-browser';
import type { AccountInfo } from '@azure/msal-browser';
import { coalesceAsync } from '../api/authRetry';
import { apiScopes, msalInstance } from './msalConfig';

function activeAccount(): AccountInfo | null {
  return msalInstance.getActiveAccount() ?? msalInstance.getAllAccounts()[0] ?? null;
}

async function acquire(forceRefresh: boolean): Promise<string | null> {
  try {
    // MSAL v5 requires initialize() before any other call; idempotent, so a request racing the mount effect is safe.
    await msalInstance.initialize();
    const account = activeAccount();
    if (!account) return null;
    const result = await msalInstance.acquireTokenSilent({ scopes: apiScopes, account, forceRefresh });
    return result.accessToken;
  } catch (err) {
    if (err instanceof InteractionRequiredAuthError) return null;
    return null;
  }
}

const acquireCached = coalesceAsync(() => acquire(false));
const acquireForced = coalesceAsync(() => acquire(true));

export async function currentAccessToken(opts?: { forceRefresh?: boolean }): Promise<string | null> {
  return opts?.forceRefresh ? acquireForced() : acquireCached();
}
