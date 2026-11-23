import { useMemo, type JSX } from 'react';
import { useNavigate } from 'react-router-dom';

import {
  projectsApi,
  repositoriesApi,
  type Project,
  type Repository,
} from '../../api/client';
import { useLoad } from '../../api/useLoad';
import OpsPage from './OpsPage';
import { objectPath } from './objectRoutes';
import RepoRow from './RepoRow.tsx';
import { REPO_ROW_COLUMNS, sortRepoRows } from './repoRowModel';
import { OPS_CARD, OPS_TABLE_DIVIDE, OPS_TABLE_HEAD } from './opsUi';

export default function Repositories(): JSX.Element {
  const navigate = useNavigate();
  const { state, reload } = useLoad(
    () =>
      repositoriesApi.list().then(async (repositories: Repository[]) => ({
        repositories,
        projects: await projectsApi.list().catch(() => [] as Project[]),
      })),
    [],
    'Failed to load repositories.',
  );
  const loading = state.kind === 'loading';
  const error = state.kind === 'error' ? state.message : null;
  const data = state.kind === 'ready' ? state.data : null;

  const projectsById = useMemo(
    () => new Map((data?.projects ?? []).map((p) => [p.id, p])),
    [data],
  );

  const rows = useMemo(() => sortRepoRows(data?.repositories ?? []), [data]);

  return (
    <OpsPage
      backTo="/ops"
      title="Repositories"
      subtitle="Every repository across all projects. Each repo is materialized into a project from an ops-template."
    >
      {error ? (
        <div className="bg-white/80 backdrop-blur rounded-xl border border-rose-200/70 shadow-sm p-6">
          <h3 className="text-sm font-semibold text-rose-700">Couldn’t load repositories</h3>
          <p className="text-sm text-slate-600 mt-1">{error}</p>
          <button
            type="button"
            onClick={reload}
            className="mt-3 px-3 py-1.5 rounded-lg bg-white border border-slate-300 text-slate-700 text-xs font-medium hover:bg-slate-50 transition-colors"
          >
            Retry
          </button>
        </div>
      ) : (
        <div className={`${OPS_CARD} overflow-hidden`}>
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
              {loading && (
                <tr>
                  <td colSpan={REPO_ROW_COLUMNS} className="px-4 py-8 text-center text-slate-400 text-sm">
                    Loading repositories…
                  </td>
                </tr>
              )}

              {!loading &&
                rows.map((r) => (
                  <RepoRow
                    key={r.id}
                    repo={r}
                    showProject
                    projectName={projectsById.get(r.project_id)?.name}
                    onNavigate={() => navigate(objectPath(r))}
                  />
                ))}

              {!loading && rows.length === 0 && (
                <tr>
                  <td colSpan={REPO_ROW_COLUMNS} className="px-4 py-8 text-center text-slate-400 text-sm">
                    No repositories yet — create one from a project.
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
      )}
    </OpsPage>
  );
}
