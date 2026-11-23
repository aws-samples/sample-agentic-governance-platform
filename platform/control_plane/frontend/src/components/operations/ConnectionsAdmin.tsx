import { useCallback, useEffect, useRef, useState } from 'react';
import { Link } from 'react-router-dom';
import { connectionsApi } from '../../api/client';
import type { Connection } from '../../api/client';
import { useLoad } from '../../api/useLoad';
import { useUser } from '../../contexts/useUser';
import {
  connectionRowActions,
  connectionStatusPill,
  oauthClientSubline,
  templatesReconcilePath,
  type RowAction,
} from './connectionRowActions';
import AddConnectionModal from './AddConnectionModal';
import { FinishSetupModal, OauthClientModal, ReplaceTokenModal } from './ConnectionModals';
import { PROVIDER_LABEL } from './opsLabels';

const NO_CONNECTIONS: Connection[] = [];
const CARD = 'bg-white/70 backdrop-blur rounded-xl border border-slate-200/60 shadow-sm';

function formatVerified(iso: string | null): string {
  if (!iso) return '—';
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return d.toLocaleString();
}

export default function ConnectionsAdmin() {
  const { user } = useUser();
  const isAdmin = (user?.role_level ?? 0) >= 2;

  const { state, reload: refetch } = useLoad(
    () => (isAdmin ? connectionsApi.list() : Promise.resolve(NO_CONNECTIONS)),
    [isAdmin],
    'Failed to load connections.',
  );
  const connections = state.data ?? NO_CONNECTIONS;
  const loading = state.kind === 'loading';

  const [actionPendingId, setActionPendingId] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);

  const [showAdd, setShowAdd] = useState(false);
  const [replaceTarget, setReplaceTarget] = useState<Connection | null>(null);
  const [finishTarget, setFinishTarget] = useState<Connection | null>(null);
  const [oauthTarget, setOauthTarget] = useState<Connection | null>(null);

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

  if (!isAdmin) return null;

  return (
    <div className="space-y-3">
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

      {state.kind === 'error' ? (
        <div className="bg-white/70 backdrop-blur rounded-xl border border-red-200/70 shadow-sm p-6">
          <h3 className="text-sm font-semibold text-red-700">Couldn’t load connections</h3>
          <p className="text-sm text-slate-600 mt-1">{state.message}</p>
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
                  const anyPending = actionPendingId !== null;
                  const pill = connectionStatusPill(c.status);
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
                        {oauthLine &&
                          (oauthLine.rendersAsButton ? (
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
            setActionError(null);
            setShowAdd(false);
            refetch();
          }}
          onManifestSuccess={() => {
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

function RowMenu({
  items,
  disabled,
  hrefFor,
  onSelect,
}: {
  items: RowAction[];
  disabled: boolean;
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
            // An element between two menuitems inside role="menu" must be announced or hidden; a border on the item is neither.
            const divider = a.dividerBefore ? 'mt-1 pt-1.5 border-t border-slate-100' : '';
            const tone =
              a.kind === 'destructive'
                ? 'text-rose-600 hover:bg-rose-50'
                : 'text-slate-700 hover:bg-slate-50';
            const cls = `block w-full text-left px-3 py-1.5 text-sm transition-colors ${tone} ${divider}`;

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
