// GitHubLink — the per-user "connect your GitHub account" surface (Epic 27B). Two hosts,
// one component: its own route `/ops/github-link`, and (E28/T8) the first General section
// of `/ops/settings`, where personal settings now live alongside each other. `embedded`
// selects which — see the prop.
//
// This page is deliberately THIN. Every decision — which card state a row is in, what it
// says, whether it offers an action at all, and whether a failure is worth retrying —
// lives in `githubLink.ts`, because only `src/**/*.test.ts` is collected by vitest and
// there is no jsdom: logic placed here could never be tested. What is left is layout.
//
// Three things this page must get right, in the order they bite:
//   • The value proposition. Without the explainer, "link your GitHub account" reads as
//     busywork. What it actually changes is ATTRIBUTION: the same deploy, recorded in the
//     org's audit log as the human rather than as the platform App.
//   • `linkCardCopy(...).action === null` ⇒ NO button. `disabled` is reserved for
//     in-flight work (the E27 rule) — it is not how an unavailable capability is shown.
//   • `revoked` and `unlinked` differ ONLY in the button label; both run the SAME
//     authorize flow. Wiring a reconnect to `verify` would re-probe an already-dead token
//     and return the same revoked answer — a dead-end loop with no error.
//
// `github_login` is a GITHUB identity, and the only thing behind it is the OAuth consent
// the human gave. It is never an AGP principal (those are Entra oids) and AGP has verified
// nothing about its ownership — the copy says so rather than implying a vouch.

import { useCallback, useEffect, useRef, useState } from 'react';
import { Link } from 'react-router-dom';
import { githubLinkApi } from '../../api/client';
import type { GitHubLinkView } from '../../api/client';
import OpsPage from './OpsPage';
import { OPS_BADGE, OPS_CARD, OPS_PRIMARY_BTN } from './opsUi';
import {
  AGP_GITHUB_LINK_STATE_KEY,
  buildLinkRedirectUrl,
  classifyLinkError,
  deriveLinkCardState,
  linkCardCopy,
} from './githubLink';

const PILL_SHAPE =
  'inline-flex items-center gap-1.5 text-[11px] font-semibold px-2 py-0.5 rounded-full';
const SECONDARY_BTN =
  'px-2.5 py-1 rounded-md bg-white border border-slate-300 text-slate-700 text-xs font-medium hover:bg-slate-50 transition-colors disabled:opacity-40';
// Unlink is destructive but recoverable (the human can re-link in two clicks), so it gets
// rose TEXT on white rather than a rose fill — the weight the delete-repo checklist owns.
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
  // When true, render only the body — no OpsPage frame. The Settings page owns the frame
  // (one `<h1>`, one back link) and the section band supplies the heading, so wrapping the
  // page frame again would nest two `max-w-7xl` containers and put a second `<h1>` on the
  // document. Everything below the frame is byte-identical in both hosts on purpose: this
  // is ONE surface with two doors, not a page and a copy of it.
  embedded = false,
}: { embedded?: boolean }) {
  const [view, setView] = useState<GitHubLinkView | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [reloadNonce, setReloadNonce] = useState(0);

  // The connection whose action is in flight — the ONLY thing that drives `disabled`.
  const [pendingId, setPendingId] = useState<string | null>(null);
  // The connection whose unlink confirm is REVEALED (null = none). Reveal-then-confirm,
  // following AgentDetail's lifecycle idiom and E27's promote confirm — not
  // `window.confirm`, which cannot name the org or be styled.
  const [confirmUnlink, setConfirmUnlink] = useState<string | null>(null);
  // A mapped failure sentence, attributed to the row it belongs to. `retryable` is what
  // separates "wait a moment" (refresh contention, an AWS/GitHub blip — normal operation)
  // from a terminal refusal; it drives the tint, never a raw backend literal.
  const [actionError, setActionError] = useState<
    { connectionId: string; message: string; retryable: boolean } | null
  >(null);
  // A politely-announced outcome after a successful verify — the pill and timestamp
  // changing are otherwise a silent visual diff.
  const [notice, setNotice] = useState<{ connectionId: string; message: string } | null>(null);

  const mountedRef = useRef(true);
  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
    };
  }, []);

  // Focus goes to the confirm on reveal and RETURNS to the trigger on cancel — otherwise
  // dismissing the confirm drops focus to document start.
  const unlinkTriggerRefs = useRef<Record<string, HTMLButtonElement | null>>({});
  const unlinkConfirmRef = useRef<HTMLButtonElement>(null);
  useEffect(() => {
    if (confirmUnlink) unlinkConfirmRef.current?.focus();
  }, [confirmUnlink]);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    githubLinkApi
      .get()
      .then((v) => {
        if (cancelled) return;
        setView(v);
        setError(null);
      })
      .catch((err: unknown) => {
        if (cancelled) return;
        setError(classifyLinkError(err, 'Could not load your GitHub links.').message);
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [reloadNonce]);

  const refetch = useCallback(() => setReloadNonce((n) => n + 1), []);

  const closeConfirm = useCallback((connectionId: string) => {
    setConfirmUnlink(null);
    unlinkTriggerRefs.current[connectionId]?.focus();
  }, []);

  // Start the authorize handshake. Same-tab redirect (unlike the admin manifest form there
  // is no `<form method="POST">` and no new tab), so localStorage is belt-and-braces — the
  // callback still clears it unconditionally, keeping the state single-use.
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
        // No cleanup on the success path: the browser is leaving this document, and
        // clearing `pendingId` would flip the button back to idle mid-navigation.
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
          // A revoked authorization is a real state change, not just a failed call — the
          // row must re-read so the card offers a reconnect instead of another check.
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
      {/* The value proposition. Attribution is the entire point, and it is not
          self-evident from the word "link" — so it is stated before any control. */}
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
            {/* E28/T8: "Operations Admin" no longer exists — that console became Settings, and
                org connections is a section on its Admin tab. Deep-linked to the section so the
                sentence names a real destination a reader can act on. */}
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
          // The rotation spinner reads the WIRE status directly: `LinkCardState` folds
          // `refreshing` into `linked` on purpose (the link works; only the token
          // rotation is in flight), so it is not recoverable from `cardState`.
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

                {/* Actions. `copy.action === null` renders nothing at all — an
                    unavailable capability is absent, not disabled. */}
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
                      // `unlinked` and `revoked` both land here: the token is gone
                      // either way, so both run the authorize flow. A reconnect wired
                      // to `verify` would loop on a dead token forever.
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

              {/* Reveal-then-confirm. It names what actually changes, because
                  "disconnect" alone doesn't say that the actions keep working — they
                  just stop being attributed to the human. */}
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
                // A retryable failure is normal operation (another task holds the token
                // refresh claim, or AWS/GitHub blipped) — amber and a wait, not rose and
                // a dead end.
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
