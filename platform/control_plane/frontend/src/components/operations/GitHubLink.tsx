import { useCallback, useEffect, useRef, useState } from 'react';
import { Link } from 'react-router-dom';
import { githubLinkApi } from '../../api/client';
import { useLoad } from '../../api/useLoad';
import OpsPage from './OpsPage';
import { OPS_BADGE, OPS_CARD, OPS_PRIMARY_BTN } from './opsUi';
import {
  AGP_GITHUB_LINK_STATE_KEY,
  buildLinkRedirectUrl,
  classifyLinkError,
  deriveLinkCardState,
  linkCardCopy,
} from './githubLinkModel';

const PILL_SHAPE =
  'inline-flex items-center gap-1.5 text-[11px] font-semibold px-2 py-0.5 rounded-full';
const SECONDARY_BTN =
  'px-2.5 py-1 rounded-md bg-white border border-slate-300 text-slate-700 text-xs font-medium hover:bg-slate-50 transition-colors disabled:opacity-40';
const DANGER_BTN =
  'px-2.5 py-1 rounded-md bg-white border border-rose-300 text-rose-600 text-xs font-medium hover:bg-rose-50 transition-colors disabled:opacity-40';
const DANGER_CONFIRM_BTN =
  'px-2.5 py-1 rounded-md bg-rose-600 text-white text-xs font-semibold hover:bg-rose-700 transition-colors disabled:opacity-40';

function formatVerified(iso: string | null): string {
  if (!iso) return 'not yet verified';
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return `verified ${d.toLocaleString()}`;
}

export default function GitHubLink({
  embedded = false,
}: { embedded?: boolean }) {
  const { state, reload: refetch } = useLoad(() => githubLinkApi.get(), [], 'Could not load your GitHub links.');
  const view = state.data;
  const loading = state.kind === 'loading';
  const error =
    state.kind === 'error'
      ? classifyLinkError(state.failure ?? state.message, 'Could not load your GitHub links.').message
      : null;

  const [pendingId, setPendingId] = useState<string | null>(null);
  const [confirmUnlink, setConfirmUnlink] = useState<string | null>(null);
  const [actionError, setActionError] = useState<
    { connectionId: string; message: string; retryable: boolean } | null
  >(null);
  const [notice, setNotice] = useState<{ connectionId: string; message: string } | null>(null);

  const mountedRef = useRef(true);
  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
    };
  }, []);

  const unlinkTriggerRefs = useRef<Record<string, HTMLButtonElement | null>>({});
  const unlinkConfirmRef = useRef<HTMLButtonElement>(null);
  useEffect(() => {
    if (confirmUnlink) unlinkConfirmRef.current?.focus();
  }, [confirmUnlink]);

  const closeConfirm = useCallback((connectionId: string) => {
    setConfirmUnlink(null);
    unlinkTriggerRefs.current[connectionId]?.focus();
  }, []);

  const handleConnect = useCallback(
    async (connectionId: string) => {
      if (pendingId) return;
      setPendingId(connectionId);
      setActionError(null);
      setNotice(null);
      try {
        const { authorize_url, state } = await githubLinkApi.start({
          connection_id: connectionId,
          redirect_uri: buildLinkRedirectUrl(window.location.origin),
        });
        window.localStorage.setItem(AGP_GITHUB_LINK_STATE_KEY, state);
        window.location.href = authorize_url;
      } catch (err: unknown) {
        if (!mountedRef.current) return;
        setActionError({
          connectionId,
          ...classifyLinkError(err, 'Could not start the GitHub authorization.'),
        });
        setPendingId(null);
      }
    },
    [pendingId],
  );

  const handleVerify = useCallback(
    async (connectionId: string) => {
      if (pendingId) return;
      setPendingId(connectionId);
      setActionError(null);
      setNotice(null);
      try {
        await githubLinkApi.verify(connectionId);
        if (mountedRef.current) {
          setNotice({ connectionId, message: 'Checked with GitHub just now.' });
          refetch();
        }
      } catch (err: unknown) {
        if (mountedRef.current) {
          setActionError({
            connectionId,
            ...classifyLinkError(err, 'Could not check the link with GitHub.'),
          });
          refetch();
        }
      } finally {
        if (mountedRef.current) setPendingId(null);
      }
    },
    [pendingId, refetch],
  );

  const handleUnlink = useCallback(
    async (connectionId: string) => {
      if (pendingId) return;
      setPendingId(connectionId);
      setActionError(null);
      setNotice(null);
      try {
        await githubLinkApi.unlink(connectionId);
        if (mountedRef.current) {
          setConfirmUnlink(null);
          setNotice({
            connectionId,
            message: 'Disconnected. Your future actions run as the platform App again.',
          });
          refetch();
        }
      } catch (err: unknown) {
        if (mountedRef.current) {
          setActionError({
            connectionId,
            ...classifyLinkError(err, 'Could not disconnect the account.'),
          });
        }
      } finally {
        if (mountedRef.current) setPendingId(null);
      }
    },
    [pendingId, refetch],
  );

  const connections = view?.connections ?? [];
  const links = view?.links ?? [];

  const body = (
    <div className="max-w-3xl space-y-3">
      <div className={`${OPS_CARD} p-5`}>
        <h2 className="text-sm font-semibold text-slate-900">Why link an account</h2>
        <p className="text-sm text-slate-600 mt-1.5 leading-relaxed">
          Today, everything AGP does on GitHub for you — creating a repository, shipping a
          release — is performed by your organization&rsquo;s platform GitHub App. In the org
          audit log they all look identical, whoever asked for them. Link your account and the
          actions you trigger are carried out under your own GitHub identity instead, so the
          record on GitHub matches the person who made the decision here.
        </p>
        <p className="text-xs text-slate-500 mt-2.5 leading-relaxed">
          AGP stores a GitHub token for you in Secrets Manager and uses it only for the
          deployment actions you start. It never sees your GitHub password, and disconnecting
          revokes the authorization at GitHub. The account shown below is the one you authorized
          on GitHub — AGP records the login GitHub returned and makes no further claim about who
          owns it.
        </p>
      </div>

      {error ? (
        <div className="bg-white/70 backdrop-blur rounded-xl border border-rose-200/70 shadow-sm p-6">
          <h3 className="text-sm font-semibold text-rose-700">Couldn&rsquo;t load your GitHub links</h3>
          <p className="text-sm text-slate-600 mt-1">{error}</p>
          <button type="button" onClick={refetch} className={`${SECONDARY_BTN} mt-3`}>
            Retry
          </button>
        </div>
      ) : loading ? (
        <div className={`${OPS_CARD} p-6`}>
          <p className="text-sm text-slate-500">Loading your GitHub links…</p>
        </div>
      ) : connections.length === 0 ? (
        <div className={`${OPS_CARD} p-6`}>
          <h3 className="text-sm font-semibold text-slate-900">No organizations to link yet</h3>
          <p className="text-sm text-slate-600 mt-1">
            Linking is offered per GitHub organization AGP is connected to. Once an admin adds a
            GitHub App connection in{' '}
            <Link to="/ops/settings#org-connections" className="text-emerald-700 hover:underline">
              Settings → Organization connections
            </Link>
            , it appears here.
          </p>
        </div>
      ) : (
        connections.map((conn) => {
          const link = links.find((l) => l.connection_id === conn.connection_id);
          const cardState = deriveLinkCardState(link, conn.oauth_client_ready);
          const copy = linkCardCopy(cardState, link?.github_login ?? null);
          const isLinked = cardState === 'linked';
          const pending = pendingId === conn.connection_id;
          const anyPending = pendingId !== null;
          const confirming = confirmUnlink === conn.connection_id;
          const rowError =
            actionError?.connectionId === conn.connection_id ? actionError : null;
          const rowNotice = notice?.connectionId === conn.connection_id ? notice : null;
          const rotating = link?.status === 'refreshing';

          return (
            <div key={conn.connection_id} className={`${OPS_CARD} p-5`}>
              <div className="flex items-start justify-between gap-4">
                <div className="min-w-0">
                  <div className="flex items-center gap-2">
                    <span className="text-sm font-semibold text-slate-900 truncate" title={conn.org}>
                      {conn.org}
                    </span>
                    {isLinked && (
                      <span className={`${PILL_SHAPE} ${OPS_BADGE.ready}`}>Linked</span>
                    )}
                    {rotating && (
                      <span
                        className={`${PILL_SHAPE} ${OPS_BADGE.provisioning}`}
                        title="AGP is rotating your GitHub token — nothing for you to do."
                      >
                        Refreshing token
                      </span>
                    )}
                  </div>
                  <p className="text-sm text-slate-600 mt-1">{copy.headline}</p>
                  {isLinked && (
                    <p className="text-[11px] text-slate-400 mt-1">
                      {formatVerified(link?.last_verified_at ?? null)}
                    </p>
                  )}
                </div>

                <div className="flex shrink-0 items-center gap-1.5">
                  {copy.action !== null &&
                    !confirming &&
                    (isLinked ? (
                      <button
                        type="button"
                        onClick={() => void handleVerify(conn.connection_id)}
                        disabled={anyPending}
                        aria-label={`${copy.action} the GitHub link for ${conn.org}`}
                        className={SECONDARY_BTN}
                      >
                        {pending ? 'Checking…' : copy.action}
                      </button>
                    ) : (
                      <button
                        type="button"
                        onClick={() => void handleConnect(conn.connection_id)}
                        disabled={anyPending}
                        aria-label={`${copy.action} for ${conn.org}`}
                        className={`${OPS_PRIMARY_BTN} disabled:opacity-40`}
                      >
                        {pending ? 'Redirecting…' : copy.action}
                      </button>
                    ))}
                  {isLinked && !confirming && (
                    <button
                      ref={(el) => {
                        unlinkTriggerRefs.current[conn.connection_id] = el;
                      }}
                      type="button"
                      onClick={() => {
                        setConfirmUnlink(conn.connection_id);
                        setActionError(null);
                      }}
                      disabled={anyPending}
                      aria-label={`Disconnect the GitHub account linked for ${conn.org}`}
                      className={DANGER_BTN}
                    >
                      Disconnect
                    </button>
                  )}
                </div>
              </div>

              {confirming && (
                <div className="mt-3 pt-3 border-t border-emerald-100/70">
                  <p className="text-sm text-slate-700">
                    Disconnect this account from{' '}
                    <span className="font-medium text-slate-900">{conn.org}</span>?
                  </p>
                  <p className="text-xs text-slate-500 mt-1 leading-relaxed">
                    AGP revokes its authorization at GitHub and deletes the stored token. Your
                    deployments keep working — they go back to running as the platform App, so
                    GitHub records them under the App instead of under you. You can link again at
                    any time.
                  </p>
                  <div className="flex items-center gap-2 mt-3">
                    <button
                      ref={unlinkConfirmRef}
                      type="button"
                      onClick={() => void handleUnlink(conn.connection_id)}
                      disabled={pending}
                      className={DANGER_CONFIRM_BTN}
                    >
                      {pending ? 'Disconnecting…' : 'Disconnect'}
                    </button>
                    <button
                      type="button"
                      onClick={() => closeConfirm(conn.connection_id)}
                      disabled={pending}
                      className={SECONDARY_BTN}
                    >
                      Cancel
                    </button>
                  </div>
                </div>
              )}

              {rowNotice && (
                <p className="text-xs text-emerald-700 mt-2.5" role="status">
                  {rowNotice.message}
                </p>
              )}
              {rowError && (
                <p
                  className={`text-xs mt-2.5 ${rowError.retryable ? 'text-amber-700' : 'text-rose-600'}`}
                  role="alert"
                >
                  {rowError.message}
                </p>
              )}
            </div>
          );
        })
      )}
    </div>
  );

  if (embedded) return body;

  return (
    <OpsPage
      title="Your GitHub account"
      subtitle="Link your personal GitHub account so the deployments you trigger here are recorded as you."
      backTo="/ops"
    >
      {body}
    </OpsPage>
  );
}
