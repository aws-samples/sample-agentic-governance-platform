import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { principalsApi } from '../../../api/client';
import type { PrincipalHit, TenantView, TenantPlatform } from '../../../api/client';
import { AgentAvatar } from '../agentUi';
import {
  EMPTY_TENANT_DRAFT,
  PLATFORM_OPTIONS,
  PRIVILEGE_STATEMENT,
  type DatabricksStageDraft,
  type StageDraft,
  type StageKey,
  type TenantDraft,
  addGroup,
  addStage,
  canSubmit,
  capabilityBadges,
  draftDatabricksStages,
  draftFromTenant,
  draftPlatform,
  draftStageKeys,
  filterGroupHits,
  hasAccountAdminCredential,
  hasBeenProbed,
  isAccountAdminCredentialUsable,
  isValidAccountId,
  isValidWorkspaceId,
  isValidWorkspaceUrl,
  nextSuggestedStageKey,
  platformLabel,
  removeGroup,
  removeStage,
} from './tenantsAdminForm';

function stageHeading(key: string): string {
  return key.length > 0 ? key.charAt(0).toUpperCase() + key.slice(1) : key;
}
import { bindingModeBadge } from '../platformLabels';

const MIN_QUERY = 2;
const DEBOUNCE_MS = 300;
const NO_HITS: PrincipalHit[] = [];

const FIELD_LABEL = 'block text-[11px] uppercase tracking-wide text-slate-400 font-medium mb-1';
const FIELD_INPUT =
  'w-full px-3 py-2 text-sm rounded-lg border border-slate-300 focus:outline-none focus:ring-2 focus:ring-blue-500/40';

const SEG_BASE = 'px-3 py-1.5 rounded-lg text-xs font-medium transition-colors whitespace-nowrap';
const SEG_ACTIVE = 'bg-white text-slate-900 shadow-sm';
const SEG_IDLE = 'text-slate-500 hover:text-slate-700';

export default function TenantModal({
  tenant,
  onSubmit,
  onClose,
}: {
  tenant: TenantView | null;
  onSubmit: (draft: TenantDraft) => Promise<void>;
  onClose: () => void;
}) {
  const [draft, setDraft] = useState<TenantDraft>(() =>
    tenant ? draftFromTenant(tenant) : EMPTY_TENANT_DRAFT,
  );

  const [query, setQuery] = useState('');
  const [settled, setSettled] = useState<{ query: string; hits: PrincipalHit[]; error: string | null } | null>(null);

  const [actionPending, setActionPending] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);

  const nameRef = useRef<HTMLInputElement>(null);

  const mountedRef = useRef(true);
  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
    };
  }, []);

  useEffect(() => {
    nameRef.current?.focus();
  }, []);

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

  // Decision: docs/adr/frontend-loading-state.md
  useEffect(() => {
    const q = query.trim();
    if (q.length < MIN_QUERY) return;
    let cancelled = false;
    const t = setTimeout(() => {
      principalsApi
        .search(q)
        .then((res) => {
          if (!cancelled) setSettled({ query: q, hits: res, error: null });
        })
        .catch((err: unknown) => {
          if (!cancelled) {
            setSettled({ query: q, hits: NO_HITS, error: err instanceof Error ? err.message : 'Search failed.' });
          }
        });
    }, DEBOUNCE_MS);
    return () => {
      cancelled = true;
      clearTimeout(t);
    };
  }, [query]);

  const trimmedQuery = query.trim();
  const searchable = trimmedQuery.length >= MIN_QUERY;
  const current = searchable && settled?.query === trimmedQuery ? settled : null;
  const hits = current?.hits ?? NO_HITS;
  const searched = current !== null;
  const searching = searchable && current === null;
  const searchError = current?.error ?? null;

  const displayedHits = useMemo(() => filterGroupHits(hits, draft.groups), [hits, draft.groups]);

  const set = useCallback(<K extends keyof TenantDraft>(key: K, value: TenantDraft[K]) => {
    setDraft((d) => ({ ...d, [key]: value }));
  }, []);

  const setStage = useCallback(
    (stage: string, field: keyof StageDraft, value: string) => {
      setDraft((d) => ({
        ...d,
        stages: { ...d.stages, [stage]: { ...d.stages[stage], [field]: value } },
      }));
    },
    [],
  );

  const setDbStage = useCallback(
    (stage: StageKey, field: keyof DatabricksStageDraft, value: string) => {
      setDraft((d) => {
        const db = draftDatabricksStages(d);
        return { ...d, databricks: { ...db, [stage]: { ...db[stage], [field]: value } } };
      });
    },
    [],
  );

  const setPlatform = useCallback((platform: TenantPlatform) => {
    setDraft((d) => ({ ...d, platform }));
  }, []);

  const handleAddGroup = useCallback((hit: PrincipalHit) => {
    setDraft((d) => ({ ...d, groups: addGroup(d.groups, hit) }));
  }, []);
  const handleRemoveGroup = useCallback((id: string) => {
    setDraft((d) => ({ ...d, groups: removeGroup(d.groups, id) }));
  }, []);

  const [newStageName, setNewStageName] = useState('');
  const handleAddStage = useCallback((key: string) => {
    setDraft((d) => addStage(d, key));
    setNewStageName('');
  }, []);
  const handleRemoveStage = useCallback((key: string) => {
    setDraft((d) => removeStage(d, key));
  }, []);

  const isEdit = tenant !== null;
  const submittable = canSubmit(draft, isEdit);
  const platform = draftPlatform(draft);
  const isDatabricks = platform === 'databricks';
  const dbStages = draftDatabricksStages(draft);
  const stageKeys = draftStageKeys(draft);
  const canRemoveStage = stageKeys.length > 1;
  const suggestedStage = nextSuggestedStageKey(draft);

  const badges = capabilityBadges(platform, tenant?.capabilities);
  const probed = hasBeenProbed(tenant?.capabilities);
  const modeBadge = bindingModeBadge(tenant?.binding_mode);
  const adminCredentialUsable = isAccountAdminCredentialUsable(draft);
  const adminCredentialPresent = hasAccountAdminCredential(draft);
  const storedAdminCredential = draft.has_account_admin_credential === true;

  const stageCardHeader = (stage: string) => (
    <div className="flex items-center justify-between gap-2">
      <h3 className="text-xs font-semibold uppercase tracking-wide text-slate-500">
        {stageHeading(stage)}
      </h3>
      {canRemoveStage && (
        <button
          type="button"
          onClick={() => handleRemoveStage(stage)}
          disabled={actionPending}
          aria-label={`Remove ${stageHeading(stage)} stage`}
          className="text-[11px] text-slate-400 hover:text-rose-600 transition-colors disabled:opacity-40"
        >
          Remove
        </button>
      )}
    </div>
  );

  const renderAddStage = () => (
    <div className="mt-3 flex items-center gap-2">
      <input
        type="text"
        value={newStageName}
        onChange={(e) => setNewStageName(e.target.value)}
        placeholder={suggestedStage ? `Add a stage (e.g. ${suggestedStage})` : 'Add a stage'}
        className={`${FIELD_INPUT} max-w-[16rem]`}
        autoComplete="off"
        aria-label="New stage name"
      />
      <button
        type="button"
        onClick={() => handleAddStage(newStageName.trim() || suggestedStage || '')}
        disabled={actionPending || (newStageName.trim().length === 0 && !suggestedStage)}
        className="px-3 py-1.5 rounded-lg bg-white border border-slate-300 text-slate-700 text-xs font-medium hover:bg-slate-50 transition-colors disabled:opacity-40"
      >
        Add stage
      </button>
    </div>
  );

  const renderStage = (stage: string) => {
    const s = draft.stages[stage] ?? { account_id: '', region: '', ecr_repo_uri: '', push_role_arn: '', deploy_role_arn: '' };
    const invalid = s.account_id.trim().length > 0 && !isValidAccountId(s.account_id.trim());
    const field = (
      label: string,
      key: keyof StageDraft,
      placeholder: string,
      opts: { optional?: boolean; numeric?: boolean; hint?: boolean } = {},
    ) => (
      <div>
        <label htmlFor={`tenant-${stage}-${key}`} className={FIELD_LABEL}>
          {label} {opts.optional && <span className="normal-case text-slate-300">(optional)</span>}
        </label>
        <input
          id={`tenant-${stage}-${key}`}
          type="text"
          inputMode={opts.numeric ? 'numeric' : undefined}
          value={s[key]}
          onChange={(e) => setStage(stage, key, e.target.value)}
          placeholder={placeholder}
          className={FIELD_INPUT}
          autoComplete="off"
        />
        {opts.hint && invalid && (
          <p className="mt-1 text-xs text-red-600">Must be a 12-digit AWS account id.</p>
        )}
      </div>
    );
    return (
      <div className="rounded-xl border border-slate-200 p-3 space-y-3">
        {stageCardHeader(stage)}
        <div className="grid grid-cols-2 gap-3">
          {field('Account', 'account_id', '123456789012', { numeric: true, hint: true })}
          {field('Region', 'region', 'us-east-1')}
        </div>
        {field('ECR repository URI', 'ecr_repo_uri', 'account.dkr.ecr.…/repo', { optional: true })}
        {field('Push role ARN', 'push_role_arn', 'arn:aws:iam::…:role/push', { optional: true })}
        {field('Deploy role ARN', 'deploy_role_arn', 'arn:aws:iam::…:role/deploy', { optional: true })}
      </div>
    );
  };

  const renderDatabricksStage = (stage: StageKey) => {
    const s = dbStages[stage];
    const urlTouched = s.workspace_url.trim().length > 0;
    const urlInvalid = urlTouched && !isValidWorkspaceUrl(s.workspace_url.trim());
    const idInvalid = s.workspace_id.trim().length > 0 && !isValidWorkspaceId(s.workspace_id.trim());
    const field = (
      label: string,
      key: keyof DatabricksStageDraft,
      placeholder: string,
      opts: { optional?: string; numeric?: boolean } = {},
    ) => (
      <div>
        <label htmlFor={`tenant-db-${stage}-${key}`} className={FIELD_LABEL}>
          {label}{' '}
          {opts.optional && <span className="normal-case text-slate-300">({opts.optional})</span>}
        </label>
        <input
          id={`tenant-db-${stage}-${key}`}
          type="text"
          inputMode={opts.numeric ? 'numeric' : undefined}
          value={String(s[key])}
          onChange={(e) => setDbStage(stage, key, e.target.value)}
          placeholder={placeholder}
          className={FIELD_INPUT}
          autoComplete="off"
        />
      </div>
    );
    return (
      <div className="rounded-xl border border-slate-200 p-3 space-y-3">
        {stageCardHeader(stage)}

        <div>
          <label htmlFor={`tenant-db-${stage}-workspace_url`} className={FIELD_LABEL}>
            Workspace URL
          </label>
          <input
            id={`tenant-db-${stage}-workspace_url`}
            type="text"
            value={s.workspace_url}
            onChange={(e) => setDbStage(stage, 'workspace_url', e.target.value)}
            placeholder="https://dbc-….cloud.databricks.com"
            className={FIELD_INPUT}
            autoComplete="off"
            aria-invalid={urlInvalid || undefined}
          />
          {urlInvalid && (
            <p className="mt-1 text-xs text-red-600">
              Must be an https workspace origin — no trailing slash, path or port.
            </p>
          )}
        </div>

        <div className="grid grid-cols-2 gap-3">
          <div>
            <label htmlFor={`tenant-db-${stage}-workspace_id`} className={FIELD_LABEL}>
              Workspace ID
            </label>
            <input
              id={`tenant-db-${stage}-workspace_id`}
              type="text"
              inputMode="numeric"
              value={s.workspace_id}
              onChange={(e) => setDbStage(stage, 'workspace_id', e.target.value)}
              placeholder="0"
              className={FIELD_INPUT}
              autoComplete="off"
              aria-invalid={idInvalid || undefined}
            />
            {idInvalid ? (
              <p className="mt-1 text-xs text-red-600">Digits only.</p>
            ) : (
              <p className="mt-1 text-[11px] text-slate-400">
                The <code>o=</code> value in the workspace URL; 0 when it has none.
              </p>
            )}
          </div>
          <div>
            <label htmlFor={`tenant-db-${stage}-cloud`} className={FIELD_LABEL}>
              Cloud
            </label>
            <select
              id={`tenant-db-${stage}-cloud`}
              value={s.cloud}
              onChange={(e) => setDbStage(stage, 'cloud', e.target.value)}
              className={FIELD_INPUT}
            >
              <option value="aws">AWS</option>
              <option value="azure">Azure</option>
              <option value="gcp">GCP</option>
            </select>
          </div>
        </div>

        {field('Region', 'region', 'us-east-1', { optional: 'optional' })}
        {field('Databricks account ID', 'account_id', 'UUID', {
          optional: 'required for federation',
        })}
        {field('SP client ID', 'sp_client_id', 'Workspace service-principal client id')}

        <div>
          <label htmlFor={`tenant-db-${stage}-sp_client_secret`} className={FIELD_LABEL}>
            SP client secret{' '}
            {s.has_sp_secret && (
              <span className="normal-case text-slate-300">(stored — leave blank to keep)</span>
            )}
          </label>
          <input
            id={`tenant-db-${stage}-sp_client_secret`}
            type="password"
            value={s.sp_client_secret}
            onChange={(e) => setDbStage(stage, 'sp_client_secret', e.target.value)}
            placeholder={s.has_sp_secret ? 'Paste a new secret to replace it' : 'Paste the client secret'}
            className={FIELD_INPUT}
            autoComplete="new-password"
          />
          <p className="mt-1 text-[11px] text-slate-400">
            {s.has_sp_secret
              ? 'A secret is stored in Secrets Manager and is never shown again. Typing here replaces it.'
              : 'Stored in Secrets Manager and never shown again.'}
          </p>
        </div>
      </div>
    );
  };

  const handleSubmit = useCallback(async () => {
    if (actionPending || !canSubmit(draft, isEdit)) return;
    setActionPending(true);
    setActionError(null);
    try {
      await onSubmit(draft);
    } catch (err: unknown) {
      if (mountedRef.current) {
        setActionError(err instanceof Error ? err.message : 'Failed to save tenant.');
      }
    } finally {
      if (mountedRef.current) setActionPending(false);
    }
  }, [actionPending, draft, isEdit, onSubmit]);

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-slate-900/30 backdrop-blur-sm p-4"
      role="dialog"
      aria-modal="true"
      aria-label={isEdit ? 'Edit tenant' : 'Create tenant'}
      onMouseDown={(e) => {
        if (e.target === e.currentTarget && !actionPending) onClose();
      }}
    >
      <div className="w-full max-w-2xl max-h-[90vh] overflow-y-auto bg-white rounded-2xl border border-slate-200 shadow-xl">
        <div className="flex items-start justify-between gap-3 px-5 py-4 border-b border-slate-200/60">
          <div className="min-w-0">
            <h2 className="text-sm font-semibold text-slate-900 leading-tight">
              {isEdit ? 'Edit tenant' : 'Create tenant'}
            </h2>
            <p className="text-xs text-slate-500 mt-0.5">
              A tenant is a line-of-business unit that owns agents, MCP servers and projects. Link
              at least one Entra group; members of those groups see the tenant’s resources.
            </p>
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

        <div className="px-5 py-4 space-y-4">
          <div>
            <span className={FIELD_LABEL} id="tenant-platform-label">
              Platform
            </span>
            {isEdit ? (
              <div className="flex items-center gap-2">
                <span className="inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium bg-slate-100 text-slate-700">
                  {platformLabel(platform)}
                </span>
                <span className="text-[11px] text-slate-400">
                  A tenant’s platform cannot be changed after it is created.
                </span>
              </div>
            ) : (
              <>
                <div
                  className="inline-flex items-center gap-1 p-1 rounded-xl bg-slate-100"
                  role="group"
                  aria-labelledby="tenant-platform-label"
                >
                  {PLATFORM_OPTIONS.map((opt) => (
                    <button
                      key={opt.value}
                      type="button"
                      onClick={() => setPlatform(opt.value)}
                      disabled={actionPending}
                      aria-pressed={platform === opt.value}
                      className={`${SEG_BASE} ${platform === opt.value ? SEG_ACTIVE : SEG_IDLE} disabled:opacity-40`}
                    >
                      {opt.label}
                    </button>
                  ))}
                </div>
                <p className="text-[11px] text-slate-400 mt-1">
                  {PLATFORM_OPTIONS.find((o) => o.value === platform)?.hint}
                </p>
              </>
            )}
          </div>

          <div className="grid grid-cols-2 gap-3">
            <div>
              <label htmlFor="tenant-name" className={FIELD_LABEL}>
                Name
              </label>
              <input
                id="tenant-name"
                ref={nameRef}
                type="text"
                value={draft.name}
                onChange={(e) => set('name', e.target.value)}
                placeholder="Retail Claims"
                className={FIELD_INPUT}
                autoComplete="off"
              />
            </div>
            <div>
              <label htmlFor="tenant-lob" className={FIELD_LABEL}>
                Line of business
              </label>
              <input
                id="tenant-lob"
                type="text"
                value={draft.line_of_business}
                onChange={(e) => set('line_of_business', e.target.value)}
                placeholder="Claims"
                className={FIELD_INPUT}
                autoComplete="off"
              />
            </div>
          </div>

          <div>
            <label htmlFor="tenant-description" className={FIELD_LABEL}>
              Description <span className="normal-case text-slate-300">(optional)</span>
            </label>
            <input
              id="tenant-description"
              type="text"
              value={draft.description}
              onChange={(e) => set('description', e.target.value)}
              placeholder="What this tenant owns…"
              className={FIELD_INPUT}
              autoComplete="off"
            />
          </div>

          <div className="rounded-lg border border-slate-200/70 bg-slate-50 p-3">
            <p className="text-[11px] leading-relaxed text-slate-500">{PRIVILEGE_STATEMENT[platform]}</p>
          </div>

          {isDatabricks ? (
            <>
              <div>
                <span className={FIELD_LABEL}>Stage workspaces</span>
                <div className="grid grid-cols-2 gap-3">
                  {stageKeys.map((stage) => (
                    <div key={stage}>{renderDatabricksStage(stage)}</div>
                  ))}
                </div>
                {renderAddStage()}
              </div>

              <div className="rounded-xl border border-slate-200 p-3 space-y-3">
                <div>
                  <h3 className="text-xs font-semibold uppercase tracking-wide text-slate-500">
                    Account admin credential{' '}
                    <span className="normal-case text-red-500 font-medium">(required)</span>
                  </h3>
                  <p className="text-[11px] text-slate-400 mt-1">
                    An account-level service principal’s <span className="text-slate-600">client
                    ID and secret</span>. It is what lets AGP establish{' '}
                    <span className="text-slate-600">Federation</span>, where Databricks and Unity
                    Catalog see the real caller. Without it the tenant is still discoverable,
                    registrable and observable, but{' '}
                    <span className="text-slate-600">invoke is refused</span> — federation needs
                    this credential and user sync from Entra to Databricks. The credential is
                    verified when the tenant is saved.
                  </p>
                  {storedAdminCredential && (
                    <p className="text-[11px] text-slate-400 mt-1">
                      A credential is already stored for this tenant. Leave both boxes blank to
                      keep it, or fill both to replace it.
                    </p>
                  )}
                </div>
                <div className="grid grid-cols-2 gap-3">
                  <div>
                    <label htmlFor="tenant-admin-client-id" className={FIELD_LABEL}>
                      Client ID
                    </label>
                    <input
                      id="tenant-admin-client-id"
                      type="text"
                      value={draft.account_admin_client_id ?? ''}
                      onChange={(e) => set('account_admin_client_id', e.target.value)}
                      placeholder="Account-level service-principal client id"
                      className={FIELD_INPUT}
                      autoComplete="off"
                    />
                  </div>
                  <div>
                    <label htmlFor="tenant-admin-secret" className={FIELD_LABEL}>
                      Client secret
                    </label>
                    <input
                      id="tenant-admin-secret"
                      type="password"
                      value={draft.account_admin_secret ?? ''}
                      onChange={(e) => set('account_admin_secret', e.target.value)}
                      placeholder="Paste the client secret"
                      className={FIELD_INPUT}
                      autoComplete="new-password"
                    />
                  </div>
                </div>
                {!adminCredentialUsable ? (
                  <p className="text-xs text-red-600">
                    Supply both the client ID and the secret{storedAdminCredential ? ', or leave both blank to keep the stored one' : ''}.
                  </p>
                ) : (
                  !adminCredentialPresent && (
                    <p className="text-[11px] text-slate-500">
                      Until both boxes are filled this tenant cannot federate, so its agents could
                      be catalogued but never invoked.{' '}
                      {isEdit
                        ? 'Supplying the pair here unlocks federation on the next probe.'
                        : 'Confirm stays disabled until then.'}
                    </p>
                  )
                )}
                <p className="text-[11px] text-slate-400">
                  Stored in Secrets Manager and never shown again.
                </p>
              </div>

              {isEdit && (
                <div>
                  <span className={FIELD_LABEL}>Capabilities</span>
                  {probed ? (
                    <div className="flex flex-wrap items-center gap-1.5">
                      {modeBadge && (
                        <span
                          className={`inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium ${modeBadge.tint}`}
                          title={modeBadge.hint}
                        >
                          {modeBadge.label}
                          <span className="sr-only"> — {modeBadge.hint}</span>
                        </span>
                      )}
                      {badges.map((b) => (
                        <span
                          key={b.key}
                          className={`inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-xs font-medium ${
                            b.on === true
                              ? 'bg-emerald-50 text-emerald-700'
                              : b.on === false
                                ? 'bg-slate-100 text-slate-500'
                                : 'bg-white border border-slate-200 text-slate-400'
                          }`}
                          title={
                            b.on === undefined
                              ? `${b.label}: not probed`
                              : `${b.label}: ${b.on ? 'available' : 'unavailable'}`
                          }
                        >
                          {b.label}
                          <span aria-hidden="true">
                            {b.on === true ? '✓' : b.on === false ? '—' : '?'}
                          </span>
                        </span>
                      ))}
                    </div>
                  ) : (
                    <p className="text-xs text-slate-400">
                      Not probed yet — saving this tenant runs the workspace probes.
                    </p>
                  )}
                </div>
              )}
            </>
          ) : (
            <div>
              <span className={FIELD_LABEL}>Stage accounts</span>
              <div className="grid grid-cols-2 gap-3">
                {stageKeys.map((stage) => (
                  <div key={stage}>{renderStage(stage)}</div>
                ))}
              </div>
              {renderAddStage()}
            </div>
          )}

          <div>
            <label htmlFor="tenant-group-search" className={FIELD_LABEL}>
              Entra groups
            </label>

            {draft.groups.length > 0 && (
              <div className="flex flex-wrap gap-1.5 mb-2">
                {draft.groups.map((g) => (
                  <span
                    key={g.id}
                    className="inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-xs font-medium bg-violet-50 text-violet-700"
                    title={g.id}
                  >
                    <span className="max-w-[14rem] truncate">{g.display_name}</span>
                    <button
                      type="button"
                      onClick={() => handleRemoveGroup(g.id)}
                      disabled={actionPending}
                      aria-label={`Remove group ${g.display_name}`}
                      className="text-violet-400 hover:text-violet-700 transition-colors disabled:opacity-40"
                    >
                      <span aria-hidden="true" className="text-sm leading-none">×</span>
                    </button>
                  </span>
                ))}
              </div>
            )}

            <input
              id="tenant-group-search"
              type="text"
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              placeholder="Search the directory for groups…"
              className={FIELD_INPUT}
              autoComplete="off"
            />

            <div className="mt-2">
              {searchError && (
                <p className="text-sm text-red-600" role="alert">
                  {searchError}
                </p>
              )}

              {!searchError && searching && <p className="text-sm text-slate-400">Searching…</p>}

              {!searchError && !searching && query.trim().length > 0 && query.trim().length < MIN_QUERY && (
                <p className="text-sm text-slate-400">Type at least {MIN_QUERY} characters to search.</p>
              )}

              {!searchError && !searching && searched && displayedHits.length === 0 && query.trim().length >= MIN_QUERY && (
                <p className="text-sm text-slate-400">No new groups match “{query.trim()}”.</p>
              )}

              {!searchError && !searching && displayedHits.length > 0 && (
                <ul className="max-h-40 overflow-y-auto -mx-1 divide-y divide-slate-100">
                  {displayedHits.map((hit) => (
                    <li key={hit.id}>
                      <button
                        type="button"
                        onClick={() => handleAddGroup(hit)}
                        className="w-full flex items-center gap-3 px-2 py-2 rounded-lg text-left transition-colors hover:bg-slate-50"
                      >
                        <AgentAvatar name={hit.display_name} size="sm" />
                        <span className="min-w-0 flex-1">
                          <span className="block text-sm font-medium text-slate-800 truncate">
                            {hit.display_name}
                          </span>
                          {hit.mail && (
                            <span className="block text-xs text-slate-400 truncate">{hit.mail}</span>
                          )}
                        </span>
                        <span className="inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium bg-violet-50 text-violet-700">
                          Group
                        </span>
                      </button>
                    </li>
                  ))}
                </ul>
              )}
            </div>
          </div>

          {actionError && (
            <p className="text-sm text-red-600" role="alert">
              {actionError}
            </p>
          )}
        </div>

        <div className="flex items-center justify-end gap-2 px-5 py-4 border-t border-slate-200/60">
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
            disabled={actionPending || !submittable}
            className="px-3.5 py-1.5 rounded-lg bg-blue-600 text-white text-sm font-medium hover:bg-blue-700 transition-colors disabled:opacity-40"
          >
            {actionPending ? 'Saving…' : isEdit ? 'Save changes' : 'Create tenant'}
          </button>
        </div>
      </div>
    </div>
  );
}
