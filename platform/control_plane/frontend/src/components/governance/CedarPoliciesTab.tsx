import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { cedarPoliciesApi, principalsApi } from '../../api/client';
import type {
  CedarPolicyRow,
  CedarPolicySet,
  ConditionIn,
  EnforcementRequest,
  McpServer,
  PrincipalHit,
} from '../../api/client';
import { useLoad } from '../../api/useLoad';
import { AgentAvatar } from './agentUi';
import { emailAlias } from './agentBadges';
import {
  buildAddPolicyBody,
  conditionLabel,
  operatorsForType,
  paramsFromSchema,
  policyToolLabel,
  isContainmentRow,
  isWholeGatewayDeny,
} from './cedarPolicyForm';
import { ENFORCEMENT_COPY, ENFORCE_FLIP_WARNING, postureWarning } from './cedarPosture';
import { useReturnFocus } from '../shared/useReturnFocus';

const CARD = 'bg-white/70 backdrop-blur rounded-xl border border-slate-200/60 shadow-sm';

const POLICY_ENGINE_SETTLE_MS = 1500;

const ALL_TOOLS = '__ALL__';
const MIN_QUERY = 2;
const DEBOUNCE_MS = 300;

const EMPTY_POLICIES: CedarPolicyRow[] = [];

function Spinner() {
  return (
    <span
      aria-hidden="true"
      className="inline-block h-4 w-4 rounded-full border-2 border-slate-200 border-t-blue-600 animate-spin"
    />
  );
}

const ENFORCEMENT_STYLE: Record<McpServer['cedar_enforcement_mode'], { ring: string; dot: string }> = {
  none: { ring: 'border-slate-200/70', dot: 'bg-slate-400' },
  log_only: { ring: 'border-amber-200/80', dot: 'bg-amber-500' },
  enforce: { ring: 'border-emerald-200/80', dot: 'bg-emerald-500' },
};

export default function CedarPoliciesTab({
  mcp,
  canManage,
}: {
  mcp: McpServer;
  canManage: boolean;
}) {
  const mcpId = mcp.id;

  const gatewayReady = mcp.identity_status === 'provisioned' && !!mcp.gateway_id;

  const { state: policyState, patch: patchPolicies } = useLoad<CedarPolicySet | null>(
    () => (gatewayReady ? cedarPoliciesApi.list(mcpId) : Promise.resolve(null)),
    [mcpId, gatewayReady],
    'Failed to load policies.',
  );
  const policies = policyState.data?.policies ?? EMPTY_POLICIES;
  const enforcementMode: McpServer['cedar_enforcement_mode'] =
    policyState.data?.enforcement_mode ?? mcp.cedar_enforcement_mode;
  const loading = policyState.kind === 'loading';
  const loadError = policyState.kind === 'error' ? policyState.message : null;

  const [actionPending, setActionPending] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);

  const [picking, setPicking] = useState(false);
  const [query, setQuery] = useState('');
  const [settledSearch, setSettledSearch] = useState<{
    query: string;
    hits: PrincipalHit[];
    error: string | null;
  } | null>(null);
  const [selectedUser, setSelectedUser] = useState<PrincipalHit | null>(null);
  const [toolValue, setToolValue] = useState<string>(ALL_TOOLS);

  const [effect, setEffect] = useState<CedarPolicyRow['effect']>('allow');
  const [allUsers, setAllUsers] = useState(false);
  const [conditions, setConditions] = useState<ConditionIn[]>([]);

  const [armedWarning, setArmedWarning] = useState<null | { kind: 'add' } | { kind: 'enforce' }>(null);

  const [expandedId, setExpandedId] = useState<string | null>(null);

  const addTriggerRef = useRef<HTMLButtonElement>(null);
  const searchInputRef = useRef<HTMLInputElement>(null);
  const returnFocus = useReturnFocus(addTriggerRef);

  const mountedRef = useRef(true);
  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
    };
  }, []);

  const resetForm = useCallback(() => {
    setQuery('');
    setSettledSearch(null);
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
    returnFocus();
  }, [resetForm, returnFocus]);

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

  useEffect(() => {
    if (picking && !selectedUser && !allUsers) searchInputRef.current?.focus();
  }, [picking, selectedUser, allUsers]);

  const reconcile = useCallback(async () => {
    const fresh = await cedarPoliciesApi.list(mcpId).catch(() => null);
    if (fresh === null || !mountedRef.current) return;
    patchPolicies(() => fresh);
  }, [mcpId, patchPolicies]);

  // Decision: docs/adr/frontend-loading-state.md
  useEffect(() => {
    if (!picking) return;
    const q = query.trim();
    if (q.length < MIN_QUERY) return;
    let cancelled = false;
    const handle = window.setTimeout(async () => {
      try {
        const hits = await principalsApi.search(q);
        if (cancelled) return;
        setSettledSearch({ query: q, hits: hits.filter((h) => h.type === 'user'), error: null });
      } catch (err: unknown) {
        if (cancelled) return;
        setSettledSearch({ query: q, hits: [], error: err instanceof Error ? err.message : 'Search failed.' });
      }
    }, DEBOUNCE_MS);
    return () => {
      cancelled = true;
      window.clearTimeout(handle);
    };
  }, [picking, query]);

  const trimmedQuery = query.trim();
  const searchable = trimmedQuery.length >= MIN_QUERY;
  const currentSearch = searchable && settledSearch?.query === trimmedQuery ? settledSearch : null;
  const results = currentSearch?.hits ?? [];
  const searched = currentSearch !== null;
  const searching = searchable && currentSearch === null;
  const searchError = currentSearch?.error ?? null;

  const toolOptions = useMemo(
    () => (mcp.available_tools ?? []).map((t) => t.name),
    [mcp.available_tools],
  );

  const allTools = toolValue === ALL_TOOLS;

  const toolParams = useMemo(() => {
    const t = (mcp.available_tools ?? []).find((tool) => tool.name === toolValue);
    return t ? paramsFromSchema(t.input_schema).filter((p) => p.type !== 'other') : [];
  }, [toolValue, mcp.available_tools]);

  const addCondition = useCallback(() => {
    const first = toolParams[0];
    if (!first) return;
    const op = operatorsForType(first.type)[0]?.value ?? '=';
    setConditions((prev) => [...prev, { param: first.name, op, value: '', type: first.type }]);
  }, [toolParams]);

  const removeCondition = useCallback((idx: number) => {
    setConditions((prev) => prev.filter((_, i) => i !== idx));
  }, []);

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

  const engineUnattached = enforcementMode === 'none';

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
      patchPolicies((cur) =>
        cur === null
          ? cur
          : {
              ...cur,
              policies: cur.policies.some((p) => p.policy_id === created.policy_id)
                ? cur.policies
                : [...cur.policies, created],
              enforcement_mode: engineUnattached ? 'enforce' : cur.enforcement_mode,
            },
      );
      closePicker();
      window.setTimeout(() => {
        void reconcile();
      }, POLICY_ENGINE_SETTLE_MS);
    } catch (err: unknown) {
      if (mountedRef.current) {
        setActionError(err instanceof Error ? err.message : 'Failed to add policy.');
        setArmedWarning(null);
      }
    } finally {
      if (mountedRef.current) setActionPending(false);
    }
  }, [actionPending, selectedUser, allUsers, allTools, toolValue, effect, conditions, mcpId, engineUnattached, closePicker, reconcile, patchPolicies]);

  const conditionsIncomplete = !allTools && conditions.some(
    (c) => !c.param || c.value.trim() === '' || (c.type === 'number' && !/^-?\d+$/.test(c.value.trim())),
  );
  const submitDisabled =
    actionPending ||
    (effect === 'allow' && !selectedUser) ||
    (effect === 'deny' && !selectedUser && !allUsers) ||
    conditionsIncomplete ||
    isWholeGatewayDeny(effect, allUsers, allTools, conditions);

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
      patchPolicies((cur) =>
        cur === null ? cur : { ...cur, policies: cur.policies.filter((p) => p.policy_id !== row.policy_id) },
      );
      if (expandedId === row.policy_id) setExpandedId(null);
      try {
        await cedarPoliciesApi.remove(mcpId, row.policy_id);
        window.setTimeout(() => {
          void reconcile();
        }, POLICY_ENGINE_SETTLE_MS);
      } catch (err: unknown) {
        if (mountedRef.current) {
          patchPolicies((cur) => (cur === null ? cur : { ...cur, policies: prev }));
          setActionError(err instanceof Error ? err.message : 'Failed to remove policy.');
        }
      } finally {
        if (mountedRef.current) setActionPending(false);
      }
    },
    [actionPending, policies, expandedId, mcpId, reconcile, patchPolicies],
  );

  const applyEnforcement = useCallback(
    async (mode: EnforcementRequest['mode']) => {
      if (actionPending) return;
      setActionPending(true);
      setActionError(null);
      setArmedWarning(null);
      try {
        const res = await cedarPoliciesApi.setEnforcement(mcpId, mode);
        if (mountedRef.current) {
          patchPolicies((cur) => (cur === null ? cur : { ...cur, enforcement_mode: res.enforcement_mode }));
        }
        window.setTimeout(() => {
          void reconcile();
        }, POLICY_ENGINE_SETTLE_MS);
      } catch (err: unknown) {
        if (mountedRef.current) {
          setActionError(err instanceof Error ? err.message : 'Failed to change enforcement.');
        }
      } finally {
        if (mountedRef.current) setActionPending(false);
      }
    },
    [actionPending, mcpId, reconcile, patchPolicies],
  );

  const handleModeClick = useCallback(
    (mode: EnforcementRequest['mode']) => {
      if (mode === enforcementMode) return;
      if (mode === 'enforce' && engineUnattached) {
        setArmedWarning({ kind: 'enforce' });
        return;
      }
      void applyEnforcement(mode);
    },
    [enforcementMode, engineUnattached, applyEnforcement],
  );

  const confirmWarning = useCallback(() => {
    if (!armedWarning) return;
    if (armedWarning.kind === 'add') {
      void submitAdd();
    } else {
      void applyEnforcement('enforce');
    }
  }, [armedWarning, submitAdd, applyEnforcement]);

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

  const banner = { ...ENFORCEMENT_STYLE[enforcementMode], ...ENFORCEMENT_COPY[enforcementMode] };

  const postureNote = postureWarning(enforcementMode);

  const MODES: { key: EnforcementRequest['mode']; label: string; active: boolean; activeCls: string }[] = [
    { key: 'enforce', label: 'Enforce', active: enforcementMode === 'enforce', activeCls: 'bg-emerald-600 text-white' },
    { key: 'log_only', label: 'Log only', active: enforcementMode === 'log_only', activeCls: 'bg-amber-500 text-white' },
    { key: 'disabled', label: 'Disable', active: enforcementMode === 'none', activeCls: 'bg-slate-600 text-white' },
  ];

  return (
    <div className="space-y-4">
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

        {armedWarning && (
          <div className="mt-4 rounded-lg border border-amber-200 bg-amber-50/70 p-4" role="alert">
            <h3 className="text-sm font-semibold text-amber-800">Switch this gateway to default-deny?</h3>
            <p className="text-sm text-amber-700 mt-1">{ENFORCE_FLIP_WARNING}</p>
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

      <div className={`${CARD} overflow-hidden`}>
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

        {picking && (
          <div className="px-5 py-4 border-b border-slate-200/60 bg-slate-50/50">
            <div className="flex items-center justify-between gap-3 mb-3">
              <div className="flex items-center gap-3 min-w-0">
                <h3 className="text-sm font-semibold text-slate-800">
                  {effect === 'allow' ? 'Allow a user to call a tool' : 'Deny a tool call'}
                </h3>
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
              <div>
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

        {actionError && (
          <p className="px-5 pt-3 text-sm text-red-600" role="alert">{actionError}</p>
        )}

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
              const isContain = isContainmentRow(row);
              const isEveryone = !isContain && row.managed === true && !row.user_oid && !!row.user_label;
              const rowConditions = row.conditions ?? [];
              return (
                <li key={row.policy_id} className="px-5 py-3">
                  <div className="flex items-center gap-3">
                    {isContain ? (
                      <span aria-hidden="true" className="shrink-0 inline-flex items-center justify-center h-8 w-8 rounded-full bg-rose-100 text-rose-700">
                        <span className="h-2.5 w-2.5 rounded-sm bg-rose-600" />
                      </span>
                    ) : isEveryone ? (
                      <span aria-hidden="true" className="shrink-0 inline-flex items-center justify-center h-8 w-8 rounded-full bg-violet-100 text-violet-700 text-sm font-semibold">
                        ∀
                      </span>
                    ) : (
                      <AgentAvatar name={row.user_label ?? '—'} size="sm" />
                    )}
                    <div className="min-w-0 flex-1">
                      {isContain ? (
                        <>
                          <span className="inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium bg-rose-50 text-rose-700">
                            Agent containment
                          </span>
                          {row.agent_app_id && (
                            <span className="block mt-0.5 font-mono text-xs text-slate-400 truncate">{row.agent_app_id}</span>
                          )}
                        </>
                      ) : isEveryone ? (
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

                    <span
                      className={`shrink-0 inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium ${
                        allTools ? 'bg-violet-50 text-violet-700' : 'bg-slate-100 text-slate-600'
                      }`}
                    >
                      {label}
                    </span>

                    <span
                      className={`shrink-0 inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium ${
                        isDeny ? 'bg-red-50 text-red-700' : 'bg-emerald-50 text-emerald-700'
                      }`}
                    >
                      {isDeny ? 'Deny' : 'Allow'}
                    </span>

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

                    {canManage && !isContain && (
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

                  {isContain && (
                    <p className="mt-1 ml-11 text-xs text-slate-500">Restore the agent on its Containment tab to lift this forbid.</p>
                  )}

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
