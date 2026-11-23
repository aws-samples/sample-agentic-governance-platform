import { useCallback, useEffect, useRef, useState } from 'react';
import type { Connection } from '../../api/client';
import { buildInstallSettingsUrl } from './manifestForm';
import { buildLinkRedirectUrl } from './githubLinkModel';
import { ModalShell } from './ModalShell';
import { FIELD_INPUT, FIELD_LABEL } from './AddConnectionModal';
import { PROVIDER_LABEL } from './opsLabels';

export function ReplaceTokenModal({
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

export function FinishSetupModal({
  connection,
  onSubmit,
  onClose,
}: {
  connection: Connection;
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

export function OauthClientModal({
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
