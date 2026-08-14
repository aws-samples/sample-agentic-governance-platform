// GitHubLinkCallback — where GitHub returns the browser after a human authorizes their
// personal account (Epic 27B). Its route path is the one `buildLinkRedirectUrl` builds and
// is registered in `App.tsx` — deliberately not repeated here, because four tasks across
// the epic depend on that literal byte-for-byte and a second copy is how it drifts.
// Structure cloned from `ConnectionCallback.tsx`, including both of its hard-won details.
//
// Correctness load-bearers:
//   • The authorization `code` is SINGLE-USE. A `useRef` ran-once guard makes the
//     exchange fire exactly once — React 19 StrictMode double-invokes effects in dev, and
//     a second POST would burn a spent code and report a failure for a link that worked.
//   • CSRF echo: the returned `state` must equal what the link card stashed in
//     localStorage before the redirect. `resolveLinkCallbackParams` validates it and this
//     page clears the key UNCONDITIONALLY after reading — matched or not — so the state is
//     single-use. A refusal never touches the backend.
//   • ⚠️ The api-client's response interceptor turns ANY 401 into
//     `removeItem('auth_token') + window.location.reload()` (`api/client.ts:50-61`). A
//     reload here would re-mount this page with a spent code and an already-cleared state,
//     which the human would read as a random logout mid-flow. No E27B route returns 401,
//     so the only way to provoke one is to send a request with a missing or expired AGP
//     bearer — which is exactly the risk on this page, because the browser has been away
//     at github.com and came back through a full-page navigation. Two guards:
//       1. This page renders inside `AuthGate`, which mounts no route until MSAL has
//          resolved the session — so the effect cannot run mid-rehydration.
//       2. The exchange refuses to fire against a null `token` from `useAuth()` at all —
//          a request with no bearer is a guaranteed 401, i.e. a guaranteed reload. It
//          reports a terminal "session couldn't be restored" instead. This is a settled
//          check, not a wait: `AuthGate` mounts no route until `isLoading` is false, by
//          which point the provider's mount effect has already run `acquireToken`, so a
//          null token here will not become non-null on its own. Waiting for one would
//          spin forever — `AuthGate`'s gate is MSAL's `isAuthenticated` (an account
//          exists), which is NOT the same as "a backend access token was acquired".
//     If a bearer is present but EXPIRED the interceptor still wins that race — so the
//     loop is broken by construction rather than by hope: the stored state is cleared
//     before the exchange, so after a reload `resolveLinkCallbackParams` refuses on "no
//     pending request" instead of re-submitting the spent code. One terminal message,
//     never a cycle.
//
// This page keeps NO logic of its own: the validation lives in `githubLink.ts` and the
// success sentence is `linkCardCopy`'s, because a `.tsx` can never be unit-tested here.
// Nothing on this page logs, stores, or renders the code, the state, or a token.

import { useEffect, useRef, useState } from 'react';
import { Link, useSearchParams } from 'react-router-dom';
import { useAuth } from '../../auth/AuthContext';
import { githubLinkApi } from '../../api/client';
import OpsPage from './OpsPage';
import { OPS_CARD, OPS_PRIMARY_BTN } from './opsUi';
import {
  AGP_GITHUB_LINK_STATE_KEY,
  classifyLinkError,
  linkCardCopy,
  resolveLinkCallbackParams,
} from './githubLink';

type Phase = 'loading' | 'success' | 'error';

const SECONDARY_BTN =
  'px-3.5 py-1.5 rounded-lg bg-white border border-slate-300 text-slate-700 text-sm font-medium hover:bg-slate-50 transition-colors';

export default function GitHubLinkCallback() {
  const [searchParams] = useSearchParams();
  const { token } = useAuth();

  const [phase, setPhase] = useState<Phase>('loading');
  const [headline, setHeadline] = useState<string | null>(null);
  const [error, setError] = useState<{ message: string; retryable: boolean } | null>(null);

  // Ran-once guard — the authorization code is single-use, and StrictMode double-invokes
  // effects in dev. Set BEFORE the async call so the second invocation returns early.
  const ranRef = useRef(false);

  useEffect(() => {
    if (ranRef.current) return;
    ranRef.current = true;

    // No bearer ⇒ do NOT call the backend: a request with no token is a guaranteed 401,
    // and the api-client turns any 401 into `removeItem('auth_token') + reload()`, which
    // mid-callback reads as a random logout. Terminal, not a wait — see the header for why
    // a null token here will not resolve on its own. The stored state is deliberately left
    // in place: this attempt never consumed the code, so re-landing on this URL can still
    // verify the echo.
    //
    // ⚠️ `react-hooks/set-state-in-effect` reports this as an ERROR (not a warning) — 1 of
    // 21 sites of that rule in the repo, 20 of them pre-existing and shipped, including
    // E27's own `ProjectDetail.tsx` and `ProjectAccessTab.tsx`. Accepted, NOT suppressed:
    // an `eslint-disable` would hide a real pattern from a future baseline cleanup, and the
    // gate for this epic is `tsc -b` + vitest, both green. Deriving this one branch from
    // `token` was tried and rejected — the very next branch (`'error' in resolved`) must
    // CONSUME the single-use localStorage state, which is irreducibly an effect, so the
    // rule still fired at the same count while the delicate null-token guard got harder to
    // read. One visible error is the honest state of this file.
    if (!token) {
      setError({
        message: 'Your AGP session could not be restored, so the link was not completed.',
        retryable: true,
      });
      setPhase('error');
      return;
    }

    // localStorage (NOT sessionStorage): the state must survive the round trip to
    // github.com, and localStorage is what the link card wrote.
    const stored = window.localStorage.getItem(AGP_GITHUB_LINK_STATE_KEY);
    const resolved = resolveLinkCallbackParams(searchParams, stored);
    // Single-use — cleared whether the echo matched or not. This is also what stops a
    // reload of this URL from re-submitting a spent code.
    window.localStorage.removeItem(AGP_GITHUB_LINK_STATE_KEY);

    if ('error' in resolved) {
      // Never call the backend on a refusal: the code is single-use, so a callback we
      // cannot verify is refused outright rather than retried.
      setError({ message: resolved.error, retryable: false });
      setPhase('error');
      return;
    }

    githubLinkApi
      .callback({ code: resolved.code, state: resolved.state })
      .then((row) => {
        setHeadline(linkCardCopy('linked', row.github_login).headline);
        setPhase('success');
      })
      .catch((err: unknown) => {
        setError(classifyLinkError(err, 'Could not finish linking your GitHub account.'));
        setPhase('error');
      });
  }, [searchParams, token]);

  return (
    <OpsPage
      title="Connecting your GitHub account"
      subtitle="Finishing the authorization you just approved on GitHub."
      backTo="/ops"
    >
      <div className={`${OPS_CARD} p-6 max-w-xl space-y-4`}>
        {phase === 'loading' && <p className="text-sm text-slate-500">Finishing up…</p>}

        {phase === 'success' && (
          <>
            <h2 className="text-lg font-semibold text-slate-900">{headline}</h2>
            <p className="text-sm text-slate-600">
              The deployments you trigger from AGP will now be carried out under your own GitHub
              identity, so the organization&rsquo;s audit log records them as you.
            </p>
            <Link to="/ops/github-link" className={`${OPS_PRIMARY_BTN} inline-block`}>
              Back to your GitHub account
            </Link>
          </>
        )}

        {phase === 'error' && (
          <>
            <h2 className="text-lg font-semibold text-slate-900">
              Couldn&rsquo;t finish connecting your account
            </h2>
            {error && (
              <p
                className={`text-sm ${error.retryable ? 'text-amber-700' : 'text-rose-600'}`}
                role="alert"
              >
                {error.message}
              </p>
            )}
            <p className="text-sm text-slate-600">
              Nothing was changed, and your existing links are untouched. Start again from your
              GitHub account page — this authorization attempt cannot be resumed, because GitHub
              only honours it once.
            </p>
            <Link to="/ops/github-link" className={`${SECONDARY_BTN} inline-block`}>
              Back to your GitHub account
            </Link>
          </>
        )}
      </div>
    </OpsPage>
  );
}
