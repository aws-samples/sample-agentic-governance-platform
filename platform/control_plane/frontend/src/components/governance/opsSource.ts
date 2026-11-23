import type { AgentRepositoryView, Origin } from '../../api/client';
import { objectPath } from '../operations/objectRoutes';

export const BUILT_IN_OPS_LABEL = 'Built in Ops';

export interface OpsProvenance {
  badge: { cls: string; label: string };
  source: { name: string; to: string } | null;
}

export function opsProvenance(
  origin: Origin,
  repo: Pick<AgentRepositoryView, 'repo_id' | 'name'> | null | undefined,
  originBadge: (o: Origin) => { cls: string; label: string },
): OpsProvenance {
  if (!repo) return { badge: originBadge(origin), source: null };
  return {
    badge: { cls: 'bg-emerald-50 text-emerald-700', label: BUILT_IN_OPS_LABEL },
    source: { name: repo.name, to: objectPath({ id: repo.repo_id, artifact_kind: 'agent' }) },
  };
}
