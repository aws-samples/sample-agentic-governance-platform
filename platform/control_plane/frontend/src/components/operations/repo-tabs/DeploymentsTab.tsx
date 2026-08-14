// DeploymentsTab — the repository's delivery history, per stage, with a role-gated rollback
// (E28/T12, contracts C1 + C2 — D5/D7).
//
// ---------------------------------------------------------------------------
// WHY THIS TAB EXISTS
//
// Before E28/T3 the platform remembered only the LATEST release: the `last_promoted_*` scalars on
// `Repository` are overwritten wholesale, so every promote erased the evidence of the one before it.
// The append-only `Deployment` partition is the record that replaced them, and this tab is the first
// surface that reads it as a history rather than as a cache. That is also what makes ROLLBACK
// possible at all — a rollback needs an earlier artifact to name, and until there was a record of
// one there was nothing to roll back TO.
//
// ---------------------------------------------------------------------------
// ONE ATTEMPT, TWO ROWS — AND THE COLLAPSE IS IMPORTED
//
// Every judgement here lives in `deploymentsTab.ts`, which IMPORTS `collapseByBuild` from
// `repositoryDetailTabs.ts` rather than re-deriving it. That matters more than any markup in this
// file: the partition is append-only, so one attempt is two rows, and nothing ever closes the launch
// row. A naive reader shows every historical deployment as perpetually in flight, and reads the
// actor and the source sha off the terminal row — which structurally carries neither. Both defects
// shipped once and review caught them; the corrected rule is shared, tested, and consumed.
//
// This file renders from `DeploymentAttempt` and never touches a raw row's join key.
//
// ---------------------------------------------------------------------------
// GROUPED BY STAGE, AND NO STAGE IS NAMED
//
// Stages come from the HISTORY (not the tenant record — see `stageHistories` for why the two
// authorities differ), are free-form (D8), and are compared to nothing. The rollback confirm sends
// the attempt's OWN stage, so this file contains no stage literal and a guard enforces that.
//
// NO RUNTIME STATUS APPEARS HERE. The runtime reading is per-AGENT — one runtime ARN, overwritten by
// whichever stage deployed last — so the page renders it ONCE in the header where its scope is
// truthfully the agent. Made mechanical the same way `EnvironmentStrip` does it: this file imports
// neither runtime table, so a per-stage runtime claim is unreachable rather than merely discouraged.
//
// House style: emerald-on-glass Ops tokens (`opsUi.ts`), the `ConnectionsAdmin` ModalShell for the
// confirm, Tailwind v4 utility strings, 2-space indent.

import { useCallback, useMemo, useState, type JSX } from 'react';

import { projectsApi, type Deployment, type Repository } from '../../../api/client';
import { ModalShell } from '../ConnectionsAdmin';
import { NO_VALUE } from '../opsLabels';
import { OPS_BADGE, OPS_CARD, OPS_TABLE_DIVIDE, OPS_TABLE_HEAD } from '../opsUi';
import { type ProjectRoleName } from '../projectRoles';
import {
  ACTOR_KIND_TITLE,
  type DeploymentActor,
  type DeploymentAttempt,
} from '../repositoryDetailTabs';
import {
  ROLLBACK_BLOCKED_NOTE,
  isCurrentAttempt,
  rollbackActionMessage,
  rollbackEligibility,
  attemptRepoRef,
  stageHistories,
  type StageHistory,
} from './deploymentsTab';

const PILL =
  'inline-flex items-center gap-1.5 text-[11px] font-semibold px-2 py-0.5 rounded-full';
const COLUMNS = 6;

/**
 * The outcome pill's tint and label.
 *
 * A LOCAL table rather than a reach for `CICD_*`: these are the append-only row's own three
 * outcomes (C1), a different and much smaller state machine than `cicd_status`'s six — and
 * `opsStatus.ts`'s whole premise is that each surface owns its styling while the DECISIONS stay
 * shared. There is no decision in a three-entry `Record` over a closed union the client already
 * declares, and `tsc` still makes a fourth outcome an error naming this table.
 *
 * `started` reads "In progress" only because the collapse has already retired every launch row whose
 * build finished; without that this label would be permanently wrong on every historical row.
 */
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

/**
 * How each actor currency is SET, over the same three kinds `ACTOR_KIND_TITLE` covers.
 *
 * A `Record` rather than a ternary, for the reason the `title` is already a `Record`: the union has
 * THREE members, so a two-way `kind === … ? … : …` gives whichever kind it does not name the other
 * one's treatment. An actor recorded without a provider would then be typeset exactly like a
 * provider login — the same false-identity mistake, one attribute along from where it was caught.
 * With this table a fourth kind is a `tsc` error naming this line.
 *
 * A raw object id is monospaced because it is an opaque identifier to be compared character by
 * character; a login is prose and is set as prose. An UNESTABLISHED currency gets the monospaced,
 * dimmer treatment of the raw id — the conservative choice, since dressing it as a login would be
 * the assertion nobody made.
 */
const ACTOR_KIND_CLASS: Record<DeploymentActor['kind'], string> = {
  github: 'text-slate-600',
  entra: 'font-mono text-slate-500',
  unknown: 'font-mono text-slate-500',
};

export interface DeploymentsTabProps {
  repo: Repository;
  /** The agent's history, any order. */
  deployments: readonly Deployment[];
  /** The history read FAILED — distinct from an empty history, which is a claim. */
  historyError: boolean;
  loading: boolean;
  /** The caller's role on this project, from the server's `effective_role`. */
  heldRole: ProjectRoleName | null;
  roleLevel: number;
  /** Refetch the page after a rollback starts — the record moves to an in-flight status. */
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

  // The attempt awaiting confirmation. A rollback is a write to production, so it is never a
  // one-click affordance — the confirm names the target, the stage and what is being replaced.
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
    // Cleared too: without this a second rollback that FAILS renders the rose error beside the stale
    // emerald notice — two contradictory statements about one action.
    setNotice(null);
    try {
      // The attempt's OWN tag and OWN stage. Neither is defaulted or invented here: the service
      // validates the pair against its succeeded rows, so guessing either would turn a validation
      // into a refusal.
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
      setError(rollbackActionMessage(err instanceof Error ? err.message : ''));
    } finally {
      setSubmitting(false);
    }
  }, [pending, submitting, repo.project_id, repo.id, onChanged]);

  return (
    <div className="space-y-6">
      {/* The rollback FAILURE is not rendered here — it belongs inside the confirm, where the
          `error` state is the only place it can be seen. See the note at the ModalShell below. The
          `notice` does belong here: it is only ever set once the confirm has closed. */}
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

      {/* A FAILED READ IS NOT AN EMPTY HISTORY. An empty array is indistinguishable from "this
          repository has never deployed", and rendering the failure as that positive claim is the
          reassuring direction — the same rule as an absent runtime reading unknown rather than ready.
          Nothing is derived from the (empty) rows in this state. */}
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

      {/* A genuinely empty history, which is an ordinary state for a freshly scaffolded repository. */}
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
          {/* The confirm's job is to name exactly what changes. `pending.currentTag` is what the
              stage is serving now — the sentence is meaningless without it, and the eligibility
              rules already guarantee the two tags differ. */}
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
          {/* P8, stated rather than hidden: the container registry retains a bounded number of
              images TENANT-WIDE while this history can offer far more rows, so a tag that was
              genuinely deployed may no longer have an image behind it. That fails later, at image
              pull — not at this confirm — and the panel does not pretend otherwise. Adding a
              registry existence check is out of scope, and promising success would be worse than
              saying this. */}
          <p className="text-[11px] text-slate-400">
            Older images are eventually pruned from the registry. If this one has already been
            removed, the build will fail when it tries to pull it — the history keeps the record
            either way.
          </p>
          {/* THE FAILURE BELONGS HERE, INSIDE THE CONFIRM. `error` is set only in the rollback's
              catch, which cannot run unless this confirm is open — and this confirm is a fixed,
              full-viewport, backdrop-blurred overlay at a raised stacking level. So the same
              paragraph placed at the top of the tab would be painted UNDERNEATH it: the operator
              would see "Roll back" re-enable with no stated reason, and every mapped literal
              (`rollbackActionMessage`) would be invisible at the one moment it matters. Rendering
              the action's error inside its own modal is the house precedent
              (`ProjectRepositoriesTab.tsx` does exactly this with its `actionError`), and a guard
              pins the placement because no `.ts` mutation can reach it. */}
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

// ---------------------------------------------------------------------------
// One section per stage. The table's columns are fixed: Image · Outcome · When · Deployed by ·
// Source · the rollback affordance.
// ---------------------------------------------------------------------------
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
        {/* The stage's own name, verbatim. Free-form (D8). */}
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
            <th className="text-left px-4 py-2 font-medium">Image</th>
            <th className="text-left px-4 py-2 font-medium">Outcome</th>
            <th className="text-left px-4 py-2 font-medium">When</th>
            {/* "Deployed by" (P10). The promotion-flavoured wording used elsewhere became false the
                moment this row could also record a rollback — a roller-back did not promote
                anything. T13 relabels the other site to match; a third wording here would be the
                drift both changes exist to remove. A guard asserts the old wording appears nowhere
                in this file, which is why this comment does not spell it out: the guard reads raw
                source and does not skip comments, deliberately. */}
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
  repo,
  heldRole,
  roleLevel,
  onPick,
}: {
  attempt: DeploymentAttempt;
  currentTag: string | null;
  repo: Repository;
  heldRole: ProjectRoleName | null;
  roleLevel: number;
  onPick: (attempt: DeploymentAttempt) => void;
}): JSX.Element {
  const row = attempt.row;
  // The gate is DECIDED in the `.ts`, where a test reaches it — never re-derived from a role here.
  const eligibility = rollbackEligibility({
    held: heldRole,
    roleLevel,
    cicdStatus: repo.cicd_status,
    outcome: attempt.outcome,
    imageTag: row.image_tag,
    currentTag,
  });
  // A FACT, not an affordance, so it renders for everyone — including a caller who may not roll
  // anything back. Withholding "this is what is live" from every viewer would be the opposite of
  // what an Ops panel is for.
  const current = isCurrentAttempt(row.image_tag, currentTag);
  // WHOSE repository is this row's? (E28C/T7, D-C5.) Deleting a repository does NOT delete its
  // deployment rows — retention is deliberate, the partition is append-only, and a deleted
  // repository's production history is exactly the evidence it exists to keep. But this page
  // ATTRIBUTES every row it renders to the repository in its own title, so a retained row from a
  // since-deleted repository would read as this one's delivery history. Decided in the `.ts`.
  const repoRef = attemptRepoRef(repo.id, row);

  return (
    <tr>
      <td className="px-4 py-3">
        <span className="font-mono text-xs text-slate-600">{row.image_tag}</span>
        {current && (
          <span className="block text-[11px] text-emerald-700 font-medium">Currently serving</span>
        )}
        {/* The row is KEPT and stays readable — this only withdraws the page's implicit claim that
            it belongs to the repository on screen. Quiet slate, not a fault tint: nothing here is
            broken, and the copy comes from the model so it cannot be re-worded per surface. */}
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
        {/* The row's SAFE short hint, when the backend recorded one. Never a token or a body (C1). */}
        {row.error && (
          <span className="block mt-1 text-[11px] text-rose-700 break-words">{row.error}</span>
        )}
      </td>

      {/* WHEN. The date only, with the full timestamp in the title. NO how-long-it-took figure of any
          kind, deliberately (P9): a build-written terminal row sets its start time to the COMPLETION
          time, so any span computed from that row alone is zero — a confident, wrong number. The
          launch partner holds the real start, but presenting a span assembled from two append-only
          rows as one measurement is more precision than this panel has earned. A guard asserts no
          such arithmetic appears in this file or its companion. */}
      <td className="px-4 py-3 text-slate-600 text-xs tabular-nums">
        <span title={row.completed_at ?? row.started_at}>{row.started_at.slice(0, 10)}</span>
      </td>

      {/* WHO, in its OWN CURRENCY. A provider login and a platform object id are never rendered as
          one thing (E27A §6). The actor comes off the attempt's launch row — the terminal row carries
          none by design — and a build with no launch partner has no actor at all, which reads as
          ABSENT rather than as "unknown user".
          Both currency labels come from `ACTOR_KIND_TITLE`, a `Record` over all three kinds, and
          neither is written in this file: a two-way ternary is what put an unlabelled actor under the
          provider's label once, asserting an identity nobody established. */}
      <td className="px-4 py-3 text-xs">
        {attempt.actor ? (
          <span
            className={ACTOR_KIND_CLASS[attempt.actor.kind]}
            title={ACTOR_KIND_TITLE[attempt.actor.kind]}
          >
            {attempt.actor.display}
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

      {/* The rollback affordance, CONDITIONALLY RENDERED rather than `disabled` (the epic's FE
          constraint): `disabled` is reserved for an in-flight request, so a caller without the
          standing is never shown a button whose every click 403s. A role refusal shows NOTHING at
          all — `ROLLBACK_BLOCKED_NOTE` is null for it — while the in-flight refusal gets a sentence,
          because that one resolves on its own. */}
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
