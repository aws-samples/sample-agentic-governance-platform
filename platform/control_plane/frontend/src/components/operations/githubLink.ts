// Pure logic for the per-user GitHub account link (Epic 27B).
// Lives in a `.ts` (not the components) because only `src/**/*.test.ts` is
// collected by vitest — there is no jsdom, so a `.tsx` can never be rendered in a
// test. Every load-bearing bit (the CSRF echo, the redirect-URL builder, the
// status→copy derivation) therefore lives here and the components stay
// presentation-only. Exact precedent: `connectionCallbackParams.ts` + `manifestForm.ts`,
// both extracted from `ConnectionCallback.tsx` for this reason.

/** localStorage key the link card writes the CSRF state to before sending the human to GitHub.
 *  localStorage (not sessionStorage) so the state is visible in the new tab where the callback
 *  lands. Distinct from `agp_manifest_state` — the admin manifest handshake and this per-user
 *  flow must never read each other's state. The callback removes it unconditionally on read, so
 *  the state is single-use. */
export const AGP_GITHUB_LINK_STATE_KEY = 'agp_github_link_state';

/** The minimum shape this module needs from a link row. `client.ts`'s wire `GitHubLinkStatus`
 *  satisfies it structurally, so this module imports NOTHING. */
export interface LinkCardInput {
  linked: boolean;
  status: string;
  github_login: string | null;
}

// Where GitHub returns the browser after the human authorizes (or declines) the App.
// Must match the backend's `LINK_CALLBACK_PATH` and the manifest's `callback_urls` entry
// byte-for-byte, or GitHub rejects the `redirect_uri`.
export function buildLinkRedirectUrl(origin: string): string {
  return `${origin}/ops/github-link/callback`;
}

/**
 * Validate the `?code=&state=` GitHub returns against the CSRF state stashed in
 * localStorage before the authorize redirect. Returns the pair on success, or an
 * `{ error }` describing the first failure — GitHub's own `error` param (the human
 * declined), then missing code, missing state, no stored state, or a state mismatch.
 * The comparison is exact. Never calls the backend on failure and never surfaces the
 * code on a refusal path: the code is single-use, so a refused callback must be
 * refused outright rather than retried. No message echoes the code or the state.
 */
export function resolveLinkCallbackParams(
  search: URLSearchParams | string,
  storedState: string | null,
): { code: string; state: string } | { error: string } {
  const params = typeof search === 'string' ? new URLSearchParams(search) : search;
  const providerError = params.get('error');
  const code = params.get('code');
  const state = params.get('state');

  if (providerError) {
    return { error: 'GitHub did not authorize the link. Nothing was connected — you can try again.' };
  }
  if (!code) return { error: 'Missing authorization code in the callback URL.' };
  if (!state) return { error: 'Missing state in the callback URL.' };
  if (!storedState) {
    return { error: 'No pending GitHub link request — start again from your profile menu.' };
  }
  if (state !== storedState) {
    return { error: 'State mismatch — the callback could not be verified. Please try again.' };
  }
  return { code, state };
}

/** Appended to a RETRYABLE failure so the human is told to wait rather than to give up. */
export const LINK_RETRY_HINT = 'Try again in a moment.';

// The route's FIXED detail literals (design §8 / the route's `_ERROR_DETAIL`), mapped back
// to the `.kind` they were emitted for.
//
// Why match on the SENTENCE and not the status: the api-client's response interceptor
// replaces the AxiosError with `new Error(response.data.detail)` (`api/client.ts:61-62`),
// so by the time a component holds the rejection the status code is gone. The detail
// literal is fixed per `.kind` server-side precisely so it can be classified — it is
// never `str(err)`.
//
// Retryable ones are NORMAL operation, not defects: `refresh_in_progress` is the other
// ECS task holding the 60s refresh claim, and `secret_error` / `provider_error` are a
// transient AWS or GitHub blip. Presenting those as terminal would tell a human to
// abandon a link that is working. `link_revoked` is the opposite — the token is already
// dead, so a retry re-probes it and returns the same answer; the card flips to `revoked`
// and offers a reconnect instead.
/** The route's eight `.kind` values — the vocabulary its `_ERROR_DETAIL` is keyed by. */
export type LinkErrorKind =
  | 'not_found'
  | 'bad_request'
  | 'conflict'
  | 'oauth_client_missing'
  | 'refresh_in_progress'
  | 'link_revoked'
  | 'provider_error'
  | 'secret_error';

// ONE table, detail sentence → kind. Deliberately not two membership arrays: a terminal
// array is indistinguishable from its own absence (the fallback also answers
// `{message: raw, retryable: false}`), so five of the eight literals could be deleted with
// every test still green — the safety net the contract note claims would not have existed.
// Recovering the KIND is what makes each literal independently observable, so removing any
// one of the eight fails a test.
//
// A `Map`, not an object literal: the lookup key is a SERVER-SUPPLIED string, and an object
// would resolve `'toString'` / `'constructor'` to `Object.prototype`'s member — classifying a
// `detail` of "toString" as a recognized kind whose value is a function.
const DETAIL_KINDS: ReadonlyMap<string, LinkErrorKind> = new Map([
  ['GitHub link not found', 'not_found'],
  ['Invalid request', 'bad_request'],
  ['That GitHub account is already linked to another user', 'conflict'],
  [
    'This org connection has no GitHub OAuth client — ask an admin to add one',
    'oauth_client_missing',
  ],
  ['GitHub token refresh in progress — retry', 'refresh_in_progress'],
  ['Your GitHub authorization was revoked — reconnect your account', 'link_revoked'],
  ['GitHub request failed', 'provider_error'],
  ['Secret store operation failed', 'secret_error'],
] as const);

// The three kinds whose remedy is "wait", derived from the kind rather than re-listing the
// sentences — so the retry verdict cannot drift from the table above.
const RETRYABLE_KINDS: readonly LinkErrorKind[] = [
  'refresh_in_progress',
  'secret_error',
  'provider_error',
];

/**
 * Turn a rejected link call into a sentence, a retry verdict, and the route `.kind` the
 * sentence came from (`null` when nothing matched). Unrecognized errors fall back to their
 * own message (or `fallback` when they carry none) and are treated as TERMINAL: an unknown
 * failure is more likely a bug than contention, and inviting a retry on it would be a guess
 * presented as advice.
 *
 * `kind` is the discriminator between "matched a pinned backend literal" and "fell through":
 * `message` and `retryable` alone cannot tell those apart for a terminal detail, which is
 * exactly how a backend reword could have silently degraded the copy.
 */
export function classifyLinkError(
  err: unknown,
  fallback: string,
): { message: string; retryable: boolean; kind: LinkErrorKind | null } {
  const raw = err instanceof Error && err.message ? err.message : '';
  const kind = DETAIL_KINDS.get(raw) ?? null;
  if (kind === null) {
    return { message: raw || fallback, retryable: false, kind: null };
  }
  if (RETRYABLE_KINDS.includes(kind)) {
    return { message: `${raw}. ${LINK_RETRY_HINT}`, retryable: true, kind };
  }
  return { message: raw, retryable: false, kind };
}

export type LinkCardState = 'no-oauth-client' | 'unlinked' | 'linked' | 'revoked';

// Precedence: a connection with no OAuth client can't be linked at all, so that wins over
// any stored row. Then no row → never linked; `linked` → linked (a `refreshing` row is still
// linked, the rotation is just in flight); a row that exists but isn't linked was revoked at
// GitHub and found on a probe.
export function deriveLinkCardState(
  link: LinkCardInput | undefined,
  oauthClientReady: boolean,
): LinkCardState {
  if (!oauthClientReady) return 'no-oauth-client';
  if (!link) return 'unlinked';
  return link.linked ? 'linked' : 'revoked';
}

// The design §8 copy. `action === null` means no button is rendered at all (E27's rule:
// `disabled` is reserved for in-flight work, not for unavailable capability).
export function linkCardCopy(
  state: LinkCardState,
  login: string | null,
): { headline: string; action: string | null } {
  switch (state) {
    case 'no-oauth-client':
      return {
        headline: 'This org connection has no GitHub OAuth client — ask an admin to add one',
        action: null,
      };
    case 'linked':
      return {
        headline: login ? `Linked as @${login}` : 'Your GitHub account is linked',
        action: 'Verify',
      };
    case 'revoked':
      return {
        headline: 'Your GitHub authorization was revoked — reconnect to continue',
        action: 'Reconnect',
      };
    case 'unlinked':
    default:
      return { headline: 'Connect your GitHub account', action: 'Connect GitHub' };
  }
}
