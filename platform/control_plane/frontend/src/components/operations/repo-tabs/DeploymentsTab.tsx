import { useCallback, useMemo, useState, type JSX } from 'react';

import { projectsApi, type Deployment, type Repository } from '../../../api/client';
import { ModalShell } from '../ModalShell';
import { NO_VALUE } from '../opsLabels';
import { shortBuildTag } from './evaluationsLogic';
import { OPS_BADGE, OPS_CARD, OPS_TABLE_DIVIDE, OPS_TABLE_HEAD } from '../opsUi';
import { type ProjectRoleName } from '../projectRoles';
import { useUser } from '../../../contexts/useUser';
import {
  actorText,
  actorTitle,
  offersRollback,
  type DeploymentActor,
  type DeploymentAttempt,
} from '../repositoryDetailTabs';
import {
  ROLLBACK_BLOCKED_NOTE,
  imageColumnLabel,
  rollbackActionMessage,
  rollbackEligibility,
  attemptRepoRef,
  stageHistories,
  type StageHistory,
} from './deploymentsTabModel';

const PILL =
  'inline-flex items-center gap-1.5 text-[11px] font-semibold px-2 py-0.5 rounded-full';
const COLUMNS = 6;

const OUTCOME_LABEL: Record<Deployment['outcome'], string> = {
  started: 'In progress',
  succeeded: 'Succeeded',
  failed: 'Failed',
};

const OUTCOME_BADGE_KEY: Record<Deployment['outcome'], keyof typeof OPS_BADGE> = {
  started: 'provisioning',
  succeeded: 'ready',
  failed: 'failed',
};

const ACTOR_KIND_CLASS: Record<DeploymentActor['kind'], string> = {
  github: 'text-slate-600',
  entra: 'text-slate-600',
  unknown: 'font-mono text-slate-500',
};

export interface DeploymentsTabProps {
  repo: Repository;
  deployments: readonly Deployment[];
  /** The history read failed, which is distinct from an empty history. */
  historyError: boolean;
  loading: boolean;
  heldRole: ProjectRoleName | null;
  roleLevel: number;
  onChanged: (updated: Repository) => void;
}

export default function DeploymentsTab({
  repo,
  deployments,
  historyError,
  loading,
  heldRole,
  roleLevel,
  onChanged,
}: DeploymentsTabProps): JSX.Element {
  const groups = useMemo(() => stageHistories(deployments), [deployments]);

  const [pending, setPending] = useState<{ attempt: DeploymentAttempt; currentTag: string | null } | null>(
    null,
  );
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  const handleRollback = useCallback(async () => {
    if (pending === null || submitting) return;
    const tag = pending.attempt.row.image_tag;
    const targetStage = pending.attempt.row.stage;
    setSubmitting(true);
    setError(null);
    setNotice(null);
    try {
      const updated = await projectsApi.rollbackRepo(repo.project_id, repo.id, {
        image_tag: tag,
        stage: targetStage,
      });
      setPending(null);
      setNotice(
        `Rolling ${targetStage} back to ${tag}. This page refreshes as the build progresses.`,
      );
      onChanged(updated);
    } catch (err: unknown) {
      setError(rollbackActionMessage(err));
    } finally {
      setSubmitting(false);
    }
  }, [pending, submitting, repo.project_id, repo.id, onChanged]);

  return (
    <div className="space-y-6">
      {notice && (
        <p className="text-sm text-emerald-700" role="status">
          {notice}
        </p>
      )}

      {loading && (
        <div className={`${OPS_CARD} p-8 text-center text-slate-400 text-sm`}>
          Loading deployment history…
        </div>
      )}

      {!loading && historyError && (
        <div className={`${OPS_CARD} p-6`}>
          <h3 className="text-sm font-semibold text-slate-800">
            Deployment history couldn’t be read
          </h3>
          <p className="text-sm text-slate-500 mt-1 max-w-2xl">
            So what this repository has shipped is unknown — it is not known to have shipped nothing.
            Reload the page to try again.
          </p>
        </div>
      )}

      {!loading && !historyError && groups.length === 0 && (
        <div className={`${OPS_CARD} p-6`}>
          <h3 className="text-sm font-semibold text-slate-800">Nothing deployed yet</h3>
          <p className="text-sm text-slate-500 mt-1 max-w-2xl">
            This repository has no delivery attempts on record. Every build that runs from here on
            appends to this history — including the ones that fail.
          </p>
        </div>
      )}

      {!loading &&
        !historyError &&
        groups.map((group) => (
          <StageSection
            key={group.stage}
            group={group}
            repo={repo}
            heldRole={heldRole}
            roleLevel={roleLevel}
            onPick={(attempt) => {
              setError(null);
              setNotice(null);
              setPending({ attempt, currentTag: group.currentTag });
            }}
          />
        ))}

      {pending !== null && (
        <ModalShell
          title={`Roll ${pending.attempt.row.stage} back`}
          description="Redeploy an image this stage previously ran successfully."
          ariaLabel="Confirm rollback"
          actionPending={submitting}
          onClose={() => {
            if (!submitting) setPending(null);
          }}
          footer={
            <>
              <button
                type="button"
                onClick={() => setPending(null)}
                disabled={submitting}
                className="px-3.5 py-1.5 rounded-lg bg-white border border-slate-300 text-slate-700 text-sm font-medium hover:bg-slate-50 transition-colors disabled:opacity-40"
              >
                Cancel
              </button>
              <button
                type="button"
                onClick={() => void handleRollback()}
                disabled={submitting}
                className="px-3.5 py-1.5 rounded-lg bg-amber-600 text-white text-sm font-medium hover:bg-amber-700 transition-colors disabled:opacity-40 disabled:cursor-not-allowed"
              >
                {submitting ? 'Starting…' : 'Roll back'}
              </button>
            </>
          }
        >
          <p className="text-sm text-slate-600">
            <span className="font-medium text-slate-900">{pending.attempt.row.stage}</span> will be
            redeployed from{' '}
            <span className="font-mono text-xs text-slate-700">{pending.attempt.row.image_tag}</span>
            {pending.currentTag !== null && (
              <>
                {' '}
                instead of{' '}
                <span className="font-mono text-xs text-slate-700">{pending.currentTag}</span>
              </>
            )}
            .
          </p>
          <p className="text-sm text-slate-600">
            This starts a real build and deploy — it is not a config switch. The pending prod
            candidate, if there is one, is left alone so it can still be promoted once the incident is
            over.
          </p>
          <p className="text-[11px] text-slate-400">
            Older images are eventually pruned from the registry. If this one has already been
            removed, the build will fail when it tries to pull it — the history keeps the record
            either way.
          </p>
          {error && (
            <p className="text-sm text-red-600" role="alert">
              {error}
            </p>
          )}
        </ModalShell>
      )}
    </div>
  );
}

function StageSection({
  group,
  repo,
  heldRole,
  roleLevel,
  onPick,
}: {
  group: StageHistory;
  repo: Repository;
  heldRole: ProjectRoleName | null;
  roleLevel: number;
  onPick: (attempt: DeploymentAttempt) => void;
}): JSX.Element {
  return (
    <div className={`${OPS_CARD} overflow-hidden`}>
      <div className="flex flex-wrap items-baseline justify-between gap-2 px-4 py-3">
        <h3 className="text-sm font-semibold text-slate-900">{group.stage}</h3>
        <span className="text-[11px] text-slate-400">
          {group.currentTag === null ? (
            'Nothing is serving here'
          ) : (
            <>
              serving <span className="font-mono">{group.currentTag}</span>
            </>
          )}
        </span>
      </div>
      <table className="min-w-full text-sm">
        <thead className={OPS_TABLE_HEAD}>
          <tr>
            <th className="text-left px-4 py-2 font-medium">{imageColumnLabel(group.attempts)}</th>
            <th className="text-left px-4 py-2 font-medium">Outcome</th>
            <th className="text-left px-4 py-2 font-medium">When</th>
            <th className="text-left px-4 py-2 font-medium">Deployed by</th>
            <th className="text-left px-4 py-2 font-medium">Source</th>
            <th className="text-right px-4 py-2 font-medium">
              <span className="sr-only">Rollback</span>
            </th>
          </tr>
        </thead>
        <tbody className={OPS_TABLE_DIVIDE}>
          {group.attempts.length === 0 && (
            <tr>
              <td colSpan={COLUMNS} className="px-4 py-6 text-center text-slate-400 text-xs">
                No attempts recorded for this stage.
              </td>
            </tr>
          )}
          {group.attempts.map((attempt) => (
            <AttemptRow
              key={attempt.row.id}
              attempt={attempt}
              currentTag={group.currentTag}
              serving={attempt.row.id === group.currentAttemptId}
              repo={repo}
              heldRole={heldRole}
              roleLevel={roleLevel}
              onPick={onPick}
            />
          ))}
        </tbody>
      </table>
    </div>
  );
}

function AttemptRow({
  attempt,
  currentTag,
  serving,
  repo,
  heldRole,
  roleLevel,
  onPick,
}: {
  attempt: DeploymentAttempt;
  currentTag: string | null;
  serving: boolean;
  repo: Repository;
  heldRole: ProjectRoleName | null;
  roleLevel: number;
  onPick: (attempt: DeploymentAttempt) => void;
}): JSX.Element {
  const { user } = useUser();
  const row = attempt.row;
  const eligibility = offersRollback(repo)
    ? rollbackEligibility({
        held: heldRole,
        roleLevel,
        cicdStatus: repo.cicd_status,
        outcome: attempt.outcome,
        imageTag: row.image_tag,
        currentTag,
      })
    : 'not-a-target';
  const repoRef = attemptRepoRef(repo.id, row);

  return (
    <tr>
      <td className="px-4 py-3">
        <span className="font-mono text-xs text-slate-600" title={row.image_tag ?? undefined}>{shortBuildTag(row.image_tag)}</span>
        {serving && (
          <span className="block text-[11px] text-emerald-700 font-medium">Currently serving</span>
        )}
        {repoRef.marker !== null && (
          <span
            className="block text-[11px] text-slate-400 italic"
            title="This delivery belongs to a repository that no longer exists. Deployment history is retained, so the record of what reached production outlives the repository."
          >
            {repoRef.marker}
          </span>
        )}
      </td>

      <td className="px-4 py-3">
        <span className={`${PILL} ${OPS_BADGE[OUTCOME_BADGE_KEY[attempt.outcome]]}`}>
          <span aria-hidden="true">●</span>
          {OUTCOME_LABEL[attempt.outcome]}
        </span>
        {row.error && (
          <span className="block mt-1 text-[11px] text-rose-700 break-words">{row.error}</span>
        )}
      </td>

      <td className="px-4 py-3 text-slate-600 text-xs tabular-nums">
        <span title={row.completed_at ?? row.started_at}>{row.started_at.slice(0, 10)}</span>
      </td>

      <td className="px-4 py-3 text-xs">
        {attempt.actor ? (
          <span
            className={ACTOR_KIND_CLASS[attempt.actor.kind]}
            title={actorTitle(attempt.actor)}
          >
            {actorText(attempt.actor, user)}
          </span>
        ) : (
          <span className="text-slate-400" title="This attempt was recorded by the build itself, which has no human actor.">
            {NO_VALUE}
          </span>
        )}
      </td>

      <td className="px-4 py-3 text-xs">
        {attempt.shortSha ? (
          <span className="font-mono text-slate-500">{attempt.shortSha}</span>
        ) : (
          <span className="text-slate-400">{NO_VALUE}</span>
        )}
      </td>

      <td className="px-4 py-3 text-right">
        {eligibility === 'ok' ? (
          <button
            type="button"
            onClick={() => onPick(attempt)}
            className="px-3 py-1.5 rounded-lg bg-white border border-amber-300 text-amber-700 text-xs font-medium hover:bg-amber-50 transition-colors"
          >
            Roll back to this
          </button>
        ) : (
          ROLLBACK_BLOCKED_NOTE[eligibility] !== null && (
            <span className="text-[11px] text-slate-400">{ROLLBACK_BLOCKED_NOTE[eligibility]}</span>
          )
        )}
      </td>
    </tr>
  );
}
