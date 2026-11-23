import { type JSX, type Ref, useId, useState } from 'react';

import type { EvalEvidence, Repository, LifecycleState } from '../../api/client';

import { ARTIFACT_TONE_CLS, PROMOTE_CANCEL_BTN, PROMOTE_CONFIRM_BTN } from './promoteConfirmModel';
import { CANDIDATE_ACTOR_VERB, isCommitShaTag, shortCommitSha } from './repoRowModel';
import { type PromotionArtifact } from './repositoryDetailTabs';
import {
  GATE_TONE_CLS,
  OVERRIDE_REASON_MAX,
  canSubmitPromote,
  evidenceLine,
  governanceLine,
  overrideReasonToSend,
  promoteGateView,
  regressionLine,
} from './promoteGate';

const DT_CLS =
  'text-[11px] uppercase tracking-wide text-slate-400 font-medium w-16 shrink-0';
const CODE_CLS =
  'px-2 py-0.5 rounded-md bg-slate-100 border border-slate-200 font-mono text-xs text-slate-700 break-all';

export interface PromoteCandidate {
  shortSha: string | null;
  actor: string | null;
  imageTag: string | null;
}

export interface PromoteConfirmProps {
  repoName: string;
  /** The candidate's provenance; null when the record holds no pending one. */
  candidate: PromoteCandidate | null;
  artifact: PromotionArtifact;
  devImageTag: string | null;
  /** The candidate build's eval gate; null or absent means unknown. */
  evalGate?: Repository['eval_gate'];
  evalRegressions?: string[] | null;
  evalEvidence?: EvalEvidence | null;
  lifecycleState?: LifecycleState | null;
  /** Opens the candidate run in the Evaluations tab; absent means no View run link. */
  onViewRun?: (runId: string) => void;
  pending: boolean;
  /** Receives the override reason only when the gate is not passed. */
  onConfirm: (overrideReason?: string) => void;
  onCancel: () => void;
  confirmRef?: Ref<HTMLButtonElement>;
}

export default function PromoteConfirm({
  repoName,
  candidate,
  artifact,
  devImageTag,
  evalGate,
  evalRegressions,
  evalEvidence,
  lifecycleState,
  onViewRun,
  pending,
  onConfirm,
  onCancel,
  confirmRef,
}: PromoteConfirmProps): JSX.Element {
  const isRevision = isCommitShaTag(candidate?.imageTag);
  const gate = promoteGateView(evalGate, evalEvidence?.status);
  const regressions = regressionLine(evalRegressions);
  const evidence = evidenceLine(evalEvidence);
  const governance = governanceLine(lifecycleState);
  const gateTone = GATE_TONE_CLS[gate.tone];
  const [reason, setReason] = useState('');
  const reasonId = useId();
  const reasonNoteId = useId();
  const canSubmit = canSubmitPromote(gate, reason);
  return (
    <div
      role="group"
      aria-label={`Confirm promoting ${repoName} to prod`}
      className="rounded-lg bg-white/80 border border-emerald-200/70 px-3.5 py-3"
    >
      <p className="text-sm text-slate-700">
        Promote <span className="font-semibold text-slate-900">{repoName}</span> to{' '}
        <span className="font-semibold text-slate-900">prod</span>?
      </p>

      <p className="mt-1.5 flex flex-wrap items-center gap-x-2 gap-y-0.5 text-xs" role="status">
        <span className={`inline-block h-2 w-2 rounded-full ${gateTone.dot}`} aria-hidden="true" />
        <span className={`font-semibold ${gateTone.text}`}>{gate.label}</span>
        <span className="text-slate-500">{gate.note}</span>
      </p>
      {(evidence || (evalEvidence && onViewRun)) && (
        <p className="mt-1 flex flex-wrap items-center gap-x-2 text-xs text-slate-600">
          {evidence && <span className="tabular-nums">{evidence}</span>}
          {evalEvidence && onViewRun && (
            <button
              type="button"
              onClick={() => onViewRun(evalEvidence.run_id)}
              className="font-medium text-blue-600 hover:underline"
            >
              View run
            </button>
          )}
        </p>
      )}
      {regressions && <p className="mt-1 text-xs text-amber-700 break-words">{regressions}</p>}
      {governance && (
        <p className={`mt-1 text-xs font-medium ${governance.ok ? 'text-emerald-700' : 'text-amber-700'}`}>
          {governance.text}
        </p>
      )}
      <p className="text-xs text-slate-500 mt-1">
        This deploys the commit that landed on{' '}
        <span className="font-medium text-slate-600">main</span>, described below. Prod traffic
        moves to it once the deployment finishes. Any review of the change happened on the
        repository — approving here authorises the deployment, not the code.
      </p>

      <dl className="mt-2.5 grid gap-1.5 text-xs">
        <div className="flex flex-wrap items-center gap-2">
          <dt className={DT_CLS}>Commit</dt>
          <dd className="flex flex-wrap items-center gap-1.5">
            <span className="text-slate-500">main @</span>
            <code className={CODE_CLS}>{candidate?.shortSha ?? '—'}</code>
          </dd>
        </div>

        <div className="flex flex-wrap items-center gap-2">
          <dt className={`${DT_CLS} capitalize`}>{CANDIDATE_ACTOR_VERB}</dt>
          <dd className="text-slate-700 font-medium">
            {candidate?.actor ?? '—'}
            <span className="ml-1.5 font-normal text-slate-400">on GitHub</span>
          </dd>
        </div>

        <div className="flex flex-wrap items-center gap-2">
          <dt className={DT_CLS}>{isRevision ? 'Revision' : 'Image'}</dt>
          <dd className="flex flex-wrap items-center gap-2">
            <code className={CODE_CLS}>{candidate?.imageTag ?? '—'}</code>
            {devImageTag &&
              (candidate?.imageTag === devImageTag ? (
                <span className="text-[11px] text-slate-400">
                  same {isRevision ? 'revision' : 'image'} dev is running
                </span>
              ) : (
                <span className="text-[11px] text-amber-700">
                  dev is running{' '}
                  <code className="font-mono break-all">
                    {isCommitShaTag(devImageTag) ? shortCommitSha(devImageTag) : devImageTag}
                  </code>{' '}
                  — prod will not match dev
                </span>
              ))}
          </dd>
        </div>

        {artifact.marker !== null && (
          <div className="flex flex-wrap items-center gap-2">
            <dt className={DT_CLS}>Artifact</dt>
            <dd className="flex flex-wrap items-center gap-2">
              <span
                className={ARTIFACT_TONE_CLS[artifact.marker.tone]}
                title={artifact.marker.note ?? undefined}
              >
                {artifact.marker.text}
              </span>
            </dd>
          </div>
        )}
      </dl>

      {gate.needsOverride && (
        <div className="mt-3">
          <label htmlFor={reasonId} className="block text-xs font-medium text-slate-700">
            Override reason <span className="text-amber-700">(required)</span>
          </label>
          <textarea
            id={reasonId}
            value={reason}
            onChange={(e) => setReason(e.target.value)}
            maxLength={OVERRIDE_REASON_MAX}
            rows={2}
            autoFocus
            disabled={pending}
            aria-required="true"
            aria-describedby={reasonNoteId}
            placeholder="Why does this build go to prod without a passing evaluation?"
            className="mt-1 w-full rounded-md border border-slate-300 bg-white px-2.5 py-1.5 text-xs text-slate-800 placeholder:text-slate-400 focus:border-emerald-500 focus:outline-none focus:ring-1 focus:ring-emerald-500 disabled:opacity-60"
          />
          <div id={reasonNoteId} className="mt-0.5 flex justify-between gap-2 text-[11px] text-slate-400">
            <span>The override is recorded with your name.</span>
            <span className="tabular-nums">
              {reason.length}/{OVERRIDE_REASON_MAX}
            </span>
          </div>
        </div>
      )}

      <div className="flex items-center gap-2 mt-3">
        <button
          ref={confirmRef}
          type="button"
          onClick={() => onConfirm(overrideReasonToSend(gate, reason))}
          disabled={pending || !canSubmit}
          className={PROMOTE_CONFIRM_BTN}
        >
          {pending ? 'Starting…' : gate.needsOverride ? 'Promote with override' : 'Promote to prod'}
        </button>
        <button type="button" onClick={onCancel} disabled={pending} className={PROMOTE_CANCEL_BTN}>
          Cancel
        </button>
      </div>
    </div>
  );
}
