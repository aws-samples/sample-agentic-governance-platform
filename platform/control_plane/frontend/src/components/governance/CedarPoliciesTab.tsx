// CedarPoliciesTab — the gateway MCP detail "Policies" tab (Epic 8, T6).
//
// Fills the E5-stubbed, gateway-only Policies tab with the Cedar per-tool
// authorization UI. The MCP-side mirror of McpConnectedAgentsTab's shape — same
// glass CARD chrome, the same CSS-only Spinner, the same optimistic-add/remove +
// REPLICATION_RECONCILE_MS reconciling refetch, the same mountedRef unmount guard,
// closePicker/Escape handling, and focus-return-to-trigger — but the resource is
// a Cedar policy on the gateway's native AgentCore Policy Engine, not an Entra
// app-role assignment.
//
// The flow: an operator picks an Entra USER (whose PrincipalHit.id IS the oid) +
// a tool (or "All tools") + Allow → the backend generates a Cedar `permit` policy
// keyed on the user's oid and applies it to the gateway's Policy Engine. The FIRST
// policy creates + attaches the engine in ENFORCE (default-deny) — so a two-step
// warning gate precedes the first add. A segmented Enforce / Log only / Disable
// control flips the gateway's enforcement posture. Once the gateway IS enforcing, a
// persistent amber note states the consequence the emerald banner cannot: every
// uncovered call is denied in AWS, and that deny never reaches this console. Its copy
// lives in `cedarPosture.ts` — the same product decision as the confirm dialog's, so
// it is single-sourced and vitest-pinned rather than duplicated here (E36/T10).

import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { cedarPoliciesApi, principalsApi } from '../../api/client';
import type {
  CedarCondition,
  CedarEffect,
  CedarEnforcementMode,
  CedarPolicyRow,
  McpServer,
  PrincipalHit,
} from '../../api/client';
import { AgentAvatar, emailAlias } from './agentUi';
import {
  buildAddPolicyBody,
  conditionLabel,
  operatorsForType,
  paramsFromSchema,
  policyToolLabel,
} from './cedarPolicyForm';
import { postureWarning } from './cedarPosture';

// Card chrome — identical to McpConnectedAgentsTab's CARD so the tab body sits in
// the same surface family as Overview/Tools/Connected Agents.
const CARD = 'bg-white/70 backdrop-blur rounded-xl border border-slate-200/60 shadow-sm';

// The Policy Engine is eventually consistent after a create/attach/delete (the
// gateway is briefly UPDATING). After a mutate we wait this long, then refetch to
// reconcile the optimistic state with the engine's truth. Mirrors the
// connected-agents tab's REPLICATION_RECONCILE_MS (Policy Engine settle is a bit
// slower than Graph replication, so we give it a touch longer).
const REPLICATION_RECONCILE_MS = 1500;

// Sentinel select value for "All tools on this gateway" (distinct from any real
// tool name, which is always a non-empty AgentCore action token).
const ALL_TOOLS = '__ALL__';
const MIN_QUERY = 2;
const DEBOUNCE_MS = 300;

// A small CSS-only spinner (no icon library) — slate ring with a blue arc.
// Copied verbatim from McpConnectedAgentsTab's Spinner.
function Spinner() {
  return (
    <span
      aria-hidden="true"
      className="inline-block h-4 w-4 rounded-full border-2 border-slate-200 border-t-blue-600 animate-spin"
    />
  );
}

// ---------------------------------------------------------------------------
// Enforcement banner — the gateway's live authorization posture.
// none=slate (open), log_only=amber (evaluated-not-enforced), enforce=emerald
// (default-deny). Semantic tints mirror the badge idiom across the governance UI.
// ---------------------------------------------------------------------------

const ENFORCEMENT_COPY: Record<CedarEnforcementMode, { ring: string; dot: string; heading: string; body: string }> = {
  none: {
    ring: 'border-slate-200/70',
    dot: 'bg-slate-400',
    heading: 'No policy engine attached',
    body: 'Every assigned user can call any tool on this gateway. Add a policy to switch to default-deny.',
  },
  log_only: {
    ring: 'border-amber-200/80',
    dot: 'bg-amber-500',
    heading: 'Logging only',
    body: 'Policies are evaluated and logged to CloudWatch, but not enforced — every assigned user can still call any tool.',
  },
  enforce: {
    ring: 'border-emerald-200/80',
    dot: 'bg-emerald-500',
    heading: 'Enforcing — default-deny',
    body: 'Only users named in a policy can call tools. Assigned users not covered by a policy are blocked.',
  },
};

// ---------------------------------------------------------------------------
// CedarPoliciesTab
// ---------------------------------------------------------------------------

export default function CedarPoliciesTab({
  mcp,
  canManage,
}: {
  mcp: McpServer;
  canManage: boolean;
}) {
  const mcpId = mcp.id;

  // The gateway must be wired (identity provisioned + gateway_id) before Cedar
  // can attach a Policy Engine to it.
  const gatewayReady = mcp.identity_status === 'provisioned' && !!mcp.gateway_id;

  const [policies, setPolicies] = useState<CedarPolicyRow[]>([]);
  const [enforcementMode, setEnforcementMode] = useState<CedarEnforcementMode>(
    mcp.cedar_enforcement_mode ?? 'none',
  );
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState<string | null>(null);

  // Mutation (add/remove/setEnforcement) UI state — mirrors the connected-agents
  // tab's actionPending / actionError idiom.
  const [actionPending, setActionPending] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);

  // Add-policy inline form state.
  const [picking, setPicking] = useState(false);
  const [query, setQuery] = useState('');
  const [results, setResults] = useState<PrincipalHit[]>([]);
  const [searching, setSearching] = useState(false);
  const [searchError, setSearchError] = useState<string | null>(null);
  const [searched, setSearched] = useState(false);
  const [selectedUser, setSelectedUser] = useState<PrincipalHit | null>(null);
  const [toolValue, setToolValue] = useState<string>(ALL_TOOLS);

  // Effect (Allow/Deny), the all-users Deny scope ("Everyone"), and the typed
  // parameter conditions appended to the policy. Conditions are only meaningful
  // for a specific tool (the param schema must be known) — All tools drops them.
  const [effect, setEffect] = useState<CedarEffect>('allow');
  const [allUsers, setAllUsers] = useState(false);
  const [conditions, setConditions] = useState<CedarCondition[]>([]);

  // Two-step warning gate: when the engine does not yet exist (mode 'none'), the
  // first add — and switching INTO enforce — must clear an explicit default-deny
  // confirm. We model it as a pending intent the confirm panel resolves.
  const [armedWarning, setArmedWarning] = useState<null | { kind: 'add' } | { kind: 'enforce' }>(null);

  // Per-row expanded raw-Cedar view.
  const [expandedId, setExpandedId] = useState<string | null>(null);

  // Focus-return-to-trigger when the picker closes.
  const addTriggerRef = useRef<HTMLButtonElement>(null);
  const searchInputRef = useRef<HTMLInputElement>(null);

  // Unmount guard — the reconcile refetch fires on a timer, so it can land after
  // the user navigates away.
  const mountedRef = useRef(true);
  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
    };
  }, []);

  const resetForm = useCallback(() => {
    setQuery('');
    setResults([]);
    setSearched(false);
    setSearchError(null);
    setSelectedUser(null);
    setToolValue(ALL_TOOLS);
    setEffect('allow');
    setAllUsers(false);
    setConditions([]);
    setArmedWarning(null);
  }, []);

  const closePicker = useCallback(() => {
    setPicking(false);
    setActionError(null);
    resetForm();
    window.setTimeout(() => addTriggerRef.current?.focus(), 0);
  }, [resetForm]);

  // Close picker on Escape while it is open.
  useEffect(() => {
    if (!picking) return;
    const onKeyDown = (e: KeyboardEvent) => {
      if (e.key === 'Escape') {
        e.preventDefault();
        closePicker();
      }
    };
    document.addEventListener('keydown', onKeyDown);
    return () => document.removeEventListener('keydown', onKeyDown);
  }, [picking, closePicker]);

  // Focus the search box when the picker opens OR when the user steps back
  // (selectedUser → null) from the tool-select step to the search step.
  // Mirrors the existing open-focus idiom: window.setTimeout(..., 0) runs after
  // the re-render so the input is in the DOM before focus is attempted.
  useEffect(() => {
    if (picking && !selectedUser && !allUsers) window.setTimeout(() => searchInputRef.current?.focus(), 0);
  }, [picking, selectedUser, allUsers]);

  // Silent reconcile fetch after a mutate (absorb Policy Engine settle). A failed
  // reconcile leaves the optimistic state in place — the next action or remount
  // resyncs.
  const reconcile = useCallback(async () => {
    try {
      const fresh = await cedarPoliciesApi.list(mcpId);
      if (mountedRef.current) {
        setPolicies(fresh.policies);
        setEnforcementMode(fresh.enforcement_mode);
      }
    } catch {
      // swallow — background refetch error must not clobber the UI.
    }
  }, [mcpId]);

  // Initial load (skipped until the gateway is wired — nothing to attach against).
  useEffect(() => {
    if (!gatewayReady) {
      setLoading(false);
      return;
    }
    let cancelled = false;
    setLoading(true);
    setLoadError(null);
    cedarPoliciesApi
      .list(mcpId)
      .then((res) => {
        if (cancelled) return;
        setPolicies(res.policies);
        setEnforcementMode(res.enforcement_mode);
      })
      .catch((err: unknown) => {
        if (!cancelled) setLoadError(err instanceof Error ? err.message : 'Failed to load policies.');
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [mcpId, gatewayReady]);

  // Debounced Entra USER search (filtered to type==='user' — Cedar keys on a
  // user's oid). Each keystroke cancels the prior in-flight debounce.
  useEffect(() => {
    if (!picking) return;
    const q = query.trim();
    if (q.length < MIN_QUERY) {
      setResults([]);
      setSearched(false);
      setSearchError(null);
      setSearching(false);
      return;
    }
    let cancelled = false;
    setSearching(true);
    setSearchError(null);
    const handle = window.setTimeout(async () => {
      try {
        const hits = await principalsApi.search(q);
        if (cancelled) return;
        setResults(hits.filter((h) => h.type === 'user'));
      } catch (err: unknown) {
        if (cancelled) return;
        setSearchError(err instanceof Error ? err.message : 'Search failed.');
        setResults([]);
      } finally {
        if (!cancelled) {
          setSearching(false);
          setSearched(true);
        }
      }
    }, DEBOUNCE_MS);
    return () => {
      cancelled = true;
      window.clearTimeout(handle);
    };
  }, [picking, query]);

  // Tool options for the select — the gateway's discovered tools + "All tools".
  const toolOptions = useMemo(
    () => (mcp.available_tools ?? []).map((t) => t.name),
    [mcp.available_tools],
  );

  // Whether the picked scope is the whole gateway (no specific tool). Conditions
  // are hidden for All tools (the param schema is unknown) and never sent.
  const allTools = toolValue === ALL_TOOLS;

  // Top-level params of the selected tool (number/string only — 'other' types
  // have no legal operators, so they're dropped from the condition builder).
  // Empty for All tools or a tool with no usable params → the builder is hidden.
  const toolParams = useMemo(() => {
    const t = (mcp.available_tools ?? []).find((tool) => tool.name === toolValue);
    return t ? paramsFromSchema(t.input_schema).filter((p) => p.type !== 'other') : [];
  }, [toolValue, mcp.available_tools]);

  // Add a condition row defaulted to the first usable param + its first legal op.
  const addCondition = useCallback(() => {
    const first = toolParams[0];
    if (!first) return;
    const op = operatorsForType(first.type)[0]?.value ?? '=';
    setConditions((prev) => [...prev, { param: first.name, op, value: '', type: first.type }]);
  }, [toolParams]);

  const removeCondition = useCallback((idx: number) => {
    setConditions((prev) => prev.filter((_, i) => i !== idx));
  }, []);

  // Re-default op + type and clear the value when the row's param changes, so a
  // stale string op can't sit on a numeric param (and vice versa).
  const changeConditionParam = useCallback(
    (idx: number, paramName: string) => {
      const param = toolParams.find((p) => p.name === paramName);
      if (!param) return;
      const op = operatorsForType(param.type)[0]?.value ?? '=';
      setConditions((prev) =>
        prev.map((c, i) => (i === idx ? { param: param.name, op, value: '', type: param.type } : c)),
      );
    },
    [toolParams],
  );

  const changeConditionOp = useCallback((idx: number, op: string) => {
    setConditions((prev) => prev.map((c, i) => (i === idx ? { ...c, op } : c)));
  }, []);

  const changeConditionValue = useCallback((idx: number, value: string) => {
    setConditions((prev) => prev.map((c, i) => (i === idx ? { ...c, value } : c)));
  }, []);

  // -- mutations ------------------------------------------------------------

  // Whether the engine does not yet exist (the first policy / first enforce must
  // clear the default-deny warning).
  const engineUnattached = enforcementMode === 'none';

  // Actually POST the policy (called once any required warning is cleared). The
  // principal is the picked user, or null for an all-users Deny ("Everyone").
  // Conditions only ride along on a specific tool (buildAddPolicyBody drops them
  // for All tools too — belt and braces).
  const submitAdd = useCallback(async () => {
    if (actionPending || (!selectedUser && !allUsers)) return;
    setActionPending(true);
    setActionError(null);
    const body = buildAddPolicyBody(
      allUsers ? null : selectedUser,
      allTools ? null : toolValue,
      allTools,
      effect,
      allTools ? [] : conditions,
    );
    try {
      const created = await cedarPoliciesApi.add(mcpId, body);
      if (!mountedRef.current) return;
      // Optimistically insert the authoritative row the server returned.
      setPolicies((prev) => {
        if (prev.some((p) => p.policy_id === created.policy_id)) return prev;
        return [...prev, created];
      });
      // The first policy attaches the engine in ENFORCE — reflect that locally so
      // the banner + the warning gate update without waiting for the reconcile.
      if (engineUnattached) setEnforcementMode('enforce');
      closePicker();
      window.setTimeout(() => {
        void reconcile();
      }, REPLICATION_RECONCILE_MS);
    } catch (err: unknown) {
      if (mountedRef.current) {
        setActionError(err instanceof Error ? err.message : 'Failed to add policy.');
        setArmedWarning(null);
      }
    } finally {
      if (mountedRef.current) setActionPending(false);
    }
  }, [actionPending, selectedUser, allUsers, allTools, toolValue, effect, conditions, mcpId, engineUnattached, closePicker, reconcile]);

  // Submit-disabled gate. A row is incomplete when its param or value is empty.
  // Allow always needs a user; Deny needs a user OR "Everyone"; an unconditional
  // all-users + all-tools Deny is rejected (would block the whole gateway).
  const conditionsIncomplete = !allTools && conditions.some(
    (c) => !c.param || c.value.trim() === '' || (c.type === 'number' && !/^-?\d+$/.test(c.value.trim())),
  );
  const submitDisabled =
    actionPending ||
    (effect === 'allow' && !selectedUser) ||
    (effect === 'deny' && !selectedUser && !allUsers) ||
    conditionsIncomplete ||
    (effect === 'deny' && allUsers && allTools && conditions.length === 0);

  // Submit handler — gate behind the default-deny warning on the first add.
  const handleAddSubmit = useCallback(() => {
    if (!selectedUser && !allUsers) return;
    if (engineUnattached) {
      setArmedWarning({ kind: 'add' });
      return;
    }
    void submitAdd();
  }, [selectedUser, allUsers, engineUnattached, submitAdd]);

  const handleRemove = useCallback(
    async (row: CedarPolicyRow) => {
      if (actionPending) return;
      setActionPending(true);
      setActionError(null);
      const prev = policies;
      setPolicies((cur) => cur.filter((p) => p.policy_id !== row.policy_id));
      if (expandedId === row.policy_id) setExpandedId(null);
      try {
        await cedarPoliciesApi.remove(mcpId, row.policy_id);
        window.setTimeout(() => {
          void reconcile();
        }, REPLICATION_RECONCILE_MS);
      } catch (err: unknown) {
        if (mountedRef.current) {
          setPolicies(prev); // rollback
          setActionError(err instanceof Error ? err.message : 'Failed to remove policy.');
        }
      } finally {
        if (mountedRef.current) setActionPending(false);
      }
    },
    [actionPending, policies, expandedId, mcpId, reconcile],
  );

  // Apply an enforcement-mode change (Enforce / Log only / Disable). Switching
  // INTO enforce while the engine is unattached is gated behind the same
  // default-deny warning.
  const applyEnforcement = useCallback(
    async (mode: CedarEnforcementMode | 'disabled') => {
      if (actionPending) return;
      setActionPending(true);
      setActionError(null);
      setArmedWarning(null);
      try {
        const res = await cedarPoliciesApi.setEnforcement(mcpId, mode);
        if (mountedRef.current) setEnforcementMode(res.enforcement_mode);
        window.setTimeout(() => {
          void reconcile();
        }, REPLICATION_RECONCILE_MS);
      } catch (err: unknown) {
        if (mountedRef.current) {
          setActionError(err instanceof Error ? err.message : 'Failed to change enforcement.');
        }
      } finally {
        if (mountedRef.current) setActionPending(false);
      }
    },
    [actionPending, mcpId, reconcile],
  );

  const handleModeClick = useCallback(
    (mode: CedarEnforcementMode | 'disabled') => {
      if (mode === enforcementMode) return; // no-op on the active mode
      // Switching into enforce while nothing is attached flips the gateway to
      // default-deny — gate it behind the warning.
      if (mode === 'enforce' && engineUnattached) {
        setArmedWarning({ kind: 'enforce' });
        return;
      }
      void applyEnforcement(mode);
    },
    [enforcementMode, engineUnattached, applyEnforcement],
  );

  // Resolve the armed default-deny warning (confirm → the underlying action).
  const confirmWarning = useCallback(() => {
    if (!armedWarning) return;
    if (armedWarning.kind === 'add') {
      void submitAdd();
    } else {
      void applyEnforcement('enforce');
    }
  }, [armedWarning, submitAdd, applyEnforcement]);

  // -- render ---------------------------------------------------------------

  // Gateway not wired yet → a single guidance banner, no policy UI.
  if (!gatewayReady) {
    return (
      <div className={`${CARD} p-6`}>
        <h2 className="text-sm font-semibold text-slate-800">Gateway identity not provisioned</h2>
        <p className="text-sm text-slate-500 mt-1">
          This gateway's identity is not provisioned yet — provision it on the Connected Agents tab
          before adding policies. Cedar needs the gateway wired to attach a policy engine.
        </p>
      </div>
    );
  }

  const banner = ENFORCEMENT_COPY[enforcementMode];

  // The persistent ENFORCE consequence note (E36/T10). The banner above states the posture;
  // this states what it costs — every uncovered call on the gateway is denied in AWS, and
  // that deny never reaches this console. Copy lives in `cedarPosture.ts` because it is the
  // same product decision as the confirm dialog below, and only the `.ts` is vitest-gated.
  const postureNote = postureWarning(enforcementMode);

  // Segmented mode control descriptors. Disable maps to the 'disabled' wire value.
  const MODES: { key: CedarEnforcementMode | 'disabled'; label: string; active: boolean; activeCls: string }[] = [
    { key: 'enforce', label: 'Enforce', active: enforcementMode === 'enforce', activeCls: 'bg-emerald-600 text-white' },
    { key: 'log_only', label: 'Log only', active: enforcementMode === 'log_only', activeCls: 'bg-amber-500 text-white' },
    { key: 'disabled', label: 'Disable', active: enforcementMode === 'none', activeCls: 'bg-slate-600 text-white' },
  ];

  return (
    <div className="space-y-4">
      {/* ----- Enforcement banner + mode control ----- */}
      <div className={`${CARD} ${banner.ring} p-5`}>
        <div className="flex items-start justify-between gap-4 flex-wrap">
          <div className="flex items-start gap-3 min-w-0">
            <span aria-hidden="true" className={`mt-1 h-2.5 w-2.5 rounded-full shrink-0 ${banner.dot}`} />
            <div className="min-w-0">
              <h2 className="text-sm font-semibold text-slate-800">{banner.heading}</h2>
              <p className="text-sm text-slate-500 mt-0.5">{banner.body}</p>
            </div>
          </div>

          {canManage && (
            <div
              role="group"
              aria-label="Enforcement mode"
              className="shrink-0 inline-flex items-center gap-1 p-1 bg-slate-100/80 rounded-lg"
            >
              {MODES.map((m) => (
                <button
                  key={m.key}
                  type="button"
                  onClick={() => handleModeClick(m.key)}
                  disabled={actionPending || m.active}
                  aria-pressed={m.active}
                  className={`px-3 py-1 rounded-md text-xs font-medium transition-colors disabled:cursor-default ${
                    m.active ? m.activeCls : 'text-slate-500 hover:text-slate-800 hover:bg-white/70 disabled:opacity-100'
                  }`}
                >
                  {m.label}
                </button>
              ))}
            </div>
          )}
        </div>

        {/* Default-deny warning gate — shown when arming the first attach (add or
            switching into enforce while unattached). */}
        {armedWarning && (
          <div className="mt-4 rounded-lg border border-amber-200 bg-amber-50/70 p-4" role="alert">
            <h3 className="text-sm font-semibold text-amber-800">Switch this gateway to default-deny?</h3>
            <p className="text-sm text-amber-700 mt-1">
              Applying a policy engine switches this gateway to default-deny — only users named in
              policies will be able to call its tools. Agents and users not covered by a policy will be
              blocked.
            </p>
            <div className="mt-3 flex items-center gap-2">
              <button
                type="button"
                onClick={confirmWarning}
                disabled={actionPending}
                className="px-3.5 py-1.5 rounded-lg bg-amber-600 text-white text-sm font-medium hover:bg-amber-700 transition-colors disabled:opacity-40"
              >
                {actionPending ? 'Applying…' : armedWarning.kind === 'add' ? 'Apply policy & enforce' : 'Enable enforcement'}
              </button>
              <button
                type="button"
                onClick={() => setArmedWarning(null)}
                disabled={actionPending}
                className="px-3.5 py-1.5 rounded-lg bg-white border border-slate-300 text-slate-700 text-sm font-medium hover:bg-slate-50 transition-colors disabled:opacity-40"
              >
                Cancel
              </button>
            </div>
          </div>
        )}

        {/* Persistent ENFORCE consequence note. Shares the warning-gate's slot and amber
            treatment, and the two can never collide: the gate only arms while the engine is
            unattached (mode 'none'), and this only renders for 'enforce'. `role="status"`
            (polite), not "alert" — it is present on every visit to an enforcing gateway, so
            an assertive announcement would be noise. The ⚠ glyph carries the warning
            without relying on colour. */}
        {postureNote && (
          <div
            role="status"
            className="mt-4 flex items-start gap-2.5 rounded-lg border border-amber-200 bg-amber-50/70 p-3.5"
          >
            <span aria-hidden="true" className="shrink-0 text-sm leading-5 text-amber-600">
              ⚠
            </span>
            <p className="text-sm text-amber-800 leading-relaxed">{postureNote}</p>
          </div>
        )}
      </div>

      {/* ----- Policies card ----- */}
      <div className={`${CARD} overflow-hidden`}>
        {/* Card header: title + Add. */}
        <div className="flex items-center justify-between gap-3 px-5 py-4 border-b border-slate-200/60">
          <div>
            <h2 className="text-sm font-semibold text-slate-800">Cedar policies</h2>
            <p className="text-xs text-slate-400 mt-0.5">
              Per-user and all-users allow / deny rules — optionally conditioned on tool arguments —
              evaluated by the gateway's policy engine.
            </p>
          </div>
          {canManage && !picking && (
            <button
              ref={addTriggerRef}
              type="button"
              onClick={() => {
                setPicking(true);
                setActionError(null);
                resetForm();
              }}
              className="shrink-0 px-3.5 py-1.5 rounded-lg bg-blue-600 text-white text-sm font-medium hover:bg-blue-700 transition-colors"
            >
              + Add Policy
            </button>
          )}
        </div>

        {/* Add-policy form (inline, above the table). */}
        {picking && (
          <div className="px-5 py-4 border-b border-slate-200/60 bg-slate-50/50">
            <div className="flex items-center justify-between gap-3 mb-3">
              <div className="flex items-center gap-3 min-w-0">
                <h3 className="text-sm font-semibold text-slate-800">
                  {effect === 'allow' ? 'Allow a user to call a tool' : 'Deny a tool call'}
                </h3>
                {/* Allow / Deny segmented control — emerald active for Allow, red for
                    Deny (mirrors the enforcement MODES segmented styling). Switching to
                    Allow clears the all-users "Everyone" scope (an all-users permit is
                    invalid). */}
                <div
                  role="group"
                  aria-label="Policy effect"
                  className="shrink-0 inline-flex items-center gap-1 p-1 bg-slate-100/80 rounded-lg"
                >
                  {([
                    { key: 'allow' as const, label: 'Allow', activeCls: 'bg-emerald-600 text-white' },
                    { key: 'deny' as const, label: 'Deny', activeCls: 'bg-red-600 text-white' },
                  ]).map((e) => {
                    const active = effect === e.key;
                    return (
                      <button
                        key={e.key}
                        type="button"
                        onClick={() => {
                          if (active) return;
                          setEffect(e.key);
                          setArmedWarning(null);
                          if (e.key === 'allow') setAllUsers(false);
                        }}
                        aria-pressed={active}
                        className={`px-3 py-1 rounded-md text-xs font-medium transition-colors ${
                          active ? e.activeCls : 'text-slate-500 hover:text-slate-800 hover:bg-white/70'
                        }`}
                      >
                        {e.label}
                      </button>
                    );
                  })}
                </div>
              </div>
              <button
                type="button"
                onClick={closePicker}
                aria-label="Close add policy"
                className="shrink-0 inline-flex items-center justify-center h-6 w-6 rounded-md text-slate-400 hover:text-slate-700 hover:bg-slate-100 transition-colors"
              >
                <span aria-hidden="true" className="text-base leading-none">×</span>
              </button>
            </div>

            {/* Step 1: pick an Entra user (or "Everyone" for a Deny). */}
            {!selectedUser && !allUsers ? (
              <>
                <label htmlFor="cedar-user-search" className="sr-only">
                  Search Entra users
                </label>
                <input
                  id="cedar-user-search"
                  ref={searchInputRef}
                  type="text"
                  value={query}
                  onChange={(e) => setQuery(e.target.value)}
                  placeholder="Search Entra users by name…"
                  className="w-full px-3 py-2 text-sm rounded-lg border border-slate-300 focus:outline-none focus:ring-2 focus:ring-blue-500/40"
                  autoComplete="off"
                />
                {/* "Everyone" — an all-users guardrail Deny. Offered only for Deny
                    (an all-users permit would defeat default-deny). Advances to Step 2. */}
                {effect === 'deny' && (
                  <button
                    type="button"
                    onClick={() => {
                      setAllUsers(true);
                      setToolValue(ALL_TOOLS);
                      setConditions([]);
                    }}
                    className="mt-3 w-full flex items-center gap-3 px-3 py-2 rounded-lg text-left border border-violet-200 bg-violet-50/60 hover:bg-violet-50 transition-colors"
                  >
                    <span aria-hidden="true" className="inline-flex items-center justify-center h-7 w-7 rounded-full bg-violet-100 text-violet-700 text-sm font-semibold">
                      ∀
                    </span>
                    <span className="min-w-0 flex-1">
                      <span className="block text-sm font-medium text-slate-800">Everyone (all users)</span>
                      <span className="block text-xs text-slate-500">
                        Guardrail deny — blocks every user when the condition holds.
                      </span>
                    </span>
                  </button>
                )}
                <div className="mt-3">
                  {searchError && (
                    <p className="text-sm text-red-600" role="alert">{searchError}</p>
                  )}
                  {!searchError && searching && (
                    <p className="text-sm text-slate-400">Searching…</p>
                  )}
                  {!searchError && !searching && query.trim().length > 0 && query.trim().length < MIN_QUERY && (
                    <p className="text-sm text-slate-400">Type at least {MIN_QUERY} characters to search.</p>
                  )}
                  {!searchError && !searching && searched && results.length === 0 && query.trim().length >= MIN_QUERY && (
                    <p className="text-sm text-slate-400">No users match “{query.trim()}”.</p>
                  )}
                  {!searchError && !searching && results.length > 0 && (
                    <ul className="max-h-64 overflow-y-auto -mx-1 divide-y divide-slate-100">
                      {results.map((hit) => (
                        <li key={hit.id}>
                          <button
                            type="button"
                            onClick={() => {
                              setSelectedUser(hit);
                              setToolValue(ALL_TOOLS);
                            }}
                            className="w-full flex items-center gap-3 px-2 py-2 rounded-lg text-left hover:bg-white transition-colors"
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
                            <span className="shrink-0 inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium bg-blue-50 text-blue-700">
                              User
                            </span>
                          </button>
                        </li>
                      ))}
                    </ul>
                  )}
                </div>
              </>
            ) : (
              /* Step 2: chosen scope → tool select + (specific-tool) conditions + submit. */
              <div>
                {/* Chosen-scope chip — a picked user, or "Everyone" for an all-users Deny. */}
                <div className="flex items-center gap-2 mb-3 text-sm text-slate-600">
                  <span className="text-slate-400">{effect === 'allow' ? 'Allowing' : 'Denying'}</span>
                  {allUsers ? (
                    <>
                      <span aria-hidden="true" className="inline-flex items-center justify-center h-6 w-6 rounded-full bg-violet-100 text-violet-700 text-xs font-semibold">
                        ∀
                      </span>
                      <span className="font-medium text-slate-800">Everyone</span>
                      <span className="shrink-0 inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium bg-violet-50 text-violet-700">
                        All users
                      </span>
                    </>
                  ) : (
                    <>
                      <AgentAvatar name={selectedUser!.display_name} size="sm" />
                      <span className="font-medium text-slate-800 truncate">{selectedUser!.display_name}</span>
                      <span className="shrink-0 inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium bg-blue-50 text-blue-700">
                        User
                      </span>
                    </>
                  )}
                </div>

                {/* Tool select. Changing the tool clears any conditions (their param
                    schema no longer applies). */}
                <div className="flex items-end gap-2 flex-wrap">
                  <div>
                    <label htmlFor="cedar-tool" className="block text-[11px] uppercase tracking-wide text-slate-400 font-medium mb-1">
                      Tool
                    </label>
                    <select
                      id="cedar-tool"
                      value={toolValue}
                      onChange={(e) => {
                        setToolValue(e.target.value);
                        setConditions([]);
                      }}
                      className="px-3 py-1.5 text-sm rounded-lg border border-slate-300 bg-white focus:outline-none focus:ring-2 focus:ring-blue-500/40"
                    >
                      <option value={ALL_TOOLS}>All tools on this gateway</option>
                      {toolOptions.map((name) => (
                        <option key={name} value={name}>{name}</option>
                      ))}
                    </select>
                  </div>
                </div>

                {/* Condition builder — only for a specific tool with usable params.
                    Each row: param select → operator select → typed value → remove. */}
                {!allTools && toolParams.length > 0 && (
                  <div className="mt-4">
                    <div className="flex items-center justify-between gap-2 mb-1.5">
                      <span className="block text-[11px] uppercase tracking-wide text-slate-400 font-medium">
                        Conditions {conditions.length > 0 && <span className="text-slate-300 normal-case">(all must hold)</span>}
                      </span>
                    </div>
                    {conditions.length > 0 && (
                      <ul className="space-y-2">
                        {conditions.map((c, idx) => {
                          const param = toolParams.find((p) => p.name === c.param);
                          const ops = operatorsForType(param?.type ?? c.type);
                          const numeric = (param?.type ?? c.type) === 'number';
                          return (
                            <li key={idx} className="flex items-center gap-2 flex-wrap">
                              <label className="sr-only" htmlFor={`cedar-cond-param-${idx}`}>Parameter</label>
                              <select
                                id={`cedar-cond-param-${idx}`}
                                value={c.param}
                                onChange={(e) => changeConditionParam(idx, e.target.value)}
                                className="px-2.5 py-1.5 text-sm rounded-lg border border-slate-300 bg-white focus:outline-none focus:ring-2 focus:ring-blue-500/40"
                              >
                                {toolParams.map((p) => (
                                  <option key={p.name} value={p.name}>{p.name} ({p.type})</option>
                                ))}
                              </select>
                              <label className="sr-only" htmlFor={`cedar-cond-op-${idx}`}>Operator</label>
                              <select
                                id={`cedar-cond-op-${idx}`}
                                value={c.op}
                                onChange={(e) => changeConditionOp(idx, e.target.value)}
                                className="px-2.5 py-1.5 text-sm rounded-lg border border-slate-300 bg-white focus:outline-none focus:ring-2 focus:ring-blue-500/40"
                              >
                                {ops.map((o) => (
                                  <option key={o.value} value={o.value}>{o.label}</option>
                                ))}
                              </select>
                              <label className="sr-only" htmlFor={`cedar-cond-value-${idx}`}>Value</label>
                              <div className="flex flex-col gap-0.5">
                                <input
                                  id={`cedar-cond-value-${idx}`}
                                  type={numeric ? 'number' : 'text'}
                                  step={numeric ? '1' : undefined}
                                  inputMode={numeric ? 'numeric' : undefined}
                                  value={c.value}
                                  onChange={(e) => changeConditionValue(idx, e.target.value)}
                                  placeholder={numeric ? '0' : 'value'}
                                  className="w-32 px-2.5 py-1.5 text-sm rounded-lg border border-slate-300 bg-white focus:outline-none focus:ring-2 focus:ring-blue-500/40"
                                />
                                {numeric && c.value.trim() !== '' && !/^-?\d+$/.test(c.value.trim()) && (
                                  <span className="text-[11px] text-amber-600 leading-none">whole numbers only</span>
                                )}
                              </div>
                              <button
                                type="button"
                                onClick={() => removeCondition(idx)}
                                aria-label={`Remove condition ${idx + 1}`}
                                className="shrink-0 inline-flex items-center justify-center h-7 w-7 rounded-md text-slate-400 hover:text-red-600 hover:bg-red-50 transition-colors"
                              >
                                <span aria-hidden="true" className="text-base leading-none">×</span>
                              </button>
                            </li>
                          );
                        })}
                      </ul>
                    )}
                    <button
                      type="button"
                      onClick={addCondition}
                      className={`inline-flex items-center gap-1 px-2.5 py-1 rounded-md text-xs font-medium text-blue-700 hover:bg-blue-50 transition-colors ${conditions.length > 0 ? 'mt-2' : ''}`}
                    >
                      <span aria-hidden="true" className="text-sm leading-none">+</span>
                      Add condition
                    </button>
                  </div>
                )}

                {/* Submit + Back. */}
                <div className="flex items-center gap-2 flex-wrap mt-4">
                  <button
                    type="button"
                    onClick={handleAddSubmit}
                    disabled={submitDisabled || !!armedWarning}
                    className={`px-3.5 py-1.5 rounded-lg text-white text-sm font-medium transition-colors disabled:opacity-40 ${
                      effect === 'allow' ? 'bg-emerald-600 hover:bg-emerald-700' : 'bg-red-600 hover:bg-red-700'
                    }`}
                  >
                    {actionPending
                      ? effect === 'allow' ? 'Allowing…' : 'Denying…'
                      : effect === 'allow' ? 'Allow access' : 'Deny access'}
                  </button>
                  <button
                    type="button"
                    onClick={() => {
                      setSelectedUser(null);
                      setAllUsers(false);
                      setConditions([]);
                      setArmedWarning(null);
                    }}
                    className="px-3.5 py-1.5 rounded-lg bg-white border border-slate-300 text-slate-700 text-sm font-medium hover:bg-slate-50 transition-colors"
                  >
                    Back
                  </button>
                </div>
                {engineUnattached && !armedWarning && (
                  <p className="text-xs text-amber-700 mt-3">
                    This is the first policy — applying it attaches the policy engine and switches the
                    gateway to default-deny.
                  </p>
                )}
              </div>
            )}
          </div>
        )}

        {/* Mutation error (e.g. a 422 from the engine) — non-blocking. */}
        {actionError && (
          <p className="px-5 pt-3 text-sm text-red-600" role="alert">{actionError}</p>
        )}

        {/* Body: loading / error / empty / table. */}
        {loading ? (
          <div className="px-5 py-8 text-center">
            <div className="inline-flex items-center gap-2.5 text-sm text-slate-400">
              <Spinner /> Loading policies…
            </div>
          </div>
        ) : loadError ? (
          <div className="px-5 py-8 text-center">
            <p className="text-sm text-red-600" role="alert">{loadError}</p>
          </div>
        ) : policies.length === 0 ? (
          <div className="px-5 py-10 text-center">
            <p className="text-sm font-medium text-slate-600">No policies yet. Add one to restrict tool access.</p>
            <p className="text-xs text-slate-400 mt-1">
              With no policy engine attached, every assigned user has full access to this gateway's tools.
            </p>
          </div>
        ) : (
          <ul className="divide-y divide-slate-100">
            {policies.map((row) => {
              const expanded = expandedId === row.policy_id;
              const allTools = !row.tool;
              const label = policyToolLabel(row);
              const isDeny = row.effect === 'deny';
              // An all-users guardrail row: managed by us, no oid, but a label
              // ("Everyone"). Foreign/headerless rows (managed === false) keep the
              // plain "—" + raw-Cedar treatment with no chips.
              const isEveryone = row.managed === true && !row.user_oid && !!row.user_label;
              const rowConditions = row.conditions ?? [];
              return (
                <li key={row.policy_id} className="px-5 py-3">
                  <div className="flex items-center gap-3">
                    {/* User cell — "Everyone" gets a distinct violet treatment. */}
                    {isEveryone ? (
                      <span aria-hidden="true" className="shrink-0 inline-flex items-center justify-center h-8 w-8 rounded-full bg-violet-100 text-violet-700 text-sm font-semibold">
                        ∀
                      </span>
                    ) : (
                      <AgentAvatar name={row.user_label ?? '—'} size="sm" />
                    )}
                    <div className="min-w-0 flex-1">
                      {isEveryone ? (
                        <span className="inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium bg-violet-50 text-violet-700">
                          Everyone
                        </span>
                      ) : (
                        <>
                          <span className="block text-sm font-medium text-slate-800 truncate">
                            {row.user_label ? emailAlias(row.user_label) : '—'}
                          </span>
                          {row.user_label && row.user_label.includes('@') && (
                            <span className="block text-xs text-slate-400 truncate">{row.user_label}</span>
                          )}
                        </>
                      )}
                    </div>

                    {/* Condition chips — one small pill per condition (managed rows only). */}
                    {rowConditions.length > 0 && (
                      <span className="hidden sm:flex shrink min-w-0 items-center gap-1 flex-wrap justify-end">
                        {rowConditions.map((c, i) => (
                          <span
                            key={i}
                            className="inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium bg-amber-50 text-amber-700 font-mono"
                          >
                            {conditionLabel(c)}
                          </span>
                        ))}
                      </span>
                    )}

                    {/* Tool pill — All tools is violet (gateway-wide), a specific tool is slate. */}
                    <span
                      className={`shrink-0 inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium ${
                        allTools ? 'bg-violet-50 text-violet-700' : 'bg-slate-100 text-slate-600'
                      }`}
                    >
                      {label}
                    </span>

                    {/* Effect badge — Allow = emerald, Deny = red (from row.effect). */}
                    <span
                      className={`shrink-0 inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium ${
                        isDeny ? 'bg-red-50 text-red-700' : 'bg-emerald-50 text-emerald-700'
                      }`}
                    >
                      {isDeny ? 'Deny' : 'Allow'}
                    </span>

                    {/* View Cedar toggle. */}
                    <button
                      type="button"
                      onClick={() => setExpandedId(expanded ? null : row.policy_id)}
                      aria-expanded={expanded}
                      aria-label={expanded ? 'Hide Cedar source' : 'View Cedar source'}
                      className="shrink-0 inline-flex items-center gap-1 px-2 py-1 rounded-md text-xs font-medium text-slate-500 hover:text-slate-800 hover:bg-slate-100 transition-colors"
                    >
                      <span aria-hidden="true" className={`inline-block transition-transform ${expanded ? 'rotate-90' : ''}`}>›</span>
                      Cedar
                    </button>

                    {/* Remove ✕ (Operator+). */}
                    {canManage && (
                      <button
                        type="button"
                        onClick={() => handleRemove(row)}
                        disabled={actionPending}
                        aria-label={`Remove policy for ${row.user_label ?? 'unknown user'} on ${label}`}
                        className="shrink-0 inline-flex items-center justify-center h-7 w-7 rounded-md text-slate-400 hover:text-red-600 hover:bg-red-50 transition-colors disabled:opacity-40"
                      >
                        <span aria-hidden="true" className="text-base leading-none">×</span>
                      </button>
                    )}
                  </div>

                  {/* Expandable raw Cedar. */}
                  {expanded && (
                    <pre className="mt-2 ml-10 overflow-x-auto rounded-lg bg-slate-900 p-3 text-xs leading-relaxed text-slate-100">
                      {row.cedar_text}
                    </pre>
                  )}
                </li>
              );
            })}
          </ul>
        )}
      </div>
    </div>
  );
}
