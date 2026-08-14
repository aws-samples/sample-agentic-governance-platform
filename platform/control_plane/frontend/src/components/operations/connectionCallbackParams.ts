// Pure helper for the GitHub App Manifest callback landing (Epic 20 / U4).
// Extracted from ConnectionCallback.tsx so the load-bearing CSRF-echo logic is
// unit-testable under vitest (node env, no jsdom — the component itself can't be
// rendered in a collected `*.test.ts`).

/** localStorage key the AddConnectionModal (U3) writes the CSRF state to before opening GitHub
 *  in a new tab. localStorage (not sessionStorage) so the state is visible in the new tab where
 *  this callback lands. */
export const AGP_MANIFEST_STATE_KEY = 'agp_manifest_state';

/**
 * Validate the `?code=&state=` GitHub returns against the CSRF state we stored
 * in localStorage before the manifest handshake. Returns the pair on success,
 * or an `{ error }` describing the first failure (missing code/state, no stored
 * state, or a state mismatch). Never calls the backend — a mismatch means the
 * caller must NOT convert the (single-use) code.
 */
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
