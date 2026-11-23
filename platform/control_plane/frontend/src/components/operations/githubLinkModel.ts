// Decision: docs/adr/frontend-refusals-classified-by-code.md
import { type ApiFailure, type ErrorCode, isApiFailure } from '../../api/failure';
import { refusalKind } from '../../api/refusalKind';

/** Written to localStorage by the link card before GitHub opens in a new tab; the callback reads it once. */
export const AGP_GITHUB_LINK_STATE_KEY = 'agp_github_link_state';

export interface LinkCardInput {
  linked: boolean;
  status: string;
  github_login: string | null;
}

// The backend's LINK_CALLBACK_PATH, verbatim; GitHub rejects a redirect_uri that differs from the App's registered callback.
export function buildLinkRedirectUrl(origin: string): string {
  return `${origin}/ops/github-link/callback`;
}

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

export interface CallbackInput {
  token: string | null;
  search: URLSearchParams | string;
  storedState: string | null;
}

export type CallbackResult =
  | { kind: 'exchange'; code: string; state: string; clearStoredState: true }
  | { kind: 'refused'; message: string; retryable: false; clearStoredState: true }
  | { kind: 'no-session'; message: string; retryable: true; clearStoredState: false };

export function resolveCallback(input: CallbackInput): CallbackResult {
  if (!input.token) {
    return {
      kind: 'no-session',
      message: 'Your AGP session could not be restored, so the link was not completed.',
      retryable: true,
      clearStoredState: false,
    };
  }
  const resolved = resolveLinkCallbackParams(input.search, input.storedState);
  if ('error' in resolved) {
    return { kind: 'refused', message: resolved.error, retryable: false, clearStoredState: true };
  }
  return { kind: 'exchange', code: resolved.code, state: resolved.state, clearStoredState: true };
}

export const LINK_RETRY_HINT = 'Try again in a moment.';

export type LinkErrorKind =
  | 'not_found'
  | 'bad_request'
  | 'conflict'
  | 'oauth_client_missing'
  | 'refresh_in_progress'
  | 'link_revoked'
  | 'provider_error'
  | 'secret_error';

export const LINK_ERROR_CODES = {
  not_found: ['github_link.not_found', 'github_link.connection_missing'],
  bad_request: ['github_link.bad_request'],
  conflict: ['github_link.conflict'],
  oauth_client_missing: ['github_link.oauth_client_missing'],
  refresh_in_progress: ['github_link.refresh_in_progress'],
  link_revoked: ['github_link.link_revoked'],
  provider_error: ['github_link.provider_error'],
  secret_error: ['github_link.secret_error'],
} as const satisfies Record<LinkErrorKind, readonly ErrorCode[]>;

const RETRYABLE_KINDS: readonly LinkErrorKind[] = [
  'refresh_in_progress',
  'secret_error',
  'provider_error',
];

export function classifyLinkFailure(failure: ApiFailure): LinkErrorKind | null {
  return refusalKind(LINK_ERROR_CODES, failure);
}

export function classifyLinkError(
  err: unknown,
  fallback: string,
): { message: string; retryable: boolean; kind: LinkErrorKind | null } {
  const raw = typeof err === 'string' ? err : err instanceof Error && err.message ? err.message : '';
  const kind = isApiFailure(err) ? classifyLinkFailure(err) : null;
  if (kind === null) {
    return { message: raw || fallback, retryable: false, kind: null };
  }
  if (RETRYABLE_KINDS.includes(kind)) {
    return { message: `${raw}. ${LINK_RETRY_HINT}`, retryable: true, kind };
  }
  return { message: raw, retryable: false, kind };
}

export type LinkCardState = 'no-oauth-client' | 'unlinked' | 'linked' | 'revoked';

export function deriveLinkCardState(
  link: LinkCardInput | undefined,
  oauthClientReady: boolean,
): LinkCardState {
  if (!oauthClientReady) return 'no-oauth-client';
  if (!link) return 'unlinked';
  return link.linked ? 'linked' : 'revoked';
}

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
      return { headline: 'Connect your GitHub account', action: 'Connect GitHub' };
  }
}
