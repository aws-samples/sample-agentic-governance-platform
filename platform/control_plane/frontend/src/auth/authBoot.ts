// Decision: docs/adr/frontend-silent-sign-in-through-the-redirect-bridge.md
import { resolveRedirectUri } from './authEnv';
import { isRedirectBridgePage } from './silentSignIn';

export type BootStep = 'mount' | 'bridged';

export async function bootAuth(): Promise<BootStep> {
  const redirectUri = resolveRedirectUri(import.meta.env);
  if (!isRedirectBridgePage(window.location.pathname, redirectUri, window.location.origin)) {
    return 'mount';
  }
  try {
    const { broadcastResponseToMainFrame } = await import('@azure/msal-browser/redirect-bridge');
    await broadcastResponseToMainFrame();
  } catch {
    if (window.self === window.top) window.location.replace('/');
  }
  return 'bridged';
}
