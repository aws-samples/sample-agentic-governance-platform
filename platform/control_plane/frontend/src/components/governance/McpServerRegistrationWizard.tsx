import { useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { mcpServersApi } from '../../api/client';
import type { DataClassification, McpServer, McpServerCreate, McpServerKind, McpTool } from '../../api/client';
import { useUser } from '../../contexts/UserContext';
import { resolveTenantName, tenantSelectOptions } from './tenantUi';
import { useTenantDirectory } from './useTenantDirectory';

// ---------------------------------------------------------------------------
// Select option tables (mirror the enum labels used in McpServerCatalog / McpServerDetail)
// ---------------------------------------------------------------------------

const KIND_OPTIONS: { value: McpServerKind; label: string }[] = [
  { value: 'standard', label: 'Standard (Runtime / external endpoint)' },
  { value: 'gateway', label: 'Gateway (AgentCore-managed)' },
  { value: 'runtime', label: 'Runtime (AgentCore Runtime-MCP)' },
];

const DATA_CLASS_OPTIONS: { value: DataClassification; label: string }[] = [
  { value: 'Public', label: 'Public' },
  { value: 'Internal', label: 'Internal' },
  { value: 'Confidential', label: 'Confidential' },
  { value: 'Restricted', label: 'Restricted' },
];

const REGION_OPTIONS = ['DE', 'IT', 'FR', 'SE', 'EU'];

const KIND_LABEL: Record<McpServerKind, string> = {
  standard: 'Standard',
  gateway: 'Gateway',
  runtime: 'Runtime',
};

const CARD = 'bg-white/70 backdrop-blur rounded-xl border border-slate-200/60 shadow-sm';

const STEPS = [
  { n: 1, label: 'Identity' },
  { n: 2, label: 'Kind' },
  { n: 3, label: 'Owner' },
  { n: 4, label: 'Classification' },
  { n: 5, label: 'Tools' },
  { n: 6, label: 'Confirm' },
];

// Shared field styling.
const INPUT =
  'mt-1 w-full px-3 py-2 text-sm rounded-lg border border-slate-300 focus:outline-none focus:ring-2 focus:ring-blue-500/40';
const LABEL = 'text-xs uppercase tracking-wide text-slate-500 font-medium';

// A single editable tool row. `uid` is a monotonic counter used as the React key
// (stable across mid-list removals — avoids the index-key footgun). `schemaText` is
// the raw JSON text the user types; it is parsed to an object on change (empty → `{}`).
// `schemaError` is the inline parse/shape error, or null when the row's schema is valid.
interface ToolRow {
  uid: number;
  name: string;
  description: string;
  schemaText: string;
  schemaError: string | null;
}

let _toolUidCounter = 0;
const nextToolUid = () => ++_toolUidCounter;

const EMPTY_TOOL_ROW: ToolRow = { uid: 0, name: '', description: '', schemaText: '', schemaError: null };

// Local draft of every editable field (strings — coerced to the McpServerCreate payload on submit).
interface Draft {
  name: string;
  description: string;
  version: string;
  kind: McpServerKind;
  endpoint_url: string;
  gateway_arn: string;
  runtime_arn: string;
  owner_email: string;
  owner_oid: string;
  tenant_id: string;
  business_unit: string;
  region: string;
  data_classification: '' | DataClassification;
  tools: ToolRow[];
}

const EMPTY_DRAFT: Draft = {
  name: '',
  description: '',
  version: '1.0.0',
  kind: 'standard',
  endpoint_url: '',
  gateway_arn: '',
  runtime_arn: '',
  owner_email: '',
  owner_oid: '',
  tenant_id: '',
  business_unit: '',
  region: '',
  data_classification: '',
  tools: [],
};

// Parse a row's schema text into a validated JSON object. Empty text → `{}`.
// Returns an error string when the text is not valid JSON or not an object.
function parseSchema(text: string): { value: Record<string, unknown> | null; error: string | null } {
  const t = text.trim();
  if (!t) return { value: {}, error: null };
  let parsed: unknown;
  try {
    parsed = JSON.parse(t);
  } catch {
    return { value: null, error: 'Invalid JSON' };
  }
  if (typeof parsed !== 'object' || parsed === null || Array.isArray(parsed)) {
    return { value: null, error: 'Input schema must be a JSON object' };
  }
  return { value: parsed as Record<string, unknown>, error: null };
}

// A row contributes a tool only if it has a non-empty name. Fully-empty rows are dropped.
function rowHasContent(r: ToolRow): boolean {
  return r.name.trim().length > 0;
}

// Omit empty optional strings so the backend applies its defaults. Required: name.
// `kind` and `version` are always sent. `available_tools` is included only when at
// least one row carries a (named) tool.
function buildPayload(d: Draft): McpServerCreate {
  const trimmed = (s: string) => {
    const t = s.trim();
    return t.length ? t : undefined;
  };
  const tools: McpTool[] = d.tools.filter(rowHasContent).map((r) => {
    const { value } = parseSchema(r.schemaText);
    return {
      name: r.name.trim(),
      description: trimmed(r.description),
      input_schema: value ?? {},
    };
  });
  return {
    name: d.name.trim(),
    description: trimmed(d.description),
    version: d.version.trim() || '1.0.0',
    kind: d.kind,
    endpoint_url: trimmed(d.endpoint_url),
    gateway_arn: trimmed(d.gateway_arn),
    runtime_arn: trimmed(d.runtime_arn),
    owner_email: trimmed(d.owner_email),
    owner_oid: trimmed(d.owner_oid),
    tenant_id: d.tenant_id,
    business_unit: trimmed(d.business_unit),
    region: trimmed(d.region),
    data_classification: d.data_classification || undefined,
    available_tools: tools.length ? tools : undefined,
  };
}

export default function McpServerRegistrationWizard() {
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
  const [created, setCreated] = useState<McpServer | null>(null);
  const [error, setError] = useState<string | null>(null);
  // A name-collision (409) error is surfaced inline on step 1.
  const [nameError, setNameError] = useState<string | null>(null);
  // A missing-ARN error is surfaced inline on step 2 (gateway/runtime require one).
  const [arnError, setArnError] = useState<string | null>(null);

  // Post-create "submit for approval" state.
  const [submitting, setSubmitting] = useState(false);
  const [submitted, setSubmitted] = useState(false);
  const [submitError, setSubmitError] = useState<string | null>(null);

  const set = <K extends keyof Draft>(key: K, value: Draft[K]) =>
    setDraft((d) => ({ ...d, [key]: value }));

  const nameValid = draft.name.trim().length > 0;
  // A gateway can only be provisioned if it carries a gateway_arn (the platform derives
  // the gatewayId from the ARN to provision its Entra identity); a runtime needs runtime_arn.
  // Standard servers are metadata-only — no ARN required. Empty is the hard gate (format is
  // only a soft hint below) so a gateway can never be registered stuck at identity_status=none.
  const arnValid =
    (draft.kind !== 'gateway' || draft.gateway_arn.trim().length > 0) &&
    (draft.kind !== 'runtime' || draft.runtime_arn.trim().length > 0);
  // tenant_id is REQUIRED by the backend since E24 — gate Create on it.
  const tenantValid = draft.tenant_id.length > 0;
  // Confirm/Create is blocked while any row has invalid JSON.
  const hasInvalidTool = draft.tools.some((r) => r.schemaError !== null);

  // -- tool-row editing -----------------------------------------------------

  const addTool = () =>
    setDraft((d) => ({ ...d, tools: [...d.tools, { ...EMPTY_TOOL_ROW, uid: nextToolUid() }] }));
  const removeTool = (idx: number) =>
    setDraft((d) => ({ ...d, tools: d.tools.filter((_, i) => i !== idx) }));
  const setToolField = (idx: number, key: 'name' | 'description', value: string) =>
    setDraft((d) => ({
      ...d,
      tools: d.tools.map((r, i) => (i === idx ? { ...r, [key]: value } : r)),
    }));
  const setToolSchema = (idx: number, value: string) =>
    setDraft((d) => ({
      ...d,
      tools: d.tools.map((r, i) =>
        i === idx ? { ...r, schemaText: value, schemaError: parseSchema(value).error } : r,
      ),
    }));

  // -- step nav -------------------------------------------------------------

  const arnErrorMessage = () =>
    draft.kind === 'runtime'
      ? 'A Runtime ARN is required to provision an AgentCore runtime.'
      : 'A Gateway ARN is required to provision an AgentCore gateway.';

  const next = () => {
    if (step === 1 && !nameValid) {
      setNameError('A name is required.');
      return;
    }
    if (step === 2 && !arnValid) {
      setArnError(arnErrorMessage());
      return;
    }
    setNameError(null);
    setArnError(null);
    setStep((s) => Math.min(6, s + 1));
  };
  const back = () => {
    setError(null);
    setStep((s) => Math.max(1, s - 1));
  };

  const handleCreate = async () => {
    if (!nameValid) {
      setNameError('A name is required.');
      setStep(1);
      return;
    }
    if (!arnValid) {
      setArnError(arnErrorMessage());
      setStep(2);
      return;
    }
    if (!tenantValid) {
      setError('A tenant is required.');
      setStep(4);
      return;
    }
    if (hasInvalidTool) {
      setError('One or more tools has an invalid input schema. Fix the JSON before creating.');
      setStep(5);
      return;
    }
    setCreating(true);
    setError(null);
    setNameError(null);
    setArnError(null);
    try {
      const mcp = await mcpServersApi.create(buildPayload(draft));
      setCreated(mcp);
    } catch (err: unknown) {
      const msg = err instanceof Error ? err.message : 'Failed to create the MCP server.';
      // A duplicate name surfaces as a 409 → its detail message. Send the user back
      // to step 1 with the message on the name field so they can rename. Any other
      // error (incl. a 422 schema-validation rejection) surfaces in the error panel.
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
      await mcpServersApi.submit(created.id);
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
                <h1 className="text-2xl font-semibold text-slate-900">MCP server registered</h1>
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
                the MCP server to review the details first.
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
                onClick={() => navigate(`/mcp-servers/${created.id}`)}
                className="px-4 py-2 rounded-lg bg-white border border-slate-300 text-slate-700 text-sm font-medium hover:bg-slate-50 transition-colors"
              >
                Go to MCP server
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
          onClick={() => navigate('/tools-mcp')}
          className="text-xs text-slate-400 hover:text-slate-600"
        >
          ← Tools &amp; MCP
        </button>
        <h1 className="text-2xl font-semibold text-slate-900 mt-2">Register an MCP server</h1>
        <p className="text-sm text-slate-500 mt-1">
          Bring an MCP server under governance — give it an owner, a classification, and declare its tools.
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
          {/* Step 1 — Identity: name + description + version */}
          {step === 1 && (
            <div className="space-y-5">
              <h2 className="text-lg font-semibold text-slate-900">Identity</h2>
              <div>
                <label htmlFor="mcp-name" className={LABEL}>
                  Name <span className="text-red-500">*</span>
                </label>
                <input
                  id="mcp-name"
                  value={draft.name}
                  onChange={(e) => {
                    set('name', e.target.value);
                    if (nameError) setNameError(null);
                  }}
                  placeholder="e.g. internal-claims-mcp"
                  aria-label="MCP server name"
                  aria-invalid={nameError ? true : undefined}
                  className={INPUT}
                />
                {nameError && <p className="mt-1 text-sm text-red-600">{nameError}</p>}
              </div>
              <div>
                <label htmlFor="mcp-description" className={LABEL}>
                  Description
                </label>
                <textarea
                  id="mcp-description"
                  value={draft.description}
                  onChange={(e) => set('description', e.target.value)}
                  rows={4}
                  placeholder="What does this MCP server expose? (optional)"
                  aria-label="MCP server description"
                  className={INPUT}
                />
              </div>
              <div>
                <label htmlFor="mcp-version" className={LABEL}>
                  Version
                </label>
                <input
                  id="mcp-version"
                  value={draft.version}
                  onChange={(e) => set('version', e.target.value)}
                  placeholder="1.0.0"
                  aria-label="MCP server version"
                  className={INPUT}
                />
              </div>
            </div>
          )}

          {/* Step 2 — Kind + endpoint */}
          {step === 2 && (
            <div className="space-y-5">
              <h2 className="text-lg font-semibold text-slate-900">Kind</h2>
              <div>
                <label htmlFor="mcp-kind" className={LABEL}>
                  Kind
                </label>
                <select
                  id="mcp-kind"
                  value={draft.kind}
                  onChange={(e) => {
                    set('kind', e.target.value as McpServerKind);
                    if (arnError) setArnError(null);
                  }}
                  aria-label="MCP server kind"
                  className={INPUT}
                >
                  {KIND_OPTIONS.map((o) => (
                    <option key={o.value} value={o.value}>
                      {o.label}
                    </option>
                  ))}
                </select>
                <p className="mt-1 text-xs text-slate-400">
                  Gateway servers are AgentCore-managed and carry a Cedar policy tab; standard
                  servers point at an external endpoint.
                </p>
              </div>
              <div>
                <label htmlFor="mcp-endpoint" className={LABEL}>
                  Endpoint URL
                </label>
                <input
                  id="mcp-endpoint"
                  value={draft.endpoint_url}
                  onChange={(e) => set('endpoint_url', e.target.value)}
                  placeholder="https://… or mcp://… (optional)"
                  aria-label="Endpoint URL"
                  className={INPUT}
                />
              </div>
              {draft.kind === 'gateway' && (
                <div>
                  <label htmlFor="mcp-gateway-arn" className={LABEL}>
                    Gateway ARN <span className="text-red-500">*</span>
                  </label>
                  <input
                    id="mcp-gateway-arn"
                    value={draft.gateway_arn}
                    onChange={(e) => {
                      set('gateway_arn', e.target.value);
                      if (arnError) setArnError(null);
                    }}
                    placeholder="arn:aws:bedrock-agentcore:eu-central-1:…:gateway/…"
                    aria-label="Gateway ARN"
                    aria-invalid={arnError ? true : undefined}
                    aria-describedby={arnError ? 'mcp-gateway-arn-error' : 'mcp-gateway-arn-help'}
                    className={`${INPUT} font-mono text-xs ${arnError ? 'border-red-400 focus:ring-red-500/40' : ''}`}
                  />
                  {arnError ? (
                    <p id="mcp-gateway-arn-error" className="mt-1 text-sm text-red-600">
                      {arnError}
                    </p>
                  ) : (
                    <p id="mcp-gateway-arn-help" className="mt-1 text-xs text-slate-400">
                      The AgentCore Gateway ARN (arn:aws:bedrock-agentcore:…:gateway/…). Required so the platform
                      can provision the gateway’s Entra identity and scan its tools.
                    </p>
                  )}
                  {!arnError &&
                    draft.gateway_arn.trim().length > 0 &&
                    !draft.gateway_arn.trim().startsWith('arn:aws:bedrock-agentcore:') && (
                      <p className="mt-1 text-xs text-amber-600">
                        This doesn’t look like a bedrock-agentcore gateway ARN — double-check it before creating.
                      </p>
                    )}
                </div>
              )}
              {draft.kind === 'runtime' && (
                <div>
                  <label htmlFor="mcp-runtime-arn" className={LABEL}>
                    Runtime ARN <span className="text-red-500">*</span>
                  </label>
                  <input
                    id="mcp-runtime-arn"
                    value={draft.runtime_arn}
                    onChange={(e) => {
                      set('runtime_arn', e.target.value);
                      if (arnError) setArnError(null);
                    }}
                    placeholder="arn:aws:bedrock-agentcore:eu-central-1:…:runtime/…"
                    aria-label="Runtime ARN"
                    aria-invalid={arnError ? true : undefined}
                    aria-describedby={arnError ? 'mcp-runtime-arn-error' : 'mcp-runtime-arn-help'}
                    className={`${INPUT} font-mono text-xs ${arnError ? 'border-red-400 focus:ring-red-500/40' : ''}`}
                  />
                  {arnError ? (
                    <p id="mcp-runtime-arn-error" className="mt-1 text-sm text-red-600">
                      {arnError}
                    </p>
                  ) : (
                    <p id="mcp-runtime-arn-help" className="mt-1 text-xs text-slate-400">
                      The AgentCore Runtime ARN (serverProtocol=MCP). Required to provision its Entra identity.
                    </p>
                  )}
                  {!arnError &&
                    draft.runtime_arn.trim().length > 0 &&
                    !draft.runtime_arn.trim().startsWith('arn:aws:bedrock-agentcore:') && (
                      <p className="mt-1 text-xs text-amber-600">
                        This doesn’t look like a bedrock-agentcore runtime ARN — double-check it before creating.
                      </p>
                    )}
                </div>
              )}
            </div>
          )}

          {/* Step 3 — Owner */}
          {step === 3 && (
            <div className="space-y-5">
              <h2 className="text-lg font-semibold text-slate-900">Owner</h2>
              <p className="text-sm text-slate-500">
                Leave blank to own it yourself (you’re recorded as the creator).
              </p>
              <div>
                <label htmlFor="owner-email" className={LABEL}>
                  Owner email
                </label>
                <input
                  id="owner-email"
                  type="email"
                  value={draft.owner_email}
                  onChange={(e) => set('owner_email', e.target.value)}
                  placeholder="owner@acme.com (optional)"
                  aria-label="Owner email"
                  className={INPUT}
                />
              </div>
              <div>
                <label htmlFor="owner-oid" className={LABEL}>
                  Owner object ID
                </label>
                <input
                  id="owner-oid"
                  value={draft.owner_oid}
                  onChange={(e) => set('owner_oid', e.target.value)}
                  placeholder="Entra objectId (optional)"
                  aria-label="Owner object ID"
                  className={INPUT}
                />
              </div>
            </div>
          )}

          {/* Step 4 — Classification: business unit + region + data classification */}
          {step === 4 && (
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

          {/* Step 5 — Tools: add-row editor */}
          {step === 5 && (
            <div className="space-y-5">
              <h2 className="text-lg font-semibold text-slate-900">Tools</h2>
              <p className="text-sm text-slate-500">
                Declare the tools this server exposes. Tools are optional — a server-only record is valid. Each
                tool needs a name; the input schema is a JSON Schema object (leave blank for <code>{'{}'}</code>).
              </p>

              {draft.tools.length === 0 && (
                <p className="text-sm text-slate-400">No tools declared yet.</p>
              )}

              <div className="space-y-4">
                {draft.tools.map((row, idx) => {
                  const schemaErrorId = `tool-${row.uid}-schema-error`;
                  return (
                    <div key={row.uid} className="rounded-lg border border-slate-200 bg-white/60 p-4 space-y-3">
                      <div className="flex items-center justify-between">
                        <span className="text-xs font-semibold uppercase tracking-wide text-slate-400">
                          Tool {idx + 1}
                        </span>
                        <button
                          type="button"
                          onClick={() => removeTool(idx)}
                          className="text-xs font-medium text-red-600 hover:text-red-700"
                          aria-label={`Remove tool ${idx + 1}`}
                        >
                          Remove
                        </button>
                      </div>
                      <div>
                        <label htmlFor={`tool-${idx}-name`} className={LABEL}>
                          Name <span className="text-red-500">*</span>
                        </label>
                        <input
                          id={`tool-${idx}-name`}
                          value={row.name}
                          onChange={(e) => setToolField(idx, 'name', e.target.value)}
                          placeholder="e.g. get_claim"
                          aria-label={`Tool ${idx + 1} name`}
                          className={INPUT}
                        />
                      </div>
                      <div>
                        <label htmlFor={`tool-${idx}-description`} className={LABEL}>
                          Description
                        </label>
                        <input
                          id={`tool-${idx}-description`}
                          value={row.description}
                          onChange={(e) => setToolField(idx, 'description', e.target.value)}
                          placeholder="What does this tool do? (optional)"
                          aria-label={`Tool ${idx + 1} description`}
                          className={INPUT}
                        />
                      </div>
                      <div>
                        <label htmlFor={`tool-${idx}-schema`} className={LABEL}>
                          Input schema (JSON)
                        </label>
                        <textarea
                          id={`tool-${idx}-schema`}
                          value={row.schemaText}
                          onChange={(e) => setToolSchema(idx, e.target.value)}
                          rows={4}
                          placeholder='{ "type": "object", "properties": { … } }'
                          aria-label={`Tool ${idx + 1} input schema (JSON)`}
                          aria-invalid={row.schemaError ? true : undefined}
                          aria-describedby={row.schemaError ? schemaErrorId : undefined}
                          className={`${INPUT} font-mono text-xs ${row.schemaError ? 'border-red-400 focus:ring-red-500/40' : ''}`}
                        />
                        {row.schemaError && (
                          <p id={schemaErrorId} className="mt-1 text-sm text-red-600">
                            {row.schemaError}
                          </p>
                        )}
                      </div>
                    </div>
                  );
                })}
              </div>

              <button
                type="button"
                onClick={addTool}
                className="px-3 py-1.5 rounded-lg bg-white border border-slate-300 text-slate-700 text-sm font-medium hover:bg-slate-50 transition-colors"
              >
                + Add tool
              </button>
            </div>
          )}

          {/* Step 6 — Confirm */}
          {step === 6 && (
            <div className="space-y-5">
              <h2 className="text-lg font-semibold text-slate-900">Confirm</h2>
              <p className="text-sm text-slate-500">
                Review the details below. Creating the MCP server registers it as a draft (proposed); you can
                submit it for approval afterwards.
              </p>
              <dl className="grid grid-cols-1 sm:grid-cols-2 gap-x-8 gap-y-4">
                <Summary label="Name">{draft.name.trim() || '—'}</Summary>
                <Summary label="Kind">{KIND_LABEL[draft.kind]}</Summary>
                <Summary label="Description" full>
                  {draft.description.trim() || <span className="text-slate-400">—</span>}
                </Summary>
                <Summary label="Version">{draft.version.trim() || '1.0.0'}</Summary>
                <Summary label="Endpoint URL">
                  {draft.endpoint_url.trim() || <span className="text-slate-400">Not set</span>}
                </Summary>
                {draft.kind === 'gateway' && (
                  <Summary label="Gateway ARN" full>
                    {draft.gateway_arn.trim() ? (
                      <span className="font-mono text-xs break-all">{draft.gateway_arn.trim()}</span>
                    ) : (
                      <span className="text-red-600">Required — set a Gateway ARN to provision this gateway</span>
                    )}
                  </Summary>
                )}
                {draft.kind === 'runtime' && (
                  <Summary label="Runtime ARN" full>
                    {draft.runtime_arn.trim() ? (
                      <span className="font-mono text-xs break-all">{draft.runtime_arn.trim()}</span>
                    ) : (
                      <span className="text-red-600">Required — set a Runtime ARN to provision this runtime</span>
                    )}
                  </Summary>
                )}
                <Summary label="Owner email">
                  {draft.owner_email.trim() || <span className="text-slate-400">You (default)</span>}
                </Summary>
                <Summary label="Owner object ID">
                  {draft.owner_oid.trim() || <span className="text-slate-400">—</span>}
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
                <Summary label="Tools" full>
                  {(() => {
                    const named = draft.tools.filter(rowHasContent);
                    if (named.length === 0) return <span className="text-slate-400">None declared</span>;
                    return (
                      <ul className="space-y-0.5">
                        {named.map((t, i) => (
                          <li key={i} className="font-mono text-xs text-slate-700">
                            {t.name.trim()}
                          </li>
                        ))}
                      </ul>
                    );
                  })()}
                </Summary>
              </dl>
              {hasInvalidTool && (
                <p className="text-sm text-red-600">
                  One or more tools has an invalid input schema. Go back to the Tools step to fix it.
                </p>
              )}
              {error && <p className="text-sm text-red-600">{error}</p>}
            </div>
          )}
        </div>

        {/* Nav buttons */}
        <div className="mt-6 flex items-center justify-between">
          <button
            onClick={back}
            disabled={step === 1 || creating}
            className="px-4 py-2 rounded-lg bg-white border border-slate-300 text-slate-700 text-sm font-medium hover:bg-slate-50 transition-colors disabled:opacity-40"
          >
            Back
          </button>
          {step < 6 ? (
            <button
              onClick={next}
              disabled={(step === 1 && !nameValid) || (step === 2 && !arnValid)}
              className="px-5 py-2 rounded-lg bg-blue-600 text-white text-sm font-medium hover:bg-blue-700 transition-colors disabled:opacity-50"
            >
              Next
            </button>
          ) : (
            <button
              onClick={handleCreate}
              disabled={creating || !nameValid || !arnValid || !tenantValid || hasInvalidTool}
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
