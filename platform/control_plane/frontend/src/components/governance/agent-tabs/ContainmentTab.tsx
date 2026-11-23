import { useEffect, useRef, useState } from 'react';
import { containmentApi } from '../../../api/client';
import type { Agent, StepResult, ContainmentView } from '../../../api/client';
import { useUser } from '../../../contexts/useUser';
import ConfirmDialog from '../../ConfirmDialog';
import {
  MIN_REASON,
  STEP_ORDER,
  auditNewestFirst,
  auditStamp,
  auditWhat,
  canSubmitReason,
  clock,
  containBlockedReason,
  containedInSeconds,
  createRequestGate,
  detectedInSeconds,
  isRestoreJob,
  maskArns,
  pollIntervalMs,
  restoreBlockedReason,
  restoredInSeconds,
  stepControl,
  stepLabel,
  stepSystem,
  type Actor,
} from './containmentLogic';

const CARD = 'bg-white/70 backdrop-blur rounded-xl border border-slate-200/60 shadow-sm';
const EYEBROW = 'text-[11px] uppercase tracking-wide text-slate-400 font-medium';
const MONO_CHIP = 'inline-flex items-center rounded-md border border-slate-200 bg-slate-50 px-1.5 py-0.5 font-mono text-[11px] text-slate-600';
const TEXTAREA = 'mt-1.5 w-full rounded-lg border border-slate-300 bg-white px-3 py-2 text-sm text-slate-800 placeholder:text-slate-400 focus:outline-none focus:ring-2 focus:ring-slate-400';
const MAX_BACKOFF_MS = 60000;

const message = (err: unknown, fallback: string) => (err instanceof Error ? err.message : fallback);

export default function ContainmentTab({
  agent,
  canOperate,
  canAdmin,
  onOpenTraces,
}: {
  agent: Agent;
  canOperate: boolean;
  canAdmin: boolean;
  onOpenTraces: () => void;
}) {
  const { user } = useUser();
  const actor = user?.name ?? user?.email ?? 'You';
  const me: Actor | null = user ? { oid: user.oid, email: user.email } : null;

  const [loaded, setLoaded] = useState<{ agentId: string; view: ContainmentView } | null>(null);
  const view = loaded !== null && loaded.agentId === agent.id ? loaded.view : null;
  const [loadError, setLoadError] = useState<string | null>(null);
  const [reason, setReason] = useState('');
  const [restoreReason, setRestoreReason] = useState('');
  const [busy, setBusy] = useState<'contain' | 'restore' | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [monitorBusy, setMonitorBusy] = useState(false);

  const gateRef = useRef(createRequestGate());
  const viewRef = useRef<ContainmentView | null>(null);
  const kickRef = useRef<(delayMs: number) => void>(() => {});
  const aliveRef = useRef(false);
  const setView = (v: ContainmentView) => {
    viewRef.current = v;
    setLoaded({ agentId: agent.id, view: v });
  };

  useEffect(() => {
    aliveRef.current = true;
    const gate = gateRef.current;
    viewRef.current = null;
    let timer: number | undefined;
    let failures = 0;

    const tick = async () => {
      timer = undefined;
      const ticket = gate.begin();
      try {
        const v = await containmentApi.get(agent.id);
        if (!aliveRef.current) return;
        if (gate.isCurrent(ticket)) {
          viewRef.current = v;
          setLoaded({ agentId: agent.id, view: v });
          setLoadError(null);
        }
        failures = 0;
      } catch (err) {
        if (!aliveRef.current) return;
        failures += 1;
        if (gate.isCurrent(ticket)) setLoadError(message(err, 'Could not load containment.'));
      }
      schedule(Math.min(pollIntervalMs(viewRef.current) * 2 ** failures, MAX_BACKOFF_MS));
    };
    const schedule = (delayMs: number) => {
      if (!aliveRef.current) return;
      if (timer !== undefined) window.clearTimeout(timer);
      timer = window.setTimeout(() => void tick(), delayMs);
    };

    kickRef.current = schedule;
    void tick();
    return () => {
      aliveRef.current = false;
      gate.begin();
      if (timer !== undefined) window.clearTimeout(timer);
      kickRef.current = () => {};
    };
  }, [agent.id]);

  const run = async (kind: 'contain' | 'restore', call: () => Promise<ContainmentView>, done: () => void) => {
    const gate = gateRef.current;
    gate.begin();
    setBusy(kind);
    setActionError(null);
    try {
      const v = await call();
      if (!aliveRef.current) return;
      gate.begin();
      setView(v);
      done();
      kickRef.current(pollIntervalMs(v));
    } catch (err) {
      if (!aliveRef.current) return;
      setActionError(message(err, 'Request failed.'));
      kickRef.current(0);
    } finally {
      if (aliveRef.current) setBusy(null);
    }
  };

  const onToggleMonitor = async () => {
    const current = viewRef.current;
    if (!current) return;
    const gate = gateRef.current;
    gate.begin();
    setMonitorBusy(true);
    setActionError(null);
    try {
      const r = await containmentApi.setMonitor(agent.id, !current.monitor.enabled);
      if (!aliveRef.current) return;
      gate.begin();
      const latest = viewRef.current ?? current;
      setView({ ...latest, monitor: { enabled: r.enabled } });
    } catch (err) {
      if (aliveRef.current) setActionError(message(err, 'Could not change the monitor.'));
    } finally {
      if (aliveRef.current) setMonitorBusy(false);
    }
  };

  if (!view) {
    return (
      <div className={`${CARD} p-8 text-center text-sm`}>
        {loadError ? <p className="text-red-600" role="alert">{loadError}</p> : <p className="text-slate-400">Loading containment…</p>}
      </div>
    );
  }

  const contained = view.state === 'CONTAINED';
  const started = view.steps.length > 0 && view.state !== null;
  const stage = !started ? (reason ? 2 : 1) : view.job === 'running' ? 3 : 4;

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h3 className="text-base font-semibold text-slate-900">Containment</h3>
          <p className="text-sm text-slate-500 mt-0.5">
            One audited verb that runs the runbook of native controls for <span className="font-medium text-slate-700">{agent.name}</span>.
          </p>
        </div>
        <StageRail stage={stage} />
      </div>

      {contained && <ContainedBanner view={view} />}
      {loadError && <p className="text-xs text-amber-700" role="status">Refresh failed: {loadError}. Showing the last known state.</p>}
      {actionError && <p className="text-sm text-red-600" role="alert">{actionError}</p>}

      <SignalCard
        view={view}
        monitorBusy={monitorBusy}
        canToggleMonitor={canOperate}
        onToggleMonitor={onToggleMonitor}
        onOpenTraces={onOpenTraces}
      />

      <ContainCard
        view={view}
        reason={reason}
        actor={actor}
        busy={busy === 'contain'}
        canOperate={canOperate}
        onReason={setReason}
        onContain={() => void run('contain', () => containmentApi.contain(agent.id, reason.trim()), () => setReason(''))}
      />

      {started && <ProgressCard view={view} />}

      {started && (
        <InvestigateCard
          view={view}
          user={me}
          restoreReason={restoreReason}
          busy={busy === 'restore'}
          canAdmin={canAdmin}
          onRestoreReason={setRestoreReason}
          onRestore={() => void run('restore', () => containmentApi.restore(agent.id, restoreReason.trim()), () => setRestoreReason(''))}
          onOpenTraces={onOpenTraces}
        />
      )}
    </div>
  );
}

const STAGES = ['Signal', 'Contain', 'Progress', 'Investigate'];

function StageRail({ stage }: { stage: number }) {
  return (
    <ol className="flex items-center gap-1.5 text-xs" aria-label="Containment stages">
      {STAGES.map((label, i) => {
        const n = i + 1;
        const state = n < stage ? 'done' : n === stage ? 'current' : 'next';
        return (
          <li key={label} className="flex items-center gap-1.5" aria-current={state === 'current' ? 'step' : undefined}>
            {i > 0 && <span className="h-px w-4 bg-slate-200" aria-hidden="true" />}
            <span
              className={`inline-flex h-5 w-5 items-center justify-center rounded-full text-[10px] font-semibold tabular-nums ${
                state === 'current' ? 'bg-slate-900 text-white' : state === 'done' ? 'bg-slate-200 text-slate-700' : 'border border-slate-200 text-slate-400'
              }`}
            >
              {n}
            </span>
            <span className={state === 'next' ? 'text-slate-400' : 'text-slate-700 font-medium'}>{label}</span>
          </li>
        );
      })}
    </ol>
  );
}

function ContainedBanner({ view }: { view: ContainmentView }) {
  return (
    <div className="flex items-start gap-3 rounded-xl border border-rose-200 bg-rose-50 px-4 py-3" role="status" data-testid="containment-banner">
      <span className="mt-1.5 h-2 w-2 flex-shrink-0 rounded-full bg-rose-600" aria-hidden="true" />
      <div className="min-w-0 text-sm">
        <p className="font-semibold text-rose-900">
          Contained{view.by_name ? ` by ${view.by_name}` : ''}{view.since ? ` at ${clock(view.since)}` : ''}
        </p>
        {view.reason && <p className="mt-0.5 text-rose-800">“{view.reason}”</p>}
      </div>
    </div>
  );
}

function RoadmapBadge() {
  return (
    <span className="inline-flex items-center gap-1 rounded-full border border-slate-200 bg-slate-50 px-2 py-0.5 text-[11px] font-medium text-slate-500">
      <span className="h-1.5 w-1.5 rounded-full bg-slate-300" aria-hidden="true" />
      Roadmap
    </span>
  );
}

export function SignalCard(props: {
  view: ContainmentView;
  monitorBusy: boolean;
  canToggleMonitor: boolean;
  onToggleMonitor: () => void;
  onOpenTraces: () => void;
}) {
  const { signals, monitor } = props.view;
  return (
    <section className={`${CARD} p-5`} aria-labelledby="containment-signal">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0">
          <div className={EYEBROW}>1 · Signal</div>
          <h4 id="containment-signal" className="mt-1 flex items-center gap-2 text-sm font-semibold text-slate-900">
            <span className={`h-2 w-2 rounded-full ${signals.length > 0 ? 'bg-rose-500' : 'bg-slate-300'}`} aria-hidden="true" />
            {signals.length > 0 ? `${signals.length} tool-misuse ${signals.length === 1 ? 'signal' : 'signals'}` : 'Watching tool calls'}
          </h4>
          <p className="mt-0.5 text-xs text-slate-500">Signal source: AgentCore Evaluations (on-demand)</p>
        </div>
        <label className="flex items-center gap-2 text-xs text-slate-600">
          Monitor
          <button
            type="button"
            role="switch"
            aria-checked={monitor.enabled}
            aria-label="Monitor this agent's tool calls"
            data-testid="containment-monitor"
            disabled={props.monitorBusy || !props.canToggleMonitor}
            onClick={props.onToggleMonitor}
            className={`relative inline-flex h-5 w-9 items-center rounded-full transition-colors focus:outline-none focus-visible:ring-2 focus-visible:ring-slate-400 disabled:opacity-50 ${
              monitor.enabled ? 'bg-slate-900' : 'bg-slate-300'
            }`}
          >
            <span className={`inline-block h-4 w-4 rounded-full bg-white shadow transition-transform ${monitor.enabled ? 'translate-x-4' : 'translate-x-0.5'}`} />
          </button>
        </label>
      </div>

      {signals.length === 0 ? (
        <p className="mt-4 border-t border-slate-100 pt-4 text-sm text-slate-500">
          No signals. The monitor judges every tool call this agent makes.
        </p>
      ) : (
        <ul className="mt-4 divide-y divide-slate-100 border-t border-slate-100">
          {signals.map((s) => (
            <li key={s.span_id} className="py-3 text-sm" data-testid="containment-signal-row">
              <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1">
                <span className="font-mono text-xs text-slate-400 tabular-nums">{clock(s.span_end)}</span>
                <span className="inline-flex items-center rounded-full bg-rose-50 px-2 py-0.5 text-[11px] font-semibold text-rose-700">{s.label}</span>
                {s.tool && <span className={MONO_CHIP}>{s.tool}</span>}
                <span className="text-xs text-slate-500 tabular-nums">Detected in {detectedInSeconds(s)} s</span>
                <button
                  type="button"
                  onClick={props.onOpenTraces}
                  className="ml-auto text-xs font-medium text-blue-600 hover:text-blue-700 transition-colors"
                  title={s.trace_id ? `Trace ${s.trace_id} · session ${s.session_id}` : `Session ${s.session_id}`}
                >
                  Open trace →
                </button>
              </div>
              <p className="mt-1 whitespace-pre-line text-slate-700">{maskArns(s.explanation.replace(/\\n/g, '\n'))}</p>
            </li>
          ))}
        </ul>
      )}

      <div className="mt-3 flex flex-wrap items-center gap-2 text-xs text-slate-400">
        <RoadmapBadge />
        <span>More signal sources: native online evaluation, Langfuse, Opik</span>
      </div>
    </section>
  );
}

export function ContainCard(props: {
  view: ContainmentView;
  reason: string;
  actor: string;
  busy: boolean;
  canOperate: boolean;
  onReason: (r: string) => void;
  onContain: () => void;
}) {
  const already = props.view.state === 'CONTAINED';
  const why = containBlockedReason(props.view, props.reason, props.canOperate);
  const ready = why === null && !props.busy;
  return (
    <section className={`${CARD} p-5`} aria-labelledby="containment-contain">
      <div className={EYEBROW}>2 · Contain</div>
      <h4 id="containment-contain" className="mt-1 text-sm font-semibold text-slate-900">Contain this agent</h4>
      <p className="mt-0.5 text-xs text-slate-500">Refuses invokes through AGP, forbids its tool calls at the gateway and denies direct invokes of its runtime.</p>

      <div className="mt-3 flex flex-wrap gap-1.5">
        {STEP_ORDER.map((id) => (
          <span key={id} className={MONO_CHIP}>{stepControl(id)}</span>
        ))}
      </div>

      <div className="mt-4 rounded-lg border border-slate-200 bg-slate-50/70 p-4">
        {already ? (
          <p className="text-xs text-slate-600">Already contained. A second person restores it under Investigate.</p>
        ) : (
          <>
            <label htmlFor="containment-reason" className="text-xs font-medium text-slate-700">
              Reason <span className="text-slate-400 font-normal">(required, goes on the audit record)</span>
            </label>
            <textarea
              id="containment-reason"
              rows={2}
              value={props.reason}
              onChange={(e) => props.onReason(e.target.value)}
              placeholder="e.g. Hiding notes for another agent in openClaim"
              className={TEXTAREA}
            />
          </>
        )}
        <div className="mt-3 flex flex-wrap items-center justify-between gap-3">
          <p className="text-xs text-slate-500">
            Acting as <span className="font-medium text-slate-700">{props.actor}</span> · {STEP_ORDER.length} native steps · Restore needs a second person
          </p>
          <button
            type="button"
            data-testid="containment-contain"
            onClick={props.onContain}
            disabled={!ready}
            aria-describedby={why ? 'containment-contain-why' : undefined}
            title={why ?? undefined}
            className="px-3.5 py-1.5 rounded-lg bg-rose-600 text-white text-xs font-semibold hover:bg-rose-700 disabled:bg-slate-300 disabled:cursor-not-allowed transition-colors"
          >
            {props.busy ? 'Containing…' : 'Contain'}
          </button>
        </div>
        {why && (
          <p id="containment-contain-why" data-testid="containment-contain-why" className="mt-2 text-right text-xs text-slate-500">
            {why}
          </p>
        )}
      </div>
    </section>
  );
}

export function ProgressCard({ view }: { view: ContainmentView }) {
  const restoring = isRestoreJob(view);
  const secs = restoring ? restoredInSeconds(view) : containedInSeconds(view);
  const done = view.job === 'contained' || view.job === 'restored';
  const failed = view.job === 'failed';
  const steps = STEP_ORDER.map((id) => view.steps.find((s) => s.id === id)).filter((s): s is StepResult => !!s);
  return (
    <section className={`${CARD} p-5`} aria-labelledby="containment-progress">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <div className={EYEBROW}>3 · Progress</div>
          <h4 id="containment-progress" className="mt-1 text-sm font-semibold text-slate-900">
            {restoring ? 'Restore: native runbook' : 'Contain: native runbook'}
          </h4>
        </div>
        <div className="text-right" aria-live="polite">
          <div className={`text-2xl font-semibold tabular-nums ${failed ? 'text-red-700' : done ? 'text-emerald-700' : 'text-slate-900'}`}>
            {secs === null ? '—' : `${secs} s`}
          </div>
          <div className="text-xs text-slate-500">
            {failed
              ? 'a step failed'
              : done && secs !== null
                ? `${restoring ? 'Restored' : 'Contained'} in ${secs} s`
                : view.job === 'running' ? 'containing…' : view.job === 'restoring' ? 'restoring…' : view.job === 'restored' ? 'Restored' : ''}
          </div>
        </div>
      </div>

      <ol className="mt-4 space-y-0">
        {steps.map((s, i) => (
          <StepRow key={s.id} step={s} restoring={restoring} last={i === steps.length - 1} />
        ))}
      </ol>
    </section>
  );
}

function StepIcon({ status }: { status: StepResult['status'] }) {
  if (status === 'ok')
    return (
      <span className="flex h-[19px] w-[19px] items-center justify-center rounded-full bg-emerald-600">
        <svg viewBox="0 0 20 20" className="h-3 w-3 text-white" fill="none" stroke="currentColor" strokeWidth={2.5} aria-hidden="true">
          <path strokeLinecap="round" strokeLinejoin="round" d="M5 10.5l3 3 7-7" />
        </svg>
      </span>
    );
  if (status === 'failed')
    return (
      <span className="flex h-[19px] w-[19px] items-center justify-center rounded-full bg-red-600">
        <svg viewBox="0 0 20 20" className="h-3 w-3 text-white" fill="none" stroke="currentColor" strokeWidth={2.5} aria-hidden="true">
          <path strokeLinecap="round" d="M6 6l8 8M14 6l-8 8" />
        </svg>
      </span>
    );
  if (status === 'skipped')
    return (
      <span className="flex h-[19px] w-[19px] items-center justify-center rounded-full border border-slate-300 bg-slate-100">
        <span className="h-px w-2 bg-slate-400" aria-hidden="true" />
      </span>
    );
  if (status === 'running')
    return <span className="h-[19px] w-[19px] rounded-full border-2 border-slate-200 border-t-slate-700 motion-safe:animate-spin" aria-hidden="true" />;
  return <span className="h-[11px] w-[11px] rounded-full border border-slate-300 bg-white" aria-hidden="true" />;
}

function StepRow({ step, restoring, last }: { step: StepResult; restoring: boolean; last: boolean }) {
  const dim = step.status === 'pending';
  return (
    <li className="relative flex gap-3 pb-3" data-testid={`containment-step-${step.id}`} data-status={step.status}>
      {!last && <span className="absolute left-[9px] top-5 bottom-0 w-px bg-slate-200" aria-hidden="true" />}
      <span className="relative mt-0.5 flex h-[19px] w-[19px] flex-shrink-0 items-center justify-center">
        <StepIcon status={step.status} />
      </span>
      <div className="min-w-0 flex-1">
        <div className="flex flex-wrap items-baseline justify-between gap-x-3">
          <span className={`text-sm ${dim ? 'text-slate-400' : 'text-slate-800'}`}>{stepLabel(step.id, restoring)}</span>
          <span className={`font-mono text-xs tabular-nums ${step.status === 'failed' ? 'text-red-600' : 'text-slate-400'}`}>
            {step.at ? clock(step.at) : step.status === 'running' ? 'running' : ''}
          </span>
        </div>
        <div className="mt-0.5 flex flex-wrap items-center gap-2 text-xs text-slate-500">
          <span>{stepSystem(step.id)}</span>
          <span className={MONO_CHIP}>{stepControl(step.id)}</span>
          {step.detail && <span className={step.status === 'failed' ? 'text-red-600' : ''} title={step.detail}>{maskArns(step.detail)}</span>}
        </div>
        {step.id === 'cedar-forbid' && !restoring && <p className="mt-1 text-xs text-slate-400">Running tasks lose their tools on their next call.</p>}
      </div>
      <span className="sr-only">{step.status}</span>
    </li>
  );
}

const RESULT_TONE: Record<string, string> = {
  ok: 'text-emerald-700',
  failed: 'text-red-600',
  skipped: 'text-slate-400',
  requested: 'text-slate-600',
};

export function InvestigateCard(props: {
  view: ContainmentView;
  user: Actor | null;
  restoreReason: string;
  busy: boolean;
  canAdmin: boolean;
  onRestoreReason: (r: string) => void;
  onRestore: () => void;
  onOpenTraces: () => void;
}) {
  const { view } = props;
  const blocked = restoreBlockedReason(view, props.user, props.canAdmin);
  const why = blocked ?? (canSubmitReason(props.restoreReason) ? null : `Give a reason (at least ${MIN_REASON} characters).`);
  const ready = why === null && !props.busy;
  const [confirming, setConfirming] = useState(false);
  return (
    <section className={`${CARD} p-5`} aria-labelledby="containment-investigate">
      <ConfirmDialog
        open={confirming}
        title="Restore this agent?"
        message={`Restore lifts all ${STEP_ORDER.length} controls: ${STEP_ORDER.map(stepControl).join(', ')}. The agent can be invoked and call its tools again. Your name and reason go on the audit record.`}
        confirmText="Restore"
        variant="warning"
        onConfirm={() => {
          setConfirming(false);
          props.onRestore();
        }}
        onCancel={() => setConfirming(false)}
      />
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <div className={EYEBROW}>4 · Investigate</div>
          <h4 id="containment-investigate" className="mt-1 text-sm font-semibold text-slate-900">Audit trail</h4>
          <p className="mt-0.5 text-xs text-slate-400">Append-only: one record per step, with the native reference.</p>
        </div>
        <button
          type="button"
          onClick={props.onOpenTraces}
          className="inline-flex items-center gap-1 text-xs font-medium text-blue-600 hover:text-blue-700 transition-colors"
        >
          Open this agent’s traces →
        </button>
      </div>

      <div className="mt-3 overflow-x-auto">
        <table className="w-full text-sm">
          <thead>
            <tr className="text-[11px] text-slate-400 uppercase tracking-wide border-b border-slate-100">
              <th className="text-left py-2 font-medium">When</th>
              <th className="text-left py-2 pl-4 font-medium">Who</th>
              <th className="text-left py-2 pl-4 font-medium">What changed</th>
              <th className="text-left py-2 pl-4 font-medium">Result</th>
            </tr>
          </thead>
          <tbody>
            {auditNewestFirst(view.audit).map((r, i) => (
              <tr key={`${r.at}-${i}`} className="border-b border-slate-50 align-top">
                <td className="py-2 font-mono text-xs text-slate-500 tabular-nums whitespace-nowrap">{auditStamp(r.at)}</td>
                <td className="py-2 pl-4 text-slate-700 whitespace-nowrap">{r.actor_name}</td>
                <td className="py-2 pl-4 text-slate-700">
                  <div>{auditWhat(r)}</div>
                  {r.detail && <div className="text-xs text-slate-500" title={r.detail}>{maskArns(r.detail)}</div>}
                  {r.native_ref && <div className="mt-0.5"><span className={`${MONO_CHIP} break-all`} title={r.native_ref}>{maskArns(r.native_ref)}</span></div>}
                  {r.step === 'cedar-forbid' && view.cloudtrail_url && (
                    <a
                      href={view.cloudtrail_url}
                      target="_blank"
                      rel="noreferrer"
                      data-testid="containment-cloudtrail"
                      className="mt-1 inline-block text-xs font-medium text-blue-600 hover:text-blue-700"
                    >
                      Find it in CloudTrail →
                    </a>
                  )}
                </td>
                <td className={`py-2 pl-4 text-xs whitespace-nowrap ${RESULT_TONE[r.result] ?? 'text-slate-600'}`}>{r.result}</td>
              </tr>
            ))}
            {view.audit.length === 0 && (
              <tr>
                <td colSpan={4} className="py-3 text-xs text-slate-400">No audit records yet.</td>
              </tr>
            )}
          </tbody>
        </table>
        <p className="mt-2 text-[11px] text-slate-400">Tool calls and invokes are CloudTrail data events (off by default).</p>
      </div>

      {view.state === 'CONTAINED' && (
        <div className="mt-4 rounded-lg border border-slate-200 bg-slate-50/70 p-4">
          <label htmlFor="containment-restore-reason" className="text-xs font-medium text-slate-700">
            Restore reason <span className="text-slate-400 font-normal">(required, a different person than {view.by_name ?? 'the one who contained'})</span>
          </label>
          <textarea
            id="containment-restore-reason"
            rows={2}
            value={props.restoreReason}
            onChange={(e) => props.onRestoreReason(e.target.value)}
            placeholder="e.g. Reviewed the traces: false positive"
            className={TEXTAREA}
          />
          <div className="mt-3 flex flex-wrap items-center justify-between gap-3">
            <p className="text-xs text-slate-500">Restore lifts the forbid, the runtime deny and the invoke gate.</p>
            <button
              type="button"
              data-testid="containment-restore"
              onClick={() => setConfirming(true)}
              disabled={!ready}
              aria-describedby={why ? 'containment-restore-why' : undefined}
              title={why ?? undefined}
              className="px-3.5 py-1.5 rounded-lg bg-white border border-slate-300 text-slate-800 text-xs font-semibold hover:bg-slate-50 disabled:text-slate-400 disabled:cursor-not-allowed transition-colors"
            >
              {props.busy ? 'Restoring…' : 'Restore'}
            </button>
          </div>
          {why && (
            <p id="containment-restore-why" data-testid="containment-restore-why" className="mt-2 text-right text-xs text-slate-500">
              {why}
            </p>
          )}
        </div>
      )}
    </section>
  );
}
