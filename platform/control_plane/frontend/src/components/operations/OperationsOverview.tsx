import { useState, type JSX } from 'react';

import {
  connectionsApi,
  projectsApi,
  repositoriesApi,
} from '../../api/client';
import type { LoadState } from '../../api/loadState';
import { useLoad } from '../../api/useLoad';
import NewObjectFlow from './NewObjectFlow.tsx';
import OpsPage from './OpsPage';
import { OPS_CARD, OPS_PRIMARY_BTN } from './opsUi';

function tile(state: LoadState<unknown[]>): { value: string; pulse: boolean } {
  if (state.kind === 'ready') return { value: String(state.data.length), pulse: false };
  return { value: '—', pulse: state.kind === 'loading' };
}

export default function OperationsOverview(): JSX.Element {
  const [showNew, setShowNew] = useState(false);
  const projects = useLoad(() => projectsApi.list(), []);
  const repositories = useLoad(() => repositoriesApi.list(), []);
  const connections = useLoad(() => connectionsApi.list(), []);
  const anyLoading = [projects, repositories, connections].some((load) => load.state.kind === 'loading');

  const kpis: { label: string; value: string; pulse: boolean; sub: string }[] = [
    { label: 'Projects', ...tile(projects.state), sub: 'Provisioning containers' },
    { label: 'Agents & MCP servers', ...tile(repositories.state), sub: 'Across all projects' },
    { label: 'Templates', value: '—', pulse: anyLoading, sub: 'Agent blueprints' },
    { label: 'Org Connections', ...tile(connections.state), sub: 'GitHub · GitLab' },
  ];

  return (
    <OpsPage
      title="Operations"
      subtitle="Build and operate agents and MCP servers — source, CI/CD and deployments in one place."
      action={
        <button type="button" onClick={() => setShowNew(true)} className={OPS_PRIMARY_BTN}>
          + New
        </button>
      }
    >
      {showNew && <NewObjectFlow onClose={() => setShowNew(false)} />}
      <div className="grid grid-cols-2 md:grid-cols-4 gap-3 mb-4">
        {kpis.map((k) => (
          <div key={k.label} className={`${OPS_CARD} p-4`}>
            <div className="text-[11px] font-medium text-slate-500 uppercase tracking-wide">{k.label}</div>
            <div
              className={`text-3xl font-semibold text-slate-900 mt-1 tabular-nums ${
                k.pulse ? 'animate-pulse text-slate-300' : ''
              }`}
            >
              {k.value}
            </div>
            <div className="text-[11px] text-slate-400 mt-0.5">{k.sub}</div>
          </div>
        ))}
      </div>
    </OpsPage>
  );
}
