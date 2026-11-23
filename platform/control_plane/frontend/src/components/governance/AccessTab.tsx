import { useCallback, useEffect, useRef, useState } from 'react';
import { grantsApi } from '../../api/client';
import type { DriftEntry, Agent, AgentGrantRead } from '../../api/client';
import { useLoad } from '../../api/useLoad';
import { AgentAvatar } from './agentUi';
import PrincipalPicker, { type PickerHit } from './PrincipalPicker';
import ConfirmDialog from '../ConfirmDialog';
import { mergeGrants, unconfirmedMutations } from './accessGrantsReconcile';
import type { PendingMutation } from './accessGrantsReconcile';
import { accessSurface, DRIFT_PANEL_TITLE, DRIFT_REASSERT_CONFIRM, DRIFT_REASSERT_LABEL } from './platformLabels';
import { driftEntriesOf, driftPanelApplies, driftRows, driftSummary } from './accessDrift';
import type { DriftSummary } from './accessDrift';
import { STALE_PENDING_MS } from './accessTiming';
import { useReturnFocus } from '../shared/useReturnFocus';

const CARD = 'bg-white/70 backdrop-blur rounded-xl border border-slate-200/60 shadow-sm';

const RECONCILE_BACKOFF_MS = [1200, 2500, 5000, 10000, 15000, 15000];

function principalTypeBadge(type: string): { cls: string; label: string } {
  if (type === 'Group') return { cls: 'bg-violet-50 text-violet-700', label: 'Group' };
  if (type === 'ServicePrincipal') return { cls: 'bg-indigo-50 text-indigo-700', label: 'Agent' };
  return { cls: 'bg-blue-50 text-blue-700', label: 'User' };
}

function roleBadge(role: string): { cls: string; label: string } {
  return role === 'Admin'
    ? { cls: 'bg-amber-50 text-amber-700', label: 'Admin' }
    : { cls: 'bg-slate-100 text-slate-600', label: 'Invoker' };
}

function StatusBanner({
  kind,
  stranded,
  canManage,
  onReprovision,
}: {
  kind: 'no-identity' | 'failed' | 'awaiting-runtime' | 'provisioning';
  stranded: boolean;
  canManage: boolean;
  onReprovision: () => void;
}) {
  if (kind === 'no-identity') {
    return (
      <div className={`${CARD} p-6`}>
        <h2 className="text-sm font-semibold text-slate-800">No Entra identity</h2>
        <p className="text-sm text-slate-500 mt-1">
          This agent has no Entra identity (metadata-only). Access grants are available once
          the agent is provisioned with a Microsoft Entra identity.
        </p>
      </div>
    );
  }

  if (kind === 'failed') {
    return (
      <div className={`${CARD} border-red-200/70 p-6`}>
        <h2 className="text-sm font-semibold text-red-700">Identity provisioning failed</h2>
        <p className="text-sm text-slate-600 mt-1">
          The Entra identity for this agent could not be provisioned. Access grants are
          unavailable until provisioning succeeds.
        </p>
        {canManage && (
          <button
            type="button"
            onClick={onReprovision}
            className="mt-3 px-3.5 py-1.5 rounded-lg bg-blue-600 text-white text-sm font-medium hover:bg-blue-700 transition-colors"
          >
            Re-provision identity
          </button>
        )}
      </div>
    );
  }

  if (kind === 'awaiting-runtime') {
    return (
      <div className={`${CARD} p-6`}>
        <h2 className="text-sm font-semibold text-slate-800">Identity ready — awaiting runtime</h2>
        <p className="text-sm text-slate-500 mt-1">
          This agent's Microsoft Entra identity is provisioned, but its runtime has not been
          deployed yet. Access grants become available once the runtime is wired.
        </p>
      </div>
    );
  }

  return (
    <div className={`${CARD} p-6`}>
      <div className="flex items-center gap-2.5">
        <Spinner />
        <h2 className="text-sm font-semibold text-slate-800">Provisioning identity…</h2>
      </div>
      <p className="text-sm text-slate-500 mt-1">
        The agent's Microsoft Entra identity is being provisioned. Access grants will be
        available once it completes.
      </p>
      {stranded && (
        <div className="mt-3">
          <p className="text-sm text-amber-700">
            This has been pending for a while. If provisioning stalled, you can re-provision to
            recover.
          </p>
          {canManage && (
            <button
              type="button"
              onClick={onReprovision}
              className="mt-2 px-3.5 py-1.5 rounded-lg bg-white border border-slate-300 text-slate-700 text-sm font-medium hover:bg-slate-50 transition-colors"
            >
              Re-provision identity
            </button>
          )}
        </div>
      )}
    </div>
  );
}

function Spinner() {
  return (
    <span
      aria-hidden="true"
      className="inline-block h-4 w-4 rounded-full border-2 border-slate-200 border-t-blue-600 animate-spin"
    />
  );
}

function DriftPanel({
  entries,
  summary,
  canManage,
  pending,
  error,
  onReassert,
}: {
  entries: DriftEntry[];
  summary: DriftSummary;
  canManage: boolean;
  pending: boolean;
  error: string | null;
  onReassert: () => void;
}) {
  const rows = driftRows(entries);
  return (
    <div className={`${CARD} border-amber-200/70 overflow-hidden`}>
      <div className="flex items-start justify-between gap-3 px-5 py-4 border-b border-amber-200/50">
        <div>
          <h2 className="text-sm font-semibold text-amber-700">
            {DRIFT_PANEL_TITLE}
            <span className="ml-1.5 font-normal text-amber-700">({summary.count})</span>
          </h2>
          <p className="text-xs text-slate-500 mt-0.5">{summary.note}</p>
        </div>
        {canManage && (
          <button
            type="button"
            onClick={onReassert}
            disabled={pending}
            className="shrink-0 px-3.5 py-1.5 rounded-lg bg-amber-600 text-white text-sm font-medium hover:bg-amber-700 transition-colors disabled:opacity-50"
          >
            {pending ? 'Re-asserting…' : DRIFT_REASSERT_LABEL}
          </button>
        )}
      </div>

      {error && (
        <p className="px-5 pt-3 text-sm text-red-600" role="alert">{error}</p>
      )}

      <ul className="divide-y divide-slate-100">
        {rows.map((row) => (
          <li key={row.key} className="flex items-center gap-3 px-5 py-3">
            <div className="min-w-0 flex-1">
              <span className="block text-sm font-medium text-slate-800 truncate">{row.principal}</span>
              <span className="block text-xs text-slate-500 mt-0.5">{row.note}</span>
            </div>
            <span className="shrink-0 inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium bg-slate-100 text-slate-600">
              {row.kindLabel}
            </span>
            <span className={`shrink-0 inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium ${row.tint}`}>
              {row.level}
            </span>
          </li>
        ))}
      </ul>
    </div>
  );
}

export default function AccessTab({
  agent,
  canManage,
  currentOid,
  onReprovision,
}: {
  agent: Agent;
  canManage: boolean;
  currentOid: string | null;
  onReprovision: () => void;
}) {
  const [actionPending, setActionPending] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);

  const [picking, setPicking] = useState(false);

  const [reassertPending, setReassertPending] = useState(false);
  const [reassertError, setReassertError] = useState<string | null>(null);
  const [confirmingReassert, setConfirmingReassert] = useState(false);

  const addTriggerRef = useRef<HTMLButtonElement>(null);
  const returnFocus = useReturnFocus(addTriggerRef);

  const closePicker = useCallback(() => {
    setPicking(false);
    setActionError(null);
    returnFocus();
  }, [returnFocus]);

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

  const surface = accessSurface(agent);
  const showGrants = surface.kind === 'grants';
  const showDrift = showGrants && driftPanelApplies(agent);
  const agentId = agent.id;

  const agentIdRef = useRef(agentId);
  agentIdRef.current = agentId;

  const mountedRef = useRef(true);

  const pendingRef = useRef<PendingMutation[]>([]);

  const reconcileTimerRef = useRef<number | null>(null);

  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
      if (reconcileTimerRef.current !== null) {
        window.clearTimeout(reconcileTimerRef.current);
        reconcileTimerRef.current = null;
      }
    };
  }, []);

  const [strandedAt, setStrandedAt] = useState<string | null>(null);
  useEffect(() => {
    if (agent.identity_status !== 'pending') return;
    const updatedAt = agent.updated_at;
    const elapsed = Date.now() - new Date(updatedAt).getTime();
    if (Number.isNaN(elapsed)) return;
    const handle = window.setTimeout(() => {
      if (mountedRef.current) setStrandedAt(updatedAt);
    }, Math.max(0, STALE_PENDING_MS - elapsed));
    return () => window.clearTimeout(handle);
  }, [agent.identity_status, agent.updated_at]);
  const stranded = agent.identity_status === 'pending' && strandedAt === agent.updated_at;

  const scheduleReconcile = (attempt: number) => {
    if (reconcileTimerRef.current !== null) {
      window.clearTimeout(reconcileTimerRef.current);
      reconcileTimerRef.current = null;
    }
    if (attempt >= RECONCILE_BACKOFF_MS.length) return;
    reconcileTimerRef.current = window.setTimeout(async () => {
      reconcileTimerRef.current = null;
      let fresh: AgentGrantRead[];
      try {
        fresh = await grantsApi.list(agentId);
      } catch {
        if (mountedRef.current && pendingRef.current.length > 0) scheduleReconcile(attempt + 1);
        return;
      }
      if (!mountedRef.current) return;
      patchGrants(() => mergeGrants(fresh, pendingRef.current));
      pendingRef.current = unconfirmedMutations(fresh, pendingRef.current);
      if (pendingRef.current.length > 0) scheduleReconcile(attempt + 1);
    }, RECONCILE_BACKOFF_MS[attempt]);
  };

  useEffect(() => {
    pendingRef.current = [];
    if (reconcileTimerRef.current !== null) {
      window.clearTimeout(reconcileTimerRef.current);
      reconcileTimerRef.current = null;
    }
  }, [agentId, showGrants]);

  const { state: grantsState, patch: patchGrants } = useLoad(
    () => (showGrants ? grantsApi.list(agentId) : Promise.resolve<AgentGrantRead[]>([])),
    [agentId, showGrants],
    'Failed to load grants.',
  );
  const grants = grantsState.data ?? [];

  const driftLoad = useLoad(
    () =>
      showDrift
        ? grantsApi
            .drift(agentId)
            .then((res) => ({ agentId, entries: driftEntriesOf(res) }))
            .catch(() => ({ agentId, entries: null as DriftEntry[] | null }))
        : Promise.resolve({ agentId, entries: null as DriftEntry[] | null }),
    [agentId, showDrift],
  );
  const driftEntries = driftLoad.state.data?.agentId === agentId ? driftLoad.state.data.entries : null;
  const refreshDrift = driftLoad.reload;

  const [driftScope, setDriftScope] = useState({ agentId, showDrift });
  if (driftScope.agentId !== agentId || driftScope.showDrift !== showDrift) {
    setDriftScope({ agentId, showDrift });
    setReassertError(null);
    setConfirmingReassert(false);
    setReassertPending(false);
  }

  const handleReassert = async () => {
    if (reassertPending) return;
    const startedFor = agentId;
    const stillHere = () => mountedRef.current && agentIdRef.current === startedFor;
    setConfirmingReassert(false);
    setReassertPending(true);
    setReassertError(null);
    try {
      const res = await grantsApi.reassert(agentId);
      if (!stillHere()) return;
      driftLoad.patch(() => ({ agentId, entries: driftEntriesOf(res) }));
      const fresh = await grantsApi.list(agentId).catch(() => null);
      if (fresh && stillHere()) patchGrants(() => mergeGrants(fresh, pendingRef.current));
    } catch (err: unknown) {
      if (stillHere()) {
        setReassertError(err instanceof Error ? err.message : 'Failed to re-assert platform access.');
      }
    } finally {
      if (stillHere()) setReassertPending(false);
    }
  };

  const handlePick = async (hit: PickerHit, role: 'Invoker' | 'Admin') => {
    if (actionPending) return;
    if (hit.type !== 'user' && hit.type !== 'group') return;
    setActionPending(true);
    setActionError(null);
    try {
      const created = await grantsApi.add(agentId, {
        principal_id: hit.id,
        principal_type: hit.type,
        role,
      });
      if (!mountedRef.current) return;
      patchGrants((prev) => {
        if (prev.some((g) => g.assignment_id === created.assignment_id)) return prev;
        return [...prev, created];
      });
      pendingRef.current = [...pendingRef.current, { kind: 'add', grant: created }];
      closePicker();
      scheduleReconcile(0);
      refreshDrift();
    } catch (err: unknown) {
      if (mountedRef.current) {
        setActionError(err instanceof Error ? err.message : 'Failed to grant access.');
      }
    } finally {
      if (mountedRef.current) setActionPending(false);
    }
  };

  const handleRemove = async (grant: AgentGrantRead) => {
    setActionPending(true);
    setActionError(null);
    const prevGrants = grants;
    patchGrants((prev) => prev.filter((g) => g.assignment_id !== grant.assignment_id));
    try {
      await grantsApi.remove(agentId, grant.assignment_id);
      pendingRef.current = [...pendingRef.current, { kind: 'remove', assignmentId: grant.assignment_id }];
      scheduleReconcile(0);
      refreshDrift();
    } catch (err: unknown) {
      if (mountedRef.current) {
        patchGrants(() => prevGrants);
        setActionError(err instanceof Error ? err.message : 'Failed to remove access.');
      }
    } finally {
      if (mountedRef.current) setActionPending(false);
    }
  };

  if (surface.kind !== 'grants') {
    return (
      <StatusBanner
        kind={surface.kind}
        stranded={stranded}
        canManage={canManage}
        onReprovision={onReprovision}
      />
    );
  }

  const hasGroupGrant = grants.some((g) => g.principal_type === 'Group');

  const canMutate = canManage && surface.notReadyNote === null;

  const drift = showDrift ? driftSummary(driftEntries) : null;

  return (
    <div className="space-y-4">
      {drift?.state === 'drifted' && driftEntries !== null && (
        <DriftPanel
          entries={driftEntries}
          summary={drift}
          canManage={canManage}
          pending={reassertPending}
          error={reassertError}
          onReassert={() => setConfirmingReassert(true)}
        />
      )}

      <ConfirmDialog
        open={confirmingReassert}
        title={DRIFT_REASSERT_LABEL}
        message={DRIFT_REASSERT_CONFIRM}
        confirmText={DRIFT_REASSERT_LABEL}
        variant="warning"
        onConfirm={handleReassert}
        onCancel={() => setConfirmingReassert(false)}
      />

      <div className={`${CARD} overflow-hidden`}>
        <div className="flex items-center justify-between gap-3 px-5 py-4 border-b border-slate-200/60">
          <div>
            <h2 className="text-sm font-semibold text-slate-800">People &amp; groups with access</h2>
            <p className="text-xs text-slate-400 mt-0.5">
              Direct Entra app-role assignments on this agent.
            </p>
            <p className="text-xs text-slate-400 mt-0.5">
              User grants in the Invoker role share their Entra assignment with marketplace subscriptions
              — revoking one affects the other. Admin-role and group grants are unaffected.
            </p>
            {surface.notReadyNote !== null && (
              <p className="text-xs text-amber-700 mt-1.5">{surface.notReadyNote}</p>
            )}
          </div>
          {canMutate && !picking && (
            <button
              ref={addTriggerRef}
              type="button"
              onClick={() => {
                setPicking(true);
                setActionError(null);
              }}
              className="shrink-0 px-3.5 py-1.5 rounded-lg bg-blue-600 text-white text-sm font-medium hover:bg-blue-700 transition-colors"
              data-testid="access-add-principal"
            >
              + Add principal
            </button>
          )}
        </div>

        {picking && (
          <div className="px-5 py-4 border-b border-slate-200/60 bg-slate-50/50">
            <PrincipalPicker
              onPick={handlePick}
              onClose={closePicker}
              pending={actionPending}
            />
          </div>
        )}

        {actionError && (
          <p className="px-5 pt-3 text-sm text-red-600" role="alert">{actionError}</p>
        )}

        {grantsState.kind === 'loading' ? (
          <div className="px-5 py-8 text-center">
            <p className="text-sm text-slate-400">Loading access…</p>
          </div>
        ) : grantsState.kind === 'error' ? (
          <div className="px-5 py-8 text-center">
            <p className="text-sm text-red-600" role="alert">{grantsState.message}</p>
          </div>
        ) : grants.length === 0 ? (
          <div className="px-5 py-10 text-center">
            <p className="text-sm font-medium text-slate-600">
              {surface.notReadyNote === null
                ? 'No one has been granted access yet.'
                : 'Access grants are not readable yet.'}
            </p>
            <p className="text-xs text-slate-400 mt-1">
              {canMutate
                ? 'Use “Add principal” to grant a user or group access to this agent.'
                : canManage && surface.notReadyNote !== null
                  ? 'Grants can be managed once identity provisioning completes.'
                  : 'An operator can grant users or groups access to this agent.'}
            </p>
          </div>
        ) : (
          <ul className="divide-y divide-slate-100">
            {grants.map((grant) => {
              const tBadge = principalTypeBadge(grant.principal_type);
              const rBadge = roleBadge(grant.role);
              const isYou = currentOid !== null && grant.principal_id === currentOid;
              return (
                <li
                  key={grant.assignment_id}
                  className={`flex items-center gap-3 px-5 py-3 ${isYou ? 'bg-blue-50/40' : ''}`}
                >
                  <AgentAvatar name={grant.principal_display} size="sm" />
                  <div className="min-w-0 flex-1">
                    <div className="flex items-center gap-2 min-w-0">
                      <span className="text-sm font-medium text-slate-800 truncate">
                        {grant.principal_display}
                      </span>
                      {isYou && (
                        <span className="shrink-0 inline-flex items-center px-1.5 py-0.5 rounded-full text-[10px] font-medium uppercase tracking-wide bg-blue-100 text-blue-700">
                          You
                        </span>
                      )}
                    </div>
                  </div>

                  <span className={`shrink-0 inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium ${tBadge.cls}`}>
                    {tBadge.label}
                  </span>

                  <span className={`shrink-0 inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium ${rBadge.cls}`}>
                    {rBadge.label}
                  </span>

                  {canMutate && (
                    <button
                      type="button"
                      onClick={() => handleRemove(grant)}
                      disabled={actionPending}
                      aria-label={`Remove access for ${grant.principal_display}`}
                      className="shrink-0 inline-flex items-center justify-center h-7 w-7 rounded-md text-slate-400 hover:text-red-600 hover:bg-red-50 transition-colors disabled:opacity-40"
                    >
                      <span aria-hidden="true" className="text-base leading-none">×</span>
                    </button>
                  )}
                </li>
              );
            })}
          </ul>
        )}
      </div>

      {hasGroupGrant && (
        <p className="text-xs text-slate-400 px-1">
          Group grants apply to direct members only — members via nested groups are not granted
          access (Entra app-role assignment limitation).
        </p>
      )}

      {drift !== null && drift.state !== 'drifted' && (
        <p
          className={`text-xs px-1 ${drift.state === 'unavailable' ? 'text-amber-700' : 'text-slate-400'}`}
        >
          {drift.note}
        </p>
      )}
      {reassertError !== null && drift?.state !== 'drifted' && (
        <p className="text-sm text-red-600 px-1" role="alert">{reassertError}</p>
      )}
    </div>
  );
}
