import { useEffect, useRef, useState } from 'react';
import { Link, useSearchParams } from 'react-router-dom';
import { useAuth } from '../../auth/useAuth';
import { githubLinkApi } from '../../api/client';
import { safeLocalStorage } from '../../api/safeLocalStorage';
import OpsPage from './OpsPage';
import { OPS_CARD, OPS_PRIMARY_BTN } from './opsUi';
import {
  AGP_GITHUB_LINK_STATE_KEY,
  classifyLinkError,
  linkCardCopy,
  resolveCallback,
} from './githubLinkModel';

type Settled =
  | { phase: 'success'; headline: string }
  | { phase: 'error'; message: string; retryable: boolean };

const SECONDARY_BTN =
  'px-3.5 py-1.5 rounded-lg bg-white border border-slate-300 text-slate-700 text-sm font-medium hover:bg-slate-50 transition-colors';

export default function GitHubLinkCallback() {
  const [searchParams] = useSearchParams();
  // Rationale: docs/token-propagation.md §2 (401 reloads)
  const { token } = useAuth();

  const [settled, setSettled] = useState<Settled | null>(null);

  // Decision: docs/adr/frontend-loading-state.md
  const ranRef = useRef(false);

  useEffect(() => {
    if (ranRef.current) return;
    ranRef.current = true;

    const result = resolveCallback({
      token,
      search: searchParams,
      storedState: safeLocalStorage.getItem(AGP_GITHUB_LINK_STATE_KEY),
    });
    if (result.clearStoredState) safeLocalStorage.removeItem(AGP_GITHUB_LINK_STATE_KEY);

    const outcome: Promise<Settled> =
      result.kind === 'exchange'
        ? githubLinkApi
            .callback({ code: result.code, state: result.state })
            .then(
              (row): Settled => ({
                phase: 'success',
                headline: linkCardCopy('linked', row.github_login).headline,
              }),
            )
            .catch((err: unknown): Settled => {
              const { message, retryable } = classifyLinkError(
                err,
                'Could not finish linking your GitHub account.',
              );
              return { phase: 'error', message, retryable };
            })
        : Promise.resolve({ phase: 'error', message: result.message, retryable: result.retryable });
    void outcome.then(setSettled);
  }, [searchParams, token]);

  return (
    <OpsPage
      title="Connecting your GitHub account"
      subtitle="Finishing the authorization you just approved on GitHub."
      backTo="/ops"
    >
      <div className={`${OPS_CARD} p-6 max-w-xl space-y-4`}>
        {settled === null && <p className="text-sm text-slate-500">Finishing up…</p>}

        {settled?.phase === 'success' && (
          <>
            <h2 className="text-lg font-semibold text-slate-900">{settled.headline}</h2>
            <p className="text-sm text-slate-600">
              The deployments you trigger from AGP will now be carried out under your own GitHub
              identity, so the organization&rsquo;s audit log records them as you.
            </p>
            <Link to="/ops/github-link" className={`${OPS_PRIMARY_BTN} inline-block`}>
              Back to your GitHub account
            </Link>
          </>
        )}

        {settled?.phase === 'error' && (
          <>
            <h2 className="text-lg font-semibold text-slate-900">
              Couldn&rsquo;t finish connecting your account
            </h2>
            <p
              className={`text-sm ${settled.retryable ? 'text-amber-700' : 'text-rose-600'}`}
              role="alert"
            >
              {settled.message}
            </p>
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
