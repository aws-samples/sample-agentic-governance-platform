// ConnectionCallback — the GitHub App Manifest callback landing (Epic 20 / U4).
//
// Last step of the App-via-Manifest browser flow. In U3 the AddConnectionModal
// POSTs a hidden manifest form to GitHub; GitHub creates the App and redirects
// the operator back to `/ops/connections/callback?code=&state=`. This page
// converts that code into a Connection and, if the App isn't installed yet,
// walks the operator through installing it.
//
// Correctness load-bearers:
//   • The manifest `code` is SINGLE-USE — calling manifestCallback twice fails.
//     A useRef ran-once guard makes the on-mount conversion fire exactly once
//     (React 19 StrictMode double-invokes effects in dev).
//   • CSRF echo: the returned `state` must equal the value the modal stashed in
//     localStorage['agp_manifest_state'] (localStorage, not sessionStorage — the
//     modal opens GitHub in a new tab and this callback lands there); we validate +
//     clear it before any backend call (resolveCallbackParams). A mismatch never
//     touches the backend.
//   • A callback can error AFTER a pending Connection exists (by-design). The
//     install step is still reachable via finalize() on that pending row, so an
//     error here does NOT mean nothing was created — the operator retries from
//     Org Connections.

import { useCallback, useEffect, useRef, useState } from 'react';
import { useNavigate, useSearchParams } from 'react-router-dom';
import { connectionsApi } from '../../api/client';
import type { Connection } from '../../api/client';
import OpsPage from './OpsPage';
import { OPS_CARD, OPS_PRIMARY_BTN } from './opsUi';
import { AGP_MANIFEST_STATE_KEY, resolveCallbackParams } from './connectionCallbackParams';

type Phase = 'loading' | 'install' | 'success' | 'error';

const SECONDARY_BTN =
  'px-3.5 py-1.5 rounded-lg bg-white border border-slate-300 text-slate-700 text-sm font-medium hover:bg-slate-50 transition-colors disabled:opacity-40';
const FIELD_INPUT =
  'w-full px-3 py-2 text-sm rounded-lg border border-slate-300 focus:outline-none focus:ring-2 focus:ring-emerald-500/40 disabled:opacity-40';

export default function ConnectionCallback() {
  const [searchParams] = useSearchParams();
  const navigate = useNavigate();

  const [phase, setPhase] = useState<Phase>('loading');
  const [error, setError] = useState<string | null>(null);
  const [connection, setConnection] = useState<Connection | null>(null);
  const [installUrl, setInstallUrl] = useState<string | null>(null);

  // Manual-install-ID fallback.
  const [showManual, setShowManual] = useState(false);
  const [installationId, setInstallationId] = useState('');
  const [finalizing, setFinalizing] = useState(false);

  // Ran-once guard — the manifest code is single-use; StrictMode double-invokes
  // effects in dev, so a second manifestCallback would fail on a spent code.
  const ranRef = useRef(false);

  useEffect(() => {
    if (ranRef.current) return;
    ranRef.current = true;

    // localStorage (NOT sessionStorage): the modal opens GitHub in a new tab and this callback
    // lands in that new tab — sessionStorage is per-tab and would be empty here.
    const sessionState = window.localStorage.getItem(AGP_MANIFEST_STATE_KEY);
    const resolved = resolveCallbackParams(searchParams, sessionState);
    // CSRF state is single-use — clear it whether the echo matched or not.
    window.localStorage.removeItem(AGP_MANIFEST_STATE_KEY);

    if ('error' in resolved) {
      setError(resolved.error);
      setPhase('error');
      return;
    }

    connectionsApi
      .manifestCallback({ code: resolved.code, state: resolved.state })
      .then((res) => {
        setConnection(res.connection);
        if (res.needs_install) {
          setInstallUrl(res.install_url);
          setPhase('install');
        } else {
          setPhase('success');
        }
      })
      .catch((err: unknown) => {
        setError(err instanceof Error ? err.message : 'Failed to complete GitHub App setup.');
        setPhase('error');
      });
  }, [searchParams]);

  // Finalize the pending connection. Empty installationId → auto-resolve ({}).
  const handleFinalize = useCallback(
    async (id: string, manualId?: string) => {
      if (finalizing) return;
      setFinalizing(true);
      setError(null);
      try {
        const trimmed = manualId?.trim();
        const conn = await connectionsApi.finalize(
          id,
          trimmed ? { installation_id: trimmed } : {},
        );
        setConnection(conn);
        setPhase('success');
      } catch (err: unknown) {
        setError(err instanceof Error ? err.message : 'Could not finish the connection yet.');
      } finally {
        setFinalizing(false);
      }
    },
    [finalizing],
  );

  return (
    <OpsPage
      title="Connect GitHub App"
      subtitle="Finishing the GitHub App handshake for your organization."
      // E28/T8: back to the Organization connections SECTION, not just to Settings. The
      // `/ops/admin` redirect this used to rely on lands on the General tab, one click short
      // of the panel the operator was configuring — and a <Navigate> drops the fragment, so
      // this cannot be fixed at the redirect. The anchor is the `settingsSections.ts` section
      // id, and Settings resolves the owning tab from it.
      backTo="/ops/settings#org-connections"
    >
      <div className={`${OPS_CARD} p-6 max-w-xl space-y-4`}>
        {phase === 'loading' && (
          <p className="text-sm text-slate-500">Completing GitHub App setup…</p>
        )}

        {phase === 'success' && (
          <>
            <h2 className="text-lg font-semibold text-slate-900">GitHub App connected</h2>
            <p className="text-sm text-slate-600">
              {connection
                ? `${connection.org} is connected and ready to use.`
                : 'Your GitHub App is connected and ready to use.'}
            </p>
            {/* THE SEEDING ROUTE (E28C/T6, design D-C3). Connecting an org stays WRITE-FREE — this
                handshake created no repository — so the cold start needs a pointer rather than a
                page that looks finished and is empty.

                It is a LINK the operator clicks, never a redirect: routing them automatically would
                make "Nothing is created until you confirm" a promise about a screen they did not ask
                for, and the whole reason this surface exists is that its preview IS the consent. The
                `seed=1` param only switches the prompt on; the surface still executes nothing until
                a confirm. */}
            {connection && (
              <p className="text-sm text-slate-600">
                Nothing has been created in {connection.org} yet. Seed AGP’s starter templates, or
                adopt repositories it already has, from the reconcile surface — it shows exactly what
                would change before anything is written.
              </p>
            )}
            <div className="flex flex-wrap items-center gap-2">
              {connection && (
                <button
                  type="button"
                  onClick={() =>
                    navigate(`/ops/templates?connection=${encodeURIComponent(connection.id)}&seed=1`)
                  }
                  className={OPS_PRIMARY_BTN}
                >
                  Seed or adopt templates
                </button>
              )}
              <button
                type="button"
                onClick={() => navigate('/ops/settings#org-connections')}
                className={SECONDARY_BTN}
              >
                Back to organization connections
              </button>
            </div>
          </>
        )}

        {phase === 'install' && connection && (
          <>
            <h2 className="text-lg font-semibold text-slate-900">Install the GitHub App</h2>
            <p className="text-sm text-slate-600">
              The GitHub App for <span className="font-medium text-slate-800">{connection.org}</span>{' '}
              was created. Install it on your organization, then finish here.
            </p>

            <div className="flex flex-wrap items-center gap-2">
              {installUrl && (
                <button
                  type="button"
                  onClick={() => {
                    window.location.href = installUrl;
                  }}
                  className={OPS_PRIMARY_BTN}
                >
                  Install on GitHub
                </button>
              )}
              <button
                type="button"
                onClick={() => void handleFinalize(connection.id)}
                disabled={finalizing}
                className={SECONDARY_BTN}
              >
                {finalizing ? 'Finishing…' : "I've installed it — finish"}
              </button>
            </div>

            <div className="pt-2 border-t border-emerald-100/70">
              <button
                type="button"
                onClick={() => setShowManual((v) => !v)}
                aria-expanded={showManual}
                className="text-xs text-slate-500 hover:text-slate-700 transition-colors font-medium"
              >
                {showManual ? '− Hide' : '+ Enter Installation ID manually'}
              </button>
              {showManual && (
                <div className="mt-2 space-y-2">
                  <label
                    htmlFor="callback-install-id"
                    className="block text-[11px] uppercase tracking-wide text-slate-400 font-medium"
                  >
                    Installation ID
                  </label>
                  <input
                    id="callback-install-id"
                    type="text"
                    inputMode="numeric"
                    value={installationId}
                    onChange={(e) => setInstallationId(e.target.value)}
                    disabled={finalizing}
                    placeholder="7654321"
                    className={FIELD_INPUT}
                    autoComplete="off"
                  />
                  <button
                    type="button"
                    onClick={() => void handleFinalize(connection.id, installationId)}
                    disabled={finalizing || installationId.trim().length === 0}
                    className={SECONDARY_BTN}
                  >
                    {finalizing ? 'Finishing…' : 'Finish with this ID'}
                  </button>
                </div>
              )}
            </div>
          </>
        )}

        {phase === 'error' && (
          <>
            <h2 className="text-lg font-semibold text-slate-900">Couldn’t finish GitHub App setup</h2>
            <p className="text-sm text-slate-600">
              You can finish setup later from Org Connections: a pending connection shows a{' '}
              <span className="font-medium text-slate-800">Finish setup</span> action — install the
              App on your org, then click it to complete the connection.
            </p>
            <button
              type="button"
              onClick={() => navigate('/ops/settings#org-connections')}
              className={OPS_PRIMARY_BTN}
            >
              Back to organization connections
            </button>
          </>
        )}

        {error && (
          <p className="text-sm text-rose-600" role="alert">
            {error}
          </p>
        )}
      </div>
    </OpsPage>
  );
}
