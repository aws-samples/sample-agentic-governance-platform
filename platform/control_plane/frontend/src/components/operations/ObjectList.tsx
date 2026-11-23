import { useMemo, useState, type JSX } from 'react';
import { useNavigate } from 'react-router-dom';

import {
  agentsApi,
  mcpServersApi,
  projectsApi,
  repositoriesApi,
  type LifecycleState,
  type Project,
} from '../../api/client';
import { useLoad } from '../../api/useLoad';
import { lifecycleBadge } from '../governance/agentBadges';
import NewObjectFlow from './NewObjectFlow.tsx';
import OpsPage from './OpsPage';
import { OBJECT_NOUN, OBJECT_NOUN_INLINE, OBJECT_NOUN_PLURAL, objectPath, objectsOfKind } from './objectRoutes';
import { CICD_BADGE_KEY, CICD_LABEL, isCicdInFlight } from './opsStatus';
import { OPS_BADGE, OPS_CARD, OPS_PRIMARY_BTN, OPS_TABLE_DIVIDE, OPS_TABLE_HEAD } from './opsUi';
import { isCommitShaTag, prodVersion, shortCommitSha } from './repoRowModel';
import { gatewayStatus } from './repositoryDetailTabs';
import { shortBuildTag } from './repo-tabs/evaluationsLogic';
import type { ArtifactKind } from './templateChain.ts';

const PILL = 'inline-flex items-center gap-1.5 text-[11px] font-semibold px-2 py-0.5 rounded-full';
const COLUMNS = 5;
const EMPTY_LIFECYCLE: ReadonlyMap<string, LifecycleState> = new Map();

const SUBTITLE: Record<ArtifactKind, string> = {
  agent: 'Every agent across the projects you can see. Each one is built from its source repository.',
  mcp_server: 'Every MCP server across the projects you can see. Each one exposes an API as tools behind a gateway.',
};

const EMPTY: Record<ArtifactKind, string> = {
  agent: 'No agents yet. Create one from a template — it gets a repository, a pipeline and a governance record.',
  mcp_server: 'No MCP servers yet. Create one from an API template — it gets a repository and a gateway.',
};

export default function ObjectList({ kind }: { kind: ArtifactKind }): JSX.Element {
  const navigate = useNavigate();
  const [showNew, setShowNew] = useState(false);

  const { state, reload } = useLoad(
    () =>
      repositoriesApi.list().then(async (repositories) => {
        const [projects, records] = await Promise.all([
          projectsApi.list().catch(() => [] as Project[]),
          (kind === 'agent' ? agentsApi.list() : mcpServersApi.list()).catch(() => []),
        ]);
        return {
          repositories,
          projects,
          lifecycle: new Map(records.map((r) => [r.id, r.lifecycle_state as LifecycleState])),
        };
      }),
    [kind],
    'Failed to load.',
  );
  const loaded = state.data;
  const loading = state.kind === 'loading' && loaded === null;
  const error = state.kind === 'error' ? state.message : null;
  const lifecycle = loaded?.lifecycle ?? EMPTY_LIFECYCLE;

  const rows = useMemo(() => objectsOfKind(loaded?.repositories ?? [], kind), [loaded, kind]);
  const projectsById = useMemo(() => new Map((loaded?.projects ?? []).map((p) => [p.id, p.name])), [loaded]);
  const isMcp = kind === 'mcp_server';
  const newLabel = `New ${OBJECT_NOUN_INLINE[kind]}`;

  return (
    <OpsPage
      backTo="/ops"
      title={OBJECT_NOUN_PLURAL[kind]}
      subtitle={SUBTITLE[kind]}
      action={
        <button
          type="button"
          onClick={() => setShowNew(true)}
          className={OPS_PRIMARY_BTN}
          data-testid="object-new"
        >
          + {newLabel}
        </button>
      }
    >
      {error ? (
        <div className="bg-white/80 backdrop-blur rounded-xl border border-rose-200/70 shadow-sm p-6">
          <h3 className="text-sm font-semibold text-rose-700">Couldn’t load {OBJECT_NOUN_PLURAL[kind].toLowerCase()}</h3>
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
                <th className="text-left font-medium px-4 py-2.5">{OBJECT_NOUN[kind]}</th>
                <th className="text-left font-medium px-4 py-2.5">Last build</th>
                <th className="text-left font-medium px-4 py-2.5">{isMcp ? 'Gateway' : 'Dev'}</th>
                <th className="text-left font-medium px-4 py-2.5">{isMcp ? 'Template' : 'Prod'}</th>
                <th className="text-left font-medium px-4 py-2.5">Governance</th>
              </tr>
            </thead>
            <tbody className={OPS_TABLE_DIVIDE}>
              {loading && (
                <tr>
                  <td colSpan={COLUMNS} className="px-4 py-8 text-center text-slate-400 text-sm">
                    Loading…
                  </td>
                </tr>
              )}

              {!loading &&
                rows.map((r) => {
                  const cicd = r.cicd_status;
                  const prod = prodVersion(r);
                  const gateway = gatewayStatus(r);
                  const recordId = isMcp ? r.mcp_server_id : r.agent_id;
                  const state = recordId ? lifecycle.get(recordId) : undefined;
                  const open = () => navigate(objectPath(r));
                  return (
                    <tr
                      key={r.id}
                      onClick={open}
                      role="button"
                      tabIndex={0}
                      aria-label={`Open ${r.name}`}
                      onKeyDown={(e) => {
                        if (e.key === 'Enter' || e.key === ' ') {
                          e.preventDefault();
                          open();
                        }
                      }}
                      className="hover:bg-emerald-50/40 transition-colors cursor-pointer focus:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-emerald-500/60"
                    >
                      <td className="px-4 py-3 font-medium text-slate-900">
                        {r.name}
                        <span className="block text-[11px] font-normal text-slate-400">
                          {projectsById.get(r.project_id) ?? r.project_id}
                        </span>
                      </td>
                      <td className="px-4 py-3">
                        <span className={`${PILL} ${OPS_BADGE[CICD_BADGE_KEY[cicd]]}`}>
                          {isCicdInFlight(cicd) ? (
                            <svg className="w-2.5 h-2.5 animate-spin" viewBox="0 0 24 24" fill="none" aria-hidden="true">
                              <circle cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" strokeDasharray="40 60" />
                            </svg>
                          ) : (
                            <span aria-hidden="true">●</span>
                          )}
                          {CICD_LABEL[cicd]}
                        </span>
                      </td>
                      {isMcp ? (
                        <>
                          <td className="px-4 py-3">
                            <span className={`${PILL} ${OPS_BADGE[gateway.deployed ? 'ready' : 'unknown']}`}>
                              <span aria-hidden="true">●</span>
                              {gateway.label}
                            </span>
                          </td>
                          <td className="px-4 py-3 text-xs text-slate-500">{r.template_name}</td>
                        </>
                      ) : (
                        <>
                          <td className="px-4 py-3">
                            {r.last_dev_image_tag ? (
                              <span className="font-mono text-xs text-slate-600" title={r.last_dev_image_tag}>
                                {isCommitShaTag(r.last_dev_image_tag) ? shortCommitSha(r.last_dev_image_tag) : shortBuildTag(r.last_dev_image_tag)}
                              </span>
                            ) : (
                              <span className="text-xs text-slate-400">Not deployed</span>
                            )}
                          </td>
                          <td className="px-4 py-3">
                            {prod.tag ? (
                              <>
                                <span className="font-mono text-xs text-slate-600" title={prod.tag}>
                                  {isCommitShaTag(prod.tag) ? shortCommitSha(prod.tag) : shortBuildTag(prod.tag)}
                                </span>
                                {prod.drifted && <span className="block text-[11px] text-amber-700">dev is ahead</span>}
                              </>
                            ) : (
                              <span className="text-xs text-slate-400">Not deployed</span>
                            )}
                          </td>
                        </>
                      )}
                      <td className="px-4 py-3">
                        {state ? (
                          <span className={`${PILL} ${lifecycleBadge(state).cls}`}>{lifecycleBadge(state).label}</span>
                        ) : (
                          <span className="text-slate-400">—</span>
                        )}
                      </td>
                    </tr>
                  );
                })}

              {!loading && rows.length === 0 && (
                <tr>
                  <td colSpan={COLUMNS} className="px-4 py-10 text-center">
                    <p className="text-sm text-slate-500">{EMPTY[kind]}</p>
                    <button type="button" onClick={() => setShowNew(true)} className={`${OPS_PRIMARY_BTN} mt-3`}>
                      + {newLabel}
                    </button>
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
      )}

      {showNew && <NewObjectFlow presetKind={kind} onClose={() => setShowNew(false)} />}
    </OpsPage>
  );
}
