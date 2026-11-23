/** Written to localStorage by the add-connection modal before GitHub opens in a new tab; sessionStorage is per tab, so the callback could not read it. */
export const AGP_MANIFEST_STATE_KEY = 'agp_manifest_state';

export function resolveCallbackParams(
  search: URLSearchParams | string,
  sessionState: string | null,
): { code: string; state: string } | { error: string } {
  const params = typeof search === 'string' ? new URLSearchParams(search) : search;
  const code = params.get('code');
  const state = params.get('state');

  if (!code) return { error: 'Missing authorization code in the callback URL.' };
  if (!state) return { error: 'Missing state in the callback URL.' };
  if (!sessionState) {
    return { error: 'No pending GitHub App request — start the connection from Org Connections.' };
  }
  if (state !== sessionState) {
    return { error: 'State mismatch — the callback could not be verified. Please try again.' };
  }
  return { code, state };
}
