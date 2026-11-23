import { Fragment, useEffect, useRef, useState, type JSX } from 'react';
import { useNavigate } from 'react-router-dom';

import { projectsApi, type Repository, type StepState } from '../../api/client';
import { useUser } from '../../contexts/useUser';
import AddRepoModal from './AddRepoModal.tsx';
import { objectPath } from './objectRoutes';
import RepoRow from './RepoRow.tsx';
import {
  isPromotionInFlight,
  keepPromotionOverride,
  mayMaintainProject,
  prodCandidateView,
  promoteBlockedReason,
  type ProjectRoleName,
} from './projectRoles';
import {
  CANDIDATE_ACTOR_VERB,
  PROMOTION_READY_LABEL,
  REPO_ROW_COLUMNS,
  repoActionBand,
  sortRepoRows,
} from './repoRowModel';
import { stepStatusText } from './materializeSteps';
import { OPS_CARD, OPS_PRIMARY_BTN, OPS_TABLE_DIVIDE, OPS_TABLE_HEAD } from './opsUi';

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
  return (
    <span className="relative z-10 shrink-0 inline-flex items-center justify-center w-6 h-6 rounded-full bg-white border-2 border-slate-200 text-slate-300 ring-4 ring-white">
      <span className="w-1.5 h-1.5 rounded-full bg-slate-300" aria-hidden="true" />
    </span>
  );
}

export function MaterializeTimeline({ steps }: { steps: StepState[] }): JSX.Element {
  return (
    <ol className="relative">
      {steps.map((s, i) => {
        const last = i === steps.length - 1;
        const active = s.status === 'running';
        const failed = s.status === 'failed';
        return (
          <li key={s.key} className="relative flex gap-3 pb-4 last:pb-0">
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

export default function ProjectRepositoriesTab({
  projectId,
  connectionId,
  tenantPlatform,
  repositories,
  heldRole,
  ungoverned,
  onChanged,
}: {
  projectId: string;
  connectionId: string;
  tenantPlatform: string;
  repositories: Repository[];
  heldRole: ProjectRoleName | null;
  /** The server's `ungoverned` bit: `undefined` means not reported, and only a literal `true` lets a role-less caller add and retry repositories. */
  ungoverned: boolean | undefined;
  onChanged: () => void;
}): JSX.Element {
  const { user } = useUser();
  const [showAddRepo, setShowAddRepo] = useState(false);

  const [liveRepos, setLiveRepos] = useState<Record<string, Repository>>({});
  const [seenRepositories, setSeenRepositories] = useState(repositories);
  if (repositories !== seenRepositories) {
    setSeenRepositories(repositories);
    setLiveRepos((prev) => {
      const next = Object.fromEntries(
        Object.entries(prev).filter(([id, live]) =>
          keepPromotionOverride(live.cicd_status, repositories.find((r) => r.id === id)?.cicd_status)
        )
      );
      return Object.keys(next).length === Object.keys(prev).length ? prev : next;
    });
  }

  const mountedRef = useRef(true);
  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
    };
  }, []);

  const navigate = useNavigate();

  const rows = sortRepoRows(
    repositories.map((r) => {
      const live = liveRepos[r.id];
      return live && keepPromotionOverride(live.cicd_status, r.cicd_status) ? live : r;
    }),
  );
  const repoCount = repositories.length;

  const promotingIds = rows.filter((r) => isPromotionInFlight(r.cicd_status)).map((r) => r.id);
  const promotingKey = promotingIds.join(',');

  const pollRef = useRef<number | null>(null);
  useEffect(() => {
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
      if (settled.length === 0) return;
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

  const mayMaintain = mayMaintainProject(heldRole, user?.role_level ?? 0, ungoverned);

  return (
    <>
      <div className={`${OPS_CARD} overflow-hidden`}>
        <div className="flex items-center justify-between gap-3 px-4 py-3.5 border-b border-emerald-200/50">
          <div className="min-w-0">
            <h2 className="text-sm font-semibold text-slate-800">Agents &amp; MCP servers</h2>
            <p className="text-xs text-slate-400 mt-0.5">
              Each one is built from its own source repository, materialized from a template.
            </p>
          </div>
          {mayMaintain && (
            <button type="button" onClick={() => setShowAddRepo(true)} className={`${OPS_PRIMARY_BTN} shrink-0`}>
              New agent or MCP server
            </button>
          )}
        </div>

        <table className="w-full text-sm">
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
              const promoteState = promoteBlockedReason(
                heldRole,
                user?.role_level ?? 0,
                r.prod_candidate_status,
                r.dev_outcome_for_candidate
              );
              const candidate = prodCandidateView(r);
              const band = repoActionBand(r, promoteState, inFlight);
              return (
                <Fragment key={r.id}>
                  <RepoRow
                    repo={r}
                    showProject={false}
                    onNavigate={() => navigate(objectPath(r))}
                  />

                  {band && (
                    <tr className="bg-emerald-50/20">
                      <td colSpan={REPO_ROW_COLUMNS} className="px-4 pb-3 pt-0">
                        <div className="flex flex-wrap items-center justify-end gap-x-3 gap-y-1.5">
                          {band === 'awaiting-approval' && candidate && promoteState === 'ok' && (
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

                          {band === 'awaiting-approval' && (
                            <span className="inline-flex items-center gap-1.5 px-2 py-0.5 rounded-md bg-emerald-50/70 border border-emerald-200/60 text-[11px] font-medium text-emerald-800">
                              {PROMOTION_READY_LABEL}
                            </span>
                          )}
                          {band === 'dev-deploy-failed' && (
                            <span className="text-[11px] text-rose-600">
                              This candidate's dev deploy failed — not ready for promotion
                            </span>
                          )}
                        </div>
                      </td>
                    </tr>
                  )}

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

      {showAddRepo && (
        <AddRepoModal
          projectId={projectId}
          connectionId={connectionId}
          tenantPlatform={tenantPlatform}
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
