import { useCallback, useEffect, useMemo, useRef, useState } from 'react';

import {
  githubTemplatesApi,
  projectsApi,
  type DataClassification,
  type Repository,
  type RepositoryCreate,
  type StepState,
  type TemplateView,
} from '../../api/client';
import { useLoad } from '../../api/useLoad';
import { ModalShell } from './ModalShell';
import { MaterializeTimeline } from './ProjectRepositoriesTab.tsx';
import { isMaterializeTerminal, nextBadgeFromSteps } from './materializeSteps.ts';
import { isNothingToRetry, maintainerActionMessage } from './projectRoles';
import { OPS_BADGE, OPS_PRIMARY_BTN } from './opsUi';
import { AGENT_NAME_RE } from './studioModel';
import { STUDIO_AGENT_MODEL_ID } from './studioRun';
import {
  ARTIFACT_KIND_OPTIONS,
  BUILD_KIND_CHIP,
  INFRA_CHIP,
  MCP_DESCRIPTION_MAX,
  PLATFORM_CHIP,
  PREVIEW_CHIP,
  buildKindLabel,
  frameworkForTemplate,
  kindEmptyReason,
  mcpDescriptionError,
  mcpNameError,
  mcpNameHint,
  pickTemplateName,
  pickerEmptyReason,
  selectedTemplate,
  templateOptionLabel,
  materializeDialogCopy,
  templatesForKind,
  templatesForPlatform,
  type ArtifactKind,
} from './templateChain.ts';

const FIELD_LABEL = 'block text-[11px] uppercase tracking-wide text-slate-400 font-medium mb-1';
const FIELD_INPUT =
  'w-full px-3 py-2 text-sm rounded-lg border border-slate-300 focus:outline-none focus:ring-2 focus:ring-emerald-500/40';
const FIELD_SELECT =
  'w-full px-3 py-2 text-sm rounded-lg border border-slate-300 bg-white focus:outline-none focus:ring-2 focus:ring-emerald-500/40 disabled:opacity-40';
const PILL_SHAPE =
  'inline-flex items-center gap-1.5 text-[11px] font-semibold px-2 py-0.5 rounded-full capitalize';

const DATA_CLASSIFICATIONS: DataClassification[] = ['Public', 'Internal', 'Confidential', 'Restricted'];
const EMPTY_TEMPLATES: TemplateView[] = [];

export default function AddRepoModal({
  projectId,
  connectionId,
  tenantPlatform,
  mayMaintain,
  initialKind = 'agent',
  doneLabel,
  onClose,
  onCreated,
}: {
  projectId: string;
  connectionId: string;
  /** The project's tenant's platform, folded by the page; `"aws"` when the tenant is unknown to this caller. */
  tenantPlatform: string;
  mayMaintain: boolean;
  initialKind?: ArtifactKind;
  /** Replaces the finished timeline's "Done" button label; the caller then opens the new object. */
  doneLabel?: string;
  onClose: () => void;
  onCreated: (repo?: Repository) => void;
}) {
  const [name, setName] = useState('');
  const [kind, setKind] = useState<ArtifactKind>(initialKind);
  const [mcpName, setMcpName] = useState('');
  const [templateName, setTemplateName] = useState('');
  const [agentName, setAgentName] = useState('');
  const [modelId, setModelId] = useState(STUDIO_AGENT_MODEL_ID);
  const [purpose, setPurpose] = useState('');

  const [businessUnit, setBusinessUnit] = useState('');
  const [region, setRegion] = useState('');
  const [dataClassification, setDataClassification] = useState<DataClassification | ''>('');

  const [overrides, setOverrides] = useState<{ key: string; value: string }[]>([]);

  const templatesLoad = useLoad(() => githubTemplatesApi.list(connectionId), [connectionId], 'Failed to load templates.');
  const templates = templatesLoad.state.data ?? EMPTY_TEMPLATES;
  const loadingTemplates = templatesLoad.state.kind === 'loading';
  const loadError = templatesLoad.state.kind === 'error' ? templatesLoad.state.message : null;

  const [actionPending, setActionPending] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);

  const [repo, setRepo] = useState<Repository | null>(null);
  const [steps, setSteps] = useState<StepState[]>([]);
  const [retrying, setRetrying] = useState(false);
  const pollRef = useRef<number | null>(null);

  const nameRef = useRef<HTMLInputElement>(null);

  const mountedRef = useRef(true);
  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
    };
  }, []);

  const stopPolling = useCallback(() => {
    if (pollRef.current) {
      window.clearInterval(pollRef.current);
      pollRef.current = null;
    }
  }, []);

  const repoId = repo?.id ?? null;
  const terminal = steps.length > 0 && isMaterializeTerminal(steps);
  useEffect(() => {
    if (!repoId || terminal) {
      stopPolling();
      return;
    }
    if (pollRef.current) return;
    pollRef.current = window.setInterval(async () => {
      const fresh = await projectsApi.getRepoStatus(projectId, repoId).catch(() => null);
      if (fresh === null || !mountedRef.current) return;
      setRepo(fresh);
      setSteps(fresh.steps);
      if (isMaterializeTerminal(fresh.steps)) stopPolling();
    }, 3000);
    return stopPolling;
  }, [repoId, terminal, projectId, stopPolling]);

  useEffect(() => stopPolling, [stopPolling]);

  useEffect(() => {
    nameRef.current?.focus();
  }, []);

  const platformOffered = useMemo(
    () => templatesForPlatform(templates, tenantPlatform),
    [templates, tenantPlatform],
  );
  const offered = useMemo(() => templatesForKind(platformOffered, kind), [platformOffered, kind]);
  const [offeredFor, setOfferedFor] = useState(offered);
  if (offeredFor !== offered) {
    setOfferedFor(offered);
    setTemplateName(pickTemplateName(offered, templateName));
  }
  const isMcp = kind === 'mcp_server';
  const changeKind = useCallback((next: ArtifactKind) => setKind(next), []);
  const chosen = useMemo(() => selectedTemplate(offered, templateName), [offered, templateName]);
  const framework = useMemo(() => frameworkForTemplate(offered, templateName), [offered, templateName]);

  const agentNameValid = AGENT_NAME_RE.test(agentName);
  const effectiveMcpName = mcpName.trim() || name.trim();
  const mcpNameProblem = mcpNameError(effectiveMcpName);
  const mcpHint = mcpNameHint(mcpName, name);
  const mcpDescriptionProblem = mcpDescriptionError(purpose);
  const canSubmit =
    name.trim().length > 0 &&
    chosen !== null &&
    (isMcp ? mcpNameProblem === null && mcpDescriptionProblem === null : agentNameValid) &&
    !actionPending;

  const serializeOverrides = useCallback((): Record<string, string> | undefined => {
    const entries = overrides
      .map((o) => [o.key.trim(), o.value] as const)
      .filter(([k]) => k.length > 0);
    return entries.length > 0 ? Object.fromEntries(entries) : undefined;
  }, [overrides]);

  const handleSubmit = useCallback(async () => {
    if (!canSubmit) return;
    setActionPending(true);
    setActionError(null);
    try {
      const body: RepositoryCreate = !isMcp ? {
        name: name.trim(),
        template_name: templateName,
        agent_config: {
          agent_name: agentName.trim(),
          framework,
          model_id: modelId.trim(),
        },
        business_unit: businessUnit.trim() || undefined,
        region: region.trim() || undefined,
        data_classification: dataClassification || undefined,
        repo_overrides: serializeOverrides(),
        purpose: purpose.trim() || undefined,
      } : {
        name: name.trim(),
        template_name: templateName,
        mcp_name: effectiveMcpName,
        business_unit: businessUnit.trim() || undefined,
        region: region.trim() || undefined,
        data_classification: dataClassification || undefined,
        repo_overrides: serializeOverrides(),
        purpose: purpose.trim() || undefined,
      };
      const created = await projectsApi.addRepo(projectId, body);
      if (mountedRef.current) {
        setRepo(created);
        setSteps(created.steps);
      }
    } catch (err: unknown) {
      if (mountedRef.current) {
        setActionError(maintainerActionMessage(err, 'Failed to create the repository.'));
      }
    } finally {
      if (mountedRef.current) setActionPending(false);
    }
  }, [
    canSubmit,
    projectId,
    name,
    isMcp,
    effectiveMcpName,
    templateName,
    framework,
    agentName,
    modelId,
    businessUnit,
    region,
    dataClassification,
    serializeOverrides,
    purpose,
  ]);

  const addOverride = useCallback(() => setOverrides((rows) => [...rows, { key: '', value: '' }]), []);
  const updateOverride = useCallback(
    (index: number, patch: Partial<{ key: string; value: string }>) =>
      setOverrides((rows) => rows.map((row, i) => (i === index ? { ...row, ...patch } : row))),
    []
  );
  const removeOverride = useCallback(
    (index: number) => setOverrides((rows) => rows.filter((_, i) => i !== index)),
    []
  );

  const handleRetry = useCallback(async () => {
    if (!repo || retrying) return;
    setRetrying(true);
    setActionError(null);
    try {
      const resumed = await projectsApi.retryRepo(projectId, repo.id);
      if (mountedRef.current) {
        setRepo(resumed);
        setSteps(resumed.steps);
      }
    } catch (err: unknown) {
      if (isNothingToRetry(err)) {
        const fresh = await projectsApi.getRepoStatus(projectId, repo.id).catch(() => null);
        if (fresh !== null && mountedRef.current) {
          setRepo(fresh);
          setSteps(fresh.steps);
        }
      } else if (mountedRef.current) {
        setActionError(maintainerActionMessage(err, 'Failed to retry.'));
      }
    } finally {
      if (mountedRef.current) setRetrying(false);
    }
  }, [repo, retrying, projectId]);

  const handleClose = useCallback(() => {
    stopPolling();
    if (repo) {
      onCreated(repo);
    } else {
      onClose();
    }
  }, [repo, stopPolling, onCreated, onClose]);

  const noTemplates = !loadingTemplates && !loadError && offered.length === 0;
  const emptyReason = noTemplates
    ? (pickerEmptyReason(templates.length, platformOffered.length, tenantPlatform) ?? kindEmptyReason(kind))
    : null;

  const materializing = repo !== null;
  const hasFailed = steps.some((s) => s.status === 'failed');
  const allDone = steps.length > 0 && steps.every((s) => s.status === 'done');
  const badgeKey = steps.length > 0 ? nextBadgeFromSteps(steps) : 'provisioning';

  if (materializing) {
    const inFlight = !terminal;
    const heading = materializeDialogCopy(repo?.name, { allDone, hasFailed });
    return (
      <ModalShell
        title={heading.title}
        description={heading.description}
        ariaLabel="Repository materialization progress"
        actionPending={false}
        onClose={handleClose}
        footer={
          <>
            {hasFailed && mayMaintain && (
              <button
                type="button"
                onClick={handleRetry}
                disabled={retrying}
                className={`${OPS_PRIMARY_BTN} disabled:opacity-40`}
              >
                {retrying ? 'Retrying…' : 'Retry from failed step'}
              </button>
            )}
            <button
              type="button"
              onClick={handleClose}
              className="px-3.5 py-1.5 rounded-lg bg-white border border-slate-300 text-slate-700 text-sm font-medium hover:bg-slate-50 transition-colors"
            >
              {allDone ? (doneLabel ?? 'Done') : inFlight ? 'Run in background' : 'Close'}
            </button>
          </>
        }
      >
        <div className="flex items-center justify-between gap-3">
          <span className={`${PILL_SHAPE} ${OPS_BADGE[badgeKey]}`}>
            <span aria-hidden="true">●</span>
            {badgeKey}
          </span>
          {inFlight && (
            <span className="inline-flex items-center gap-1.5 text-[11px] text-slate-400">
              <svg className="w-3 h-3 animate-spin" viewBox="0 0 24 24" fill="none">
                <circle cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="3" strokeDasharray="40 60" />
              </svg>
              Polling every 3s…
            </span>
          )}
        </div>

        {allDone && (
          <div className="rounded-lg bg-emerald-50 border border-emerald-200/70 px-3 py-2 text-sm text-emerald-800">
            {doneLabel ? `Ready. ${doneLabel} to build and test it.` : 'Repository materialized. Close to return to the project.'}
          </div>
        )}
        {hasFailed && (
          <div className="rounded-lg bg-rose-50 border border-rose-200/70 px-3 py-2 text-sm text-rose-700">
            Materialization stopped at a failed step. Fix the cause, then retry from where it failed.
          </div>
        )}

        <MaterializeTimeline steps={steps} />

        {repo?.repo_url && (
          <a
            href={repo.repo_url}
            target="_blank"
            rel="noreferrer"
            className="inline-flex text-sm text-emerald-700 hover:underline"
          >
            Open repository ↗
          </a>
        )}

        {actionError && (
          <p className="text-sm text-red-600" role="alert">
            {actionError}
          </p>
        )}
      </ModalShell>
    );
  }

  return (
    <ModalShell
      title={isMcp ? 'New MCP server' : 'New agent'}
      description={`Creates the ${isMcp ? 'MCP server' : 'agent'} and its source repository from a GitHub template in this project's org.`}
      ariaLabel={isMcp ? 'New MCP server' : 'New agent'}
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
            disabled={!canSubmit || noTemplates}
            className={`${OPS_PRIMARY_BTN} disabled:opacity-40`}
            data-testid="add-repo-submit"
          >
            {actionPending ? 'Provisioning…' : isMcp ? 'Create MCP server' : 'Create agent'}
          </button>
        </>
      }
    >
      {loadError && (
        <p className="text-sm text-red-600" role="alert">
          {loadError}
        </p>
      )}

      {emptyReason && <p className="text-sm text-slate-500">{emptyReason}</p>}

      <div>
        <span id="add-repo-kind-label" className={FIELD_LABEL}>
          Builds
        </span>
        <div
          role="radiogroup"
          aria-labelledby="add-repo-kind-label"
          className="inline-flex p-0.5 rounded-lg bg-slate-100 border border-slate-200"
        >
          {ARTIFACT_KIND_OPTIONS.map((o) => {
            const active = kind === o.kind;
            return (
              <button
                key={o.kind}
                type="button"
                role="radio"
                aria-checked={active}
                onClick={() => changeKind(o.kind)}
                disabled={actionPending}
                className={`px-3.5 py-1 text-sm font-medium rounded-md transition-colors focus:outline-none focus-visible:ring-2 focus-visible:ring-emerald-500/40 disabled:opacity-40 ${
                  active ? 'bg-white text-emerald-800 shadow-sm' : 'text-slate-500 hover:text-slate-700'
                }`}
              >
                {o.label}
              </button>
            );
          })}
        </div>
      </div>

      <div>
        <label htmlFor="add-repo-name" className={FIELD_LABEL}>
          Repository name
        </label>
        <input
          id="add-repo-name"
          ref={nameRef}
          type="text"
          value={name}
          onChange={(e) => setName(e.target.value)}
          disabled={actionPending}
          placeholder={isMcp ? 'claims-api-mcp' : 'my-agent-repo'}
          className={`${FIELD_INPUT} disabled:opacity-40`}
          autoComplete="off"
        />
      </div>

      <div>
        <label htmlFor="add-repo-template" className={FIELD_LABEL}>
          Template{' '}
          <span className="text-slate-400 font-normal normal-case tracking-normal">
            ({tenantPlatform} templates)
          </span>
        </label>
        <select
          id="add-repo-template"
          value={templateName}
          onChange={(e) => setTemplateName(e.target.value)}
          disabled={actionPending || loadingTemplates || noTemplates}
          className={FIELD_SELECT}
        >
          <option value="">
            {loadingTemplates
              ? 'Loading templates…'
              : noTemplates
                ? 'No templates available'
                : 'Select a template'}
          </option>
          {/* An option element holds text only, so the badges ride in templateOptionLabel's words; the chips below are the selected template's. */}
          {offered.map((t) => (
            <option key={t.name} value={t.name}>
              {templateOptionLabel(t)}
            </option>
          ))}
        </select>

        {chosen && (
          <div className="mt-2 flex flex-wrap items-center gap-1.5">
            <span className={`text-[10px] font-medium px-1.5 py-0.5 rounded ${PLATFORM_CHIP}`}>
              {chosen.platform}
            </span>
            <span className={`text-[10px] font-medium px-1.5 py-0.5 rounded ${BUILD_KIND_CHIP}`}>
              {buildKindLabel(chosen.build_kind)}
            </span>
            <span className={`text-[11px] font-medium px-2 py-0.5 rounded-full ${INFRA_CHIP}`}>
              {chosen.infra_template_id}
            </span>
            {chosen.preview && (
              <span className={`text-[11px] font-medium px-2 py-0.5 rounded-full ${PREVIEW_CHIP}`}>
                Preview
              </span>
            )}
          </div>
        )}
      </div>

      {isMcp && (
        <div className="space-y-4">
          <div>
            <label htmlFor="add-repo-mcp-name" className={FIELD_LABEL}>
              MCP server name
            </label>
            <input
              id="add-repo-mcp-name"
              type="text"
              value={mcpName}
              onChange={(e) => setMcpName(e.target.value)}
              disabled={actionPending}
              placeholder={name.trim() || 'claims-api'}
              aria-invalid={mcpHint.tone === 'error'}
              aria-describedby="add-repo-mcp-name-hint"
              className={`${FIELD_INPUT} disabled:opacity-40`}
              autoComplete="off"
            />
            <p
              id="add-repo-mcp-name-hint"
              className={`text-[11px] mt-1 ${mcpHint.tone === 'error' ? 'text-rose-600' : 'text-slate-400'}`}
            >
              {mcpHint.text}
            </p>
          </div>
          <div>
            <div className="flex items-baseline justify-between">
              <label htmlFor="add-repo-mcp-description" className={FIELD_LABEL}>
                Description <span className="text-slate-400 font-normal">(optional)</span>
              </label>
              <span
                className={`text-[11px] tabular-nums ${mcpDescriptionProblem ? 'text-rose-600 font-medium' : 'text-slate-400'}`}
                aria-live="polite"
              >
                {purpose.length}/{MCP_DESCRIPTION_MAX}
              </span>
            </div>
            <input
              id="add-repo-mcp-description"
              type="text"
              value={purpose}
              onChange={(e) => setPurpose(e.target.value)}
              maxLength={MCP_DESCRIPTION_MAX}
              disabled={actionPending}
              placeholder="What these tools do — shown in the MCP Servers governance view."
              aria-invalid={mcpDescriptionProblem !== null}
              aria-describedby={mcpDescriptionProblem ? 'add-repo-mcp-description-error' : undefined}
              className={`${FIELD_INPUT} disabled:opacity-40`}
              autoComplete="off"
            />
            {mcpDescriptionProblem && (
              <p id="add-repo-mcp-description-error" className="text-[11px] text-rose-600 mt-1">
                {mcpDescriptionProblem}
              </p>
            )}
          </div>
        </div>
      )}

      {!isMcp && (
      <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
        <div>
          <label htmlFor="add-repo-agent-name" className={FIELD_LABEL}>
            Agent name
          </label>
          <input
            id="add-repo-agent-name"
            type="text"
            value={agentName}
            onChange={(e) => setAgentName(e.target.value)}
            disabled={actionPending}
            placeholder="my_agent"
            className={`${FIELD_INPUT} disabled:opacity-40`}
            autoComplete="off"
          />
          {agentName.length > 0 && !agentNameValid && (
            <p className="text-[11px] text-rose-600 mt-1">
              Must start with a letter, then letters, digits, or underscores (max 32).
            </p>
          )}
        </div>
        <div>
          <label htmlFor="add-repo-framework" className={FIELD_LABEL}>
            Framework
          </label>
          <input
            id="add-repo-framework"
            type="text"
            value={framework || '—'}
            readOnly
            disabled
            className={`${FIELD_INPUT} bg-slate-50 text-slate-500`}
          />
        </div>
      </div>
      )}

      {!isMcp && (
      <div>
        <label htmlFor="add-repo-model" className={FIELD_LABEL}>
          Model ID
        </label>
        <input
          id="add-repo-model"
          type="text"
          value={modelId}
          onChange={(e) => setModelId(e.target.value)}
          disabled={actionPending}
          placeholder={STUDIO_AGENT_MODEL_ID}
          className={`${FIELD_INPUT} disabled:opacity-40`}
          autoComplete="off"
        />
      </div>
      )}

      <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
        <div>
          <label htmlFor="add-repo-business-unit" className={FIELD_LABEL}>
            Business unit <span className="text-slate-400 font-normal">(optional)</span>
          </label>
          <input
            id="add-repo-business-unit"
            type="text"
            value={businessUnit}
            onChange={(e) => setBusinessUnit(e.target.value)}
            disabled={actionPending}
            placeholder="Retail Banking"
            className={`${FIELD_INPUT} disabled:opacity-40`}
            autoComplete="off"
          />
        </div>
        <div>
          <label htmlFor="add-repo-region" className={FIELD_LABEL}>
            Region <span className="text-slate-400 font-normal">(optional)</span>
          </label>
          <input
            id="add-repo-region"
            type="text"
            value={region}
            onChange={(e) => setRegion(e.target.value)}
            disabled={actionPending}
            placeholder="us-east-1"
            className={`${FIELD_INPUT} disabled:opacity-40`}
            autoComplete="off"
          />
        </div>
      </div>

      <div>
        <label htmlFor="add-repo-data-classification" className={FIELD_LABEL}>
          Data classification <span className="text-slate-400 font-normal">(optional)</span>
        </label>
        <select
          id="add-repo-data-classification"
          value={dataClassification}
          onChange={(e) => setDataClassification(e.target.value as DataClassification | '')}
          disabled={actionPending}
          className={FIELD_SELECT}
        >
          <option value="">Not set</option>
          {DATA_CLASSIFICATIONS.map((c) => (
            <option key={c} value={c}>
              {c}
            </option>
          ))}
        </select>
      </div>

      <div>
        <div className="flex items-center justify-between mb-1">
          <span className={`${FIELD_LABEL} mb-0`}>
            CI variable overrides <span className="text-slate-400 font-normal">(optional)</span>
          </span>
          <button
            type="button"
            onClick={addOverride}
            disabled={actionPending}
            className="text-[11px] font-semibold text-emerald-700 hover:text-emerald-800 disabled:opacity-40 transition-colors"
          >
            + Add variable
          </button>
        </div>
        {overrides.length === 0 ? (
          <p className="text-[11px] text-slate-400">
            Per-repo scaffold variables passed to the template materialization.
          </p>
        ) : (
          <div className="space-y-2">
            {overrides.map((row, i) => (
              <div key={i} className="flex items-center gap-2">
                <input
                  type="text"
                  value={row.key}
                  onChange={(e) => updateOverride(i, { key: e.target.value })}
                  disabled={actionPending}
                  placeholder="KEY"
                  aria-label={`Override ${i + 1} key`}
                  className={`${FIELD_INPUT} font-mono text-xs disabled:opacity-40`}
                  autoComplete="off"
                />
                <input
                  type="text"
                  value={row.value}
                  onChange={(e) => updateOverride(i, { value: e.target.value })}
                  disabled={actionPending}
                  placeholder="value"
                  aria-label={`Override ${i + 1} value`}
                  className={`${FIELD_INPUT} font-mono text-xs disabled:opacity-40`}
                  autoComplete="off"
                />
                <button
                  type="button"
                  onClick={() => removeOverride(i)}
                  disabled={actionPending}
                  aria-label={`Remove override ${i + 1}`}
                  className="shrink-0 w-8 h-8 grid place-items-center rounded-lg text-slate-400 hover:text-rose-600 hover:bg-rose-50 disabled:opacity-40 transition-colors"
                >
                  ✕
                </button>
              </div>
            ))}
          </div>
        )}
      </div>

      {!isMcp && (
      <div>
        <label htmlFor="add-repo-purpose" className={FIELD_LABEL}>
          Purpose <span className="text-slate-400 font-normal">(optional)</span>
        </label>
        <textarea
          id="add-repo-purpose"
          value={purpose}
          onChange={(e) => setPurpose(e.target.value)}
          disabled={actionPending}
          rows={2}
          placeholder="What this agent does — shown in the Agents governance view."
          className={`${FIELD_INPUT} disabled:opacity-40`}
        />
      </div>
      )}

      {actionError && (
        <p className="text-sm text-red-600" role="alert">
          {actionError}
        </p>
      )}
    </ModalShell>
  );
}
