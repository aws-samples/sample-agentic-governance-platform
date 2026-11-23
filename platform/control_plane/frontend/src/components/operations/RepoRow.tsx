import type { JSX } from 'react';

import type { Repository, RuntimeStatus } from '../../api/client';
import { useUser } from '../../contexts/useUser';
import { CICD_BADGE_KEY, CICD_LABEL, RUNTIME_BADGE_KEY, isCicdInFlight } from './opsStatus';
import {
  LAST_DEPLOYED_ACTOR_VERB,
  LAST_DEPLOYED_LABEL,
  REPO_ACTION_LABEL,
  REPO_ACTION_TONE,
  ownerLabel,
  prodVersion,
  repoAction,
} from './repoRowModel';
import {
  actorTitle,
  agentRuntimeLabel,
  entraActorName,
  gatewayStatus,
  repoKindChip,
  representativeImageTag,
  rowActionForKind,
  runtimeStatusTitle,
} from './repositoryDetailTabs';
import { OPS_BADGE } from './opsUi';
import { KIND_CHIP } from './templateCardStyles';

const PILL =
  'inline-flex items-center gap-1.5 text-[11px] font-semibold px-2 py-0.5 rounded-full';

const ACTION_TONE_CLS: Record<'fault' | 'attention' | 'quiet', string> = {
  fault: `${PILL} ${OPS_BADGE.failed}`,
  attention: `${PILL} ${OPS_BADGE.warn}`,
  quiet: 'text-[11px] text-slate-400',
};

export interface RepoRowProps {
  repo: Repository;
  /** Absent reads as unknown, never as ready. */
  runtime?: RuntimeStatus;
  projectName?: string;
  showProject: boolean;
  onNavigate: (repoId: string) => void;
}

export default function RepoRow({
  repo,
  runtime,
  projectName,
  showProject,
  onNavigate,
}: RepoRowProps): JSX.Element {
  const cicd = repo.cicd_status;
  const runtimeKey = runtime?.status ?? 'unknown';
  const kindChip = repoKindChip(repo);
  const gateway = kindChip !== null ? gatewayStatus(repo) : null;
  const action = rowActionForKind(repo, repoAction(repo, runtimeKey));
  const prod = prodVersion(repo);
  const owner = ownerLabel(repo.created_by);
  const inFlight = isCicdInFlight(cicd);
  const { user } = useUser();

  return (
    <tr
      onClick={() => onNavigate(repo.id)}
      role="button"
      tabIndex={0}
      aria-label={`Open ${repo.name}`}
      onKeyDown={(e) => {
        if (e.key === 'Enter' || e.key === ' ') {
          e.preventDefault();
          onNavigate(repo.id);
        }
      }}
      className="hover:bg-emerald-50/40 transition-colors cursor-pointer focus:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-emerald-500/60"
    >
      <td className="px-4 py-3 font-medium text-slate-900">
        {repo.name}
        {kindChip !== null && (
          <span className={`ml-2 align-middle text-[10px] font-medium px-1.5 py-0.5 rounded ${KIND_CHIP.mcp_server}`}>
            {kindChip}
          </span>
        )}
        {showProject && (
          <span className="block text-[11px] font-normal text-slate-400">
            {projectName ?? repo.project_id}
          </span>
        )}
      </td>

      <td className="px-4 py-3">
        <span className={ACTION_TONE_CLS[REPO_ACTION_TONE[action]]}>{REPO_ACTION_LABEL[action]}</span>
      </td>

      <td className="px-4 py-3">
        {gateway !== null ? (
          <span className={`${PILL} ${OPS_BADGE[gateway.deployed ? 'ready' : 'unknown']}`}>
            <span aria-hidden="true">●</span>
            {gateway.label}
          </span>
        ) : (
        <span
          className={`${PILL} ${OPS_BADGE[RUNTIME_BADGE_KEY[runtimeKey]]}`}
          title={runtimeStatusTitle(runtime) ?? undefined}
        >
          <span aria-hidden="true">●</span>
          {agentRuntimeLabel(runtimeKey, representativeImageTag(repo))}
        </span>
        )}
      </td>

      <td className="px-4 py-3">
        <span className={`${PILL} ${OPS_BADGE[CICD_BADGE_KEY[cicd]]}`}>
          {inFlight ? (
            <svg className="w-2.5 h-2.5 animate-spin" viewBox="0 0 24 24" fill="none" aria-hidden="true">
              <circle cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" strokeDasharray="40 60" />
            </svg>
          ) : (
            <span aria-hidden="true">●</span>
          )}
          {CICD_LABEL[cicd]}
        </span>
      </td>

      <td className="px-4 py-3">
        {prod.tag ? (
          <>
            <span className="font-mono text-xs text-slate-600">{prod.tag}</span>
            {prod.drifted && prod.devTag && (
              <span className="block text-[11px] text-amber-700">dev at {prod.devTag}</span>
            )}
          </>
        ) : (
          <span className="text-slate-400">—</span>
        )}
        {repo.last_promoted_at && (
          <span className="block text-[11px] text-slate-400">
            {LAST_DEPLOYED_LABEL} {repo.last_promoted_at.slice(0, 10)}
            {repo.last_promoted_by && (
              <span
                title={`${LAST_DEPLOYED_ACTOR_VERB} — ${actorTitle({ kind: 'entra', display: repo.last_promoted_by })}`}
              >
                {` · ${entraActorName(repo.last_promoted_by, user)}`}
              </span>
            )}
          </span>
        )}
      </td>

      <td className="px-4 py-3">
        <span className="inline-flex items-center gap-2 min-w-0">
          <span
            className="shrink-0 h-6 w-6 rounded-md bg-slate-100 text-slate-500 text-[10px] font-semibold inline-flex items-center justify-center"
            aria-hidden="true"
          >
            {owner.initials}
          </span>
          <span className="text-slate-600 text-xs truncate max-w-[12rem]" title={owner.name}>
            {owner.name}
          </span>
        </span>
      </td>
    </tr>
  );
}
