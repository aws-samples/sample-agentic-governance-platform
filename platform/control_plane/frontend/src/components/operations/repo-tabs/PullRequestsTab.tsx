import { useCallback, useMemo, useState, type JSX } from 'react';

import { pullRequestsApi, type PullRequestView } from '../../../api/client';
import { ModalShell } from '../ModalShell';
import { OPS_BADGE, OPS_CARD, OPS_TABLE_DIVIDE, OPS_TABLE_HEAD } from '../opsUi';
import { type ProjectRoleName } from '../projectRoles';
import {
  NO_VALUE,
  authorDisplay,
  createPullRequestBody,
  prReadOnlyNote,
  prRowActions,
  prStateBadgeKey,
  prStateLabel,
  pullRequestActionMessage,
  shortHeadSha,
  sortPullRequests,
  withLocalMerges,
} from './pullRequestsTabModel.ts';

const PILL =
  'inline-flex items-center gap-1.5 text-[11px] font-semibold px-2 py-0.5 rounded-full';
const FIELD_LABEL = 'block text-[11px] uppercase tracking-wide text-slate-400 font-medium mb-1';
const INPUT =
  'w-full px-3 py-2 rounded-lg border border-slate-300 text-sm text-slate-800 focus:outline-none focus:border-emerald-500 transition-colors';

export interface PullRequestsTabProps {
  repoId: string;
  pullRequests: readonly PullRequestView[];
  heldRole: ProjectRoleName | null;
  roleLevel: number;
  /** The project's `ungoverned` bit; null or absent means not reported. */
  ungoverned: boolean | null | undefined;
  loading: boolean;
  onChanged: () => void;
  /** The operator asked for a fresh read; unlike `onChanged`, nothing was written. */
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
  const [busy, setBusy] = useState<number | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [createOpen, setCreateOpen] = useState(false);
  const [mergedHere, setMergedHere] = useState<ReadonlySet<number>>(new Set());

  const sorted = useMemo(
    () => sortPullRequests(withLocalMerges(pullRequests, mergedHere)),
    [pullRequests, mergedHere],
  );
  const readOnlyNote = prReadOnlyNote(heldRole, roleLevel, ungoverned);

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
      setNotice(null);
      try {
        const updated = await run();
        setNotice(succeeded(updated));
        onChanged();
      } catch (err: unknown) {
        setActionError(pullRequestActionMessage(err, fallback));
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
        async () => {
          const pr = await pullRequestsApi.merge(repoId, number);
          setMergedHere((prev) => new Set(prev).add(number));
          return pr;
        },
        (pr) => `Merged #${pr.number}.`,
        'The merge could not be completed.',
      ),
    [act, repoId],
  );

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
              Opened, approved and merged as your own linked GitHub account — never as the
              platform.
            </p>
            {readOnlyNote && <p className="text-[11px] text-slate-400 mt-2">{readOnlyNote}</p>}
          </div>
          <div className="flex items-center gap-2 shrink-0">
            <button
              type="button"
              onClick={onRefresh}
              disabled={loading}
              title="Re-read the pull requests from GitHub"
              className="px-3.5 py-1.5 rounded-lg bg-white border border-slate-300 text-slate-700 text-sm font-medium hover:bg-slate-50 transition-colors disabled:opacity-40 disabled:cursor-not-allowed"
            >
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
                            {actions.blockedReason}
                          </p>
                        )}
                      </td>
                      <td className="px-3 py-2.5">
                        <span className={`${PILL} ${OPS_BADGE[prStateBadgeKey(pr.state)]}`}>
                          <span aria-hidden="true">●</span>
                          {prStateLabel(pr.state)}
                        </span>
                      </td>
                      <td className="px-3 py-2.5 text-slate-700" title="GitHub login">
                        {authorDisplay(pr.author)}
                      </td>
                      <td className="px-3 py-2.5 font-mono text-xs text-slate-500">
                        {shortHeadSha(pr.head_sha) || NO_VALUE}
                      </td>
                      <td className="px-3 py-2.5">
                        <div className="flex items-center justify-end gap-2">
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

  const draft = createPullRequestBody(title, head, base);
  const ready = draft.title.length > 0 && draft.head.length > 0;

  const submit = useCallback(async () => {
    if (!ready || submitting) return;
    setSubmitting(true);
    setError(null);
    try {
      const pr = await pullRequestsApi.create(repoId, { ...draft, body: body.trim() || undefined });
      onCreated(pr);
    } catch (err: unknown) {
      setError(pullRequestActionMessage(err, 'The pull request could not be opened.'));
    } finally {
      setSubmitting(false);
    }
  }, [ready, submitting, repoId, draft, body, onCreated]);

  return (
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
