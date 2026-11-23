import { DEVAUTH_MODE } from './arm';

export async function currentAccessToken(opts?: { forceRefresh?: boolean }): Promise<string | null> {
  if (opts?.forceRefresh) console.info(`[tokenBroker] the ${DEVAUTH_MODE} arm holds no token to refresh`);
  return null;
}
