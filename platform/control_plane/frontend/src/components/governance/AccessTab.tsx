// AccessTab — the agent "Access" tab (Epic 6, T-FE-ACCESS).
//
// Models the Microsoft Agent-365 "Users" layout (a dense, scannable table of
// principals granted access to this agent) re-skinned in the governance slate
// idiom. Grants are live Entra app-role assignments read straight from Graph
// (no DynamoDB) via grantsApi; Microsoft Graph is the source of truth, so all
// mutations tolerate Graph's eventual consistency with a short reconciling
// refetch after the optimistic update.
//
// When the agent has no usable Entra identity, the grants UI is replaced by a status
// banner reflecting identity_status (none | pending | failed), with a
// Re-provision affordance for failed — and for a *stranded* pending (a
// mid-provision ECS task death leaves pending forever, so once it looks stale
// we surface Re-provision there too).
//
// E29/T9: WHICH surface to show is no longer decided here. `platformLabels.accessSurface` owns it
// as a pure function over a closed union, because the old inline conditions were the bug: they
// suppressed the grants table for the entire life of every Databricks agent (`entra_sp_id &&
// !agent_arn` is permanently true for a platform that has no ARNs). The full account is in that
// function's docstring; this file now binds and renders.

import { useCallback, useEffect, useRef, useState } from 'react';
import { grantsApi } from '../../api/client';
import type { AccessDriftEntry, Agent, Grant, PrincipalHit } from '../../api/client';
import { AgentAvatar } from './agentUi';
import PrincipalPicker from './PrincipalPicker';
import ConfirmDialog from '../ConfirmDialog';
import { mergeGrants, unconfirmedMutations } from './accessGrantsReconcile';
import type { PendingMutation } from './accessGrantsReconcile';
import { accessSurface, DRIFT_PANEL_TITLE, DRIFT_REASSERT_CONFIRM, DRIFT_REASSERT_LABEL } from './platformLabels';
import { driftEntriesOf, driftPanelApplies, driftRows, driftSummary } from './accessDrift';
import type { DriftSummary } from './accessDrift';

// Card chrome — identical to AgentDetail's CARD so the tab body sits in the
// same surface family as Overview.
const CARD = 'bg-white/70 backdrop-blur rounded-xl border border-slate-200/60 shadow-sm';

// A pending identity older than this is treated as stranded (a mid-provision
// crash with no other recovery path), unlocking the Re-provision button.
const STALE_PENDING_MS = 2.5 * 60 * 1000;

// Graph app-role assignments are eventually consistent: a just-written grant
// may not show on an immediate re-list. After a mutate we wait then refetch to
// reconcile the optimistic state with Graph's truth — but the refetch overlays
// the still-unconfirmed mutations (accessGrantsReconcile.mergeGrants) so a stale
// read NEVER clobbers the known-good optimistic row. We poll on this backoff
// until every pending mutation is confirmed by the read (the optimistic row stays
// visible the whole time regardless). Graph replication is usually seconds but
// can take ~a minute under load, so the schedule reaches ~50s total before giving
// up; the optimistic state stays in place even then (next remount resyncs).
const RECONCILE_BACKOFF_MS = [1200, 2500, 5000, 10000, 15000, 15000];

// principalType is capitalized in the READ shape ('User' | 'Group', Graph's
// principalType). User=blue, Group=violet — same tints as the picker.
function principalTypeBadge(type: 'User' | 'Group' | 'ServicePrincipal'): { cls: string; label: string } {
  if (type === 'Group') return { cls: 'bg-violet-50 text-violet-700', label: 'Group' };
  if (type === 'ServicePrincipal') return { cls: 'bg-indigo-50 text-indigo-700', label: 'Agent' };
  return { cls: 'bg-blue-50 text-blue-700', label: 'User' };
}

// Invoker=slate (the common case), Admin=amber (elevated — reads as the
// noteworthy one without alarming like red).
function roleBadge(role: 'Invoker' | 'Admin'): { cls: string; label: string } {
  return role === 'Admin'
    ? { cls: 'bg-amber-50 text-amber-700', label: 'Admin' }
    : { cls: 'bg-slate-100 text-slate-600', label: 'Invoker' };
}

// Whether a `pending` identity has been pending long enough to look stranded.
function isPendingStranded(updatedAt: string): boolean {
  const t = new Date(updatedAt).getTime();
  if (Number.isNaN(t)) return false; // unparseable → don't offer recovery yet
  return Date.now() - t > STALE_PENDING_MS;
}

// ---------------------------------------------------------------------------
// Status banner (shown for every AccessSurface kind except 'grants')
// ---------------------------------------------------------------------------

// `kind` is the already-resolved verdict from `accessSurface` — this component no longer
// re-derives it from `identity_status`/`agent_arn`, which is what stops the two from drifting.
// (`agent` is still needed for `updated_at`, which decides whether a pending provision looks
// stranded.)
function StatusBanner({
  kind,
  agent,
  canManage,
  onReprovision,
}: {
  kind: 'no-identity' | 'failed' | 'awaiting-runtime' | 'provisioning';
  agent: Agent;
  canManage: boolean;
  onReprovision: () => void;
}) {
  // none → metadata-only agent; the grants UI is hidden entirely (no identity
  // to assign roles against).
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

  // failed → red banner + Re-provision (Operator+).
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

  // AgentCore-shaped, SP minted, no runtime ARN → the E20 "pre-registered" state: the Entra
  // identity is fully minted (app + SP + OBO consent), but identity_status only flips to
  // 'provisioned' once the runtime authorizer is wired (provision_runtime), which needs the
  // runtime to exist. For a template-materialized agent the runtime pipeline is deferred, so
  // this sits here indefinitely — by design, NOT in-progress. Show an honest static banner
  // (no spinner, no Re-provision — re-provision 409s since the identity already exists).
  //
  // E29/T9: this branch is now reached ONLY for an `aws_bedrock` agent (see `accessSurface`).
  // It used to key on `!agent.agent_arn`, which caught every Databricks agent forever.
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

  // pending → spinner + "provisioning…"; if it looks stranded, also offer
  // Re-provision (the only recovery path for a mid-provision crash before the SP was minted).
  const stranded = isPendingStranded(agent.updated_at);
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

// A small CSS-only spinner (no icon library) — slate ring with a blue arc.
function Spinner() {
  return (
    <span
      aria-hidden="true"
      className="inline-block h-4 w-4 rounded-full border-2 border-slate-200 border-t-blue-600 animate-spin"
    />
  );
}

// ---------------------------------------------------------------------------
// Platform-access drift (E29/T13e, design §3A) — Databricks-governed agents only
// ---------------------------------------------------------------------------

// The drift card, shown ONLY when the platform ACL and AGP's grants actually disagree. A clean
// or unavailable read is a one-line footnote below the table instead (see the render), because a
// full card claiming "nothing is wrong" would compete with the surface an operator came for.
//
// Every judgement here came from `accessDrift.ts` already — this function chooses no copy, no
// severity, and no ordering. It renders `driftRows` in the tab's existing chrome (the same CARD,
// the same button sizes, the amber the `notReadyNote` line uses) so drift reads as part of this
// page rather than a new design language.
function DriftPanel({
  entries,
  summary,
  canManage,
  pending,
  error,
  onReassert,
}: {
  entries: AccessDriftEntry[];
  /** Passed in, not recomputed: the parent already derived it to choose card-vs-footnote, and
      one computation cannot disagree with itself about the count. */
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
        {/* Repair is a human's button, never automatic — silently re-asserting would hide that
            someone is editing the platform's ACL around AGP, which is the fact §3A exists to
            surface. */}
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
              {/* The platform's own principal name (a Databricks username / group / SP name) —
                  NOT an Entra display name. These entries exist precisely because they may have
                  no AGP-side counterpart to look a friendly name up from. */}
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

// ---------------------------------------------------------------------------
// AccessTab
// ---------------------------------------------------------------------------

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
  const [grants, setGrants] = useState<Grant[]>([]);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState<string | null>(null);

  // Mutation (add/remove) UI state — mirrors AgentDetail's actionPending /
  // actionError idiom so the two pages behave identically.
  const [actionPending, setActionPending] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);

  const [picking, setPicking] = useState(false);

  // Platform-ACL drift (E29/T13e) — Databricks-governed agents only.
  // `null` means the drift read has not succeeded (never loaded, or it failed): distinct from
  // `[]`, which is the positive "AGP and the platform agree" answer. `driftSummary` owns that
  // distinction; this state only has to keep the two spellings apart.
  const [driftEntries, setDriftEntries] = useState<AccessDriftEntry[] | null>(null);
  const [reassertPending, setReassertPending] = useState(false);
  const [reassertError, setReassertError] = useState<string | null>(null);
  const [confirmingReassert, setConfirmingReassert] = useState(false);

  // FIX 2 — focus return + Escape.
  // Ref on the "+ Add principal" trigger so focus returns there when the picker
  // closes (both via the ✕ close and after a successful grant), preventing
  // focus from dropping to document start.
  const addTriggerRef = useRef<HTMLButtonElement>(null);

  const closePicker = useCallback(() => {
    setPicking(false);
    setActionError(null);
    // Return focus to the trigger that opened the picker.
    window.setTimeout(() => addTriggerRef.current?.focus(), 0);
  }, []);

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

  // WHICH surface this tab shows — one pure decision, taken once (E29/T9).
  const surface = accessSurface(agent);
  // Whether to LIST grants at all. Formerly `identity_status === 'provisioned'`, which was the
  // second half of the suppression bug: an agent whose grants render must also fetch them, or the
  // table renders permanently empty. Keyed on the surface so the fetch gate and the render gate
  // cannot disagree — one is derived from the other.
  const showGrants = surface.kind === 'grants';
  // Whether this agent HAS a platform ACL that can drift at all — the backend's
  // `is_databricks_governed` gate, mirrored (see `accessDrift.driftPanelApplies`). The drift
  // routes 404/409 on any other agent, so gating the FETCH on the same fact is what keeps that
  // from ever being an error the operator sees.
  const showDrift = showGrants && driftPanelApplies(agent);
  const agentId = agent.id;

  // Which agent this tab is CURRENTLY showing, readable from an async continuation. AccessTab is
  // not remounted when the route's agent changes (that is why the effects below reset state on
  // `agentId` instead of relying on a fresh mount), so `mountedRef` alone cannot tell "still
  // mounted" from "still the same agent" — and an in-flight write resolving after a switch would
  // otherwise paint agent A's drift and A's grant roster onto agent B's tab. Assigned during
  // render so it is already correct for every continuation that can observe the new agent.
  const agentIdRef = useRef(agentId);
  agentIdRef.current = agentId;

  // Guards against setState after unmount (the reconcile refetch fires on a
  // timer, so it can land after the user navigates away).
  const mountedRef = useRef(true);

  // Read-your-writes overlay: the successful (2xx) mutations not yet reflected by
  // a server read. A ref (not state) so the reconcile timer closure always reads
  // the latest set, and pushing/dropping overlays never itself triggers a render
  // (renders are driven through setGrants). Cleared on agent/provisioned change in
  // the initial-load effect so overlays never leak across agents.
  const pendingRef = useRef<PendingMutation[]>([]);

  // Handle for the in-flight bounded reconcile poll, so a new mutation (or
  // unmount) can cancel a pending attempt before scheduling its own.
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

  // FIX 3 — stranded-pending timer.
  // isPendingStranded() only re-evaluates on render, so if the tab is already
  // open when the ~2.5-min threshold passes the Re-provision affordance would
  // never appear without a user-triggered re-render. We schedule a one-shot
  // timeout for exactly when the threshold crosses and bump a nonce to force
  // one re-render at that moment. Cleared safely on unmount via mountedRef.
  const [, setStaleNonce] = useState(0);
  useEffect(() => {
    if (agent.identity_status !== 'pending') return;
    const elapsed = Date.now() - new Date(agent.updated_at).getTime();
    if (Number.isNaN(elapsed) || elapsed >= STALE_PENDING_MS) return; // already stranded or unparseable
    const remaining = STALE_PENDING_MS - elapsed;
    const handle = window.setTimeout(() => {
      if (mountedRef.current) setStaleNonce((n) => n + 1);
    }, remaining);
    return () => window.clearTimeout(handle);
  }, [agent.identity_status, agent.updated_at]);

  // Silent, non-destructive, self-healing reconcile poll — used after a mutate to
  // absorb Graph replication lag (never surfaced as the blocking load spinner).
  // scheduleReconcile(attempt) arms the backoff step for `attempt`; when it fires
  // it refetches the server list, then overlays the still-unconfirmed optimistic
  // mutations on top (mergeGrants) so a not-yet-replicated read can NEVER clobber a
  // known-good optimistic row (the add-disappears / remove-resurrects bug). It then
  // drops the overlays the read has now confirmed (unconfirmedMutations); if any
  // remain it re-arms the next attempt, stopping when none remain or the backoff
  // budget (RECONCILE_BACKOFF_MS) is exhausted. Any in-flight timer is cancelled
  // first so overlapping mutations collapse to a single poll chain. A failed fetch
  // leaves the optimistic state (and overlays) in place and simply retries.
  const scheduleReconcile = useCallback((attempt: number) => {
    if (reconcileTimerRef.current !== null) {
      window.clearTimeout(reconcileTimerRef.current);
      reconcileTimerRef.current = null;
    }
    if (attempt >= RECONCILE_BACKOFF_MS.length) return; // budget exhausted — overlay stays; remount resyncs
    reconcileTimerRef.current = window.setTimeout(async () => {
      reconcileTimerRef.current = null;
      let fresh: Grant[];
      try {
        fresh = await grantsApi.list(agentId);
      } catch {
        // Don't clobber the UI with a background-refetch error; retry if anything
        // is still pending and budget remains.
        if (mountedRef.current && pendingRef.current.length > 0) scheduleReconcile(attempt + 1);
        return;
      }
      if (!mountedRef.current) return;
      // Overlay unconfirmed optimistic mutations over the (possibly stale) read —
      // NEVER a bare setGrants(fresh).
      setGrants(mergeGrants(fresh, pendingRef.current));
      // Drop the overlays the read has now caught up to.
      pendingRef.current = unconfirmedMutations(fresh, pendingRef.current);
      // Keep polling until the read confirms everything or the budget runs out.
      if (pendingRef.current.length > 0) scheduleReconcile(attempt + 1);
    }, RECONCILE_BACKOFF_MS[attempt]);
  }, [agentId]);

  // Initial load (and on agent/showGrants change). Skipped when the tab is not showing grants
  // — there's nothing to list until an identity exists. Reset the read-your-writes
  // overlays (and cancel any in-flight reconcile poll) on every agent/showGrants
  // change so a previous agent's optimistic mutations never leak onto this one;
  // a fresh mount starts clean and the initial list reflects true server state.
  useEffect(() => {
    pendingRef.current = [];
    if (reconcileTimerRef.current !== null) {
      window.clearTimeout(reconcileTimerRef.current);
      reconcileTimerRef.current = null;
    }
    if (!showGrants) {
      setLoading(false);
      return;
    }
    let cancelled = false;
    setLoading(true);
    setLoadError(null);
    grantsApi
      .list(agentId)
      .then((res) => {
        if (!cancelled) setGrants(res);
      })
      .catch((err: unknown) => {
        if (!cancelled) setLoadError(err instanceof Error ? err.message : 'Failed to load grants.');
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [agentId, showGrants]);

  // Drift read — SECONDARY, and never allowed to cost the operator the grant list.
  //
  // A named function rather than an effect body because it has THREE callers: the mount/agent
  // effect below, and the success path of both grant mutations. A grant or a revoke is the one
  // action that can break the ACL mirror (design §3A: a failed ACL removal keeps the agent
  // drifted and retryable), so leaving the drift state from before the mutation on screen would
  // keep asserting "platform permissions match" in exactly the window where they may not — the
  // same lie as reporting a failed read as clean, reached through staleness. Re-reading (rather
  // than blanking to `null`) is right because the ACL write completes before the grant route
  // answers 2xx, so an immediate re-read is consistent; blanking would downgrade a known state
  // to "could not be checked".
  //
  // A failure is deliberately swallowed to `null`: that value already MEANS "could not be
  // checked", and surfacing a red error would put a Databricks/route outage in front of an
  // operator whose actual task — reading and changing grants — still works.
  const refreshDrift = useCallback(async () => {
    if (!showDrift) return;
    const startedFor = agentId;
    let entries: AccessDriftEntry[] | null;
    try {
      entries = driftEntriesOf(await grantsApi.drift(agentId));
    } catch {
      entries = null;
    }
    if (!mountedRef.current || agentIdRef.current !== startedFor) return;
    setDriftEntries(entries);
  }, [agentId, showDrift]);

  // Mount / agent change. Its own effect (not folded into the list load) precisely so the two
  // reads cannot take each other down: a failed drift read leaves `driftEntries` at `null`, the
  // footnote says the check could not be made, and the table above it is unaffected. A failed
  // list load likewise says nothing about the ACL. Every drift-related piece of state is reset
  // here — including `reassertPending`, or a re-assert still in flight on the previous agent
  // leaves this one's button reading "Re-asserting…" and disabled until that promise settles.
  useEffect(() => {
    setDriftEntries(null);
    setReassertError(null);
    setConfirmingReassert(false);
    setReassertPending(false);
    void refreshDrift();
  }, [agentId, showDrift, refreshDrift]);

  // -- mutations ------------------------------------------------------------

  // Re-assert: rewrite the app's platform ACL from AGP's grants.
  //
  // The route answers with FRESH drift rather than an ack, so its response IS the new state —
  // empty entries means it converged. The grant list is refetched too because the two halves of
  // this tab must show one moment in time: an operator who just repaired the mirror will read the
  // table next, and a stale list there would raise a question the repair just answered. That
  // refetch goes through `mergeGrants` like every other read on this tab, so an unconfirmed
  // optimistic row cannot be clobbered by it (Graph's replication lag is unrelated to, and
  // unaffected by, the ACL write).
  // Every continuation is gated on `stillHere()` — still mounted AND still the agent the click
  // started on. `mountedRef` alone is not enough: an ACL rewrite can take seconds, and this is the
  // one handler that writes BOTH drift state and the grant roster, so a switch mid-flight would
  // paint agent A's verdict and A's principals into agent B's Access tab.
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
      setDriftEntries(driftEntriesOf(res));
      try {
        const fresh = await grantsApi.list(agentId);
        if (stillHere()) setGrants(mergeGrants(fresh, pendingRef.current));
      } catch {
        // The re-assert SUCCEEDED; only the courtesy refresh failed. Reporting an error here
        // would contradict the drift panel that just went clean.
      }
    } catch (err: unknown) {
      if (stillHere()) {
        setReassertError(err instanceof Error ? err.message : 'Failed to re-assert platform access.');
      }
    } finally {
      if (stillHere()) setReassertPending(false);
    }
  };

  const handlePick = async (hit: PrincipalHit, role: 'Invoker' | 'Admin') => {
    // FIX 1b — belt-and-suspenders re-entry guard. The picker button is also
    // disabled via the `pending` prop (FIX 1a), but guard here too so that two
    // rapid calls from any path cannot both reach grantsApi.add.
    if (actionPending) return;
    // This (E6 user→agent) flow only ever surfaces user/group principals;
    // 'agent' belongs to the E7 MCP-grant flow and is unreachable here.
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
      // Optimistically insert the authoritative grant the server returned
      // (carries the real assignment_id + capitalized principal_type).
      setGrants((prev) => {
        // De-dupe in case a prior reconcile already pulled it in.
        if (prev.some((g) => g.assignment_id === created.assignment_id)) return prev;
        return [...prev, created];
      });
      // Record the 2xx-confirmed add as a read-your-writes overlay so the bounded
      // reconcile poll keeps the row visible until Graph's read path catches up
      // (and never clobbers it with a stale list).
      pendingRef.current = [...pendingRef.current, { kind: 'add', grant: created }];
      // closePicker focuses the trigger and clears actionError, so the user
      // lands back at the "+ Add principal" button with the new row visible.
      closePicker();
      // Graph is eventually consistent — start the bounded reconcile poll; it
      // overlays this (and any other) pending mutation over each read until the
      // read confirms them, then self-stops.
      scheduleReconcile(0);
      // A grant is a mirrored write: re-read the ACL so the drift verdict below the table is
      // about the list as it is NOW (see refreshDrift). Not awaited — the grant already landed.
      void refreshDrift();
    } catch (err: unknown) {
      if (mountedRef.current) {
        setActionError(err instanceof Error ? err.message : 'Failed to grant access.');
      }
    } finally {
      if (mountedRef.current) setActionPending(false);
    }
  };

  const handleRemove = async (grant: Grant) => {
    setActionPending(true);
    setActionError(null);
    // Optimistically drop the row; restore it if the call fails.
    const prevGrants = grants;
    setGrants((prev) => prev.filter((g) => g.assignment_id !== grant.assignment_id));
    try {
      await grantsApi.remove(agentId, grant.assignment_id);
      // Record the 2xx-confirmed remove as a read-your-writes overlay so the
      // bounded reconcile poll keeps the row hidden until Graph's read path drops
      // it (a removed assignment can briefly still appear on an immediate re-list —
      // the resurrection bug).
      pendingRef.current = [...pendingRef.current, { kind: 'remove', assignmentId: grant.assignment_id }];
      // Start the bounded reconcile poll; it overlays this (and any other) pending
      // mutation over each read until the read confirms them, then self-stops.
      scheduleReconcile(0);
      // The revoke is the mutation drift exists for: §3A keeps the agent drifted (and retryable)
      // when the Entra assignment is gone but the platform ACL removal failed, so the pre-revoke
      // verdict must not stay on screen claiming the two still match.
      void refreshDrift();
    } catch (err: unknown) {
      if (mountedRef.current) {
        setGrants(prevGrants); // rollback
        setActionError(err instanceof Error ? err.message : 'Failed to remove access.');
      }
    } finally {
      if (mountedRef.current) setActionPending(false);
    }
  };

  // -- render ---------------------------------------------------------------

  // No usable identity → banner instead of the grants UI. The `kind` is narrowed by the guard, so
  // adding a member to `AccessSurface` is a compile error here rather than a silently-missing case.
  if (surface.kind !== 'grants') {
    return (
      <StatusBanner
        kind={surface.kind}
        agent={agent}
        canManage={canManage}
        onReprovision={onReprovision}
      />
    );
  }

  const hasGroupGrant = grants.some((g) => g.principal_type === 'Group');

  // Grants are MUTABLE only once the backend agrees they are: `grants.py::_is_provisioned` gates
  // both POST and DELETE on `identity_status == 'provisioned'`, so offering Add/Remove while
  // `notReadyNote` is set would put buttons on the page whose only possible outcome is a 409.
  // The table stays visible (that is the fix); the controls wait for the server.
  const canMutate = canManage && surface.notReadyNote === null;

  // The drift verdict. `state === 'drifted'` gets the card ABOVE the table (it is a warning about
  // the very list below it); 'clean' and 'unavailable' get a one-line footnote instead, in the
  // same quiet idiom as the nested-group caveat — stated, because silence is not proof, but not
  // dressed up as an incident.
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

      {/* The confirm names the destructive half (hand-granted platform access is removed) —
          reusing the app's existing dialog rather than inventing a second one. */}
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
        {/* Card header: title + count + Add. */}
        <div className="flex items-center justify-between gap-3 px-5 py-4 border-b border-slate-200/60">
          <div>
            <h2 className="text-sm font-semibold text-slate-800">People &amp; groups with access</h2>
            <p className="text-xs text-slate-400 mt-0.5">
              Direct Entra app-role assignments on this agent.
            </p>
            {/* Shared-assignment advisory (E33 A2/C13) — a marketplace subscription approval
                writes the SAME (user, agent, Invoker) assignment a USER grant in the INVOKER role
                here does, so removing one side removes the other's access. Admin-role grants and
                group grants are distinct assignments and never collide. Static copy, no logic. */}
            <p className="text-xs text-slate-400 mt-0.5">
              User grants in the Invoker role share their Entra assignment with marketplace subscriptions
              — revoking one affects the other. Admin-role and group grants are unaffected.
            </p>
            {/* The identity exists but provisioning has not finished, so the backend declines to
                read or write grants (`grants.py::_is_provisioned` demands
                `identity_status == 'provisioned'`). Stated rather than badged, and paired with
                hiding the Add button below — an affordance that always 409s is worse than none.
                Amber, not red: nothing is broken, it is not finished. */}
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
            >
              + Add principal
            </button>
          )}
        </div>

        {/* Picker (inline, above the table). */}
        {picking && (
          <div className="px-5 py-4 border-b border-slate-200/60 bg-slate-50/50">
            <PrincipalPicker
              onPick={handlePick}
              onClose={closePicker}
              pending={actionPending}
            />
          </div>
        )}

        {/* Mutation error (e.g. 409 already-assigned) — non-blocking. */}
        {actionError && (
          <p className="px-5 pt-3 text-sm text-red-600" role="alert">{actionError}</p>
        )}

        {/* Body: loading / error / empty / table. */}
        {loading ? (
          <div className="px-5 py-8 text-center">
            <p className="text-sm text-slate-400">Loading access…</p>
          </div>
        ) : loadError ? (
          <div className="px-5 py-8 text-center">
            <p className="text-sm text-red-600" role="alert">{loadError}</p>
          </div>
        ) : grants.length === 0 ? (
          <div className="px-5 py-10 text-center">
            {/* While `notReadyNote` is set the server returns [] because it declines to READ the
                list, not because the list is empty — so the heading must not assert emptiness.
                The note above the table already explains why; this line stops short of a claim. */}
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

                  {/* Type pill (User / Group). */}
                  <span className={`shrink-0 inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium ${tBadge.cls}`}>
                    {tBadge.label}
                  </span>

                  {/* Role pill (Invoker / Admin). */}
                  <span className={`shrink-0 inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium ${rBadge.cls}`}>
                    {rBadge.label}
                  </span>

                  {/* Remove ✕ (Operator+). */}
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

      {/* Nested-group caveat — shown whenever a Group is currently granted.
          (The picker shows the same note at pick time for a chosen Group.) */}
      {hasGroupGrant && (
        <p className="text-xs text-slate-400 px-1">
          Group grants apply to direct members only — members via nested groups are not granted
          access (Entra app-role assignment limitation).
        </p>
      )}

      {/* Drift footnote for the two non-incident states. 'clean' is a positive claim earned by a
          successful read; 'unavailable' claims nothing about the ACL, only that AGP could not
          check it — keeping those two apart is why `driftSummary` has three states and not a
          boolean. A re-assert error is shown here too when the panel has since gone clean, so a
          failure never disappears with the card that triggered it. */}
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
