// Repositories — the API-backed flat list of every repository across all projects
// (Epic 20 / Task 17). Rows come from repositoriesApi.list(); the Project column
// resolves project_id → project name via projectsApi.list().
//
// ---------------------------------------------------------------------------
// E28/T13 — THE ROW IS THE SHARED `RepoRow` (contract C4)
//
// This page and a project's own repositories tab render the SAME component now. They were two
// implementations of one row, and the divergence shipped: this file used to carry its own copy of
// the status→tint helper, and when E27 added `promoting` / `deployed` only the OTHER copy was
// extended, so both new statuses fell through to the amber `pending` here and A REPO LIVE IN
// PRODUCTION RENDERED IN THE SAME AMBER AS ONE STILL PROVISIONING — wrong, and reassuring, on the
// one list whose question is "what is in production right now?".
//
// A guard asserts that neither list names any status table at all, which is why this comment
// describes that helper rather than naming it: the guard reads raw source and does not skip
// comments, deliberately — a guard that has to decide what is "only a comment" is one a comment
// can defeat.
//
// That was fixed by consolidating the tables and leaving a comment asking the next author not to
// re-fork them. This is the mechanical version of that request: there is no row markup in this
// file to fork. `showProject` is the ONLY prop that differs between the two call sites (true here,
// false inside a project — a project's own table would repeat its own name on every row), and a
// second boolean would be the beginning of the same divergence.
//
// WHAT LEFT WITH IT. The Provider column and its chip are gone: the provider is a property of the
// repo's CONNECTION, it was identical on every row of any single-org deployment, and it cost two
// best-effort API reads to render. It lives on the detail page (as `orgLabel`, which names the
// provider AND the org) along with the template, the agent and the provider link — the row states
// STATUS, and `/ops/repositories/:id` answers everything else.
//
// House style: emerald-on-glass Ops tokens (opsUi.ts), the OpsPage frame, Tailwind
// v4 utility strings, 2-space indent — mirroring Projects.tsx / ProjectDetail.tsx.

import { useEffect, useMemo, useState, type JSX } from 'react';
import { useNavigate } from 'react-router-dom';

import {
  projectsApi,
  repositoriesApi,
  type Project,
  type Repository,
} from '../../api/client';
import OpsPage from './OpsPage';
// THE EXPLICIT `.tsx` IS LOAD-BEARING on a case-insensitive filesystem: an extensionless
// specifier probes `<Name>.ts` FIRST, so the `repoRowModel`-style companion beside a component
// can win the resolution and the import silently binds to a module with no default export
// (TS1149). This epic hit that four times.
import RepoRow, { REPO_ROW_COLUMNS } from './RepoRow.tsx';
// The SHARED sort, so this list and the project's table put the same repo in the same place.
// C4 makes "Action required" the sort key; two independent sorts over one fact is the same class
// of drift as the two status tables that caused the amber-prod bug.
import { sortRepoRows } from './repoRowModel';
import { OPS_CARD, OPS_TABLE_DIVIDE, OPS_TABLE_HEAD } from './opsUi';

export default function Repositories(): JSX.Element {
  const navigate = useNavigate();
  const [repositories, setRepositories] = useState<Repository[]>([]);
  const [projects, setProjects] = useState<Project[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [reloadNonce, setReloadNonce] = useState(0);

  // Load repositories (primary — its failure is the error state), then resolve
  // projects best-effort (they feed the row's Project sub-line). A secondary failure
  // — e.g. a 403 for a true OPERATOR — must NOT blank the repositories list; the
  // sub-line just falls back to the raw project id, which is what `RepoRow` does when
  // `projectName` is undefined. Mirrors Projects.tsx (T16) and ProjectDetail.tsx.
  //
  // The connections read is GONE: it existed only for the Provider column, which moved to the
  // detail page. One fewer best-effort request per page load, and one fewer failure mode.
  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    repositoriesApi
      .list()
      .then(async (repoRows) => {
        if (cancelled) return;
        setRepositories(repoRows);
        setError(null);
        try {
          const projectRows = await projectsApi.list();
          if (!cancelled) setProjects(projectRows);
        } catch {
          if (!cancelled) setProjects([]);
        }
      })
      .catch((err: unknown) => {
        if (cancelled) return;
        setError(err instanceof Error ? err.message : 'Failed to load repositories.');
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [reloadNonce]);

  const projectsById = useMemo(
    () => new Map(projects.map((p) => [p.id, p])),
    [projects],
  );

  // Ordered by the SHARED sort — "Action required" first (C4), so a fleet scan reads failures
  // and pending approvals before anything else. No runtime probe on this page: the runtime route
  // is per-AGENT and a fleet list would need one request per row, so every row's `runtime` is
  // undefined, which `RepoRow` renders as UNREACHABLE and never as ready.
  const rows = useMemo(() => sortRepoRows(repositories), [repositories]);

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
            onClick={() => setReloadNonce((n) => n + 1)}
            className="mt-3 px-3 py-1.5 rounded-lg bg-white border border-slate-300 text-slate-700 text-xs font-medium hover:bg-slate-50 transition-colors"
          >
            Retry
          </button>
        </div>
      ) : (
        <div className={`${OPS_CARD} overflow-hidden`}>
          <table className="w-full text-sm">
            {/* The columns are `RepoRow`'s, in C4's pinned order, and the `colSpan`s below come
                from `REPO_ROW_COLUMNS` — the row's own exported width — so a loading or empty
                cell cannot drift from the number of cells a row actually renders. */}
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
                    // TRUE here and false in a project's own table — the ONE difference between
                    // the two call sites (C4). A fleet list must say which project a repo is in;
                    // a project's own list would repeat its own name on every row.
                    showProject
                    projectName={projectsById.get(r.project_id)?.name}
                    onNavigate={(repoId) => navigate(`/ops/repositories/${repoId}`)}
                  />
                ))}

              {!loading && repositories.length === 0 && (
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
