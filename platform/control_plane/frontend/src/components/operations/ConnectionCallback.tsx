import { useCallback, useEffect, useRef, useState } from 'react';
import { useNavigate, useSearchParams } from 'react-router-dom';
import { connectionsApi } from '../../api/client';
import type { Connection, ManifestCallbackResponse } from '../../api/client';
import { safeLocalStorage } from '../../api/safeLocalStorage';
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

  const [showManual, setShowManual] = useState(false);
  const [installationId, setInstallationId] = useState('');
  const [finalizing, setFinalizing] = useState(false);

  // Decision: docs/adr/frontend-loading-state.md
  const ranRef = useRef(false);

  useEffect(() => {
    if (ranRef.current) return;
    ranRef.current = true;

    const sessionState = safeLocalStorage.getItem(AGP_MANIFEST_STATE_KEY);
    const resolved = resolveCallbackParams(searchParams, sessionState);
    safeLocalStorage.removeItem(AGP_MANIFEST_STATE_KEY);

    const exchange =
      'error' in resolved
        ? Promise.reject<ManifestCallbackResponse>(new Error(resolved.error))
        : connectionsApi.manifestCallback({ code: resolved.code, state: resolved.state });
    exchange
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
