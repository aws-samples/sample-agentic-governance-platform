// TenantModal — the create/edit tenant dialog for the Tenants admin tab (Epic 24;
// platform-typed in E29).
//
// Shell cloned from AddUserModal (fixed-inset backdrop bg-slate-900/30
// backdrop-blur-sm, role="dialog" aria-modal, mountedRef unmount guard,
// Escape-to-close, footer Cancel/Confirm, inline actionError <p role="alert">).
// Body = the tenant form (name, LoB, description, region, dev/prod 12-digit AWS
// accounts) + Entra-group linking: AddUserModal's plain debounced
// principalsApi.search block, CLIENT-SIDE-filtered to type === 'group'
// (principalsApi.search has no kind param — research §4), rendered as
// multi-select chips with a remove ×. Confirm gates on canSubmit (name + LoB
// present, >= 1 group, both account ids valid); on confirm it calls
// onSubmit(draft) and the parent closes + refetches. Edit mode seeds the draft
// from the existing tenant — group chips fall back to the raw ids as labels
// (no Graph round-trip for display names).
//
// E29 — the form branches ONCE, at the top, on a platform picker (aws | databricks), which
// then selects the stage-config body. Three rules this file follows and does not relitigate:
//
//   • The picker appears on CREATE only. A tenant's platform is immutable afterwards (the
//     backend's update model has no such field), so edit renders it as a static label —
//     offering a control that cannot take effect is the dishonest option.
//   • The SP client secret is WRITE-ONLY. It is never seeded, never echoed, and an empty box
//     on edit means "keep the stored secret" — the stored-vs-absent state comes from the
//     record's Secrets Manager ARN, the same way ConnectionsAdmin uses `has_secret`.
//   • This file DECIDES nothing. Validity, payload shape, badge text and the has-secret
//     reading all live in tenantsAdminForm.ts, where they are unit-tested; here they are
//     only bound and rendered.

import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { principalsApi } from '../../../api/client';
import type { PrincipalHit, TenantInfo, TenantPlatform } from '../../../api/client';
import { AgentAvatar } from '../agentUi';
import {
  EMPTY_TENANT_DRAFT,
  PLATFORM_OPTIONS,
  type DatabricksStageDraft,
  type StageKey,
  type TenantDraft,
  addGroup,
  canSubmit,
  capabilityBadges,
  draftDatabricksStages,
  draftFromTenant,
  draftPlatform,
  filterGroupHits,
  hasAccountAdminCredential,
  hasBeenProbed,
  isAccountAdminCredentialUsable,
  isValidAccountId,
  isValidWorkspaceId,
  isValidWorkspaceUrl,
  platformLabel,
  removeGroup,
} from './tenantsAdminForm';
// The binding-mode badge (label + consequence + tint) lives in `platformLabels` alongside every
// other platform-conditional rendering decision, and it is the SAME helper the agent-detail badge
// uses — which is the point: two surfaces, one string, no way for them to drift.
import { bindingModeBadge } from '../platformLabels';

const MIN_QUERY = 2;
const DEBOUNCE_MS = 300;

const FIELD_LABEL = 'block text-[11px] uppercase tracking-wide text-slate-400 font-medium mb-1';
const FIELD_INPUT =
  'w-full px-3 py-2 text-sm rounded-lg border border-slate-300 focus:outline-none focus:ring-2 focus:ring-blue-500/40';

// Platform picker segmented control — the same three classes ConnectionsAdmin's auth-path
// picker uses, so the two admin surfaces' "pick a path" control reads identically.
const SEG_BASE = 'px-3 py-1.5 rounded-lg text-xs font-medium transition-colors whitespace-nowrap';
const SEG_ACTIVE = 'bg-white text-slate-900 shadow-sm';
const SEG_IDLE = 'text-slate-500 hover:text-slate-700';

export default function TenantModal({
  tenant,
  onSubmit,
  onClose,
}: {
  // null → create; a TenantInfo → edit (seeds the draft).
  tenant: TenantInfo | null;
  onSubmit: (draft: TenantDraft) => Promise<void>;
  onClose: () => void;
}) {
  const [draft, setDraft] = useState<TenantDraft>(() =>
    tenant ? draftFromTenant(tenant) : EMPTY_TENANT_DRAFT,
  );

  const [query, setQuery] = useState('');
  const [hits, setHits] = useState<PrincipalHit[]>([]);
  const [searching, setSearching] = useState(false);
  // Whether a search has completed for the current query — gates the "No matches"
  // empty state so it doesn't flash before the first fetch (AddUserModal idiom).
  const [searched, setSearched] = useState(false);
  const [searchError, setSearchError] = useState<string | null>(null);

  const [actionPending, setActionPending] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);

  const nameRef = useRef<HTMLInputElement>(null);

  // Unmount guard — onSubmit resolves async and can land after the modal closes.
  const mountedRef = useRef(true);
  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
    };
  }, []);

  // Focus the name field on open.
  useEffect(() => {
    nameRef.current?.focus();
  }, []);

  // Close on Escape (cloned from AddUserModal).
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

  // Debounced Entra directory search (AddUserModal's 300ms idiom). Stores the
  // raw hits — the groups-not-already-linked filter happens at render (below)
  // so chip add/remove never re-fires the fetch.
  useEffect(() => {
    const q = query.trim();
    if (q.length < MIN_QUERY) {
      setHits([]);
      setSearched(false);
      setSearching(false);
      setSearchError(null);
      return;
    }
    let cancelled = false;
    setSearching(true);
    setSearchError(null);
    const t = setTimeout(() => {
      principalsApi
        .search(q)
        .then((res) => {
          if (!cancelled) {
            setHits(res);
            setSearchError(null);
          }
        })
        .catch((err: unknown) => {
          if (!cancelled) {
            setHits([]);
            // The axios interceptor surfaces the backend `detail` as err.message.
            setSearchError(err instanceof Error ? err.message : 'Search failed.');
          }
        })
        .finally(() => {
          if (!cancelled) {
            setSearching(false);
            setSearched(true);
          }
        });
    }, DEBOUNCE_MS);
    return () => {
      cancelled = true;
      clearTimeout(t);
    };
  }, [query]);

  // Displayed results — groups not already linked, derived at render so chip
  // add/remove updates the list without re-fetching.
  const displayedHits = useMemo(() => filterGroupHits(hits, draft.groups), [hits, draft.groups]);

  const set = useCallback(<K extends keyof TenantDraft>(key: K, value: TenantDraft[K]) => {
    setDraft((d) => ({ ...d, [key]: value }));
  }, []);

  // Update one field of one stage (dev|prod) sub-draft, immutably.
  const setStage = useCallback(
    (stage: 'dev' | 'prod', field: keyof TenantDraft['stages']['dev'], value: string) => {
      setDraft((d) => ({
        ...d,
        stages: { ...d.stages, [stage]: { ...d.stages[stage], [field]: value } },
      }));
    },
    [],
  );

  // The same, for a Databricks stage. `draftDatabricksStages` materialises a blank pair when
  // the draft has none, so toggling to Databricks and typing works without a seeding step.
  const setDbStage = useCallback(
    (stage: StageKey, field: keyof DatabricksStageDraft, value: string) => {
      setDraft((d) => {
        const db = draftDatabricksStages(d);
        return { ...d, databricks: { ...db, [stage]: { ...db[stage], [field]: value } } };
      });
    },
    [],
  );

  // Switching platform keeps BOTH halves of the draft intact — only which one is read
  // changes. A mis-click therefore costs nothing, which is why the picker needs no confirm.
  const setPlatform = useCallback((platform: TenantPlatform) => {
    setDraft((d) => ({ ...d, platform }));
  }, []);

  const handleAddGroup = useCallback((hit: PrincipalHit) => {
    setDraft((d) => ({ ...d, groups: addGroup(d.groups, hit) }));
  }, []);
  const handleRemoveGroup = useCallback((id: string) => {
    setDraft((d) => ({ ...d, groups: removeGroup(d.groups, id) }));
  }, []);

  const isEdit = tenant !== null;
  // `isEdit` is passed because the account-admin requirement is CREATE-only (E29/T14): an existing
  // tenant in the supported `invoke_unavailable` state must stay renameable/administrable.
  const submittable = canSubmit(draft, isEdit);
  const platform = draftPlatform(draft);
  const isDatabricks = platform === 'databricks';
  const dbStages = draftDatabricksStages(draft);

  // Probe results belong to the RECORD, not the draft: they are written by the backend's
  // connect-time probe, so only an existing tenant has any. A create modal shows none.
  const badges = capabilityBadges(platform, tenant?.capabilities);
  const probed = hasBeenProbed(tenant?.capabilities);
  const modeBadge = bindingModeBadge(tenant?.binding_mode);
  const adminCredentialUsable = isAccountAdminCredentialUsable(draft);
  // Presence, as distinct from coherence (E29/T14): typed here, or already stored on the record.
  const adminCredentialPresent = hasAccountAdminCredential(draft);
  const storedAdminCredential = draft.has_account_admin_credential === true;

  // One stage column (Dev | Prod). account_id + region lead (account_id the only
  // required field), then the three optional ECR/role targets. `invalid` drives
  // the inline 12-digit hint under the account field.
  const renderStage = (stage: 'dev' | 'prod', heading: string, invalid: boolean) => {
    const s = draft.stages[stage];
    const field = (
      label: string,
      key: keyof TenantDraft['stages']['dev'],
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
        <h3 className="text-xs font-semibold uppercase tracking-wide text-slate-500">{heading}</h3>
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

  // One Databricks stage column. Field order is deliberate: the workspace origin identifies
  // the stage, then its coordinates, then the service-principal identity, and the write-only
  // secret last — so the credential is the final thing typed, next to the note saying where
  // it goes.
  const renderDatabricksStage = (stage: StageKey, heading: string) => {
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
        <h3 className="text-xs font-semibold uppercase tracking-wide text-slate-500">{heading}</h3>

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

        {/* Write-only SP secret. The secret itself is never seeded and never echoed, exactly
            as ConnectionsAdmin treats a stored PAT; `has_sp_secret` drives the copy below.
            The stage's `sp_client_secret_arn` is held in draft state but deliberately has NO
            input here — it is carried only so the update re-emits it instead of clearing it. */}
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
      // Parent closes the modal + refetches on success; nothing to do here.
    } catch (err: unknown) {
      if (mountedRef.current) {
        setActionError(err instanceof Error ? err.message : 'Failed to save tenant.');
      }
    } finally {
      if (mountedRef.current) setActionPending(false);
    }
  }, [actionPending, draft, isEdit, onSubmit]);

  // Inline invalid-account hint — shown once the field has content that fails the
  // 12-digit rule (blank fields just gate the Confirm button, no red noise).
  const devInvalid =
    draft.stages.dev.account_id.trim().length > 0 && !isValidAccountId(draft.stages.dev.account_id.trim());
  const prodInvalid =
    draft.stages.prod.account_id.trim().length > 0 && !isValidAccountId(draft.stages.prod.account_id.trim());

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-slate-900/30 backdrop-blur-sm p-4"
      role="dialog"
      aria-modal="true"
      aria-label={isEdit ? 'Edit tenant' : 'Create tenant'}
      onMouseDown={(e) => {
        // Click on the backdrop (not the panel) closes — unless mid-submit.
        if (e.target === e.currentTarget && !actionPending) onClose();
      }}
    >
      <div className="w-full max-w-2xl max-h-[90vh] overflow-y-auto bg-white rounded-2xl border border-slate-200 shadow-xl">
        {/* Header. */}
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

        {/* Body. */}
        <div className="px-5 py-4 space-y-4">
          {/* Platform — the ONE branch point, at the top so it is chosen before anything
              below it is typed. Immutable after create, so edit renders a static label. */}
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

          {/* Name + LoB. */}
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

          {/* Description. */}
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

          {/* Per-stage config — AWS cross-account CICD targets (E25), or Databricks
              workspaces (E29). One or the other, never both: the platform picked above
              decides which, and the other half's state is simply not read. */}
          {isDatabricks ? (
            <>
              <div>
                <span className={FIELD_LABEL}>Stage workspaces</span>
                <div className="grid grid-cols-2 gap-3">
                  {renderDatabricksStage('dev', 'Dev')}
                  {renderDatabricksStage('prod', 'Prod')}
                </div>
              </div>

              {/* Account-admin credential — REQUIRED since E29/T14 (§3B, tenet 4). Federation is
                  the model, not a mode: without this credential AGP cannot amend the account
                  federation policy, so the tenant can be inventoried but its agents cannot be
                  invoked. The copy says that outcome plainly instead of offering a "fallback",
                  because the fallback it used to name (SP secret) collapses every caller into one
                  service identity — and that is not a lite version of governance, it is a
                  different thing. The GATE is on CREATE only (see `canSubmit`): on an EDIT blank
                  boxes always mean "keep whatever is stored, including nothing", because §3B makes
                  the un-federated tenant a supported state that must stay administrable. The ask
                  stays visible on both paths — it just stops being a blocker on an edit. */}
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
                {/* Two different things, said in two different registers. An incoherent pair is a
                    TYPO — content that fails a rule — so it takes the modal's red inline-error
                    idiom, the same one the 12-digit account hint uses. A missing credential is
                    NOT an error to shout: on a create form it is simply a required field not yet
                    filled, and this modal's stated convention (see the invalid-account note above)
                    is that "blank fields just gate the Confirm button, no red noise" — name, LoB,
                    groups and workspace_url are all required and all silent when blank. So the
                    consequence is carried as an informational notice in the same tone as the rest
                    of this block, always on while the credential is absent: on create the disabled
                    Confirm is the refusal, and on edit (where T14's gate does not apply) the notice
                    is the whole point — it states the state and what unlocks it. */}
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

              {/* What the last probe found. Only meaningful on an existing tenant — a create
                  modal has nothing to report, and says so rather than showing empty badges. */}
              {isEdit && (
                <div>
                  <span className={FIELD_LABEL}>Capabilities</span>
                  {probed ? (
                    <div className="flex flex-wrap items-center gap-1.5">
                      {/* One badge helper, three modes (E29/T14). This used to be a fixed blue
                          pill plus an inline `=== 'federation' || === 'sp_secret'` guard, which
                          had to be extended every time the vocabulary grew and silently dropped
                          the hint for anything it did not enumerate. `bindingModeBadge` owns the
                          label, the consequence sentence and the tint together, so this surface
                          cannot disagree with the agent-detail badge — and the sr-only span keeps
                          the consequence off the hover-only path. */}
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
                {renderStage('dev', 'Dev', devInvalid)}
                {renderStage('prod', 'Prod', prodInvalid)}
              </div>
            </div>
          )}

          {/* Entra groups — multi-select chips + directory search. */}
          <div>
            <label htmlFor="tenant-group-search" className={FIELD_LABEL}>
              Entra groups
            </label>

            {/* Selected chips. */}
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

            {/* Results region. */}
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

        {/* Footer actions. */}
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
