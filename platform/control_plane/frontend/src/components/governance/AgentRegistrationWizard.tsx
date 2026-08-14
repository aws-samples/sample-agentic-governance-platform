import { useCallback, useEffect, useRef, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { agentsApi, tenantsAdminApi } from '../../api/client';
import type {
  Agent,
  AuthType,
  DataClassification,
  DiscoveredAgent,
  Origin,
  TenantPlatform,
} from '../../api/client';
import { useUser } from '../../contexts/UserContext';
import { resolveTenantName, tenantSelectOptions } from './tenantUi';
import { useTenantDirectory } from './useTenantDirectory';
import {
  DISCOVERY_EMPTY_COPY,
  EMPTY_DRAFT,
  PLATFORM_READONLY_HINT,
  buildPayload,
  canSubmit,
  defaultDiscoveryStage,
  discoveredRowState,
  discoveryStageNames,
  discoveryState,
  manualHandleError,
  manualHandleField,
  resolvePlatform,
  selectedRow,
  shouldAcceptDiscovery,
  tenantPlatformLabel,
  type DiscoveryScope,
  type Draft,
  type TenantLike,
} from './agentRegistrationWizardModel';

// ---------------------------------------------------------------------------
// Select option tables (mirror the enum labels used in AgentsList / AgentDetail)
//
// E29/T8: the PLATFORM table is GONE. A platform is no longer something an operator types —
// it is inferred from the (platform-typed, immutable) tenant, because the platform decides
// which create body a registration becomes and an operator who picks the wrong one registers
// an agent the invoke path can never reach. See `agentRegistrationWizardModel`, which owns
// every decision on this page; this component binds and renders.
// ---------------------------------------------------------------------------

const DATA_CLASS_OPTIONS: { value: DataClassification; label: string }[] = [
  { value: 'Public', label: 'Public' },
  { value: 'Internal', label: 'Internal' },
  { value: 'Confidential', label: 'Confidential' },
  { value: 'Restricted', label: 'Restricted' },
];

const REGION_OPTIONS = ['DE', 'IT', 'FR', 'SE', 'EU'];

const ORIGIN_OPTIONS: { value: Origin; label: string }[] = [
  { value: 'Registered', label: 'Registered (running elsewhere, brought under governance)' },
  { value: 'Deployed', label: 'Deployed (provisioned by the platform)' },
];

const AUTH_TYPE_OPTIONS: { value: AuthType; label: string }[] = [
  { value: 'none', label: 'None' },
  { value: 'entra', label: 'Microsoft Entra' },
  { value: 'api_key', label: 'API key' },
];

const AUTH_TYPE_LABEL: Record<AuthType, string> = {
  none: 'None',
  entra: 'Microsoft Entra',
  api_key: 'API key',
};

const CARD = 'bg-white/70 backdrop-blur rounded-xl border border-slate-200/60 shadow-sm';

const STEPS = [
  { n: 1, label: 'Identity' },
  { n: 2, label: 'Sponsor' },
  { n: 3, label: 'Classification' },
  { n: 4, label: 'Platform' },
  { n: 5, label: 'Confirm' },
];

// Shared field styling.
const INPUT =
  'mt-1 w-full px-3 py-2 text-sm rounded-lg border border-slate-300 focus:outline-none focus:ring-2 focus:ring-blue-500/40';
const LABEL = 'text-xs uppercase tracking-wide text-slate-500 font-medium';

// `Draft`, `EMPTY_DRAFT` and `buildPayload` now live in `agentRegistrationWizardModel` — the
// draft shape and the payload mapping are DECISIONS (which platform, which handle field, what a
// selected discovery row becomes), and the model is where decisions are testable.

export default function AgentRegistrationWizard() {
  const navigate = useNavigate();

  // Tenant select source (E24): the caller's memberships, or the full admin
  // directory for role_level >= 2 (degrades to memberships while it loads).
  const { user } = useUser();
  const isAdmin = (user?.role_level ?? 0) >= 2;
  const tenantDirectory = useTenantDirectory(isAdmin);
  const tenantOptions = tenantSelectOptions(user?.tenants ?? [], tenantDirectory, isAdmin);

  const [step, setStep] = useState(1);
  const [draft, setDraft] = useState<Draft>(EMPTY_DRAFT);

  // Submission / result state.
  const [creating, setCreating] = useState(false);
  const [created, setCreated] = useState<Agent | null>(null);
  const [error, setError] = useState<string | null>(null);
  // A name-collision (409) error is surfaced inline on step 1.
  const [nameError, setNameError] = useState<string | null>(null);

  // Post-create "submit for approval" state.
  const [submitting, setSubmitting] = useState(false);
  const [submitted, setSubmitted] = useState(false);
  const [submitError, setSubmitError] = useState<string | null>(null);

  const set = <K extends keyof Draft>(key: K, value: Draft[K]) =>
    setDraft((d) => ({ ...d, [key]: value }));

  // -- the tenant, and everything inferred from it (E29/T8) -----------------
  //
  // The selected tenant record, from whichever directory knows the id. `findTenantAccount`'s
  // sibling `resolveTenantName` is already used below for the same lookup on the name.
  const tenant: TenantLike | null =
    tenantOptions.find((t) => t.id === draft.tenant_id) ?? null;
  const stageNames = discoveryStageNames(tenant);
  // Discovery is ADMIN-gated while agent creation is OPERATOR-gated, so a real operator may
  // legitimately be unable to list. That is a STATE, not an error (see the model's union).
  const canDiscover = isAdmin;

  const [stage, setStage] = useState<string | null>(null);
  const [rows, setRows] = useState<DiscoveredAgent[] | null>(null);
  // C-3's envelope platform, held as state beside the rows because it is the AUTHORITATIVE
  // answer: the route read it off the tenant RECORD, whereas `tenant` here comes from a directory
  // that can be stale or (for a non-admin's `/users/me` memberships) carry no platform at all.
  const [discoveredPlatform, setDiscoveredPlatform] = useState<TenantPlatform | null>(null);
  const [discoveryLoading, setDiscoveryLoading] = useState(false);
  const [discoveryError, setDiscoveryError] = useState<string | null>(null);

  // The platform is READ, never picked — from the discovery response when there is one, else
  // inferred from the tenant (manual-entry-only flows). See `resolvePlatform` for the CRITICAL
  // this ordering closes: an admin whose tenant directory has not loaded still discovers real
  // Databricks rows, and inferring `aws` from the absent key posted the app URL into `agent_arn`.
  const tenantPlatform = resolvePlatform(tenant, discoveredPlatform);

  // The stage follows the tenant: a stage name from the previous tenant is meaningless on this
  // one (and would 400), so it is re-derived from the tenant's OWN stage map — never a literal
  // (contract C5). `null` when the tenant carries none, which suppresses the request entirely.
  const stageKey = stageNames.join('|');
  useEffect(() => {
    setStage(defaultDiscoveryStage(tenant));
    setRows(null);
    // Cleared with the rows: a platform proven for the PREVIOUS tenant says nothing about this
    // one, and keeping it would let a stale `databricks` build this tenant's payload.
    setDiscoveredPlatform(null);
    setDiscoveryError(null);
    // The selection is cleared too, and that is load-bearing rather than tidiness: a handle
    // picked on the previous tenant is not a row on this one, and leaving it would make the
    // Confirm step show a handle the new tenant's list never offered.
    setDraft((d) => ({ ...d, selected_handle: '' }));
    // `stageKey` (not `tenant`) so a re-rendered but unchanged tenant does not reset the
    // operator's stage pick. Keyed on the tenant id as well, since two tenants can share a
    // stage-name set.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [draft.tenant_id, stageKey]);

  // What the form is showing RIGHT NOW, readable from inside a settled promise. A ref rather than
  // the closure's own variables because that is the whole problem being solved: `runDiscovery`
  // closes over the tenant/stage it was created with, so after an await those values describe the
  // request, not the form. `shouldAcceptDiscovery` compares the two.
  const scopeRef = useRef<{ tenantId: string; stage: string | null }>({
    tenantId: draft.tenant_id,
    stage,
  });
  scopeRef.current = { tenantId: draft.tenant_id, stage };

  const runDiscovery = useCallback(async () => {
    if (!draft.tenant_id || !stage || !canDiscover) return;
    // Captured BEFORE the await — what this request is about (see `shouldAcceptDiscovery`).
    const snapshot: DiscoveryScope = { tenantId: draft.tenant_id, stage };
    setDiscoveryLoading(true);
    setDiscoveryError(null);
    try {
      const res = await tenantsAdminApi.discoveredAgents(snapshot.tenantId, snapshot.stage);
      // A response for a tenant/stage the operator has already left is DISCARDED, not rendered:
      // out-of-order settling would otherwise paint tenant A's rows and platform onto tenant B's
      // form, and `buildPayload` would post B's id with A's handle.
      if (!shouldAcceptDiscovery(snapshot, scopeRef.current)) return;
      setRows(res.agents);
      // Taken from the SAME response as the rows, so the platform and the handles an operator
      // picks from can never come from different readings of the tenant.
      setDiscoveredPlatform(res.platform);
    } catch (err: unknown) {
      // A stale FAILURE is discarded on the same rule. Showing tenant A's 502 on tenant B's form
      // sends the operator to debug a platform that was never asked about.
      if (!shouldAcceptDiscovery(snapshot, scopeRef.current)) return;
      // The message is SHOWN rather than flattened to "discovery failed": the route's 400
      // (this tenant has no such stage) and 502 (the platform could not be reached, with a safe
      // code) are different problems with different fixes, and the detail is already redacted
      // server-side.
      setDiscoveryError(err instanceof Error ? err.message : 'Discovery failed.');
      setRows(null);
      // A failed discovery proves nothing about the platform, so the claim is dropped and the
      // tenant-derived inference takes over — never a stale value from an earlier stage.
      setDiscoveredPlatform(null);
    } finally {
      // The spinner is cleared ONLY by the request that still owns the scope. A stale request
      // clearing it would report "done" while the live request is still in flight.
      if (shouldAcceptDiscovery(snapshot, scopeRef.current)) setDiscoveryLoading(false);
    }
  }, [draft.tenant_id, stage, canDiscover]);

  // Fetch when a tenant + stage are in hand and the caller may discover. Re-running on the
  // stage is the point: switching environments must re-ask the platform, not re-filter a
  // cached list from another workspace.
  useEffect(() => {
    void runDiscovery();
  }, [runDiscovery]);

  const discovery = discoveryState({
    tenant,
    canDiscover,
    stage,
    loading: discoveryLoading,
    error: discoveryError,
    rows,
  });
  const chosenRow = selectedRow(draft, rows);
  const handleField = manualHandleField(tenantPlatform);
  // Gated on `handle_source`, matching `canSubmit`. Ungated, a stale value left in the manual box
  // before the operator switched back to the discovery list blocked Create while `canSubmit`
  // (which DOES check the source) still enabled the button — and the message had no field on
  // screen to attach to. The two gates must agree or the form dead-ends.
  const handleFieldError =
    draft.handle_source === 'manual'
      ? manualHandleError(tenantPlatform, draft[handleField.key])
      : null;

  const nameValid = draft.name.trim().length > 0;
  // tenant_id is REQUIRED by the backend since E24 — gate the Classification step.
  const tenantValid = draft.tenant_id.length > 0;
  const submitOk = canSubmit(draft, tenantPlatform);

  // The body that WOULD be posted. Derived rather than described, so the read-only fields and the
  // Confirm summary cannot drift from what `handleCreate` actually sends — the failure mode being
  // a summary that says "None" while the payload says "entra" (or the reverse), which teaches an
  // operator the wrong thing about a governance record.
  const preview = buildPayload(draft, tenantPlatform, chosenRow);

  const next = () => {
    if (step === 1 && !nameValid) {
      setNameError('A name is required.');
      return;
    }
    setNameError(null);
    setStep((s) => Math.min(5, s + 1));
  };
  const back = () => setStep((s) => Math.max(1, s - 1));

  const handleCreate = async () => {
    if (!nameValid) {
      setNameError('A name is required.');
      setStep(1);
      return;
    }
    if (!tenantValid) {
      setError('A tenant is required.');
      setStep(3);
      return;
    }
    // A manual handle that fails its platform's rule is refused HERE rather than posted: the
    // backend's invoke-time guard would reject it too, but only at the first invoke — long after
    // the operator left this page believing the registration was sound.
    if (handleFieldError) {
      setError(handleFieldError);
      setStep(4);
      return;
    }
    setCreating(true);
    setError(null);
    setNameError(null);
    try {
      const agent = await agentsApi.create(buildPayload(draft, tenantPlatform, chosenRow));
      setCreated(agent);
    } catch (err: unknown) {
      const msg = err instanceof Error ? err.message : 'Failed to create the agent.';
      // A duplicate name surfaces as a 409 → its detail message. Send the user back
      // to step 1 with the message on the name field so they can rename.
      if (/already|exist|taken|409|duplicate/i.test(msg)) {
        setNameError(msg);
        setStep(1);
      } else {
        setError(msg);
      }
    } finally {
      setCreating(false);
    }
  };

  const handleSubmitForApproval = async () => {
    if (!created) return;
    setSubmitting(true);
    setSubmitError(null);
    try {
      await agentsApi.submit(created.id);
      setSubmitted(true);
    } catch (err: unknown) {
      setSubmitError(err instanceof Error ? err.message : 'Submit for approval failed.');
    } finally {
      setSubmitting(false);
    }
  };

  // -- success state --------------------------------------------------------

  if (created) {
    return (
      <div className="min-h-[calc(100vh-4rem)] relative">
        <div className="relative max-w-2xl mx-auto px-6 py-10">
          <div className={`${CARD} p-8`}>
            <div className="flex items-center gap-3">
              <span className="inline-flex h-9 w-9 items-center justify-center rounded-full bg-emerald-50 text-emerald-600">
                <svg viewBox="0 0 20 20" fill="currentColor" className="h-5 w-5" aria-hidden="true">
                  <path fillRule="evenodd" d="M16.704 5.29a1 1 0 0 1 .006 1.414l-7.5 7.6a1 1 0 0 1-1.42.005l-3.5-3.5a1 1 0 1 1 1.414-1.414l2.79 2.79 6.795-6.889a1 1 0 0 1 1.415-.006Z" clipRule="evenodd" />
                </svg>
              </span>
              <div>
                <h1 className="text-2xl font-semibold text-slate-900">Agent registered</h1>
                <p className="text-sm text-slate-500 mt-0.5">
                  <span className="font-medium text-slate-700">{created.name}</span> was created as a draft
                  (proposed).
                </p>
              </div>
            </div>

            {submitted ? (
              <div className="mt-6 rounded-lg bg-amber-50 border border-amber-200/70 px-4 py-3 text-sm text-amber-800">
                Submitted for approval. An admin will review and approve or reject it.
              </div>
            ) : (
              <p className="mt-6 text-sm text-slate-600">
                It’s in <span className="font-medium">proposed</span> state. Submit it for approval now, or open
                the agent to review the details first.
              </p>
            )}

            {submitError && <p className="mt-3 text-sm text-red-600">{submitError}</p>}

            <div className="mt-6 flex flex-wrap items-center gap-3">
              {!submitted && (
                <button
                  onClick={handleSubmitForApproval}
                  disabled={submitting}
                  className="px-4 py-2 rounded-lg bg-blue-600 text-white text-sm font-medium hover:bg-blue-700 transition-colors disabled:opacity-50"
                >
                  {submitting ? 'Submitting…' : 'Submit for approval'}
                </button>
              )}
              <button
                onClick={() => navigate(`/agents/${created.id}`)}
                className="px-4 py-2 rounded-lg bg-white border border-slate-300 text-slate-700 text-sm font-medium hover:bg-slate-50 transition-colors"
              >
                Go to agent
              </button>
            </div>
          </div>
        </div>
      </div>
    );
  }

  // -- wizard ---------------------------------------------------------------

  return (
    <div className="min-h-[calc(100vh-4rem)] relative">
      <div className="relative max-w-2xl mx-auto px-6 py-10">
        <button
          onClick={() => navigate('/agents/all')}
          className="text-xs text-slate-400 hover:text-slate-600"
        >
          ← All agents
        </button>
        <h1 className="text-2xl font-semibold text-slate-900 mt-2">Register an agent</h1>
        <p className="text-sm text-slate-500 mt-1">
          Bring an agent under governance — give it an owner, a classification, and a home platform.
        </p>

        {/* Step indicator */}
        <nav aria-label="Progress" className="mt-6 mb-6">
          <ol className="flex items-center gap-2">
            {STEPS.map((s, i) => {
              const state = step === s.n ? 'current' : step > s.n ? 'done' : 'upcoming';
              return (
                <li key={s.n} className="flex items-center gap-2">
                  <span
                    aria-current={state === 'current' ? 'step' : undefined}
                    className={`inline-flex h-7 w-7 items-center justify-center rounded-full text-xs font-semibold ${
                      state === 'current'
                        ? 'bg-blue-600 text-white'
                        : state === 'done'
                          ? 'bg-emerald-100 text-emerald-700'
                          : 'bg-slate-100 text-slate-400'
                    }`}
                  >
                    {state === 'done' ? '✓' : s.n}
                  </span>
                  <span
                    className={`hidden sm:inline text-xs font-medium ${
                      state === 'upcoming' ? 'text-slate-400' : 'text-slate-700'
                    }`}
                  >
                    {s.label}
                  </span>
                  {i < STEPS.length - 1 && <span className="w-4 h-px bg-slate-200" aria-hidden="true" />}
                </li>
              );
            })}
          </ol>
        </nav>

        <div className={`${CARD} p-6`}>
          {/* Step 1 — Name + purpose */}
          {step === 1 && (
            <div className="space-y-5">
              <h2 className="text-lg font-semibold text-slate-900">Identity</h2>
              <div>
                <label htmlFor="agent-name" className={LABEL}>
                  Name <span className="text-red-500">*</span>
                </label>
                <input
                  id="agent-name"
                  value={draft.name}
                  onChange={(e) => {
                    set('name', e.target.value);
                    if (nameError) setNameError(null);
                  }}
                  placeholder="e.g. claims-triage-de"
                  aria-label="Agent name"
                  aria-invalid={nameError ? true : undefined}
                  className={INPUT}
                />
                {nameError && <p className="mt-1 text-sm text-red-600">{nameError}</p>}
              </div>
              <div>
                <label htmlFor="agent-purpose" className={LABEL}>
                  Purpose
                </label>
                <textarea
                  id="agent-purpose"
                  value={draft.purpose}
                  onChange={(e) => set('purpose', e.target.value)}
                  rows={4}
                  placeholder="What does this agent do? (optional)"
                  aria-label="Agent purpose"
                  className={INPUT}
                />
              </div>
            </div>
          )}

          {/* Step 2 — Sponsor */}
          {step === 2 && (
            <div className="space-y-5">
              <h2 className="text-lg font-semibold text-slate-900">Sponsor</h2>
              <p className="text-sm text-slate-500">
                Leave blank to sponsor it yourself (you’re recorded as the creator).
              </p>
              <div>
                <label htmlFor="sponsor-email" className={LABEL}>
                  Sponsor email
                </label>
                <input
                  id="sponsor-email"
                  type="email"
                  value={draft.sponsor_email}
                  onChange={(e) => set('sponsor_email', e.target.value)}
                  placeholder="owner@acme.com (optional)"
                  aria-label="Sponsor email"
                  className={INPUT}
                />
              </div>
              <div>
                <label htmlFor="sponsor-oid" className={LABEL}>
                  Sponsor object ID
                </label>
                <input
                  id="sponsor-oid"
                  value={draft.sponsor_oid}
                  onChange={(e) => set('sponsor_oid', e.target.value)}
                  placeholder="Entra objectId (optional)"
                  aria-label="Sponsor object ID"
                  className={INPUT}
                />
              </div>
            </div>
          )}

          {/* Step 3 — Business unit + region + data classification */}
          {step === 3 && (
            <div className="space-y-5">
              <h2 className="text-lg font-semibold text-slate-900">Classification</h2>
              <div>
                <label htmlFor="tenant" className={LABEL}>
                  Tenant <span className="text-red-500">*</span>
                </label>
                <select
                  id="tenant"
                  value={draft.tenant_id}
                  onChange={(e) => set('tenant_id', e.target.value)}
                  aria-label="Tenant"
                  className={INPUT}
                >
                  <option value="">Select a tenant (required)</option>
                  {tenantOptions.map((t) => (
                    <option key={t.id} value={t.id}>
                      {t.name}
                    </option>
                  ))}
                </select>
                {tenantOptions.length === 0 && (
                  <p className="mt-1 text-xs text-slate-400">
                    You aren’t a member of any tenant yet — ask an admin to link your Entra group.
                  </p>
                )}
              </div>
              <div>
                <label htmlFor="business-unit" className={LABEL}>
                  Business unit
                </label>
                <input
                  id="business-unit"
                  value={draft.business_unit}
                  onChange={(e) => set('business_unit', e.target.value)}
                  placeholder="e.g. Claims, Underwriting, Finance (optional)"
                  aria-label="Business unit"
                  className={INPUT}
                />
              </div>
              <div>
                <label htmlFor="region" className={LABEL}>
                  Region
                </label>
                <select
                  id="region"
                  value={draft.region}
                  onChange={(e) => set('region', e.target.value)}
                  aria-label="Region"
                  className={INPUT}
                >
                  <option value="">Select a region (optional)</option>
                  {REGION_OPTIONS.map((r) => (
                    <option key={r} value={r}>
                      {r}
                    </option>
                  ))}
                </select>
              </div>
              <div>
                <label htmlFor="data-classification" className={LABEL}>
                  Data classification
                </label>
                <select
                  id="data-classification"
                  value={draft.data_classification}
                  onChange={(e) => set('data_classification', e.target.value as '' | DataClassification)}
                  aria-label="Data classification"
                  className={INPUT}
                >
                  <option value="">Select a classification (optional)</option>
                  {DATA_CLASS_OPTIONS.map((o) => (
                    <option key={o.value} value={o.value}>
                      {o.label}
                    </option>
                  ))}
                </select>
              </div>
            </div>
          )}

          {/* Step 4 — Platform + framework + origin */}
          {step === 4 && (
            <div className="space-y-5">
              <h2 className="text-lg font-semibold text-slate-900">Platform</h2>

              {/* Read-only platform — inferred from the tenant, not chosen here. */}
              <div>
                <span className={LABEL}>Platform</span>
                <p className="mt-1 text-sm font-medium text-slate-800">
                  {tenantValid ? (
                    tenantPlatformLabel(tenantPlatform)
                  ) : (
                    <span className="font-normal text-slate-400">Select a tenant first</span>
                  )}
                </p>
                <p className="mt-1 text-xs text-slate-400">{PLATFORM_READONLY_HINT}</p>
              </div>

              {/* Runtime — the discovery list, or manual entry as the documented fallback. */}
              <div className="pt-2 border-t border-slate-200/70 space-y-4">
                <div className="flex items-start justify-between gap-4">
                  <div>
                    <h3 className="text-sm font-semibold text-slate-800">
                      Runtime{' '}
                      <span className="font-normal text-slate-400">
                        (which running agent this record governs)
                      </span>
                    </h3>
                  </div>
                  <button
                    type="button"
                    onClick={() =>
                      set('handle_source', draft.handle_source === 'manual' ? 'discovery' : 'manual')
                    }
                    className="shrink-0 text-xs font-medium text-blue-600 hover:text-blue-700"
                  >
                    {draft.handle_source === 'manual' ? 'Choose from the tenant' : 'Enter manually'}
                  </button>
                </div>

                {draft.handle_source === 'discovery' ? (
                  <div className="space-y-3">
                    {/* Stage picker — only when the tenant has more than one, and always from
                        the tenant's own stage map (never a hardcoded name, contract C5). */}
                    {stageNames.length > 1 && (
                      <div>
                        <label htmlFor="discovery-stage" className={LABEL}>
                          Environment
                        </label>
                        <select
                          id="discovery-stage"
                          value={stage ?? ''}
                          onChange={(e) => {
                            setStage(e.target.value);
                            set('selected_handle', '');
                          }}
                          aria-label="Discovery environment"
                          className={INPUT}
                        >
                          {stageNames.map((s) => (
                            <option key={s} value={s}>
                              {s}
                            </option>
                          ))}
                        </select>
                      </div>
                    )}

                    {/* Four honest states. An empty list is NEVER rendered as silence: a 502,
                        a not-yet-asked panel and a genuinely empty platform each say so. */}
                    {discovery.kind === 'loading' && (
                      <p className="text-sm text-slate-500" role="status">
                        Asking {tenantPlatformLabel(tenantPlatform)} what this tenant is running…
                      </p>
                    )}

                    {discovery.kind === 'error' && (
                      <div className="rounded-lg bg-red-50 border border-red-200/70 px-4 py-3">
                        <p className="text-sm text-red-700">{discovery.message}</p>
                        <div className="mt-2 flex flex-wrap items-center gap-3">
                          <button
                            type="button"
                            onClick={() => void runDiscovery()}
                            className="text-xs font-medium text-red-700 underline hover:no-underline"
                          >
                            Try again
                          </button>
                          <button
                            type="button"
                            onClick={() => set('handle_source', 'manual')}
                            className="text-xs font-medium text-red-700 underline hover:no-underline"
                          >
                            Enter the runtime manually instead
                          </button>
                        </div>
                      </div>
                    )}

                    {(discovery.kind === 'no-tenant' ||
                      discovery.kind === 'not-permitted' ||
                      discovery.kind === 'no-stages' ||
                      discovery.kind === 'empty') && (
                      <div className="rounded-lg bg-slate-50 border border-slate-200/70 px-4 py-3">
                        <p className="text-sm text-slate-600">
                          {DISCOVERY_EMPTY_COPY[discovery.kind]}
                        </p>
                        {discovery.kind !== 'no-tenant' && (
                          <button
                            type="button"
                            onClick={() => set('handle_source', 'manual')}
                            className="mt-2 text-xs font-medium text-blue-600 hover:text-blue-700"
                          >
                            Enter the runtime manually
                          </button>
                        )}
                      </div>
                    )}

                    {discovery.kind === 'list' && (
                      <ul className="space-y-2" aria-label="Discovered agents">
                        {discovery.rows.map((r) => {
                          const rowState = discoveredRowState(r);
                          const picked = draft.selected_handle === r.runtime_handle;
                          return (
                            <li key={`${r.name}:${r.runtime_handle}`}>
                              <button
                                type="button"
                                disabled={rowState.disabled}
                                aria-pressed={picked}
                                title={rowState.reason ?? rowState.warning ?? undefined}
                                onClick={() =>
                                  set('selected_handle', picked ? '' : r.runtime_handle)
                                }
                                className={`w-full text-left px-3 py-2.5 rounded-lg border transition-colors ${
                                  rowState.disabled
                                    ? 'border-slate-200 bg-slate-50 cursor-not-allowed'
                                    : picked
                                      ? 'border-blue-500 bg-blue-50/70 ring-1 ring-blue-500/30'
                                      : 'border-slate-300 bg-white hover:bg-slate-50'
                                }`}
                              >
                                <span className="flex items-center justify-between gap-3">
                                  <span className="min-w-0">
                                    <span
                                      className={`block text-sm font-medium truncate ${
                                        rowState.disabled ? 'text-slate-400' : 'text-slate-800'
                                      }`}
                                    >
                                      {r.name}
                                    </span>
                                    <span className="block text-xs text-slate-400 truncate">
                                      {r.runtime_handle}
                                    </span>
                                  </span>
                                  {/* The platform's RAW state string — display-only, never
                                      mapped onto AGP's runtime-status union (C-3). */}
                                  {r.state && (
                                    <span className="shrink-0 text-xs px-2 py-0.5 rounded-full bg-slate-100 text-slate-500">
                                      {r.state}
                                    </span>
                                  )}
                                </span>
                                {/* A disabled row ALWAYS carries its reason — a greyed-out row
                                    with no explanation is a swallowed error. */}
                                {rowState.reason && (
                                  <span className="mt-1 block text-xs text-slate-400">
                                    {rowState.reason}
                                  </span>
                                )}
                                {/* A SELECTABLE row's caveat (today: a serving endpoint, which
                                    registers for inventory but cannot be governed yet). Amber,
                                    not grey: without it the operator's first signal is a failed
                                    provision on a record they already created. */}
                                {rowState.warning && (
                                  <span className="mt-1 block text-xs text-amber-600">
                                    {rowState.warning}
                                  </span>
                                )}
                              </button>
                            </li>
                          );
                        })}
                      </ul>
                    )}
                  </div>
                ) : (
                  <div>
                    <label htmlFor="manual-handle" className={LABEL}>
                      {handleField.label}
                    </label>
                    <input
                      id="manual-handle"
                      value={draft[handleField.key]}
                      onChange={(e) => set(handleField.key, e.target.value)}
                      placeholder={handleField.placeholder}
                      aria-label={handleField.label}
                      aria-invalid={handleFieldError ? true : undefined}
                      className={INPUT}
                    />
                    {handleFieldError ? (
                      <p className="mt-1 text-sm text-red-600">{handleFieldError}</p>
                    ) : (
                      <p className="mt-1 text-xs text-slate-400">{handleField.hint}</p>
                    )}
                  </div>
                )}
              </div>

              <div>
                <label htmlFor="framework" className={LABEL}>
                  Framework
                </label>
                <input
                  id="framework"
                  value={draft.framework}
                  onChange={(e) => set('framework', e.target.value)}
                  placeholder="e.g. LangGraph, Strands, CrewAI (optional)"
                  aria-label="Framework"
                  className={INPUT}
                />
              </div>
              <div>
                <label htmlFor="origin" className={LABEL}>
                  Origin
                </label>
                <select
                  id="origin"
                  value={draft.origin}
                  onChange={(e) => set('origin', e.target.value as Origin)}
                  aria-label="Origin"
                  className={INPUT}
                >
                  {ORIGIN_OPTIONS.map((o) => (
                    <option key={o.value} value={o.value}>
                      {o.label}
                    </option>
                  ))}
                </select>
              </div>

              {/* Invocation sub-group — how callers reach this agent */}
              <div className="pt-2 border-t border-slate-200/70 space-y-5">
                <div>
                  <h3 className="text-sm font-semibold text-slate-800">
                    Invocation <span className="font-normal text-slate-400">(how callers reach this agent)</span>
                  </h3>
                </div>
                <div>
                  <label htmlFor="endpoint-url" className={LABEL}>
                    Endpoint URL
                  </label>
                  <input
                    id="endpoint-url"
                    value={draft.endpoint_url}
                    onChange={(e) => set('endpoint_url', e.target.value)}
                    placeholder="https://… or mcp://… (optional)"
                    aria-label="Endpoint URL"
                    className={INPUT}
                  />
                </div>
                {/* Authentication. On a Databricks tenant WITH A RUNTIME BOUND, `buildPayload`
                    FORCES this to Entra — `is_databricks_governed` requires it before any
                    identity work happens, so a Databricks agent left on "None" is a record that
                    looks governed and provisions nothing. The condition reads the payload rather
                    than the platform, so the two cannot disagree: a Databricks registration with
                    no runtime yet is metadata-only and keeps the operator's choice, exactly as
                    the posted body does. Read-only rather than silently overridden — a control
                    whose value the payload ignores is worse than no control. */}
                {preview.auth_type === 'entra' && draft.auth_type !== 'entra' ? (
                  <div>
                    <span className={LABEL}>Authentication</span>
                    <p className="mt-1 text-sm font-medium text-slate-800">
                      {AUTH_TYPE_LABEL[preview.auth_type ?? 'none']}
                    </p>
                    <p className="mt-1 text-xs text-slate-400">
                      Databricks-hosted agents are governed through Entra — callers are exchanged
                      onto the workspace on every invoke.
                    </p>
                  </div>
                ) : (
                  <div>
                    <label htmlFor="auth-type" className={LABEL}>
                      Authentication
                    </label>
                    <select
                      id="auth-type"
                      value={draft.auth_type}
                      onChange={(e) => set('auth_type', e.target.value as AuthType)}
                      aria-label="Authentication type"
                      className={INPUT}
                    >
                      {AUTH_TYPE_OPTIONS.map((o) => (
                        <option key={o.value} value={o.value}>
                          {o.label}
                        </option>
                      ))}
                    </select>
                  </div>
                )}
              </div>
            </div>
          )}

          {/* Step 5 — Confirm */}
          {step === 5 && (
            <div className="space-y-5">
              <h2 className="text-lg font-semibold text-slate-900">Confirm</h2>
              <p className="text-sm text-slate-500">
                Review the details below. Creating the agent registers it as a draft (proposed); you can submit
                it for approval afterwards.
              </p>
              <dl className="grid grid-cols-1 sm:grid-cols-2 gap-x-8 gap-y-4">
                <Summary label="Name">{draft.name.trim() || '—'}</Summary>
                <Summary label="Origin">{draft.origin}</Summary>
                <Summary label="Purpose" full>
                  {draft.purpose.trim() || <span className="text-slate-400">—</span>}
                </Summary>
                <Summary label="Sponsor email">
                  {draft.sponsor_email.trim() || <span className="text-slate-400">You (default)</span>}
                </Summary>
                <Summary label="Sponsor object ID">
                  {draft.sponsor_oid.trim() || <span className="text-slate-400">—</span>}
                </Summary>
                <Summary label="Tenant">
                  {draft.tenant_id ? (
                    resolveTenantName(draft.tenant_id, tenantOptions) ?? draft.tenant_id
                  ) : (
                    <span className="text-red-600">Required — select a tenant</span>
                  )}
                </Summary>
                <Summary label="Business unit">
                  {draft.business_unit.trim() || <span className="text-slate-400">—</span>}
                </Summary>
                <Summary label="Region">
                  {draft.region || <span className="text-slate-400">—</span>}
                </Summary>
                <Summary label="Data classification">
                  {draft.data_classification || <span className="text-slate-400">—</span>}
                </Summary>
                <Summary label="Platform">
                  {tenantValid ? (
                    tenantPlatformLabel(tenantPlatform)
                  ) : (
                    <span className="text-slate-400">—</span>
                  )}
                </Summary>
                <Summary label="Framework">
                  {draft.framework.trim() || <span className="text-slate-400">—</span>}
                </Summary>
                <Summary label="Endpoint URL" full>
                  {draft.endpoint_url.trim() || <span className="text-slate-400">Not set</span>}
                </Summary>
                {/* Every field below is read off the PAYLOAD, not the draft: what the operator
                    confirms must be what gets posted. `runtime_handle` and `agent_arn` are
                    mutually exclusive by platform (C-4), so exactly one row carries a value. */}
                <Summary label="Authentication">
                  {AUTH_TYPE_LABEL[preview.auth_type ?? 'none']}
                </Summary>
                {tenantPlatform === 'databricks' ? (
                  <>
                    <Summary label="App URL" full>
                      {preview.runtime_handle ?? (
                        <span className="text-slate-400">Not set — no runtime bound</span>
                      )}
                    </Summary>
                    <Summary label="Runtime kind">
                      {preview.runtime_kind ?? <span className="text-slate-400">—</span>}
                    </Summary>
                  </>
                ) : (
                  <Summary label="Agent ARN" full>
                    {preview.agent_arn ?? <span className="text-slate-400">Not set</span>}
                  </Summary>
                )}
              </dl>
            </div>
          )}
        </div>

        {/* Submit error — OUTSIDE the step blocks, deliberately. It used to live inside step 5,
            which meant every `setError(...)` paired with a `setStep(...)` away from 5 set a
            message nothing rendered: the wizard jumped to another step with no explanation and
            the operator saw only a Create button that had stopped working. An error that can be
            set from any step must be visible from any step. */}
        {error && (
          <p role="alert" className="mt-4 text-sm text-red-600">
            {error}
          </p>
        )}

        {/* Nav buttons */}
        <div className="mt-6 flex items-center justify-between">
          <button
            onClick={back}
            disabled={step === 1 || creating}
            className="px-4 py-2 rounded-lg bg-white border border-slate-300 text-slate-700 text-sm font-medium hover:bg-slate-50 transition-colors disabled:opacity-40"
          >
            Back
          </button>
          {step < 5 ? (
            <button
              onClick={next}
              disabled={step === 1 && !nameValid}
              className="px-5 py-2 rounded-lg bg-blue-600 text-white text-sm font-medium hover:bg-blue-700 transition-colors disabled:opacity-50"
            >
              Next
            </button>
          ) : (
            <button
              onClick={handleCreate}
              disabled={creating || !submitOk}
              className="px-5 py-2 rounded-lg bg-blue-600 text-white text-sm font-medium hover:bg-blue-700 transition-colors disabled:opacity-50"
            >
              {creating ? 'Creating…' : 'Create'}
            </button>
          )}
        </div>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Confirm-step summary field
// ---------------------------------------------------------------------------

function Summary({
  label,
  children,
  full,
}: {
  label: string;
  children: React.ReactNode;
  full?: boolean;
}) {
  return (
    <div className={full ? 'sm:col-span-2' : undefined}>
      <dt className="text-xs uppercase tracking-wide text-slate-400 font-medium">{label}</dt>
      <dd className="mt-1 text-sm text-slate-700 whitespace-pre-wrap break-words">{children}</dd>
    </div>
  );
}
