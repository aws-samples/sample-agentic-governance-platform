import { useCallback, useEffect, useRef, useState } from 'react';
import { connectionsApi } from '../../api/client';
import type { ConnectionAuthType, ConnectionCreate, Provider } from '../../api/client';
import { AGP_MANIFEST_STATE_KEY } from './connectionCallbackParams';
import { buildManifestRedirectUrl, manifestFormFields } from './manifestForm';
import { ModalShell } from './ModalShell';

const PROVIDER_BASE_PLACEHOLDER: Record<Provider, string> = {
  github: 'https://api.github.com',
  gitlab: 'https://gitlab.com',
};

export const FIELD_LABEL = 'block text-[11px] uppercase tracking-wide text-slate-400 font-medium mb-1';
export const FIELD_INPUT =
  'w-full px-3 py-2 text-sm rounded-lg border border-slate-300 focus:outline-none focus:ring-2 focus:ring-blue-500/40';

type GithubAuthMode = 'manifest' | 'manual';

const AUTH_SEG_BASE =
  'px-3 py-1.5 rounded-lg text-xs font-medium transition-colors whitespace-nowrap';
const AUTH_SEG_ACTIVE = 'bg-white text-slate-900 shadow-sm';
const AUTH_SEG_IDLE = 'text-slate-500 hover:text-slate-700';

function trimmedOrNull(value: string): string | null {
  const trimmed = value.trim();
  return trimmed.length > 0 ? trimmed : null;
}

export default function AddConnectionModal({
  onSubmit,
  onManifestSuccess,
  onClose,
}: {
  onSubmit: (body: ConnectionCreate) => Promise<void>;
  onManifestSuccess: () => void;
  onClose: () => void;
}) {
  const [provider, setProvider] = useState<Provider>('github');
  const [authType, setAuthType] = useState<ConnectionAuthType>('github_app');
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

  const mountedRef = useRef(true);
  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
    };
  }, []);

  useEffect(() => {
    orgRef.current?.focus();
  }, []);

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

  const handleCreate = useCallback(async () => {
    if (actionPending || !canSubmit) return;
    setActionPending(true);
    setActionError(null);
    try {
      const body: ConnectionCreate = isManual
        ? {
            provider: 'github',
            org: org.trim(),
            base_url: trimmedOrNull(baseUrl),
            auth_type: 'github_app',
            app_id: appId.trim(),
            installation_id: installationId.trim(),
            private_key: privateKey,
          }
        : {
            provider,
            org: org.trim(),
            base_url: trimmedOrNull(baseUrl),
            token: token.trim(),
          };
      await onSubmit(body);
    } catch (err: unknown) {
      if (mountedRef.current) {
        setActionError(err instanceof Error ? err.message : 'Failed to add connection.');
      }
    } finally {
      if (mountedRef.current) setActionPending(false);
    }
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

  const handleCreateApp = useCallback(async () => {
    if (actionPending || !canSubmit) return;
    setActionPending(true);
    setActionError(null);
    try {
      const redirect_url = buildManifestRedirectUrl(window.location.origin);
      const { post_url, manifest, state } = await connectionsApi.manifestStart({
        org: org.trim(),
        base_url: trimmedOrNull(baseUrl),
        redirect_url,
      });
      window.localStorage.setItem(AGP_MANIFEST_STATE_KEY, state);
      onManifestSuccess();

      const form = document.createElement('form');
      form.method = 'POST';
      form.action = post_url;
      form.target = '_blank';
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
