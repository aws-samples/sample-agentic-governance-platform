// AddRepoModal — the "New repository from template" modal, EXTRACTED VERBATIM out of
// `ProjectRepositoriesTab.tsx` (E28/T13).
//
// ---------------------------------------------------------------------------
// WHY IT MOVED
//
// It was 620 lines at the bottom of a 1542-line file, and it owns a SECOND POLLER — its own 3s
// `getRepoStatus` interval for the live materialize timeline, entirely separate from the
// promoting-row poller the table above it runs. Two independent intervals, two `pollRef`s and two
// `mountedRef`s in one module made the file unreadable on the question that matters most about it
// ("what polls, when, and what tears it down?"), and T13 unloads that table into the shared
// `RepoRow`, so leaving the modal behind would have left the file's bulk exactly where it was.
//
// NOTHING ABOUT THE BEHAVIOUR CHANGED IN THE MOVE. The component body, its state, both effects,
// every callback and all the markup are byte-identical to the block that was deleted; the only
// additions are this comment, the imports it needs and the `export default`. The form-field and
// pill class-string constants came WITH it — they were declared in the old file and, apart from
// the pill, are used only here, matching the house idiom where each surface re-declares the
// label/input strings it needs (`AddUserModal` inlines the same label classes; `PrincipalPicker`
// re-declares CARD).
//
// The step-timeline pieces (`isMaterializeTerminal`, `nextBadgeFromSteps`, `stepStatusText`,
// `StepNode`, `MaterializeTimeline`) deliberately did NOT move with it. They stayed in
// `ProjectRepositoriesTab.tsx` because `ProjectDetail.tsx` re-exports the first two for
// `ProjectDetail.stepTimeline.test.ts`'s pinned import path, and `RepositoryDetail.tsx` imports
// two of them from that module directly — moving them would break a pinned test path and three
// import sites for no gain. So this file imports them, which is why the timeline renders here
// through the same code the project page and the detail page use.
//
// THE EXPLICIT `.tsx` on that import is load-bearing on a case-insensitive filesystem: an
// extensionless specifier probes `<Name>.ts` first, and a sibling companion module differing only
// in casing wins — which is a silent bind to a module with no default export (TS1149). This epic
// hit that trap four times.
//
// House style: emerald-on-glass Ops tokens (opsUi.ts), Tailwind v4 utility strings, 2-space
// indent — unchanged from the module it left.

import { useCallback, useEffect, useRef, useState } from 'react';

import {
  githubTemplatesApi,
  projectsApi,
  type DataClassification,
  type Repository,
  type StepState,
  type TemplateView,
} from '../../api/client';
import { ModalShell } from './ConnectionsAdmin';
// The timeline and its two pure predicates stay in their home module — see the note above on why
// they did not travel with the modal.
import {
  MaterializeTimeline,
  isMaterializeTerminal,
  nextBadgeFromSteps,
} from './ProjectRepositoriesTab.tsx';
import { maintainerActionMessage } from './projectRoles';
import { OPS_BADGE, OPS_PRIMARY_BTN } from './opsUi';

// Form-field tokens — moved WITH the modal, which is their only consumer. Declared locally rather
// than shared, matching the house idiom where each surface re-declares the label/input class
// strings it needs (AddUserModal inlines the same label classes; PrincipalPicker re-declares CARD).
const FIELD_LABEL = 'block text-[11px] uppercase tracking-wide text-slate-400 font-medium mb-1';
const FIELD_INPUT =
  'w-full px-3 py-2 text-sm rounded-lg border border-slate-300 focus:outline-none focus:ring-2 focus:ring-emerald-500/40';
const FIELD_SELECT =
  'w-full px-3 py-2 text-sm rounded-lg border border-slate-300 bg-white focus:outline-none focus:ring-2 focus:ring-emerald-500/40 disabled:opacity-40';
// The materialize-modal header pill KEEPS `capitalize`: it is passed a lowercase OPS_BADGE key
// (`nextBadgeFromSteps`'s return, which doubles as the label) and relies on the transform. The
// status tables' sentence-case labels must never go through this — `capitalize` upper-cases every
// word, so "Promoting to prod…" would render as "Promoting To Prod…".
const PILL_SHAPE =
  'inline-flex items-center gap-1.5 text-[11px] font-semibold px-2 py-0.5 rounded-full capitalize';

// agent_name mirror of the backend regex (models/project.py AGENT_NAME_RE): leading letter,
// then up to 31 more [A-Za-z0-9_] — 32 total. E28A/T1b narrowed this from 48: agent_name is the
// STEM the runtime Terraform module derives two stage-scoped, account-global names from
// (`{name}_{stage}`, AWS cap 48; `{name}-{stage}-agentcore-exec`, IAM cap 64), and at 48 both
// overflowed. Keep it byte-identical to the backend — a looser mirror here calls a name valid
// that the API then rejects with a 502; a tighter one refuses names the platform accepts.
const AGENT_NAME_RE = /^[a-zA-Z][a-zA-Z0-9_]{0,31}$/;

// Class-A data-classification options — the CAPITALIZED wire values the backend
// DataClassification enum accepts (models/agent.py). Order runs least→most sensitive.
const DATA_CLASSIFICATIONS: DataClassification[] = ['Public', 'Internal', 'Confidential', 'Restricted'];

// --- Add-repository-from-template modal -------------------------------------
// Loads the org's GitHub template catalog (githubTemplatesApi.list(connectionId))
// as the template select options, then collects a repo name + agent.config +
// Class-A governance attributes (business_unit, region, data_classification) + an
// optional Class-B CI-var override editor (repo_overrides). On submit
// projectsApi.addRepo materializes the repo+agent; the 502 materialize errors
// surface inline. The agent.config field idiom (agent_name + AGENT_NAME_RE,
// model_id default, framework fixed 'strands' read-only) is copied from the
// T19-deleted CreateProjectFromTemplate.
export default function AddRepoModal({
  projectId,
  connectionId,
  mayMaintain,
  onClose,
  onCreated,
}: {
  projectId: string;
  connectionId: string;
  /** Whether the caller may run the MAINTAINER-gated retry (see `mayMaintainProject`). */
  mayMaintain: boolean;
  onClose: () => void;
  onCreated: () => void;
}) {
  const [name, setName] = useState('');
  const [templateName, setTemplateName] = useState('');
  const [agentName, setAgentName] = useState('');
  const [modelId, setModelId] = useState('us.anthropic.claude-sonnet-4-6');
  const [purpose, setPurpose] = useState('');
  // framework is fixed to 'strands' (the only supported scaffold) — shown read-only.

  // Class-A governance attributes for the pre-registered Agent record.
  const [businessUnit, setBusinessUnit] = useState('');
  const [region, setRegion] = useState('');
  const [dataClassification, setDataClassification] = useState<DataClassification | ''>('');

  // Class-B CI-var overrides — a small {key,value} list serialized to a
  // Record<string,string> on submit. Blank keys are dropped; last wins on dupes.
  const [overrides, setOverrides] = useState<{ key: string; value: string }[]>([]);

  const [templates, setTemplates] = useState<TemplateView[]>([]);
  const [loadingTemplates, setLoadingTemplates] = useState(true);
  const [loadError, setLoadError] = useState<string | null>(null);

  const [actionPending, setActionPending] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);

  // Live materialize timeline (E25C): once addRepo returns 202 we keep the modal open,
  // swap the body form → timeline, and poll getRepoStatus every 3s until terminal.
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

  // Poll the live timeline while a repo is materializing. Mirrors the
  // Polling precedent: a ref-tracked 3s interval that GETs the
  // status, updates steps, and clears itself on the terminal state or on unmount/close.
  const repoId = repo?.id ?? null;
  const terminal = steps.length > 0 && isMaterializeTerminal(steps);
  useEffect(() => {
    if (!repoId || terminal) {
      stopPolling();
      return;
    }
    if (pollRef.current) return;
    pollRef.current = window.setInterval(async () => {
      try {
        const fresh = await projectsApi.getRepoStatus(projectId, repoId);
        if (!mountedRef.current) return;
        setRepo(fresh);
        setSteps(fresh.steps);
        if (isMaterializeTerminal(fresh.steps)) stopPolling();
      } catch {
        /* transient — keep polling */
      }
    }, 3000);
    return stopPolling;
  }, [repoId, terminal, projectId, stopPolling]);

  // Belt-and-suspenders: clear the interval on unmount.
  useEffect(() => stopPolling, [stopPolling]);

  useEffect(() => {
    nameRef.current?.focus();
  }, []);

  // Load the org's GitHub template catalog → the template select options.
  useEffect(() => {
    let cancelled = false;
    setLoadingTemplates(true);
    githubTemplatesApi
      .list(connectionId)
      .then((data) => {
        if (cancelled) return;
        const names = data.map((t) => t.name);
        setTemplates(data);
        setTemplateName((prev) => (names.includes(prev) ? prev : names[0] ?? ''));
        setLoadError(null);
      })
      .catch((err: unknown) => {
        if (cancelled) return;
        setLoadError(err instanceof Error ? err.message : 'Failed to load templates.');
      })
      .finally(() => {
        if (!cancelled) setLoadingTemplates(false);
      });
    return () => {
      cancelled = true;
    };
  }, [connectionId]);

  const agentNameValid = AGENT_NAME_RE.test(agentName);
  const canSubmit =
    name.trim().length > 0 &&
    templateName.length > 0 &&
    agentNameValid &&
    !actionPending;

  // Serialize the {key,value} override rows → Record<string,string>, dropping
  // blank keys (trimmed); returns undefined when empty so the field is omitted.
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
      // 202: the record comes back PENDING with a full pending timeline. Keep the modal
      // open and switch the body to the live timeline instead of closing + refetching.
      const created = await projectsApi.addRepo(projectId, {
        name: name.trim(),
        template_name: templateName,
        agent_config: {
          agent_name: agentName.trim(),
          framework: 'strands',
          model_id: modelId.trim(),
        },
        business_unit: businessUnit.trim() || undefined,
        region: region.trim() || undefined,
        data_classification: dataClassification || undefined,
        repo_overrides: serializeOverrides(),
        purpose: purpose.trim() || undefined,
      });
      if (mountedRef.current) {
        setRepo(created);
        setSteps(created.steps);
      }
    } catch (err: unknown) {
      if (mountedRef.current) {
        // Mapped, not raw (E27/T11 FIX 3): the route is MAINTAINER-gated, so a caller whose
        // standing changed since this page loaded would otherwise read the lowercase internal
        // fragment `insufficient project role` after filling in the whole form.
        setActionError(
          maintainerActionMessage(
            err instanceof Error ? err.message : '',
            'Failed to create the repository.'
          )
        );
      }
    } finally {
      if (mountedRef.current) setActionPending(false);
    }
  }, [
    canSubmit,
    projectId,
    name,
    templateName,
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

  // Retry from the first failed step. 202 → the returned record has its failed steps
  // reset to pending; poll resumes. 409 "Nothing to retry" → treat as already-complete:
  // refetch the current status once and let the terminal (done) view render.
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
      const message = err instanceof Error ? err.message : 'Failed to retry.';
      if (/nothing to retry/i.test(message)) {
        try {
          const fresh = await projectsApi.getRepoStatus(projectId, repo.id);
          if (mountedRef.current) {
            setRepo(fresh);
            setSteps(fresh.steps);
          }
        } catch {
          /* leave the current (failed) view in place */
        }
      } else if (mountedRef.current) {
        // Same mapping as the create path — the retry route carries the same MAINTAINER
        // gate and the same 403 literal. The "nothing to retry" 409 is matched on the RAW
        // message above, before this, because it is control flow rather than copy.
        setActionError(maintainerActionMessage(message, 'Failed to retry.'));
      }
    } finally {
      if (mountedRef.current) setRetrying(false);
    }
  }, [repo, retrying, projectId]);

  // Closing the modal always stops polling. Once a repo exists (materialize started),
  // closing folds back into the table via onCreated() so the new row + its live
  // cicd_status show up; before that it's a plain form cancel.
  const handleClose = useCallback(() => {
    stopPolling();
    if (repo) {
      onCreated();
    } else {
      onClose();
    }
  }, [repo, stopPolling, onCreated, onClose]);

  const noTemplates = !loadingTemplates && !loadError && templates.length === 0;

  // Timeline-view derived flags.
  const materializing = repo !== null;
  const hasFailed = steps.some((s) => s.status === 'failed');
  const allDone = steps.length > 0 && steps.every((s) => s.status === 'done');
  const badgeKey = steps.length > 0 ? nextBadgeFromSteps(steps) : 'provisioning';

  // --- Live timeline view (post-202) --------------------------------------
  // Modal stays open; the form body is replaced by the polled step timeline. The
  // backdrop/Escape close is disabled while a step is still running (actionPending on
  // ModalShell) so the operator doesn't lose the view mid-flight.
  //
  // THE COUNT IN THE DESCRIPTION IS FIVE (E28B/T3, D-B2). It said eight, and five of those eight
  // steps did not get renamed — they stopped existing: a second branch cut, two GitHub
  // Environments and their variables are gone, because AGP now creates an empty repo and pushes the
  // whole template in ONE commit rather than making six sequential writes into a repository GitHub
  // was concurrently writing to. The number is stated at all because the operator is watching a
  // timeline and a promised total is something they can count the rows against — which is also why
  // a stale one is worse than none: it invites them to wait for three steps that will never appear.
  //
  // The rows themselves are NOT affected by this number: the timeline renders `steps[].label` from
  // the record, so a historical repo carrying the OLD eight keys still renders its own eight stored
  // labels. That is deliberate and there is no key→label map anywhere in the frontend to make an
  // unknown key crash or blank a row.
  if (materializing) {
    const inFlight = !terminal;
    return (
      <ModalShell
        title={repo?.name ? `Materializing ${repo.name}` : 'Materializing repository'}
        description="Provisioning the repository + agent — this runs five steps in the background."
        ariaLabel="Repository materialization progress"
        actionPending={inFlight}
        onClose={handleClose}
        footer={
          <>
            {/* MAINTAINER-gated server-side, so conditionally rendered rather than shown
                and refused (E27/T11 FIX 3). `disabled` stays reserved for in-flight. */}
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
              {allDone ? 'Done' : inFlight ? 'Run in background' : 'Close'}
            </button>
          </>
        }
      >
        {/* Status header pill — color AND label both derive from badgeKey (one source),
            so a provisioning repo reads amber "provisioning" here exactly as it does in
            the repos-table row; ready is emerald, failed is rose. */}
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
            Repository materialized. Close to return to the project.
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
      title="New repository from template"
      description="Materialize a repository + agent from a GitHub template in this project's org."
      ariaLabel="New repository from template"
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
          >
            {actionPending ? 'Provisioning…' : 'Create repository'}
          </button>
        </>
      }
    >
      {loadError && (
        <p className="text-sm text-red-600" role="alert">
          {loadError}
        </p>
      )}

      {noTemplates && (
        <p className="text-sm text-slate-500">
          No templates in this org — roll out the base templates under Admin › Org Connections ›
          Manage templates.
        </p>
      )}

      {/* Repository name. */}
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
          placeholder="my-agent-repo"
          className={`${FIELD_INPUT} disabled:opacity-40`}
          autoComplete="off"
        />
      </div>

      {/* Template (org GitHub catalog). */}
      <div>
        <label htmlFor="add-repo-template" className={FIELD_LABEL}>
          Template
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
          {templates.map((t) => (
            <option key={t.name} value={t.name}>
              {t.name}
            </option>
          ))}
        </select>
      </div>

      {/* agent.config fields. */}
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
            value="strands"
            readOnly
            disabled
            className={`${FIELD_INPUT} bg-slate-50 text-slate-500`}
          />
        </div>
      </div>

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
          placeholder="us.anthropic.claude-sonnet-4-6"
          className={`${FIELD_INPUT} disabled:opacity-40`}
          autoComplete="off"
        />
      </div>

      {/* Class-A governance attributes for the pre-registered Agent (optional). */}
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

      {/* Class-B CI-var overrides — optional per-repo scaffold variables. */}
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

      {/* Governance purpose for the pre-registered Agent record (optional). */}
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

      {actionError && (
        <p className="text-sm text-red-600" role="alert">
          {actionError}
        </p>
      )}
    </ModalShell>
  );
}
