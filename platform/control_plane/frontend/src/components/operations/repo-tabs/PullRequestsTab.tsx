// PullRequestsTab — the repository's pull requests, acted on AS THE LINKED HUMAN
// (E28/T14, contract C2 — D14+D15).
//
// ---------------------------------------------------------------------------
// WHY THIS TAB EXISTS
//
// The delivery story had a hole in the middle. AGP could materialize a repository and it could
// promote a prod candidate, but the step between them — the pull request a human opens, someone
// reviews, and somebody merges — happened entirely on github.com. So the one surface that claims
// to answer "what is happening to this repository?" could not show the work in flight, and the
// operator had to leave the platform to find out whether anything was waiting on them.
//
// ---------------------------------------------------------------------------
// AGP ACTS AS THE HUMAN, NEVER AS ITSELF (D15)
//
// Every verb here runs under the caller's own E27B GitHub link, resolved server-side from their
// validated Entra principal. The platform also holds an org-scoped App token that could open and
// approve pull requests as AGP — and using it would be a security defect rather than a shortcut:
// AGP's App is never the author of anybody's pull request, so an App-token path would sail
// straight through the self-approval refusal and let the platform approve a human's own PR on
// their behalf. That refusal IS the reviewer independence this surface exists to preserve.
//
// The consequence a reader should expect: a human with no linked GitHub account is REFUSED here,
// not silently served. That is correct, and the copy says what to do about it.
//
// ---------------------------------------------------------------------------
// A REFUSAL IS A STATE THIS PAGE RENDERS CALMLY
//
// `can_approve === false` means the backend has already decided this caller may not approve this
// pull request — usually because they opened it. The button is ABSENT (conditionally rendered,
// never `disabled` — `disabled` is reserved for an in-flight request) and the server's own reason
// is stated beside it. Nothing failed, so nothing is red.
//
// The frontend NEVER re-derives that decision. It does not know which GitHub account the caller's
// link names — that is a provider currency resolved backend-side — so comparing an author to
// anything here would merge two currencies (E27A §6) and would offer exactly the self-approval
// the backend then refuses.
//
// ---------------------------------------------------------------------------
// THE TAB IS HIDDEN WHEN THE ORG CANNOT SERVE IT (A3) — AND WHY THE PAGE OWNS THE READ
//
// The App's `pull_requests` permission is a MANUAL per-org grant and GitHub does not retro-apply a
// manifest change to an already-created App, so an org onboarded before this feature answers 403
// forever until an admin grants it. The tab must then be ABSENT, not present-and-failing: a tab
// that renders and cannot answer is worse than no tab, because the tablist announces it and hands
// the keyboard user somewhere broken.
//
// That forces the LIST READ up into `RepositoryDetail`, and the reason is a genuine ordering
// constraint rather than a preference: the probe's answer decides whether this tab appears in the
// strip AT ALL, but a tab body only mounts once its tab is SELECTED. A tab that fetched its own
// visibility could therefore never become visible — nothing would mount to ask. So the page reads
// the list on load (exactly as it already reads the runtime probe, the deployment history and the
// two Access counts best-effort), resolves `prTabVisibility`, and passes the rows down here.
//
// Every probe failure resolves to hidden, not only the recognized one. See `prTabVisibility`.
//
// This file therefore owns the three WRITE verbs and no read: it reports a completed action with
// `onChanged` so the page re-reads, rather than keeping a second copy of the list that could
// disagree with the one the strip's visibility was decided from.
//
// ---------------------------------------------------------------------------
// WHERE THE DECISIONS LIVE
//
// `pullRequestsTab.ts`. vitest collects only `src/**/*.test.ts`, so anything decided in this file
// is unpinnable — this is wiring: props in, markup out. Same split as `DeploymentsTab.tsx` /
// `deploymentsTab.ts`.
//
// House style: emerald-on-glass Ops tokens (`opsUi.ts`), the `ConnectionsAdmin` ModalShell for the
// create form, Tailwind v4 utility strings, 2-space indent.

import { useCallback, useMemo, useState, type JSX } from 'react';

import { pullRequestsApi, type PullRequestView } from '../../../api/client';
import { ModalShell } from '../ConnectionsAdmin';
import { OPS_BADGE, OPS_CARD, OPS_TABLE_DIVIDE, OPS_TABLE_HEAD } from '../opsUi';
import { type ProjectRoleName } from '../projectRoles';
import {
  NO_VALUE,
  authorDisplay,
  prReadOnlyNote,
  prRowActions,
  prStateBadgeKey,
  prStateLabel,
  pullRequestActionMessage,
  shortHeadSha,
  sortPullRequests,
} from './pullRequestsTab.ts';

const PILL =
  'inline-flex items-center gap-1.5 text-[11px] font-semibold px-2 py-0.5 rounded-full';
const FIELD_LABEL = 'block text-[11px] uppercase tracking-wide text-slate-400 font-medium mb-1';
const INPUT =
  'w-full px-3 py-2 rounded-lg border border-slate-300 text-sm text-slate-800 focus:outline-none focus:border-emerald-500 transition-colors';

export interface PullRequestsTabProps {
  repoId: string;
  /**
   * The pull requests, READ BY THE PAGE (see the header). Passed in rather than fetched here
   * because the same read decides whether this tab renders at all, and a tab body cannot fetch
   * the answer to whether it should exist.
   */
  pullRequests: readonly PullRequestView[];
  /** The caller's project role, for the MAINTAINER-gated write verbs. */
  heldRole: ProjectRoleName | null;
  roleLevel: number;
  /** The project's `ungoverned` bit — the design-§3 fallback these routes carry. */
  ungoverned: boolean | null | undefined;
  /** The page's load is in flight — labels the refresh control. Same prop `DeploymentsTab` takes. */
  loading: boolean;
  /**
   * A write LANDED — the page re-reads the list.
   *
   * Reported upward rather than patched locally: an approve can change ANOTHER row's mergeability
   * (branch protection counts reviews), so a local splice would leave the rest of the table
   * stating something no longer true. And the page owns the list, so a second copy here could
   * disagree with the one the tab's own visibility was decided from.
   */
  onChanged: () => void;
  /**
   * THE OPERATOR ASKED FOR A FRESH READ — nothing was written (T9, finding #8).
   *
   * A SEPARATE prop from `onChanged` even though the page binds both to the same `refetch`. The two
   * mean different things: `onChanged` is a report that this tab mutated something, and a future
   * reader who found a refresh button wired to it would reasonably conclude a refresh writes. The
   * honest name is worth more than the saved prop.
   *
   * It exists because a pull request's life happens on github.com — someone merges it, someone
   * closes it — and this tab would otherwise show the rows it was handed on mount until a full page
   * navigation. Polling was rejected outright, so the operator gets an explicit control instead.
   */
  onRefresh: () => void;
}

export default function PullRequestsTab({
  repoId,
  pullRequests,
  heldRole,
  roleLevel,
  ungoverned,
  loading,
  onChanged,
  onRefresh,
}: PullRequestsTabProps): JSX.Element {
  // A per-row in-flight marker (the PR number), so only the row being acted on shows a pending
  // button. One global flag would disable every row's buttons on any action.
  const [busy, setBusy] = useState<number | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [createOpen, setCreateOpen] = useState(false);

  const sorted = useMemo(() => sortPullRequests(pullRequests), [pullRequests]);
  const readOnlyNote = prReadOnlyNote(heldRole, roleLevel, ungoverned);

  // One handler for both write verbs — they differ only in the call and the success sentence, and
  // two copies of the clear-state/act/refetch dance is two chances for them to drift.
  const act = useCallback(
    async (
      number: number,
      run: () => Promise<PullRequestView>,
      succeeded: (pr: PullRequestView) => string,
      fallback: string,
    ) => {
      if (busy !== null) return;
      setBusy(number);
      setActionError(null);
      // Cleared too: without this a failed action renders the rose error beside the stale
      // emerald notice — two contradictory statements about one action.
      setNotice(null);
      try {
        const updated = await run();
        setNotice(succeeded(updated));
        // The PAGE re-reads. An approve can change another row's mergeability (branch protection
        // counts reviews), so a local patch would leave the rest of the table stating something
        // no longer true.
        onChanged();
      } catch (err: unknown) {
        setActionError(
          pullRequestActionMessage(err instanceof Error ? err.message : '', fallback),
        );
      } finally {
        setBusy(null);
      }
    },
    [busy, onChanged],
  );

  const handleApprove = useCallback(
    (number: number) =>
      act(
        number,
        () => pullRequestsApi.approve(repoId, number),
        (pr) => `Approved #${pr.number} as you.`,
        'The approval could not be recorded.',
      ),
    [act, repoId],
  );

  const handleMerge = useCallback(
    (number: number) =>
      act(
        number,
        () => pullRequestsApi.merge(repoId, number),
        (pr) => `Merged #${pr.number}.`,
        'The merge could not be completed.',
      ),
    [act, repoId],
  );

  // No loading branch and no hidden branch here, deliberately: this component only ever renders
  // once the page has resolved the probe to `visible` and put the tab in the strip. A pending or
  // hidden state drawn into a panel nobody can reach would be markup for its own sake.
  return (
    <div className="space-y-4">
      {actionError && (
        <p className="text-sm text-red-600" role="alert">
          {actionError}
        </p>
      )}
      {notice && (
        <p className="text-sm text-emerald-700" role="status">
          {notice}
        </p>
      )}

      <div className={`${OPS_CARD} p-5`}>
        <div className="flex items-start justify-between gap-4 mb-4">
          <div>
            <h3 className="text-sm font-semibold text-slate-800">Pull requests</h3>
            <p className="text-sm text-slate-500 mt-1 max-w-2xl">
              {/* Says whose authority is being used, because that is the surprising part: these
                  actions are attributed to the operator's own GitHub account, not to AGP. */}
              Opened, approved and merged as your own linked GitHub account — never as the
              platform.
            </p>
            {readOnlyNote && <p className="text-[11px] text-slate-400 mt-2">{readOnlyNote}</p>}
          </div>
          {/* The header's action cluster — `ConnectionsAdmin`'s idiom: the secondary read sits to
              the LEFT of the primary write, white-on-slate against emerald-fill, so there is still
              exactly one primary here and the refresh does not compete with it.
              Refresh is rendered UNCONDITIONALLY while the create button is not: re-reading is not
              a governed verb, so a viewer who cannot open a pull request can still see the current
              state of the ones that exist. */}
          <div className="flex items-center gap-2 shrink-0">
            <button
              type="button"
              onClick={onRefresh}
              disabled={loading}
              title="Re-read the pull requests from GitHub"
              className="px-3.5 py-1.5 rounded-lg bg-white border border-slate-300 text-slate-700 text-sm font-medium hover:bg-slate-50 transition-colors disabled:opacity-40 disabled:cursor-not-allowed"
            >
              {/* The in-flight label, because this re-runs the whole page load and a control that
                  looks identical before and after being pressed reads as broken. */}
              {loading ? 'Refreshing…' : 'Refresh'}
            </button>
            {readOnlyNote === null && (
              <button
                type="button"
                onClick={() => setCreateOpen(true)}
                className="px-3.5 py-1.5 rounded-lg bg-emerald-600 text-white text-sm font-medium hover:bg-emerald-700 transition-colors"
              >
                New pull request
              </button>
            )}
          </div>
        </div>

        {sorted.length === 0 ? (
          <p className="text-sm text-slate-500 py-6 text-center">
            {/* An honest empty state, and a DIFFERENT statement from the hidden tab: "this
                repository has no pull requests" is a fact, whereas "we could not ask" removes the
                tab entirely. Reaching here means the read SUCCEEDED and returned nothing. */}
            This repository has no pull requests.
          </p>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead className={OPS_TABLE_HEAD}>
                <tr>
                  <th className="text-left font-medium px-3 py-2">Pull request</th>
                  <th className="text-left font-medium px-3 py-2">State</th>
                  <th className="text-left font-medium px-3 py-2">Author</th>
                  <th className="text-left font-medium px-3 py-2">Head</th>
                  <th className="text-right font-medium px-3 py-2">Actions</th>
                </tr>
              </thead>
              <tbody className={OPS_TABLE_DIVIDE}>
                {sorted.map((pr) => {
                  const actions = prRowActions(pr, heldRole, roleLevel, ungoverned);
                  const rowBusy = busy === pr.number;
                  return (
                    <tr key={pr.number}>
                      <td className="px-3 py-2.5 max-w-md">
                        {/* A real anchor out to the provider — the pull request itself lives
                            there, and this surface does not pretend to be a PR browser. */}
                        <a
                          href={pr.url}
                          target="_blank"
                          rel="noreferrer"
                          className="text-emerald-700 hover:text-emerald-800 font-medium transition-colors"
                        >
                          #{pr.number} {pr.title || NO_VALUE}
                        </a>
                        {actions.blockedReason && (
                          <p className="text-[11px] text-slate-400 mt-1">
                            {/* The SERVER's reason, stated beside the absent button. Without
                                this a suppressed affordance reads as a bug. */}
                            {actions.blockedReason}
                          </p>
                        )}
                        {/* NO MERGEABILITY HINT HERE, deliberately (T7). A line used to tell the
                            operator that the provider was still working out whether the row could
                            merge — on every row, forever: these rows come from the LIST endpoint,
                            which omits `mergeable` entirely, and only the single-PR read computes
                            it. So it described a transient wait that had never begun. Merge is
                            still offered; the row simply does not speculate, and a real refusal
                            arrives as the backend's `not_mergeable` copy.
                            The wording itself is NOT quoted here: a guard in
                            `pullRequestsTab.test.ts` searches this source for it, and E28 had
                            five guards defeated by a comment quoting the string they forbade. */}
                      </td>
                      <td className="px-3 py-2.5">
                        <span className={`${PILL} ${OPS_BADGE[prStateBadgeKey(pr.state)]}`}>
                          <span aria-hidden="true">●</span>
                          {prStateLabel(pr.state)}
                        </span>
                      </td>
                      <td className="px-3 py-2.5 text-slate-700" title="GitHub login">
                        {/* `@login` — the provider's currency, marked as one. Never joined to an
                            Entra identity, and never guessed into one. */}
                        {authorDisplay(pr.author)}
                      </td>
                      <td className="px-3 py-2.5 font-mono text-xs text-slate-500">
                        {shortHeadSha(pr.head_sha) || NO_VALUE}
                      </td>
                      <td className="px-3 py-2.5">
                        <div className="flex items-center justify-end gap-2">
                          {/* CONDITIONALLY RENDERED, not `disabled` — `disabled` is reserved for
                              the in-flight request below. */}
                          {actions.approve && (
                            <button
                              type="button"
                              onClick={() => void handleApprove(pr.number)}
                              disabled={rowBusy}
                              className="px-3 py-1.5 rounded-lg bg-white border border-slate-300 text-slate-700 text-xs font-medium hover:bg-slate-50 transition-colors disabled:opacity-40 disabled:cursor-not-allowed"
                            >
                              {rowBusy ? 'Working…' : 'Approve'}
                            </button>
                          )}
                          {actions.merge && (
                            <button
                              type="button"
                              onClick={() => void handleMerge(pr.number)}
                              disabled={rowBusy}
                              className="px-3 py-1.5 rounded-lg bg-emerald-600 text-white text-xs font-medium hover:bg-emerald-700 transition-colors disabled:opacity-40 disabled:cursor-not-allowed"
                            >
                              {rowBusy ? 'Merging…' : 'Merge'}
                            </button>
                          )}
                        </div>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
      </div>

      {createOpen && (
        <CreatePullRequestModal
          repoId={repoId}
          onClose={() => setCreateOpen(false)}
          onCreated={(pr) => {
            setCreateOpen(false);
            setNotice(`Opened #${pr.number} as you.`);
            setActionError(null);
            onChanged();
          }}
        />
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// The create form.
//
// `base` IS OPTIONAL AND CARRIES NO DEFAULT. A tenant's branch set is open (D8), so an empty base
// means "the repository's own default branch" — a fact the PROVIDER holds and resolves. Putting a
// placeholder branch name in this form would be the same hardcode the design forbids one layer
// down, and would silently retarget a pull request on any repository whose default differs.
// ---------------------------------------------------------------------------
function CreatePullRequestModal({
  repoId,
  onClose,
  onCreated,
}: {
  repoId: string;
  onClose: () => void;
  onCreated: (pr: PullRequestView) => void;
}): JSX.Element {
  const [title, setTitle] = useState('');
  const [head, setHead] = useState('');
  const [base, setBase] = useState('');
  const [body, setBody] = useState('');
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Trimmed, so a title of spaces cannot satisfy the button and then 422 at the route (which
  // rejects blanks — the two agree deliberately).
  const ready = title.trim().length > 0 && head.trim().length > 0;

  const submit = useCallback(async () => {
    if (!ready || submitting) return;
    setSubmitting(true);
    setError(null);
    try {
      const pr = await pullRequestsApi.create(repoId, {
        title: title.trim(),
        head: head.trim(),
        // OMITTED when blank rather than sent empty — the backend distinguishes the two, and an
        // empty string is not a branch name.
        base: base.trim() || undefined,
        body: body.trim() || undefined,
      });
      onCreated(pr);
    } catch (err: unknown) {
      setError(
        pullRequestActionMessage(
          err instanceof Error ? err.message : '',
          'The pull request could not be opened.',
        ),
      );
    } finally {
      setSubmitting(false);
    }
  }, [ready, submitting, repoId, title, head, base, body, onCreated]);

  return (
    // The EXISTING Ops modal, on its full contract — every prop is required, and `actionPending`
    // is what stops a backdrop click from dismissing the dialog mid-request. The buttons go in
    // `footer` rather than into the body, because that is where this shell puts its own actions on
    // every other Ops surface.
    <ModalShell
      title="New pull request"
      description="Opened as your own linked GitHub account — you will not be able to approve it yourself."
      ariaLabel="New pull request"
      actionPending={submitting}
      onClose={onClose}
      footer={
        <div className="flex items-center justify-end gap-2">
          <button
            type="button"
            onClick={onClose}
            disabled={submitting}
            className="px-3.5 py-1.5 rounded-lg bg-white border border-slate-300 text-slate-700 text-sm font-medium hover:bg-slate-50 transition-colors disabled:opacity-40 disabled:cursor-not-allowed"
          >
            Cancel
          </button>
          <button
            type="button"
            onClick={() => void submit()}
            disabled={!ready || submitting}
            className="px-3.5 py-1.5 rounded-lg bg-emerald-600 text-white text-sm font-medium hover:bg-emerald-700 transition-colors disabled:opacity-40 disabled:cursor-not-allowed"
          >
            {submitting ? 'Opening…' : 'Open pull request'}
          </button>
        </div>
      }
    >
      <div className="space-y-4">
        <div>
          <label className={FIELD_LABEL} htmlFor="pr-title">
            Title
          </label>
          <input
            id="pr-title"
            className={INPUT}
            value={title}
            onChange={(e) => setTitle(e.target.value)}
            placeholder="What does this change?"
          />
        </div>
        <div>
          <label className={FIELD_LABEL} htmlFor="pr-head">
            Source branch
          </label>
          <input
            id="pr-head"
            className={INPUT}
            value={head}
            onChange={(e) => setHead(e.target.value)}
            placeholder="the branch holding your changes"
          />
        </div>
        <div>
          <label className={FIELD_LABEL} htmlFor="pr-base">
            Target branch (optional)
          </label>
          <input
            id="pr-base"
            className={INPUT}
            value={base}
            onChange={(e) => setBase(e.target.value)}
            placeholder="leave empty to use the repository’s default branch"
          />
        </div>
        <div>
          <label className={FIELD_LABEL} htmlFor="pr-body">
            Description (optional)
          </label>
          <textarea
            id="pr-body"
            className={`${INPUT} h-24 resize-none`}
            value={body}
            onChange={(e) => setBody(e.target.value)}
          />
        </div>
        {error && (
          <p className="text-sm text-red-600" role="alert">
            {error}
          </p>
        )}
      </div>
    </ModalShell>
  );
}
