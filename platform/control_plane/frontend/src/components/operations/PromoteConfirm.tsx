// PromoteConfirm — THE consent moment for a production deployment (E28C/T7, D-C4d).
//
// ---------------------------------------------------------------------------
// WHY THIS FILE EXISTS
//
// Promotion is the highest-consequence verb in the product, and until this task there were TWO
// entry points to it with ONE dialog between them: the project tab's row had the ~140-line
// reveal-then-confirm block below, and the repository detail page's header button fired
// `handlePromote` straight off a bare `onClick` — no dialog, no provenance, no marker. So the
// surface with MORE evidence available had LESS consent around it.
//
// 4d's ruling inverted the obvious fix. Rather than clone the dialog onto the second surface, the
// ROW's button was removed (the row keeps a passive `PROMOTION_READY_LABEL` indicator, see
// `promotionReadiness`) and this component became the one dialog behind the one remaining entry
// point. Two copies of a consent screen is strictly worse than one: they drift, and the half that
// drifts is the half a reviewer is not reading.
//
// ---------------------------------------------------------------------------
// WHAT IT NAMES, AND WHY EACH LINE IS LOAD-BEARING
//
// Everything here is DISPLAYED, never entered. `promoteRepo` takes no body: the backend resolves
// the image from `prod_candidate_image_tag`, written from a validated GitHub OIDC token, and the
// actor from the validated principal. There is no input on this screen through which an arbitrary
// image could reach production, which is what makes it safe for the dialog to be purely a reveal.
//
//   • THE COMMIT and WHO PUSHED IT — provenance. The verb is `CANDIDATE_ACTOR_VERB` and it is
//     "pushed by", NEVER "merged by": the candidate is registered on ANY push to `main`, so
//     nothing in the record distinguishes a merged PR from a direct commit, and the merge-flavoured
//     wording implied a review the platform never saw.
//   • THE IMAGE, and whether prod will MATCH DEV. Both tags embed the same tree sha, so equal tags
//     mean prod ends up running exactly what dev has been running. Stated outright rather than
//     left to a reader diffing two near-identical strings.
//   • THE ARTIFACT — exact bytes, or a mutable pointer. This is the epic's headline guarantee and
//     the reason this dialog is not merely a "are you sure?": a digest-pinned candidate and a
//     tag-only one used to look IDENTICAL at the moment an owner authorises production.
//
// IT IS CAREFUL NOT TO IMPLY AGP REVIEWED THE CODE. AGP holds no PR or review state (design §6) —
// the provider governs what lands on `main`; this surface governs only whether the result reaches
// production. So the copy says the commit "landed on main" and asks the owner to authorise the
// DEPLOYMENT, never that the change was approved or checked here.
//
// ---------------------------------------------------------------------------
// ONE BRANCHLESS ARTIFACT VALUE, BY CONSTRUCTION
//
// `promotionArtifact` resolves the marker's text, tooltip AND tone; this renders that one value.
// The history is why it matters: an earlier version returned `{kind, imageTag, digest}` and let
// the component branch (`artifact.digest !== null ? <digest/> : <caution/>`), a reviewer made the
// caution arm unreachable, and the whole suite stayed green — there is no jsdom here, so a guard
// over a `.tsx` reads text and cannot tell a live arm from a dead one. DO NOT reintroduce a
// condition over the marker. The occurrence-counting guard in `repositoryDetailTabs.test.ts` now
// counts THIS file for exactly that reason.
//
// The tone table is the ONLY copy: it was duplicated in `RepositoryDetail.tsx` and
// `ProjectRepositoriesTab.tsx`, which is the same two-copies-of-one-table shape that once shipped a
// production repo wearing provisioning's amber. It now lives in `promoteConfirm.ts` beside this file,
// where a test can index it instead of regexing a source slice.
//
// House style: emerald-on-glass Ops tokens, Tailwind v4 utility strings, 2-space indent. This
// file DECIDES nothing — `prodCandidateView` and `promotionArtifact` do, where tests reach them.

import { type JSX, type Ref } from 'react';

// The class TABLES live in the `.ts` companion — see its header. Short version: the tone table is a
// judgement wearing classes, so a test must be able to index it directly rather than regex a slice
// of this file's source, which is the guard shape this project has found vacuous nine times.
//
// THE EXPLICIT `.ts` IS NOT NEEDED HERE (this specifier has no `.tsx` twin to lose to), but note the
// mirror-image trap the codebase hits repeatedly: an extensionless import of THIS component would
// probe `promoteConfirm.ts` FIRST on a case-insensitive filesystem and bind to the companion, which
// has no default export. Every importer of this file therefore writes `./PromoteConfirm.tsx`.
import { ARTIFACT_TONE_CLS, PROMOTE_CANCEL_BTN, PROMOTE_CONFIRM_BTN } from './promoteConfirm';
import { CANDIDATE_ACTOR_VERB } from './repoRowModel';
import { type PromotionArtifact } from './repositoryDetailTabs';

const DT_CLS =
  'text-[11px] uppercase tracking-wide text-slate-400 font-medium w-16 shrink-0';
const CODE_CLS =
  'px-2 py-0.5 rounded-md bg-slate-100 border border-slate-200 font-mono text-xs text-slate-700 break-all';

/** The candidate as `prodCandidateView` shapes it — the three read-only facts. */
export interface PromoteCandidate {
  shortSha: string | null;
  actor: string | null;
  imageTag: string | null;
}

export interface PromoteConfirmProps {
  /** The repository's name, for the question and the group's accessible label. */
  repoName: string;
  /** The candidate's provenance, or null when the record holds no pending one. */
  candidate: PromoteCandidate | null;
  /** What an approval would approve — bytes or a pointer. Resolved, never re-derived. */
  artifact: PromotionArtifact;
  /** What DEV is currently running, so the contrast can be stated rather than inferred. */
  devImageTag: string | null;
  /** True while the promote request is in flight. Disables both buttons. */
  pending: boolean;
  onConfirm: () => void;
  onCancel: () => void;
  /** Focus target on reveal, so the action is reachable by keyboard immediately. */
  confirmRef?: Ref<HTMLButtonElement>;
}

/**
 * The dialog's inner block, with no positioning of its own: the surrounding markup is the caller's,
 * the consent screen is this file's.
 *
 * Today it has exactly ONE caller — the repository detail page — which is the point of 4d. The split
 * is kept anyway because it costs nothing and it is what made the extraction possible: a component
 * that also owned its wrapper could not have been lifted out of a `<tr>`/`colSpan` and dropped into a
 * page body. If a second surface ever legitimately needs this screen, it renders this block inside
 * its own container and re-decides none of the copy.
 */
export default function PromoteConfirm({
  repoName,
  candidate,
  artifact,
  devImageTag,
  pending,
  onConfirm,
  onCancel,
  confirmRef,
}: PromoteConfirmProps): JSX.Element {
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
      <p className="text-xs text-slate-500 mt-1">
        This deploys the commit that landed on{' '}
        <span className="font-medium text-slate-600">main</span>, described below. Prod traffic
        moves to it once the deployment finishes. Any review of the change happened on the
        repository — approving here authorises the deployment, not the code.
      </p>

      {/* The candidate, as the read-only facts it is: the commit, the person who pushed it, and
          the image that commit built. A labelled list rather than a sentence, so each fact can be
          read on its own — this is the one moment an owner is accountable for all three. */}
      <dl className="mt-2.5 grid gap-1.5 text-xs">
        <div className="flex flex-wrap items-center gap-2">
          <dt className={DT_CLS}>Commit</dt>
          <dd className="flex flex-wrap items-center gap-1.5">
            <span className="text-slate-500">main @</span>
            <code className={CODE_CLS}>{candidate?.shortSha ?? '—'}</code>
          </dd>
        </div>

        <div className="flex flex-wrap items-center gap-2">
          {/* A3: the verb is `CANDIDATE_ACTOR_VERB` — "pushed by", never "merged by". The record
              is written on ANY push to `main`, so it cannot establish that a merge (and therefore
              a review) happened, and this is the one dialog where provenance is the whole point. */}
          <dt className={`${DT_CLS} capitalize`}>{CANDIDATE_ACTOR_VERB}</dt>
          <dd className="text-slate-700 font-medium">
            {candidate?.actor ?? '—'}
            {/* Named as the provider identity it is — the other identity on these surfaces is a
                raw Entra oid, and the two are never joined (§6). Saying which directory a name
                comes from is the honest presentation. */}
            <span className="ml-1.5 font-normal text-slate-400">on GitHub</span>
          </dd>
        </div>

        <div className="flex flex-wrap items-center gap-2">
          <dt className={DT_CLS}>Image</dt>
          <dd className="flex flex-wrap items-center gap-2">
            <code className={CODE_CLS}>{candidate?.imageTag ?? '—'}</code>
            {/* THE CONTRAST an owner is here for: is prod about to MATCH dev, or move past it?
                Both tags embed the same tree sha, so equal tags mean prod ends up running exactly
                what dev has been running — and unequal tags mean it does not. */}
            {devImageTag &&
              (candidate?.imageTag === devImageTag ? (
                <span className="text-[11px] text-slate-400">same image dev is running</span>
              ) : (
                <span className="text-[11px] text-amber-700">
                  dev is running <code className="font-mono break-all">{devImageTag}</code> — prod
                  will not match dev
                </span>
              ))}
          </dd>
        </div>

        {/* WHAT THE APPROVAL ACTUALLY NAMES — the exact bytes, or a mutable pointer.
            ONE BRANCHLESS VALUE: `promotionArtifact` resolved the text, the tooltip and the tone,
            and this renders it. Do not reintroduce a condition over the marker — see the file
            header for the dead-branch that survived a green suite, and note that the
            occurrence-counting guard now counts this file.
            Narrowed on the MARKER rather than on `kind`: the two are equivalent by construction
            (a test pins that), and narrowing on the thing rendered is what lets `tsc` prove the
            accesses safe without an assertion. */}
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

      <div className="flex items-center gap-2 mt-3">
        <button
          ref={confirmRef}
          type="button"
          onClick={onConfirm}
          disabled={pending}
          className={PROMOTE_CONFIRM_BTN}
        >
          {pending ? 'Starting…' : 'Promote to prod'}
        </button>
        <button type="button" onClick={onCancel} disabled={pending} className={PROMOTE_CANCEL_BTN}>
          Cancel
        </button>
      </div>
    </div>
  );
}
