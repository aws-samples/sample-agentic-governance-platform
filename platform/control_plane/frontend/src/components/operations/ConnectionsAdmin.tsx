// ConnectionsAdmin — the Git org-connections admin panel (Epic 19), rendered as
// the "Org Connections" tab inside the Operations Admin console.
//
// Admin-gated (role_level >= 2). Like UsersAdmin the non-admin path returns null:
// the OperationsAdmin shell already shows the not-authorized panel, so this tab
// body never renders for non-admins. Skeleton cloned from UsersAdmin: the
// cancelled-guard loader keyed on [isAdmin, reloadNonce] calling
// connectionsApi.list(), the refetch nonce-bump, the shared runAction(id, fn)
// single-flight mutation helper, the glass-CARD table, inline loading/error/empty
// states, the per-row disabled/… spinner idiom, and the top-level actionError
// <p role="alert">.
// Layout:
//   • a header row — a short description <p> + an "Add connection" primary button;
//   • the glass-CARD table of connectionsApi.list() rows. Columns:
//     Provider · Org/Group · Status pill (connected = emerald, error = rose) ·
//     Account (account_login or —) · Last verified (last_verified_at or —) ·
//     actions: ONE inline health verb + a [⋮] menu. WHICH verb, what the menu
//     holds, its order and where its divider falls are decided by
//     `connectionRowActions.ts` (E28D/T4) — this file only renders that list, and
//     nothing about the split may be re-decided here. That module's header carries
//     the rule and the reasons; the short version is that a judgement made in a
//     1400-line `.tsx` is a judgement no test in this project can reach.
//   • Add-connection opens AddConnectionModal; Replace-token opens
//     ReplaceTokenModal. Both close + refetch on success.
//   • The reconcile/rollout modal is NOT mounted here. The menu's "Roll out
//     templates" NAVIGATES to `/ops/templates?connection=<id>` — one surface mounts
//     that modal and owns the template state it mutates.
// Write-only secret discipline (new pattern, no prior art): the token is sent on
// create/replace only and is NEVER returned by any GET — the row shows status +
// last_verified_at, never a token value or masked echo. "Replace token" is the
// only token affordance.

import { useCallback, useEffect, useRef, useState } from 'react';
import { Link } from 'react-router-dom';
import { connectionsApi } from '../../api/client';
import type { Connection, ConnectionAuthType, ConnectionCreate, ConnStatus, Provider } from '../../api/client';
import { useUser } from '../../contexts/UserContext';
// The row's action split + the amber sub-line's state, as pure functions (E28D/T4). This file
// renders them verbatim — see that module's header for the rule and for why the reconcile modal is
// no longer mounted here.
import {
  connectionRowActions,
  oauthClientSubline,
  templatesReconcilePath,
  type RowAction,
} from './connectionRowActions';
import { buildInstallSettingsUrl, buildManifestRedirectUrl, manifestFormFields } from './manifestForm';
import { buildLinkRedirectUrl } from './githubLink';

const CARD = 'bg-white/70 backdrop-blur rounded-xl border border-slate-200/60 shadow-sm';

// Connection-status pill — same semantic tints as the rest of the governance UI
// (emerald = healthy, rose = error). Mirrors MarketplaceAdmin's STATUS_BADGE shape.
const STATUS_PILL: Record<ConnStatus, { cls: string; label: string }> = {
  connected: { cls: 'bg-emerald-50 text-emerald-700', label: 'Connected' },
  error: { cls: 'bg-rose-50 text-rose-700', label: 'Error' },
  pending: { cls: 'bg-amber-50 text-amber-700', label: 'Pending install' },
};

const PROVIDER_LABEL: Record<Provider, string> = {
  github: 'GitHub',
  gitlab: 'GitLab',
};

// SaaS API base defaults — shown as the Base URL placeholder so an operator
// leaving it blank knows where the connection will point.
const PROVIDER_BASE_PLACEHOLDER: Record<Provider, string> = {
  github: 'https://api.github.com',
  gitlab: 'https://gitlab.com',
};

// Human date for last_verified_at; falls back to em-dash when never verified.
function formatVerified(iso: string | null): string {
  if (!iso) return '—';
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return d.toLocaleString();
}

export default function ConnectionsAdmin() {
  const { user } = useUser();
  const isAdmin = (user?.role_level ?? 0) >= 2;

  const [connections, setConnections] = useState<Connection[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [reloadNonce, setReloadNonce] = useState(0);

  // Per-row action state (Re-test / Delete).
  const [actionPendingId, setActionPendingId] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);

  const [showAdd, setShowAdd] = useState(false);
  // The connection whose token is being replaced (null = modal closed).
  const [replaceTarget, setReplaceTarget] = useState<Connection | null>(null);
  // The pending github_app connection being finished via "Finish setup" (null = closed).
  const [finishTarget, setFinishTarget] = useState<Connection | null>(null);
  // NO rollout/reconcile modal state here any more (E28D/T4). The reconcile surface had two mount
  // sites under two labels, and this one never refetched the template catalog after a write — so an
  // operator who rolled out from this page saw a stale catalog on the page that owns template state.
  // The row's kebab now NAVIGATES to `/ops/templates?connection=<id>` instead; that page is the one
  // surface that mounts the modal and owns the state it mutates.
  // The github_app connection whose OAuth-client paste modal is open (null = closed).
  const [oauthTarget, setOauthTarget] = useState<Connection | null>(null);

  // Load the connections. Skipped entirely for non-admins (this tab body never
  // renders for them — the OperationsAdmin shell short-circuits to the
  // not-authorized panel).
  useEffect(() => {
    if (!isAdmin) {
      setLoading(false);
      return;
    }
    let cancelled = false;
    setLoading(true);
    connectionsApi
      .list()
      .then((rows) => {
        if (cancelled) return;
        setConnections(rows);
        setError(null);
      })
      .catch((err: unknown) => {
        if (cancelled) return;
        setError(err instanceof Error ? err.message : 'Failed to load connections.');
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [isAdmin, reloadNonce]);

  const refetch = useCallback(() => setReloadNonce((n) => n + 1), []);

  const runAction = useCallback(
    async (id: string, fn: () => Promise<unknown>) => {
      if (actionPendingId) return;
      setActionPendingId(id);
      setActionError(null);
      try {
        await fn();
        refetch();
      } catch (err: unknown) {
        setActionError(err instanceof Error ? err.message : 'Action failed.');
      } finally {
        setActionPendingId(null);
      }
    },
    [actionPendingId, refetch],
  );

  const handleTest = useCallback(
    (id: string) => {
      void runAction(id, () => connectionsApi.test(id));
    },
    [runAction],
  );
  const handleRemove = useCallback(
    (id: string) => {
      if (
        !window.confirm(
          'Delete this connection? Its stored token will be permanently removed from Secrets Manager.',
        )
      ) {
        return;
      }
      void runAction(id, () => connectionsApi.remove(id));
    },
    [runAction],
  );

  // The non-admin path returns null — the OperationsAdmin shell already renders
  // the not-authorized panel, so there is nothing to show here.
  if (!isAdmin) return null;

  return (
    <div className="space-y-3">
      {/* Header row: description + Add connection. */}
      <div className="flex items-start justify-between gap-3">
        <p className="text-xs text-slate-500">
          Connect a GitHub organization (via a GitHub App or a personal access token) or a GitLab
          group. Credentials are verified on save and stored in Secrets Manager — they are never
          displayed again.
        </p>
        <div className="flex shrink-0 items-center gap-2">
          <button
            type="button"
            onClick={refetch}
            disabled={loading}
            title="Refresh the connections list"
            className="px-3.5 py-1.5 rounded-lg bg-white border border-slate-300 text-slate-700 text-sm font-medium hover:bg-slate-50 transition-colors disabled:opacity-40"
          >
            {loading ? 'Refreshing…' : 'Refresh'}
          </button>
          <button
            type="button"
            onClick={() => setShowAdd(true)}
            className="px-3.5 py-1.5 rounded-lg bg-blue-600 text-white text-sm font-medium hover:bg-blue-700 transition-colors"
          >
            Add connection
          </button>
        </div>
      </div>

      {actionError && (
        <p className="text-sm text-red-600" role="alert">
          {actionError}
        </p>
      )}

      {/* Table. */}
      {error ? (
        <div className="bg-white/70 backdrop-blur rounded-xl border border-red-200/70 shadow-sm p-6">
          <h3 className="text-sm font-semibold text-red-700">Couldn’t load connections</h3>
          <p className="text-sm text-slate-600 mt-1">{error}</p>
          <button
            onClick={refetch}
            className="mt-3 px-3 py-1.5 rounded-lg bg-white border border-slate-300 text-slate-700 text-xs font-medium hover:bg-slate-50 transition-colors"
          >
            Retry
          </button>
        </div>
      ) : (
        <div className={`${CARD} overflow-visible`}>
          <table className="w-full text-sm">
            <thead className="bg-slate-50/80 text-slate-500 text-xs uppercase tracking-wide">
              <tr>
                <th className="text-left font-medium px-4 py-2">Provider</th>
                <th className="text-left font-medium px-4 py-2">Org / Group</th>
                <th className="text-left font-medium px-4 py-2">Status</th>
                <th className="text-left font-medium px-4 py-2">Account</th>
                <th className="text-left font-medium px-4 py-2">Last verified</th>
                <th className="text-right font-medium px-4 py-2">Actions</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-slate-100">
              {loading && (
                <tr>
                  <td colSpan={6} className="px-4 py-8 text-center text-slate-400 text-sm">
                    Loading connections…
                  </td>
                </tr>
              )}

              {!loading &&
                connections.map((c) => {
                  const rowPending = actionPendingId === c.id;
                  // While ANY row's action is in flight, disable every row's controls:
                  // runAction is single-flight (it drops a second concurrent call), so an
                  // enabled control on another row would silently no-op.
                  const anyPending = actionPendingId !== null;
                  const pill = STATUS_PILL[c.status];
                  // THE ROW'S ACTIONS ARE DECIDED, NOT LAID OUT HERE (E28D/T4). Which verb is
                  // inline, what the kebab holds, the order, and where the divider falls all come
                  // from `connectionRowActions` — a module with tests, unlike this file.
                  const { inline, menu } = connectionRowActions(c);
                  const oauthLine = oauthClientSubline(c);
                  return (
                    <tr key={c.id} className="hover:bg-blue-50/40 transition-colors">
                      <td className="px-4 py-2 text-slate-700">{PROVIDER_LABEL[c.provider]}</td>
                      <td className="px-4 py-2">
                        <span className="text-slate-700 font-medium truncate" title={c.org}>
                          {c.org}
                        </span>
                      </td>
                      <td className="px-4 py-2">
                        <span
                          className={`inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium ${pill.cls}`}
                          title={c.status_detail ?? undefined}
                        >
                          {pill.label}
                        </span>
                        {/* OAuth-client readiness (E27B) — github_app only, since the
                            OAuth client is a property of an App. It is NOT part of the
                            connection's own health (the App works fine without one), so it
                            sits under the pill as a quiet sub-line rather than recoloring
                            it. "Not set" is the state that blocks every human in the org
                            from linking their account, so it is named, not merely absent.
                            E28D/T4: when it IS blocking, the line is also the AFFORDANCE —
                            it opens the same modal the kebab item opens, so the operator
                            acts where they read the problem instead of hunting column 6.
                            Presence, wording, tone and clickability are all
                            `oauthClientSubline`'s call. */}
                        {oauthLine &&
                          (oauthLine.actionable ? (
                            <button
                              type="button"
                              onClick={() => setOauthTarget(c)}
                              disabled={anyPending}
                              title={oauthLine.title}
                              className="block mt-1 text-[11px] text-amber-700 underline decoration-dotted underline-offset-2 hover:text-amber-900 transition-colors disabled:opacity-40"
                            >
                              {oauthLine.label}
                            </button>
                          ) : (
                            <span className="block mt-1 text-[11px] text-slate-400" title={oauthLine.title}>
                              {oauthLine.label}
                            </span>
                          ))}
                      </td>
                      <td className="px-4 py-2 text-slate-600">{c.account_login || '—'}</td>
                      <td className="px-4 py-2 text-slate-500 whitespace-nowrap">
                        {formatVerified(c.last_verified_at)}
                      </td>
                      <td className="px-4 py-2">
                        <div className="flex items-center justify-end gap-1.5">
                          {/* ONE inline slot — the model guarantees exactly one entry, so this
                              map cannot grow into a toolbar. `finish-setup` keeps the primary
                              blue: it is the only row state where an operator MUST act before
                              the connection works at all. */}
                          {inline.map((a) =>
                            a.key === 'finish-setup' ? (
                              <button
                                key={a.key}
                                type="button"
                                onClick={() => setFinishTarget(c)}
                                disabled={anyPending}
                                className="px-2.5 py-1 rounded-md bg-blue-600 text-white text-xs font-medium hover:bg-blue-700 transition-colors disabled:opacity-40"
                              >
                                {a.label}
                              </button>
                            ) : (
                              <button
                                key={a.key}
                                type="button"
                                onClick={() => handleTest(c.id)}
                                disabled={rowPending || anyPending}
                                className="px-2.5 py-1 rounded-md bg-white border border-slate-300 text-slate-700 text-xs font-medium hover:bg-slate-50 transition-colors disabled:opacity-40"
                              >
                                {rowPending ? '…' : a.label}
                              </button>
                            ),
                          )}
                          <RowMenu
                            items={menu}
                            disabled={anyPending}
                            hrefFor={(a) =>
                              a.key === 'roll-out-templates' ? templatesReconcilePath(c.id) : null
                            }
                            onSelect={(key) => {
                              if (key === 'oauth-client') setOauthTarget(c);
                              else if (key === 'replace-token') setReplaceTarget(c);
                              else if (key === 'delete') handleRemove(c.id);
                            }}
                          />
                        </div>
                      </td>
                    </tr>
                  );
                })}

              {!loading && connections.length === 0 && (
                <tr>
                  <td colSpan={6} className="px-4 py-8 text-center text-slate-400 text-sm">
                    No connections yet.
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
      )}

      {showAdd && (
        <AddConnectionModal
          onClose={() => setShowAdd(false)}
          onSubmit={async (body) => {
            await connectionsApi.create(body);
            // Add doesn't go through runAction, so clear any stale banner from a
            // prior failed Re-test/Delete before closing + refetching.
            setActionError(null);
            setShowAdd(false);
            refetch();
          }}
          onManifestSuccess={() => {
            // The manifest submit opens GitHub in a NEW tab, so this tab stays put — close
            // the modal (don't leave it frozen behind the new tab) and refetch so the newly
            // created "pending install" row is visible to Finish setup once the operator returns.
            setActionError(null);
            setShowAdd(false);
            refetch();
          }}
        />
      )}

      {replaceTarget && (
        <ReplaceTokenModal
          connection={replaceTarget}
          onClose={() => setReplaceTarget(null)}
          onSubmit={async (token) => {
            await connectionsApi.replaceToken(replaceTarget.id, { token });
            setActionError(null);
            setReplaceTarget(null);
            refetch();
          }}
        />
      )}

      {oauthTarget && (
        <OauthClientModal
          connection={oauthTarget}
          onClose={() => setOauthTarget(null)}
          onSubmit={async (clientId, clientSecret) => {
            await connectionsApi.setOauthClient(oauthTarget.id, {
              client_id: clientId,
              client_secret: clientSecret,
            });
            setActionError(null);
            setOauthTarget(null);
            refetch();
          }}
        />
      )}

      {finishTarget && (
        <FinishSetupModal
          connection={finishTarget}
          onClose={() => setFinishTarget(null)}
          onSubmit={async (installationId) => {
            await connectionsApi.finalize(
              finishTarget.id,
              installationId ? { installation_id: installationId } : {},
            );
            setActionError(null);
            setFinishTarget(null);
            refetch();
          }}
        />
      )}
    </div>
  );
}

// --- Row [⋮] menu ----------------------------------------------------------
// Small self-contained kebab menu (no prior dropdown component in the repo).
// Closes on outside-click or Escape; the panel mirrors the modal-panel styling
// (bg-white border-slate-200 shadow-xl rounded-lg).
//
// PURELY A RENDERER since E28D/T4: it takes the item list `connectionRowActions` produced and
// decides nothing. It used to take one boolean per item, which meant the WHICH-ITEMS-EXIST question
// was answered twice — once at the call site's props and once in this file's `&&` guards — with
// nothing able to test either half. What is left here is the parts a pure module cannot own:
// open/closed state, the outside-click and Escape listeners, and the class strings.
function RowMenu({
  items,
  disabled,
  hrefFor,
  onSelect,
}: {
  items: RowAction[];
  disabled: boolean;
  /** A `navigate` item's destination. Returning null for one is a bug the `.tsx` can't hide. */
  hrefFor: (action: RowAction) => string | null;
  onSelect: (key: string) => void;
}) {
  const [open, setOpen] = useState(false);
  const wrapRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return;
    const onDocClick = (e: MouseEvent) => {
      if (wrapRef.current && !wrapRef.current.contains(e.target as Node)) setOpen(false);
    };
    const onKeyDown = (e: KeyboardEvent) => {
      if (e.key === 'Escape') setOpen(false);
    };
    document.addEventListener('mousedown', onDocClick);
    document.addEventListener('keydown', onKeyDown);
    return () => {
      document.removeEventListener('mousedown', onDocClick);
      document.removeEventListener('keydown', onKeyDown);
    };
  }, [open]);

  return (
    <div ref={wrapRef} className="relative">
      <button
        type="button"
        onClick={() => setOpen((o) => !o)}
        disabled={disabled}
        aria-haspopup="menu"
        aria-expanded={open}
        aria-label="More actions"
        className="inline-flex items-center justify-center h-7 w-7 rounded-md text-slate-500 hover:text-slate-800 hover:bg-slate-100 transition-colors disabled:opacity-40"
      >
        <span aria-hidden="true" className="text-base leading-none">⋮</span>
      </button>
      {open && (
        <div
          role="menu"
          className="absolute right-0 z-10 mt-1 w-48 rounded-lg bg-white border border-slate-200 shadow-xl py-1"
        >
          {items.map((a) => {
            // `dividerBefore` is a border on the item rather than a separate <hr>: an element
            // between two `role="menuitem"`s in a `role="menu"` has to be announced or hidden,
            // and a border is neither.
            const divider = a.dividerBefore ? 'mt-1 pt-1.5 border-t border-slate-100' : '';
            const tone =
              a.kind === 'destructive'
                ? 'text-rose-600 hover:bg-rose-50'
                : 'text-slate-700 hover:bg-slate-50';
            const cls = `block w-full text-left px-3 py-1.5 text-sm transition-colors ${tone} ${divider}`;

            // A `navigate` item is a real <Link>: middle-click, ⌘-click and "copy link" all work,
            // which they would not on a button calling `navigate()`.
            if (a.kind === 'navigate') {
              const href = hrefFor(a);
              if (!href) return null;
              return (
                <Link key={a.key} role="menuitem" to={href} onClick={() => setOpen(false)} className={cls}>
                  {a.label}
                </Link>
              );
            }
            return (
              <button
                key={a.key}
                type="button"
                role="menuitem"
                onClick={() => {
                  setOpen(false);
                  onSelect(a.key);
                }}
                className={cls}
              >
                {a.label}
              </button>
            );
          })}
        </div>
      )}
    </div>
  );
}

// --- Modal shell helper -----------------------------------------------------
// Shared dialog chrome cloned from AddUserModal (fixed-inset backdrop, role=dialog
// aria-modal, Escape-to-close, backdrop-click-to-close-unless-pending, header with
// a close button). Both modals below reuse it so they stay visually identical.
export function ModalShell({
  title,
  description,
  ariaLabel,
  actionPending,
  onClose,
  children,
  footer,
}: {
  title: string;
  description: string;
  ariaLabel: string;
  actionPending: boolean;
  onClose: () => void;
  children: React.ReactNode;
  footer: React.ReactNode;
}) {
  useEffect(() => {
    const onKeyDown = (e: KeyboardEvent) => {
      if (e.key === 'Escape') {
        e.preventDefault();
        onClose();
      }
    };
    document.addEventListener('keydown', onKeyDown);
    return () => document.removeEventListener('keydown', onKeyDown);
  }, [onClose]);

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-slate-900/30 backdrop-blur-sm p-4"
      role="dialog"
      aria-modal="true"
      aria-label={ariaLabel}
      onMouseDown={(e) => {
        if (e.target === e.currentTarget && !actionPending) onClose();
      }}
    >
      <div className="w-full max-w-lg bg-white rounded-2xl border border-slate-200 shadow-xl">
        {/* Header. */}
        <div className="flex items-start justify-between gap-3 px-5 py-4 border-b border-slate-200/60">
          <div className="min-w-0">
            <h2 className="text-sm font-semibold text-slate-900 leading-tight">{title}</h2>
            <p className="text-xs text-slate-500 mt-0.5">{description}</p>
          </div>
          <button
            type="button"
            onClick={onClose}
            disabled={actionPending}
            aria-label="Close"
            className="shrink-0 inline-flex items-center justify-center h-7 w-7 rounded-md text-slate-400 hover:text-slate-700 hover:bg-slate-100 transition-colors disabled:opacity-40"
          >
            <span aria-hidden="true" className="text-base leading-none">×</span>
          </button>
        </div>

        {/* Body. */}
        <div className="px-5 py-4 space-y-4">{children}</div>

        {/* Footer actions. */}
        <div className="flex items-center justify-end gap-2 px-5 py-4 border-t border-slate-200/60">
          {footer}
        </div>
      </div>
    </div>
  );
}

const FIELD_LABEL = 'block text-[11px] uppercase tracking-wide text-slate-400 font-medium mb-1';
const FIELD_INPUT =
  'w-full px-3 py-2 text-sm rounded-lg border border-slate-300 focus:outline-none focus:ring-2 focus:ring-blue-500/40';

// --- Add-connection modal ---------------------------------------------------
// Unified 3-way onboarding (E20/U3). For GitHub a segmented control picks the
// auth path; GitLab always uses a PAT (control hidden):
//   • App via Manifest (default, recommended) — platform auto-creates the App,
//     captures its key. Org (+ optional Base URL) → "Create GitHub App" POSTs a
//     hidden manifest form to GitHub (full-page nav); the U4 callback finishes it.
//   • App (manual)  — paste App ID + Installation ID + Private key (PEM).
//   • PAT           — the original personal-access-token path (unchanged wire).
// Secret discipline: token (password) and private key (textarea) are write-only —
// never re-rendered or read back after submit. Mirrors AddUserModal mechanics
// (mountedRef guard, actionPending, canSubmit, inline <p role="alert">).
const AGP_MANIFEST_STATE_KEY = 'agp_manifest_state';
type GithubAuthMode = 'manifest' | 'manual';

const AUTH_SEG_BASE =
  'px-3 py-1.5 rounded-lg text-xs font-medium transition-colors whitespace-nowrap';
const AUTH_SEG_ACTIVE = 'bg-white text-slate-900 shadow-sm';
const AUTH_SEG_IDLE = 'text-slate-500 hover:text-slate-700';

function AddConnectionModal({
  onSubmit,
  onManifestSuccess,
  onClose,
}: {
  onSubmit: (body: ConnectionCreate) => Promise<void>;
  onManifestSuccess: () => void;
  onClose: () => void;
}) {
  const [provider, setProvider] = useState<Provider>('github');
  // For GitHub: 'pat' | 'github_app'. GitLab is forced to 'pat'.
  const [authType, setAuthType] = useState<ConnectionAuthType>('github_app');
  // Sub-mode when authType === 'github_app'.
  const [githubMode, setGithubMode] = useState<GithubAuthMode>('manifest');

  const [org, setOrg] = useState('');
  const [baseUrl, setBaseUrl] = useState('');
  const [token, setToken] = useState('');
  const [appId, setAppId] = useState('');
  const [installationId, setInstallationId] = useState('');
  const [privateKey, setPrivateKey] = useState('');

  const [actionPending, setActionPending] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);

  const orgRef = useRef<HTMLInputElement>(null);

  // Unmount guard — onSubmit resolves async and can land after the modal closes.
  const mountedRef = useRef(true);
  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
    };
  }, []);

  // Focus the org field on open.
  useEffect(() => {
    orgRef.current?.focus();
  }, []);

  // Effective path: GitLab forces PAT; for GitHub honour the segmented control.
  const isGithub = provider === 'github';
  const effectiveAuth: ConnectionAuthType = isGithub ? authType : 'pat';
  const isApp = effectiveAuth === 'github_app';
  const isManifest = isApp && githubMode === 'manifest';
  const isManual = isApp && githubMode === 'manual';
  const isPat = effectiveAuth === 'pat';

  const orgFilled = org.trim().length > 0;
  const canSubmit =
    (isPat && orgFilled && token.trim().length > 0) ||
    (isManual &&
      orgFilled &&
      appId.trim().length > 0 &&
      installationId.trim().length > 0 &&
      privateKey.trim().length > 0) ||
    (isManifest && orgFilled);

  const trimmedBaseOrNull = () => {
    const b = baseUrl.trim();
    return b.length > 0 ? b : null;
  };

  // PAT + App-manual both go through connectionsApi.create (via onSubmit).
  const handleCreate = useCallback(async () => {
    if (actionPending || !canSubmit) return;
    setActionPending(true);
    setActionError(null);
    try {
      const body: ConnectionCreate = isManual
        ? {
            provider: 'github',
            org: org.trim(),
            base_url: trimmedBaseOrNull(),
            auth_type: 'github_app',
            app_id: appId.trim(),
            installation_id: installationId.trim(),
            private_key: privateKey,
          }
        : {
            provider,
            org: org.trim(),
            base_url: trimmedBaseOrNull(),
            token: token.trim(),
          };
      await onSubmit(body);
      // Parent closes the modal + refetches on success; nothing to do here.
    } catch (err: unknown) {
      if (mountedRef.current) {
        setActionError(err instanceof Error ? err.message : 'Failed to add connection.');
      }
    } finally {
      if (mountedRef.current) setActionPending(false);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [
    actionPending,
    canSubmit,
    isManual,
    provider,
    org,
    baseUrl,
    token,
    appId,
    installationId,
    privateKey,
    onSubmit,
  ]);

  // App-via-Manifest: start the handshake, persist the CSRF state, then POST a
  // programmatic hidden form to GitHub (full-page navigation — the U4 callback
  // route handles the return). On failure we surface the error inline.
  const handleCreateApp = useCallback(async () => {
    if (actionPending || !canSubmit) return;
    setActionPending(true);
    setActionError(null);
    try {
      const redirect_url = buildManifestRedirectUrl(window.location.origin);
      const { post_url, manifest, state } = await connectionsApi.manifestStart({
        org: org.trim(),
        base_url: trimmedBaseOrNull(),
        redirect_url,
      });
      // localStorage (NOT sessionStorage): the manifest form opens GitHub in a NEW TAB, and
      // the U4 callback lands in that new tab whose sessionStorage would be empty. localStorage
      // is shared across tabs; the state is still short-lived + single-use (the callback clears
      // it, and the server-side state record enforces the 15-min TTL + single-use).
      window.localStorage.setItem(AGP_MANIFEST_STATE_KEY, state);
      onManifestSuccess();

      const form = document.createElement('form');
      form.method = 'POST';
      form.action = post_url;
      form.target = '_blank'; // open GitHub in a new tab; the current tab keeps the app
      for (const { name, value } of manifestFormFields(manifest)) {
        const input = document.createElement('input');
        input.type = 'hidden';
        input.name = name;
        input.value = value;
        form.appendChild(input);
      }
      document.body.appendChild(form);
      form.submit();
      form.remove();
    } catch (err: unknown) {
      if (mountedRef.current) {
        setActionError(err instanceof Error ? err.message : 'Failed to start GitHub App creation.');
        setActionPending(false);
      }
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [actionPending, canSubmit, org, baseUrl, onManifestSuccess]);

  return (
    <ModalShell
      title="Add org connection"
      description="Connect a GitHub organization or GitLab group. Credentials are verified on save and stored in Secrets Manager."
      ariaLabel="Add org connection"
      actionPending={actionPending}
      onClose={onClose}
      footer={
        <>
          <button
            type="button"
            onClick={onClose}
            disabled={actionPending}
            className="px-3.5 py-1.5 rounded-lg bg-white border border-slate-300 text-slate-700 text-sm font-medium hover:bg-slate-50 transition-colors disabled:opacity-40"
          >
            Cancel
          </button>
          {isManifest ? (
            <button
              type="button"
              onClick={handleCreateApp}
              disabled={actionPending || !canSubmit}
              className="px-3.5 py-1.5 rounded-lg bg-blue-600 text-white text-sm font-medium hover:bg-blue-700 transition-colors disabled:opacity-40"
            >
              {actionPending ? 'Redirecting…' : 'Create GitHub App'}
            </button>
          ) : (
            <button
              type="button"
              onClick={handleCreate}
              disabled={actionPending || !canSubmit}
              className="px-3.5 py-1.5 rounded-lg bg-blue-600 text-white text-sm font-medium hover:bg-blue-700 transition-colors disabled:opacity-40"
            >
              {actionPending ? 'Verifying…' : 'Add connection'}
            </button>
          )}
        </>
      }
    >
      {/* Provider. */}
      <div>
        <label htmlFor="add-conn-provider" className={FIELD_LABEL}>
          Provider
        </label>
        <select
          id="add-conn-provider"
          value={provider}
          onChange={(e) => setProvider(e.target.value as Provider)}
          disabled={actionPending}
          className="w-full px-3 py-1.5 text-sm rounded-lg border border-slate-300 bg-white focus:outline-none focus:ring-2 focus:ring-blue-500/40 disabled:opacity-40"
        >
          <option value="github">GitHub</option>
          <option value="gitlab">GitLab</option>
        </select>
      </div>

      {/* Auth-type segmented control — GitHub only (GitLab forces PAT). */}
      {isGithub && (
        <div>
          <span className={FIELD_LABEL}>Authentication</span>
          <div className="flex items-center gap-1 p-1 bg-slate-100 rounded-xl w-fit">
            <button
              type="button"
              onClick={() => {
                setAuthType('github_app');
                setGithubMode('manifest');
              }}
              disabled={actionPending}
              className={`${AUTH_SEG_BASE} ${isManifest ? AUTH_SEG_ACTIVE : AUTH_SEG_IDLE} disabled:opacity-40`}
            >
              App via Manifest
            </button>
            <button
              type="button"
              onClick={() => {
                setAuthType('github_app');
                setGithubMode('manual');
              }}
              disabled={actionPending}
              className={`${AUTH_SEG_BASE} ${isManual ? AUTH_SEG_ACTIVE : AUTH_SEG_IDLE} disabled:opacity-40`}
            >
              App (manual)
            </button>
            <button
              type="button"
              onClick={() => setAuthType('pat')}
              disabled={actionPending}
              className={`${AUTH_SEG_BASE} ${isPat ? AUTH_SEG_ACTIVE : AUTH_SEG_IDLE} disabled:opacity-40`}
            >
              PAT
            </button>
          </div>
          <p className="text-[11px] text-slate-400 mt-1">
            {isApp
              ? 'GitHub App — recommended: org-owned, short-lived installation tokens.'
              : 'Personal access token — quick to set up, tied to your personal account.'}
          </p>
        </div>
      )}

      {/* Org / Group. */}
      <div>
        <label htmlFor="add-conn-org" className={FIELD_LABEL}>
          Org / Group
        </label>
        <input
          id="add-conn-org"
          ref={orgRef}
          type="text"
          value={org}
          onChange={(e) => setOrg(e.target.value)}
          disabled={actionPending}
          placeholder={provider === 'github' ? 'my-org' : 'my-group'}
          className={`${FIELD_INPUT} disabled:opacity-40`}
          autoComplete="off"
        />
      </div>

      {/* Base URL (optional). */}
      <div>
        <label htmlFor="add-conn-base" className={FIELD_LABEL}>
          Base URL <span className="text-slate-300 normal-case">(optional)</span>
        </label>
        <input
          id="add-conn-base"
          type="text"
          value={baseUrl}
          onChange={(e) => setBaseUrl(e.target.value)}
          disabled={actionPending}
          placeholder={PROVIDER_BASE_PLACEHOLDER[provider]}
          className={`${FIELD_INPUT} disabled:opacity-40`}
          autoComplete="off"
        />
      </div>

      {/* PAT path — write-only token. */}
      {isPat && (
        <div>
          <label htmlFor="add-conn-token" className={FIELD_LABEL}>
            Personal access token
          </label>
          <input
            id="add-conn-token"
            type="password"
            value={token}
            onChange={(e) => setToken(e.target.value)}
            disabled={actionPending}
            placeholder="Paste a personal access token"
            className={`${FIELD_INPUT} disabled:opacity-40`}
            autoComplete="off"
          />
          <p className="text-[11px] text-slate-400 mt-1">
            Stored in Secrets Manager and never shown again. Use “Replace token” to rotate it.
          </p>
        </div>
      )}

      {/* App-manual path — App ID + Installation ID + write-only Private key. */}
      {isManual && (
        <>
          <div className="rounded-lg border border-slate-200 bg-slate-50/70 p-3 text-[11px] leading-relaxed text-slate-500">
            <p className="font-medium text-slate-600 mb-1">Create the App on your org first:</p>
            <ol className="list-decimal ml-4 space-y-0.5">
              <li>
                Org <span className="text-slate-600">Settings → Developer settings → GitHub Apps →
                New GitHub App</span>. Name it (e.g. <code>agp-{org || 'your-org'}</code>), set any
                Homepage URL, and uncheck <span className="text-slate-600">Webhook → Active</span>.
              </li>
              <li>
                Under <span className="text-slate-600">Repository permissions</span> grant:
                Administration, Contents, Workflows, Actions, Variables ={' '}
                <span className="text-slate-600">Read &amp; write</span>; Metadata ={' '}
                <span className="text-slate-600">Read-only</span>.
              </li>
              <li>
                <span className="text-slate-600">Install App</span> on the org (All repositories or a
                selected set). The install URL ends in <code>/installations/&lt;ID&gt;</code> — that
                number is your <span className="text-slate-600">Installation ID</span>.
              </li>
              <li>
                On the App’s <span className="text-slate-600">General</span> page: copy the{' '}
                <span className="text-slate-600">App ID</span>, then{' '}
                <span className="text-slate-600">Generate a private key</span> and paste the
                downloaded <code>.pem</code> contents below.
              </li>
            </ol>
            <a
              href="https://docs.github.com/en/apps/creating-github-apps/registering-a-github-app/registering-a-github-app"
              target="_blank"
              rel="noreferrer"
              className="inline-block mt-1.5 text-blue-600 hover:underline"
            >
              GitHub App setup guide ↗
            </a>
          </div>
          <div>
            <label htmlFor="add-conn-app-id" className={FIELD_LABEL}>
              App ID
            </label>
            <input
              id="add-conn-app-id"
              type="text"
              value={appId}
              onChange={(e) => setAppId(e.target.value)}
              disabled={actionPending}
              placeholder="123456"
              className={`${FIELD_INPUT} disabled:opacity-40`}
              autoComplete="off"
            />
          </div>
          <div>
            <label htmlFor="add-conn-install-id" className={FIELD_LABEL}>
              Installation ID
            </label>
            <input
              id="add-conn-install-id"
              type="text"
              value={installationId}
              onChange={(e) => setInstallationId(e.target.value)}
              disabled={actionPending}
              placeholder="7654321"
              className={`${FIELD_INPUT} disabled:opacity-40`}
              autoComplete="off"
            />
          </div>
          <div>
            <label htmlFor="add-conn-private-key" className={FIELD_LABEL}>
              Private key (PEM)
            </label>
            <textarea
              id="add-conn-private-key"
              value={privateKey}
              onChange={(e) => setPrivateKey(e.target.value)}
              disabled={actionPending}
              rows={4}
              placeholder="-----BEGIN RSA PRIVATE KEY-----"
              className={`${FIELD_INPUT} font-mono text-xs disabled:opacity-40`}
              autoComplete="off"
            />
            <p className="text-[11px] text-slate-400 mt-1">
              Stored in Secrets Manager and never shown again.
            </p>
          </div>
        </>
      )}

      {/* App-via-Manifest path — just org (+ base URL above); GitHub creates the App. */}
      {isManifest && (
        <p className="text-[11px] text-slate-400">
          The platform creates a GitHub App in your org and captures its key automatically — you
          never copy any credential. You’ll approve it on GitHub, then install it. See the{' '}
          <a
            href="https://docs.github.com/apps"
            target="_blank"
            rel="noreferrer"
            className="text-blue-600 hover:underline"
          >
            GitHub App setup guide
          </a>
          .
        </p>
      )}

      {actionError && (
        <p className="text-sm text-red-600" role="alert">
          {actionError}
        </p>
      )}
    </ModalShell>
  );
}

// --- Replace-token modal ----------------------------------------------------
// A single write-only password field → connectionsApi.replaceToken(id, {token}).
// Same AddUserModal mechanics (mountedRef, actionPending, canSubmit, inline error,
// label flip). Never renders the existing token — there is nothing to show.
function ReplaceTokenModal({
  connection,
  onSubmit,
  onClose,
}: {
  connection: Connection;
  onSubmit: (token: string) => Promise<void>;
  onClose: () => void;
}) {
  const [token, setToken] = useState('');
  const [actionPending, setActionPending] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);

  const inputRef = useRef<HTMLInputElement>(null);

  const mountedRef = useRef(true);
  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
    };
  }, []);

  useEffect(() => {
    inputRef.current?.focus();
  }, []);

  const canSubmit = token.trim().length > 0;

  const handleSubmit = useCallback(async () => {
    if (actionPending || !canSubmit) return;
    setActionPending(true);
    setActionError(null);
    try {
      await onSubmit(token.trim());
      // Parent closes the modal + refetches on success.
    } catch (err: unknown) {
      if (mountedRef.current) {
        setActionError(err instanceof Error ? err.message : 'Failed to replace token.');
      }
    } finally {
      if (mountedRef.current) setActionPending(false);
    }
  }, [actionPending, canSubmit, token, onSubmit]);

  return (
    <ModalShell
      title="Replace token"
      description={`Rotate the personal access token for ${PROVIDER_LABEL[connection.provider]} · ${connection.org}. The new token is verified on save.`}
      ariaLabel="Replace token"
      actionPending={actionPending}
      onClose={onClose}
      footer={
        <>
          <button
            type="button"
            onClick={onClose}
            disabled={actionPending}
            className="px-3.5 py-1.5 rounded-lg bg-white border border-slate-300 text-slate-700 text-sm font-medium hover:bg-slate-50 transition-colors disabled:opacity-40"
          >
            Cancel
          </button>
          <button
            type="button"
            onClick={handleSubmit}
            disabled={actionPending || !canSubmit}
            className="px-3.5 py-1.5 rounded-lg bg-blue-600 text-white text-sm font-medium hover:bg-blue-700 transition-colors disabled:opacity-40"
          >
            {actionPending ? 'Verifying…' : 'Replace token'}
          </button>
        </>
      }
    >
      <div>
        <label htmlFor="replace-conn-token" className={FIELD_LABEL}>
          New personal access token
        </label>
        <input
          id="replace-conn-token"
          ref={inputRef}
          type="password"
          value={token}
          onChange={(e) => setToken(e.target.value)}
          disabled={actionPending}
          placeholder="Paste the new personal access token"
          className={`${FIELD_INPUT} disabled:opacity-40`}
          autoComplete="off"
        />
        <p className="text-[11px] text-slate-400 mt-1">
          The current token is never displayed. Saving overwrites it in Secrets Manager.
        </p>
      </div>

      {actionError && (
        <p className="text-sm text-red-600" role="alert">
          {actionError}
        </p>
      )}
    </ModalShell>
  );
}

// --- Finish-setup modal -----------------------------------------------------
// Recovery affordance for a pending github_app connection (E20/U-review). The
// manifest flow created the App + pending row but the install/finalize step never
// completed (tab closed, callback errored). Re-test can't fix it — it mints
// against installation_id=None and fails — so this modal calls finalize instead:
//   • "Finish" → connectionsApi.finalize(id, {}) — auto-resolves the installation
//     once the App is installed on the org.
//   • the collapsible fallback → finalize(id, {installation_id}) with a pasted
//     numeric ID. Mirrors ConnectionCallback's install step + AddUserModal
//     mechanics (mountedRef, actionPending, inline error). Never echoes a secret.
function FinishSetupModal({
  connection,
  onSubmit,
  onClose,
}: {
  connection: Connection;
  // installationId: '' → auto-resolve ({}); non-empty → { installation_id }.
  onSubmit: (installationId: string) => Promise<void>;
  onClose: () => void;
}) {
  const [showManual, setShowManual] = useState(false);
  const [installationId, setInstallationId] = useState('');
  const [actionPending, setActionPending] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);

  const mountedRef = useRef(true);
  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
    };
  }, []);

  const installSettingsUrl = buildInstallSettingsUrl(connection.org);

  const handleFinalize = useCallback(
    async (manualId: string) => {
      if (actionPending) return;
      setActionPending(true);
      setActionError(null);
      try {
        await onSubmit(manualId.trim());
        // Parent closes the modal + refetches on success.
      } catch (err: unknown) {
        if (mountedRef.current) {
          setActionError(err instanceof Error ? err.message : 'Could not finish setup yet.');
        }
      } finally {
        if (mountedRef.current) setActionPending(false);
      }
    },
    [actionPending, onSubmit],
  );

  const manualDisabled = actionPending || installationId.trim().length === 0;

  return (
    <ModalShell
      title={`Finish GitHub App setup — ${connection.org}`}
      description="Complete the connection once the App is installed on your organization."
      ariaLabel="Finish GitHub App setup"
      actionPending={actionPending}
      onClose={onClose}
      footer={
        <>
          <button
            type="button"
            onClick={onClose}
            disabled={actionPending}
            className="px-3.5 py-1.5 rounded-lg bg-white border border-slate-300 text-slate-700 text-sm font-medium hover:bg-slate-50 transition-colors disabled:opacity-40"
          >
            Cancel
          </button>
          <button
            type="button"
            onClick={() => void handleFinalize('')}
            disabled={actionPending}
            className="px-3.5 py-1.5 rounded-lg bg-blue-600 text-white text-sm font-medium hover:bg-blue-700 transition-colors disabled:opacity-40"
          >
            {actionPending ? 'Finishing…' : 'Finish'}
          </button>
        </>
      }
    >
      <p className="text-sm text-slate-600">
        Install the App on your organization, then click Finish — the platform resolves the
        installation automatically.{' '}
        <a
          href={installSettingsUrl}
          target="_blank"
          rel="noreferrer"
          className="text-blue-600 hover:underline"
        >
          Open installed apps on GitHub
        </a>
        .
      </p>

      <div className="pt-2 border-t border-slate-200/60">
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
            <label htmlFor="finish-conn-install-id" className={FIELD_LABEL}>
              Installation ID
            </label>
            <input
              id="finish-conn-install-id"
              type="text"
              inputMode="numeric"
              value={installationId}
              onChange={(e) => setInstallationId(e.target.value)}
              disabled={actionPending}
              placeholder="7654321"
              className={`${FIELD_INPUT} disabled:opacity-40`}
              autoComplete="off"
            />
            <button
              type="button"
              onClick={() => void handleFinalize(installationId)}
              disabled={manualDisabled}
              className="px-3.5 py-1.5 rounded-lg bg-white border border-slate-300 text-slate-700 text-sm font-medium hover:bg-slate-50 transition-colors disabled:opacity-40"
            >
              {actionPending ? 'Finishing…' : 'Finish with this ID'}
            </button>
          </div>
        )}
      </div>

      {actionError && (
        <p className="text-sm text-rose-600" role="alert">
          {actionError}
        </p>
      )}
    </ModalShell>
  );
}

// --- OAuth-client modal (E27B) ----------------------------------------------
// The one-time admin paste that unblocks per-user account linking for an entire org.
// A GitHub App's OAuth client secret exists only inside GitHub's UI — no API returns it,
// and the manifest flow only surfaces it at creation time — so an App onboarded before
// E27B has no way to reach AGP except by hand. Without this surface nobody in the org can
// link their account at all, which is why it is a first-class menu item and not a
// developer-only path.
//
// Two fields: a plain `client_id` (non-secret, and the backend verifies it against
// GET /app before writing anything, mirroring replace_key's verify-then-write) and a
// write-only password `client_secret`. Secret discipline follows this file's stated rule
// (`:23`): never echo the stored secret, never render a masked placeholder standing in for
// one, and clear the field on close. The response is the ordinary Connection read model,
// so there is nothing to echo even by accident.
//
// The Callback URL shown here is built with `buildLinkRedirectUrl(window.location.origin)`
// — the SAME function the link card and the backend's LINK_CALLBACK_PATH agree on — so the
// string an admin pastes into GitHub can never drift from what AGP actually sends. A
// mismatch there makes GitHub reject the redirect_uri, which is the easiest way to break
// the flow and the hardest to diagnose from the error GitHub returns.
function OauthClientModal({
  connection,
  onSubmit,
  onClose,
}: {
  connection: Connection;
  onSubmit: (clientId: string, clientSecret: string) => Promise<void>;
  onClose: () => void;
}) {
  const [clientId, setClientId] = useState('');
  const [clientSecret, setClientSecret] = useState('');
  const [actionPending, setActionPending] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);

  const inputRef = useRef<HTMLInputElement>(null);

  const mountedRef = useRef(true);
  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
      // Belt-and-braces: drop the pasted secret from state on unmount so it does not
      // linger in a retained closure if the modal is re-opened.
      setClientSecret('');
    };
  }, []);

  useEffect(() => {
    inputRef.current?.focus();
  }, []);

  const callbackUrl = buildLinkRedirectUrl(window.location.origin);

  const canSubmit = clientId.trim().length > 0 && clientSecret.trim().length > 0;

  const handleClose = useCallback(() => {
    setClientSecret('');
    onClose();
  }, [onClose]);

  const handleSubmit = useCallback(async () => {
    if (actionPending || !canSubmit) return;
    setActionPending(true);
    setActionError(null);
    try {
      await onSubmit(clientId.trim(), clientSecret);
      // Parent closes the modal + refetches on success.
    } catch (err: unknown) {
      if (mountedRef.current) {
        setActionError(err instanceof Error ? err.message : 'Failed to save the OAuth client.');
      }
    } finally {
      if (mountedRef.current) setActionPending(false);
    }
  }, [actionPending, canSubmit, clientId, clientSecret, onSubmit]);

  return (
    <ModalShell
      title={`${connection.has_oauth_client ? 'Replace' : 'Add'} OAuth client — ${connection.org}`}
      description="Lets members of this organization link their personal GitHub account. The client ID is verified against the App before anything is stored."
      ariaLabel={`${connection.has_oauth_client ? 'Replace' : 'Add'} OAuth client`}
      actionPending={actionPending}
      onClose={handleClose}
      footer={
        <>
          <button
            type="button"
            onClick={handleClose}
            disabled={actionPending}
            className="px-3.5 py-1.5 rounded-lg bg-white border border-slate-300 text-slate-700 text-sm font-medium hover:bg-slate-50 transition-colors disabled:opacity-40"
          >
            Cancel
          </button>
          <button
            type="button"
            onClick={handleSubmit}
            disabled={actionPending || !canSubmit}
            className="px-3.5 py-1.5 rounded-lg bg-blue-600 text-white text-sm font-medium hover:bg-blue-700 transition-colors disabled:opacity-40"
          >
            {actionPending ? 'Verifying…' : 'Save OAuth client'}
          </button>
        </>
      }
    >
      <p className="text-sm text-slate-600">
        With an OAuth client in place, a member can authorize AGP once and the deployments they
        trigger are carried out under their own GitHub identity instead of the platform App&rsquo;s.
      </p>

      <div className="rounded-lg border border-slate-200 bg-slate-50/70 p-3 text-[11px] leading-relaxed text-slate-500">
        <p className="font-medium text-slate-600 mb-1">On GitHub, for this App:</p>
        <ol className="list-decimal ml-4 space-y-0.5">
          <li>
            Org <span className="text-slate-600">Settings → Developer settings → GitHub Apps →
            Edit</span> on the AGP App.
          </li>
          <li>
            Add the <span className="text-slate-600">Callback URL</span> below. Leave{' '}
            <span className="text-slate-600">
              Request user authorization (OAuth) during installation
            </span>{' '}
            unticked — AGP asks each member itself, when they link.
          </li>
          <li>
            Copy the <span className="text-slate-600">Client ID</span>, then{' '}
            <span className="text-slate-600">Generate a new client secret</span> and paste both
            below. GitHub shows the secret once.
          </li>
        </ol>
        <div className="mt-2">
          <span className="block text-slate-600 font-medium">Callback URL to add:</span>
          <code className="mt-0.5 block px-2 py-1 rounded-md bg-white border border-slate-200 font-mono text-[11px] text-slate-700 break-all">
            {callbackUrl}
          </code>
        </div>
      </div>

      <div>
        <label htmlFor="oauth-client-id" className={FIELD_LABEL}>
          Client ID
        </label>
        <input
          id="oauth-client-id"
          ref={inputRef}
          type="text"
          value={clientId}
          onChange={(e) => setClientId(e.target.value)}
          disabled={actionPending}
          placeholder="Iv1.0123456789abcdef"
          className={`${FIELD_INPUT} font-mono text-xs disabled:opacity-40`}
          autoComplete="off"
        />
        <p className="text-[11px] text-slate-400 mt-1">
          Not a secret. It must belong to this App — a mismatch is rejected and nothing is stored.
        </p>
      </div>

      <div>
        <label htmlFor="oauth-client-secret" className={FIELD_LABEL}>
          Client secret
        </label>
        <input
          id="oauth-client-secret"
          type="password"
          value={clientSecret}
          onChange={(e) => setClientSecret(e.target.value)}
          disabled={actionPending}
          placeholder="Paste the generated client secret"
          className={`${FIELD_INPUT} disabled:opacity-40`}
          autoComplete="off"
        />
        <p className="text-[11px] text-slate-400 mt-1">
          Stored in Secrets Manager and never shown again. Saving replaces any existing client
          secret and leaves the App&rsquo;s private key untouched.
        </p>
      </div>

      {actionError && (
        <p className="text-sm text-red-600" role="alert">
          {actionError}
        </p>
      )}
    </ModalShell>
  );
}
