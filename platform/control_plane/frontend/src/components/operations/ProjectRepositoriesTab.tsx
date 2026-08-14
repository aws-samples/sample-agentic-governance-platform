// ProjectRepositoriesTab — the "Repositories" tab of the project detail page
// (E27/T11). This is the repositories table + its add/delete surface, LIFTED
// VERBATIM out of ProjectDetail.tsx when the tab shell landed: the table, the
// live materialize timeline (E25C), the AddRepoModal, and the delete-repo modal
// wiring all moved here unchanged so ProjectDetail shrank to the page frame +
// tab shell instead of growing a second tab's worth of code.
//
// Nothing about the repository behaviour changed in the move. The only edits are
// structural: the "New repository from template" button moved from the OpsPage
// header action into this tab's card header (with a tab shell, a repos-only
// action in the page chrome would sit above the Access tab too — and it now
// mirrors AccessTab.tsx's card-header "+ Add principal" idiom), and this file
// owns the showAddRepo / deleteTarget state that ProjectDetail used to hold.
//
// E27/T12 added the per-repo PROMOTE TO PROD action to the table: the epic's headline
// verb. An OWNER clicks it, an inline confirm NAMES WHAT WILL SHIP (displayed, never
// entered — the backend resolves the tag from a record written out-of-band and accepts none
// from any caller), and after the 202 the row sits at `cicd_status: "promoting"` while a
// 3s `getRepoStatus` poll drives it to `deployed` / `failed`. All of the decisions live in
// the pure `projectRoles.ts` (only `src/**/*.test.ts` is collected) — this file is wiring.
//
// E27A NARROWED that verb into an APPROVAL. Promotion is no longer "ship whatever last
// reached dev"; it is one owner approving ONE PUSH TO `main`, registered by the candidate
// route from a validated GitHub OIDC token. So the row now DESCRIBES what is being approved
// — `main @ 3f9a1c2 · pushed by @jorge · awaiting your approval` — above the button that
// approves it, gates on `prod_candidate_status` instead of `last_dev_image_tag`, and keeps
// showing the dev tag purely as CONTRAST: is prod about to match dev, or move past it?
//
// E28/T13 CORRECTED THAT VERB. It credited the actor with MERGING, and NOTHING RECORDS THAT A
// MERGE HAPPENED: the candidate is registered on ANY push to `main` (that is what the template
// workflow triggers on), the actor and sha come only from the OIDC token, and no `merged` flag, PR
// number or `event_name` is persisted — so a direct commit to `main` was described as a merge. The
// verb is now `CANDIDATE_ACTOR_VERB`, ONE constant with a test, because the wrong wording had four
// copies in this file. See its comment in `repoRowModel.ts`.
//
// A guard asserts the old phrasing appears NOWHERE in this file, comments included — a comment
// documenting the wrong verb as intended design is an instruction to the next author to put it
// back. That is why this one describes it instead of quoting it.
//
// The copy is careful about one thing throughout: AGP did not review the code. It holds no
// PR or review state (design §6) — the provider governs what lands on `main`, and this surface
// governs only whether the result reaches production. Likewise `@jorge` is a GITHUB login and
// `last_promoted_by` is a raw Entra oid: two directories, deliberately not joined, each shown
// in its own currency rather than reconciled by a guess.
//
// ---------------------------------------------------------------------------
// E28/T13 — THE ROW IS NO LONGER THIS FILE'S (contract C4)
//
// Both repository lists now render the SHARED `RepoRow`. They were separate implementations,
// and the divergence shipped: a module-private status→tint table was extended on one page and
// not the other, so a repo LIVE IN PRODUCTION rendered in the same amber as one still
// provisioning. `showProject` is the ONLY difference between the two call sites — a second
// boolean would be the beginning of the same fork.
//
// So the six status columns, the two pills and every judgement behind them left this file for
// `RepoRow.tsx` / `repoRowModel.ts`, and the per-repo DETAIL they replaced (template name, agent
// id, provider link) sheds to `/ops/repositories/:id`, which the row now navigates to.
//
// WHAT DELIBERATELY STAYED, and why removing either would have been the wrong trade:
//
//   • THE PROMOTE AFFORDANCE. A project OWNER uses it today. It is a working governed action,
//     and a refactor is not a reason to delete one. It moved from a column into an ACTION BAND
//     — a sub-row beneath the repo's row — because `RepoRow` renders exactly six cells and C4
//     caps it there; a seventh column would have desynced the row from both `<thead>`s. The
//     band carries what the action needs and nothing else: what is being approved, the
//     trigger, and the delete verb.
//   • THE AMBER DIFFERING-TAG WARNING (in the confirm, below). It passed its live check and is
//     explicitly protected. BOTH branches are intact: equal tags read a quiet slate "same image
//     dev is running", differing tags read the amber "dev is running <tag> — prod will not match
//     dev". It stays HERE rather than moving to the detail page because it belongs to the
//     confirm dialog it sits inside — it is the contrast an owner needs at the moment of
//     deciding — and because `RepositoryDetail.tsx` is another task's reviewed file.
//
// ---------------------------------------------------------------------------
// E28C/T7 (D-C4d) — THE PROMOTE AFFORDANCE LEFT AFTER ALL, AND THIS REVERSES THE NOTE ABOVE
//
// T13 kept the button here on the argument that a working governed action must not be deleted by
// a refactor. That was right about the ACTION and wrong about the PLACE, and the live test found
// out why: there were TWO promote entry points and only ONE dialog between them — this row had
// the reveal-then-confirm, and the repository detail page's header button fired the promotion
// straight off a bare `onClick`. The surface with MORE evidence available had LESS consent.
//
// The obvious fix was to clone the dialog onto the detail page. 4d ruled the other way, and the
// reason is what promotion IS: an approval of specific bytes. Judging it needs what dev is
// running, the candidate's commit and image, and whether the candidate is digest-pinned or a
// mutable tag — and none of that fits in a table row. An approval offered where its object cannot
// be seen is the weaker act dressed as the stronger one, which is the defect class this epic is
// named for. So promotion now lives at ONE entry point, on the detail page, behind the extracted
// `PromoteConfirm` dialog. Two copies of a consent screen is strictly worse than one: they drift,
// and the half that drifts is the half nobody is reading.
//
// WHAT THIS ROW KEEPS IS THE INFORMATION, which was never the problem. `promotionReadiness` (the
// SAME `'pending'` predicate that gated the button) drives a passive `Ready for promotion`
// indicator, so the fleet still shows at a glance which repositories are waiting on an owner. It
// is a statement, not an affordance: no verb, no button styling, no handler, and — unlike the
// button — NOT role-gated, because "an approval is outstanding" is a fact about the repository
// rather than an offer to the reader (the same call `isCurrentAttempt` makes for "this is live").
//
// The differing-tag warning and the artifact marker went WITH the dialog into
// `PromoteConfirm.tsx`, both branches intact — they belong to the confirm they were written for.
// The action band remains for DELETE, the other governed verb on this surface.
//
// `AddRepoModal` (620 lines, its own second poller) was EXTRACTED to `AddRepoModal.tsx`
// unchanged. The step-timeline pieces stayed: `ProjectDetail.tsx` re-exports two of them for a
// pinned test path and `RepositoryDetail.tsx` imports two directly, so this module remains their
// home and the modal imports them back.
//
// House style: emerald-on-glass Ops tokens (opsUi.ts), Tailwind v4 utility
// strings, 2-space indent — unchanged from ProjectDetail.

// `useCallback` is no longer imported: its three uses here were the promote handler and the two
// per-row callbacks the confirm needed (E28C/T7).
import { Fragment, useEffect, useRef, useState, type JSX } from 'react';
import { useNavigate } from 'react-router-dom';

import { projectsApi, type Repository, type StepState } from '../../api/client';
import { useUser } from '../../contexts/UserContext';
// THE EXPLICIT `.tsx` IS LOAD-BEARING on a case-insensitive filesystem: an extensionless
// specifier probes `<Name>.ts` FIRST, so a companion module differing only in casing wins and
// the import silently binds to a module with no default export (TS1149). This epic hit that
// four times.
import AddRepoModal from './AddRepoModal.tsx';
import DeleteRepositoryModal from './DeleteRepositoryModal';
import RepoRow, { REPO_ROW_COLUMNS } from './RepoRow.tsx';
// One import left this list in E28C/T7 — the deploy route's error mapper, which turned that route's
// five fixed literals into a sentence for a request this file no longer issues. It is described and
// not named, here and at its former call site below, because a guard asserts this file names no part
// of the removed machinery anywhere, comments included.
import {
  canDestroy,
  isPromotionInFlight,
  keepPromotionOverride,
  mayMaintainProject,
  prodCandidateView,
  promoteBlockedReason,
  type ProjectRoleName,
} from './projectRoles';
// The row's pure companion. `sortRepoRows` is imported rather than re-derived so BOTH lists put
// the same repo in the same place — C4 makes "Action required" the sort key, and two independent
// sorts is the same class of drift as two independent status tables. The two label constants are
// the corrected wordings (A3/A4), each ONE source with a test.
//
// `promotionReadiness` + `PROMOTION_READY_LABEL` (E28C/T7) drive the passive indicator that replaced
// this row's Promote button. The PREDICATE is imported rather than re-asked here for the reason the
// sort is: it is the same `'pending'` comparison the promote gate makes, so this row and the detail
// page cannot come to disagree about whether an approval is outstanding.
import {
  CANDIDATE_ACTOR_VERB,
  LAST_DEPLOYED_ACTOR_VERB,
  LAST_DEPLOYED_LABEL,
  PROMOTION_READY_LABEL,
  promotionReadiness,
  sortRepoRows,
} from './repoRowModel';
// `OPS_BADGE` is imported as a TYPE ONLY: the row's pills moved to `RepoRow`, so nothing here
// renders a badge class any more — but `nextBadgeFromSteps` returns `keyof typeof OPS_BADGE`, and
// that annotation is the contract letting `AddRepoModal` use the key to index the palette. Its
// value is unused here; dropping the import entirely makes the signature unresolvable.
import type { OPS_BADGE } from './opsUi';
import { OPS_CARD, OPS_PRIMARY_BTN, OPS_TABLE_DIVIDE, OPS_TABLE_HEAD } from './opsUi';

// `ARTIFACT_TONE_CLS` and `PROMOTE_BTN` both left in E28C/T7. The tone table was the SECOND copy of
// one judgement's tint (its twin was on the detail page) and now lives once, in
// `PromoteConfirm.tsx`. `PROMOTE_BTN` styled a weighty emerald trigger for a row that no longer
// offers the act — the readiness indicator that replaced it is deliberately quiet, because a passive
// statement wearing a button's weight is the affordance this task removed, re-created in CSS.

// --- Live materialize timeline (E25C/T4) — pure, testable helpers -----------
// The AddRepoModal polls getRepoStatus every 3s and stops when the run is terminal:
// every step done, or one failed. A failure HALTS the backend run, so trailing pending
// steps never execute — hence a `failed` step is terminal even with pending steps after
// it. Kept pure (no DOM) so vitest can pin the predicate.
//
// These live here (with the timeline they drive) and are RE-EXPORTED from
// ProjectDetail.tsx, because ProjectDetail.stepTimeline.test.ts imports them from
// './ProjectDetail' — the move must not break the pinned E25C test path.
export function isMaterializeTerminal(steps: StepState[]): boolean {
  if (steps.some((s) => s.status === 'failed')) return true;
  return !steps.some((s) => s.status === 'running' || s.status === 'pending');
}

// Map the timeline onto the same OPS_BADGE palette (and the same semantic labels) the
// repos table uses, so the modal header pill reads identically to the table row for the
// SAME repo at the SAME moment: any failure → failed (rose); all done → ready (emerald);
// otherwise still in flight → the in-flight amber, the SAME key `provisioning` resolves to in
// the shared delivery table — not the emerald `running` key. Emerald is reserved for the
// terminal ready state. The returned key doubles as the pill's lowercase label (capitalized by
// the modal's pill shape), so pill color + label derive from this ONE source instead of two
// independent paths.
//
// A guard asserts that neither repository LIST names a status table, which is why this comment
// describes the delivery table's mapping rather than naming its accessor: the guard reads raw
// source and does not skip comments, deliberately.
export function nextBadgeFromSteps(steps: StepState[]): keyof typeof OPS_BADGE {
  if (steps.some((s) => s.status === 'failed')) return 'failed';
  if (steps.length > 0 && steps.every((s) => s.status === 'done')) return 'ready';
  return 'provisioning';
}

// Per-step status → the short lowercase label shown to the right of each row.
//
// EXPORTED (E28/T11): the repository detail page renders the SAME steps and was rendering
// `{s.status}` raw, so one repo's step read "Waiting" here and "pending" there — two spellings
// of one state, which is the divergence this epic exists to remove. This is the mapping; both
// surfaces render through it. (Only the `export` keyword was added to this file.)
export function stepStatusText(status: StepState['status']): string {
  return status === 'done' ? 'done' : status === 'running' ? 'running' : status === 'failed' ? 'failed' : 'waiting';
}

// The timeline node — a 24px circle carrying the step's state icon, in the Ops
// emerald/amber/rose palette (the platform's statusIcon idiom).
function StepNode({ status }: { status: StepState['status'] }): JSX.Element {
  if (status === 'done') {
    return (
      <span className="relative z-10 shrink-0 inline-flex items-center justify-center w-6 h-6 rounded-full bg-emerald-100 text-emerald-700 ring-4 ring-white">
        <svg className="w-3.5 h-3.5" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={3}>
          <path strokeLinecap="round" strokeLinejoin="round" d="M5 13l4 4L19 7" />
        </svg>
      </span>
    );
  }
  if (status === 'running') {
    return (
      <span className="relative z-10 shrink-0 inline-flex items-center justify-center w-6 h-6 rounded-full bg-emerald-600 text-white ring-4 ring-white">
        <svg className="w-3.5 h-3.5 animate-spin" viewBox="0 0 24 24" fill="none">
          <circle cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="3" strokeDasharray="40 60" />
        </svg>
      </span>
    );
  }
  if (status === 'failed') {
    return (
      <span className="relative z-10 shrink-0 inline-flex items-center justify-center w-6 h-6 rounded-full bg-rose-100 text-rose-700 ring-4 ring-white">
        <svg className="w-3.5 h-3.5" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={3}>
          <path strokeLinecap="round" strokeLinejoin="round" d="M6 18L18 6M6 6l12 12" />
        </svg>
      </span>
    );
  }
  // pending
  return (
    <span className="relative z-10 shrink-0 inline-flex items-center justify-center w-6 h-6 rounded-full bg-white border-2 border-slate-200 text-slate-300 ring-4 ring-white">
      <span className="w-1.5 h-1.5 rounded-full bg-slate-300" aria-hidden="true" />
    </span>
  );
}

// The live materialize step timeline (E25C). A clean vertical timeline: each of the 8
// steps is a node on a rail, running is highlighted, and a failed step shows its short
// error hint inline so the operator diagnoses the failure without reading logs.
//
// EXPORTED (E28/T13): `AddRepoModal` moved to its own file and renders this. The timeline stays
// HERE rather than travelling with the modal because this module is the home of the step-timeline
// group — `ProjectDetail.tsx` re-exports two of its predicates for
// `ProjectDetail.stepTimeline.test.ts`'s pinned import path, and `RepositoryDetail.tsx` imports
// two directly. Moving them would break a pinned test path and three import sites for no gain.
// (Only the `export` keyword was added.)
export function MaterializeTimeline({ steps }: { steps: StepState[] }): JSX.Element {
  return (
    <ol className="relative">
      {steps.map((s, i) => {
        const last = i === steps.length - 1;
        const active = s.status === 'running';
        const failed = s.status === 'failed';
        return (
          <li key={s.key} className="relative flex gap-3 pb-4 last:pb-0">
            {/* Rail connecting this node to the next. */}
            {!last && (
              <span
                aria-hidden="true"
                className={`absolute left-[11px] top-6 -bottom-0 w-px ${
                  s.status === 'done' ? 'bg-emerald-200' : 'bg-slate-200'
                }`}
              />
            )}
            <StepNode status={s.status} />
            <div className="min-w-0 flex-1 pt-0.5">
              <div className="flex items-center justify-between gap-2">
                <span
                  className={`text-sm truncate ${
                    failed
                      ? 'text-rose-700 font-medium'
                      : active
                        ? 'text-slate-900 font-semibold'
                        : s.status === 'done'
                          ? 'text-slate-700'
                          : 'text-slate-400'
                  }`}
                >
                  {s.label}
                </span>
                <span
                  className={`shrink-0 text-[11px] font-medium capitalize ${
                    failed
                      ? 'text-rose-600'
                      : active
                        ? 'text-emerald-700'
                        : s.status === 'done'
                          ? 'text-emerald-600'
                          : 'text-slate-400'
                  }`}
                >
                  {stepStatusText(s.status)}
                </span>
              </div>
              {failed && s.error && (
                <p className="mt-1 rounded-md bg-rose-50 border border-rose-200/70 px-2 py-1 text-[11px] text-rose-700 break-words">
                  {s.error}
                </p>
              )}
            </div>
          </li>
        );
      })}
    </ol>
  );
}

// ---------------------------------------------------------------------------
// ProjectRepositoriesTab
// ---------------------------------------------------------------------------

export default function ProjectRepositoriesTab({
  projectId,
  connectionId,
  repositories,
  heldRole,
  ungoverned,
  onChanged,
}: {
  projectId: string;
  connectionId: string;
  repositories: Repository[];
  // The caller's EFFECTIVE role on this project as the SERVER reported it on the detail
  // read (`ProjectDetail.effective_role`). Gates the per-row Delete — see below.
  heldRole: ProjectRoleName | null;
  // The SERVER's `ProjectDetail.ungoverned` bit. Needed IN ADDITION to `heldRole` because
  // the two MAINTAINER verbs here ride the design-§3 fallback: on a project with no role
  // rows a tenant-visible caller may still add and retry repositories. Only a literal
  // `true` counts (`undefined` = not reported).
  ungoverned: boolean | undefined;
  /** Refetch the parent's project detail (a repo was materialized or deleted). */
  onChanged: () => void;
}): JSX.Element {
  const { user } = useUser();
  const [showAddRepo, setShowAddRepo] = useState(false);
  // The repository whose delete-checklist modal is open (null = closed).
  const [deleteTarget, setDeleteTarget] = useState<Repository | null>(null);

  // --- Promotion (E27/T12, ENTRY POINT REMOVED IN E28C/T7) ------------------
  // There is no promote state left in this file. The confirm-reveal slot, the in-flight slot,
  // the mapped failure sentence and the announced success notice all existed to run and report a
  // promotion STARTED HERE, and D-C4d moved that act to the repository detail page — the only
  // surface that can show what an approval would approve. Reporting the outcome of an action a
  // file cannot initiate is machinery with no trigger.
  //
  // What remains below is NOT promote machinery even though it reacts to promotions: the
  // `liveRepos` override and its poller watch `cicd_status`, so a promotion started from the
  // detail page still drives this table's rows through `promoting` → `deployed`/`failed` exactly
  // as before. They were never coupled to the button.

  // Records read back from `getRepoStatus` (and the 202 body), merged OVER the parent's
  // `repositories`. Each override is fresher THE MOMENT IT IS WRITTEN, but that says nothing
  // about later — a parent refetch is by construction newer still. So an override is a BRIDGE
  // over the promoting window and nothing more; `keepPromotionOverride` (pure, tested) is what
  // decides when it stops being one, and the effect below drops it then. It must never
  // outlive the parent's data: the poller tears itself down once a promotion is terminal, so
  // a retained override would be frozen and authoritative forever.
  const [liveRepos, setLiveRepos] = useState<Record<string, Repository>>({});

  const mountedRef = useRef(true);
  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
    };
  }, []);

  // The three promote focus refs went with the dialog (E28C/T7): a trigger-return ref, a
  // confirm-focus ref and a notice-focus ref exist to manage focus across a reveal this file no
  // longer performs. `PromoteConfirm` takes a `confirmRef` and its one caller owns it.

  const navigate = useNavigate();

  // The parent's records with any live promotion override merged over them, then ordered by the
  // SHARED sort so this list and the fleet list put the same repo in the same place (C4:
  // "`Action required` sorts first"). No runtime probe on this page — the runtime route is
  // per-agent and this tab holds no agent reads, so every row's runtime is `undefined`, which
  // `RepoRow` renders as UNREACHABLE and never as ready.
  const rows = sortRepoRows(repositories.map((r) => liveRepos[r.id] ?? r));
  const repoCount = repositories.length;

  // The rows a promotion is currently running for — the poller's input, and the reason a
  // second Promote is never offered on them (the route refuses it with a 409).
  const promotingIds = rows.filter((r) => isPromotionInFlight(r.cicd_status)).map((r) => r.id);
  // Serialized so the poll effect depends on the SET, not on a fresh array each render.
  const promotingKey = promotingIds.join(',');

  // Drop an override the moment it stops being a BRIDGE over the promoting window — the
  // decision is `keepPromotionOverride`, in the pure module with its reasoning. Nothing here
  // decides freshness: this effect only re-runs the rule against each new parent read.
  useEffect(() => {
    setLiveRepos((prev) => {
      const next = Object.fromEntries(
        Object.entries(prev).filter(([id, live]) =>
          keepPromotionOverride(
            live.cicd_status,
            repositories.find((r) => r.id === id)?.cicd_status
          )
        )
      );
      // A filter can only shrink, so equal length ⇒ nothing was dropped. Returning `prev`
      // then keeps the identity stable and avoids a render loop.
      return Object.keys(next).length === Object.keys(prev).length ? prev : next;
    });
  }, [repositories]);

  // The promoting-row poller. ONE interval for the whole table (not one per row) on the
  // SAME 3s cadence and the SAME `getRepoStatus` endpoint the materialize timeline uses —
  // there is no promotion-specific endpoint and no second poller. It exists only while a
  // row is `promoting` and clears itself the moment none is, so a table with nothing in
  // flight does no polling at all.
  //
  // The 202 is NOT the outcome: the CodeBuild run is what reaches prod, so the row stays at
  // `promoting` until the buildspec writes `deployed` (or the record is left `failed`).
  const pollRef = useRef<number | null>(null);
  useEffect(() => {
    // Read the id set off the SERIALIZED key, not off `promotingIds` — the array is a fresh
    // reference every render, so depending on it would tear down and rebuild the interval
    // continuously and no 3s tick would ever fire.
    const ids = promotingKey ? promotingKey.split(',') : [];
    if (ids.length === 0) {
      if (pollRef.current) {
        window.clearInterval(pollRef.current);
        pollRef.current = null;
      }
      return;
    }
    pollRef.current = window.setInterval(async () => {
      const fresh = await Promise.all(
        ids.map((repoId) => projectsApi.getRepoStatus(projectId, repoId).catch(() => null))
      );
      if (!mountedRef.current) return;
      const settled = fresh.filter((r): r is Repository => r !== null);
      if (settled.length === 0) return; // transient — keep polling
      setLiveRepos((prev) => ({
        ...prev,
        ...Object.fromEntries(settled.map((r) => [r.id, r])),
      }));
    }, 3000);
    return () => {
      if (pollRef.current) {
        window.clearInterval(pollRef.current);
        pollRef.current = null;
      }
    };
  }, [promotingKey, projectId]);

  // The confirm-focus effect, the per-row notice clear, the confirm-close (focus return) and the
  // promote handler itself were removed in E28C/T7 with the dialog they served. All four were about
  // STARTING and reporting a promotion from this table; D-C4d put that act on the repository detail
  // page, behind `PromoteConfirm`, where the artifact being approved is visible. The
  // Escape-to-dismiss listener went with them — there is no revealed confirm here to dismiss.
  //
  // The handler's NAME is deliberately not written above: a guard asserts this file names no promote
  // trigger, handler or route call anywhere, comments included, because a comment naming the removed
  // machinery is an instruction to the next author to put it back. That rule already caught the
  // merge-flavoured verb four times in this file.

  // Delete repository is the IRREVERSIBLE E23 five-item cascade (AgentCore runtime + TF
  // state, ECR images, GitHub repo, Entra identity, registry record) and its route — plus
  // the delete-preview the modal opens with — are BOTH OWNER-gated server-side, with the
  // design-§3 ungoverned fallback deliberately stopping short of OWNER. So a Maintainer is
  // not offered it: previously the button rendered for everyone, and clicking through the
  // "this cannot be undone" checklist ended in a raw `insufficient project role`.
  // Conditionally rendered, never `disabled` (the epic's FE constraint).
  const mayDestroy = canDestroy(heldRole, user?.role_level ?? 0);

  // The two MAINTAINER-gated verbs in this tab: "New repository from template" (POST
  // /{id}/repos) and, inside the modal, "Retry from failed step". Both gate on MAINTAINER
  // server-side THROUGH the §3 fallback, so `mayMaintainProject` takes `ungoverned` too —
  // a role gate alone would hide the only way to add a repository on every pre-migration
  // project. Previously both rendered for anyone who could load the tab and set `err.message`
  // raw, so a Viewer filled in the whole create form and read `insufficient project role`.
  const mayMaintain = mayMaintainProject(heldRole, user?.role_level ?? 0, ungoverned);

  return (
    <>
      <div className={`${OPS_CARD} overflow-hidden`}>
        {/* Card header — title + the tab's own create action (the AccessTab
            card-header idiom), so a repos-only button never sits over Access. */}
        <div className="flex items-center justify-between gap-3 px-4 py-3.5 border-b border-emerald-200/50">
          <div className="min-w-0">
            <h2 className="text-sm font-semibold text-slate-800">Repositories</h2>
            <p className="text-xs text-slate-400 mt-0.5">
              Each repository is one template-materialized repo + its registered agent.
            </p>
          </div>
          {mayMaintain && (
            <button type="button" onClick={() => setShowAddRepo(true)} className={`${OPS_PRIMARY_BTN} shrink-0`}>
              New repository from template
            </button>
          )}
        </div>

        {/* The announced promotion notice went with the entry point (E28C/T7). It existed to tell
            a screen reader that THIS table's button had started a promotion and which image was
            shipping; a table that starts none has nothing to announce, and the detail page reports
            its own. The rows still move through `promoting` → `deployed`/`failed` via the poller
            above, and `RepoRow`'s status pill is what states that — as it does for a build nobody
            on this page started either. */}

        <table className="w-full text-sm">
          {/* The columns are `RepoRow`'s, in C4's pinned order. Template name, agent id and the
              provider link used to live here and are now on the detail page the row navigates
              to — the row states STATUS, and the detail page answers everything else. */}
          <thead className={OPS_TABLE_HEAD}>
            <tr>
              <th className="text-left font-medium px-4 py-2.5">Repository</th>
              <th className="text-left font-medium px-4 py-2.5">Action required</th>
              <th className="text-left font-medium px-4 py-2.5">Runtime</th>
              <th className="text-left font-medium px-4 py-2.5">Delivery</th>
              <th className="text-left font-medium px-4 py-2.5">Prod version</th>
              <th className="text-left font-medium px-4 py-2.5">Owner</th>
            </tr>
          </thead>
          <tbody className={OPS_TABLE_DIVIDE}>
            {rows.map((r) => {
              const inFlight = isPromotionInFlight(r.cicd_status);
              // WHY Promote is or isn't offered on this row. Gated on `heldRole` (the
              // server's `effective_role`) — never on `ungoverned`, which would offer the
              // button on exactly the pre-migration projects where the STRICT gate 403s.
              // A promotion already in flight suppresses it too: the route answers a second
              // one with a 409, so there is nothing to offer.
              // E27A: the precondition is the PENDING CANDIDATE, not the dev image. Promoting
              // is the approval of what landed on `main`, so a repo whose dev build is green but
              // whose `main` has not moved offers nothing — and the route now 409s
              // ("no prod candidate to promote") rather than shipping a stale image.
              const promoteState = promoteBlockedReason(
                heldRole,
                user?.role_level ?? 0,
                r.prod_candidate_status
              );
              // WHAT is being approved. Derived in the pure module (only `src/**/*.test.ts` is
              // collected) so the sha truncation and the `@login` shaping are pinned. Still read
              // HERE for the band's provenance line — the line survived the dialog's departure
              // because it is a STATEMENT about what is waiting, not part of the approval act.
              const candidate = prodCandidateView(r);
              // `artifact` (the bytes-or-pointer marker) is no longer read on this surface: it
              // belongs to the moment of approval, which is now only on the detail page. Rendering
              // it beside a passive indicator would put the epic's most consequential distinction
              // somewhere nobody can act on it, and duplicate it where they can.
              // Is there anything to put in this row's ACTION BAND? The band is a sub-row, so an
              // empty one would be a blank stripe under every repo a caller cannot act on.
              //
              // The readiness indicator counts too, and it is NOT role-gated (see the file header),
              // so a Viewer looking at a waiting repo now gets a band where they previously got
              // none — which is the point: the fact is theirs to see.
              const bandActions =
                (promotionReadiness(r) && !inFlight) ||
                (promoteState === 'no-candidate' && !inFlight) ||
                mayDestroy;
              return (
                <Fragment key={r.id}>
                  {/* THE SHARED ROW (C4). Identical component, identical props shape, on both
                      lists — `showProject` false here because every repo in this table is in
                      THIS project, so a project column would repeat the page's own title on
                      every row. `runtime` is deliberately omitted: this tab makes no agent
                      runtime read, and `RepoRow` renders an absent answer as UNREACHABLE rather
                      than inventing a ready. */}
                  <RepoRow
                    repo={r}
                    showProject={false}
                    onNavigate={(repoId) => navigate(`/ops/repositories/${repoId}`)}
                  />

                  {/* THE ACTION BAND — the governed verbs, kept (see the file header). It is a
                      sub-row rather than a seventh column because `RepoRow` renders exactly six
                      cells and C4 caps it there; a seventh `<th>` would desync this table from
                      the row and from the fleet table's `<thead>`.

                      Not rendered at all when there is nothing in it, so a Viewer's table is the
                      six status columns and no empty stripes. */}
                  {bandActions && (
                    <tr className="bg-emerald-50/20">
                      <td colSpan={REPO_ROW_COLUMNS} className="px-4 pb-3 pt-0">
                        <div className="flex flex-wrap items-center justify-end gap-x-3 gap-y-1.5">
                          {/* WHAT IS BEING APPROVED, immediately beside the control that
                              approves it (E27A). Promotion is not "ship the newest image" — it is
                              one owner taking responsibility for ONE named commit by ONE named
                              author, so the button without this line would be a verb with no
                              object.

                              Shown on exactly the same condition as the button ('ok'), never to
                              a non-owner: "awaiting YOUR approval" is false for someone who
                              cannot approve, and `promoteBlockedReason`'s rule is that a role
                              refusal renders NOTHING. The sha is git-short and the actor is a
                              GitHub login — the provider's own currency, deliberately not
                              reconciled with the Entra oid on the "Last deployed" line (§6).

                              A3: the verb is `CANDIDATE_ACTOR_VERB`. It used to credit the actor
                              with merging, and the candidate record cannot distinguish a merge
                              from a direct push to `main` — see the constant. */}
                          {candidate && promoteState === 'ok' && !inFlight && (
                            <p className="text-[11px] text-slate-500 leading-tight mr-auto">
                              main
                              {candidate.shortSha && (
                                <>
                                  {' @ '}
                                  <span className="font-mono text-slate-700">{candidate.shortSha}</span>
                                </>
                              )}
                              {candidate.actor && (
                                <>
                                  {` · ${CANDIDATE_ACTOR_VERB} `}
                                  <span className="font-medium text-slate-700">{candidate.actor}</span>
                                </>
                              )}
                              <span className="ml-1.5 font-medium text-emerald-700">
                                — awaiting your approval
                              </span>
                            </p>
                          )}

                          {/* The audit line — what prod is RUNNING, when it landed, and which
                              image. The provenance an FSI reviewer asks for.

                              A4/P10: "Last deployed", NOT "Promoted". A ROLLBACK writes
                              `last_promoted_*` too (T4), so "Promoted" was false after one — the
                              field means what prod is running, not what was approved. Both
                              wordings are single constants shared with the detail page, which
                              already says "Deployed by". `last_promoted_by` stays a RAW platform
                              id: it is an Entra oid, a different currency from the `@login`
                              above, and the two are never joined. */}
                          {r.last_promoted_at && (
                            <span className="text-[11px] text-slate-400">
                              {LAST_DEPLOYED_LABEL} {r.last_promoted_at.slice(0, 10)}
                              {r.last_promoted_by ? ` · ${LAST_DEPLOYED_ACTOR_VERB} ${r.last_promoted_by}` : ''}
                            </span>
                          )}

                          {/* THE PASSIVE READINESS INDICATOR, where the Promote button was
                              (E28C/T7, D-C4d). A STATEMENT, not an affordance: the approval itself
                              is on the repository detail page, which is the only surface that can
                              show what would ship (the candidate's commit, whether prod would
                              match dev, and whether the bytes are digest-pinned).

                              Styled deliberately QUIET — emerald text on a hairline emerald tint,
                              no fill, no ring, no glyph, and no hover state. It must not read as a
                              button on a surface that no longer has one; the row already links to
                              the detail page, and that link is the way to act.

                              NOT ROLE-GATED, unlike the button it replaces. `promoteBlockedReason`
                              renders NOTHING for a role refusal (no row nags anyone about
                              privilege), but that rule is about withholding an OFFER. This is a
                              fact about the repository — an owner's approval is outstanding — and
                              a viewer scanning the fleet needs it as much as an owner does. Same
                              call `isCurrentAttempt` makes for "this is what is live".

                              Suppressed while a delivery is in flight: mid-promotion the answer to
                              "is this waiting?" is no, and the row's own status pill is already
                              reporting the in-flight state honestly. */}
                          {promotionReadiness(r) && !inFlight && (
                            <span className="inline-flex items-center gap-1.5 px-2 py-0.5 rounded-md bg-emerald-50/70 border border-emerald-200/60 text-[11px] font-medium text-emerald-800">
                              {PROMOTION_READY_LABEL}
                            </span>
                          )}
                          {/* "Not yet" gets a sentence; "not you" gets nothing (see
                              `promoteBlockedReason`). The missing precondition is something
                              landing on `main`, not a dev build — a dev build is routine and says
                              nothing about prod, so "Nothing in dev yet" would name the wrong
                              remedy. A3: "merge to main" would name a mechanism the record does
                              not establish; "push to main" is what actually creates a candidate.
                              Still gated on the ROLE: this one IS addressed to a reader who could
                              act, and telling a viewer what is missing implies they should fix it. */}
                          {promoteState === 'no-candidate' && !inFlight && (
                            <span className="text-[11px] text-slate-400">
                              Nothing on main awaiting approval
                            </span>
                          )}
                          {mayDestroy && (
                            <button
                              type="button"
                              onClick={() => setDeleteTarget(r)}
                              aria-label={`Delete ${r.name}`}
                              className="px-2.5 py-1 rounded-md bg-white border border-rose-300 text-rose-700 text-xs font-medium hover:bg-rose-50 transition-colors"
                            >
                              Delete
                            </button>
                          )}
                        </div>
                      </td>
                    </tr>
                  )}

                  {/* The per-row mapped failure sentence went with the request it reported
                      (E28C/T7). It turned the deploy route's five fixed literals — the 403, both
                      409s, the 502 and the 404 — into a sentence, and this table issues none of
                      them; the detail page maps its own through the shared helper in
                      `projectRoles.ts`, so there is still exactly one mapping and no raw literal
                      reaches an operator. A row whose deployment FAILED still says so where it
                      always did: `cicd_status` goes to `failed` and `RepoRow`'s delivery pill
                      reports it, whichever surface started the run.

                      The helper is described rather than NAMED, deliberately — see the note beside
                      the removed handler above. A guard asserts this file names no part of that
                      machinery anywhere, comments included, because a comment naming what was
                      removed is an instruction to the next author to wire it back. */}
                </Fragment>
              );
            })}

            {repoCount === 0 && (
              <tr>
                <td colSpan={REPO_ROW_COLUMNS} className="px-4 py-8 text-center text-slate-400 text-sm">
                  No repositories yet.
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>

      {deleteTarget && (
        <DeleteRepositoryModal
          open
          repo={deleteTarget}
          projectId={projectId}
          onClose={() => setDeleteTarget(null)}
          onDeleted={() => {
            setDeleteTarget(null);
            onChanged();
          }}
        />
      )}

      {showAddRepo && (
        <AddRepoModal
          projectId={projectId}
          connectionId={connectionId}
          // Reaching this modal already required `mayMaintain`, but the retry inside it is
          // gated on the LIVE value: the parent's detail refetch no longer unmounts this tab
          // (E27/T11 FIX 3), so a caller whose standing drops while the modal is open sees
          // the affordance go away rather than click into a 403.
          mayMaintain={mayMaintain}
          onClose={() => setShowAddRepo(false)}
          onCreated={() => {
            setShowAddRepo(false);
            onChanged();
          }}
        />
      )}
    </>
  );
}
