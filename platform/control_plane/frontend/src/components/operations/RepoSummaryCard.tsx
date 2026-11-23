import type { JSX } from 'react';
import { Link } from 'react-router-dom';

import type { Agent, Connection, McpServer, Repository, RuntimeStatus } from '../../api/client';
import { lifecycleBadge } from '../governance/agentBadges';
import { NO_VALUE, orgLabel } from './opsLabels';
import { CICD_BADGE_KEY, RUNTIME_BADGE_KEY } from './opsStatus';
import { OPS_BADGE, OPS_CARD } from './opsUi';
import { CopyField, FIELD_LABEL, Field, HeaderName } from './RepoFields';
import {
  agentRuntimeLabel,
  headerName,
  representativeImageTag,
  runtimeScope,
  runtimeStatusTitle,
  type DeliveryHeaderState,
  type RepoArtifactLink,
} from './repositoryDetailTabs';

const PILL =
  'inline-flex items-center gap-1.5 text-[11px] font-semibold px-2 py-0.5 rounded-full';

export function RepoSummaryCard({
  repo,
  agent,
  mcp,
  agentError,
  isMcp,
  artifactLink,
  projectName,
  projectRead,
  connection,
  connectionId,
  runtime,
  delivery,
  deliveryFailure,
  gateway,
  setActiveKey,
}: {
  repo: Repository;
  agent: Agent | null;
  mcp: McpServer | null;
  agentError: boolean;
  isMcp: boolean;
  artifactLink: RepoArtifactLink;
  projectName: string | null;
  projectRead: boolean;
  connection: Connection | undefined;
  connectionId: string | null;
  runtime: RuntimeStatus | undefined;
  delivery: DeliveryHeaderState;
  deliveryFailure: string | null;
  gateway: { deployed: boolean; label: string };
  setActiveKey: (key: string) => void;
}): JSX.Element {
  const cicd = repo.cicd_status;
  const runtimeKey = runtime?.status ?? 'unknown';
  const scope = runtimeScope(runtime);

  return (
    <div className={`${OPS_CARD} px-4 py-3 mb-6 flex flex-wrap items-center gap-x-8 gap-y-3`}>
      <div>
        <span className={FIELD_LABEL}>{artifactLink.label}</span>
        <Link
          to={artifactLink.to}
          className="text-sm text-emerald-700 hover:text-emerald-800 font-medium transition-colors"
        >
          <HeaderName
            text={headerName(
              isMcp ? mcp?.name : agent?.name,
              (isMcp ? mcp !== null : agent !== null) || agentError,
              artifactLink.id,
            )}
          />
        </Link>
      </div>
      <div>
        <span className={FIELD_LABEL}>Source</span>
        {repo.repo_url ? (
          <a
            href={repo.repo_url}
            target="_blank"
            rel="noreferrer"
            className="text-sm text-emerald-700 hover:text-emerald-800 font-medium transition-colors"
          >
            {repo.name} ↗
          </a>
        ) : (
          <span className="text-sm text-slate-700">{repo.name}</span>
        )}
        <span className="block text-[11px] text-slate-400">from {repo.template_name}</span>
      </div>
      <div>
        <span className={FIELD_LABEL}>Project</span>
        <Link
          to={`/ops/projects/${repo.project_id}`}
          className="text-sm text-emerald-700 hover:text-emerald-800 font-medium transition-colors"
        >
          <HeaderName text={headerName(projectName, projectRead, repo.project_id)} />
        </Link>
      </div>
      <div>
        <span className={FIELD_LABEL}>Org</span>
        <span className="text-sm text-slate-700">
          <HeaderName text={projectRead ? orgLabel(connection, connectionId) : null} />
        </span>
      </div>
      <div>
        <span className={FIELD_LABEL}>Owner</span>
        <span className="text-sm text-slate-700">{repo.created_by || NO_VALUE}</span>
      </div>

      <div>
        <span className={FIELD_LABEL}>Status</span>
        <span className="inline-flex items-center gap-2">
          <span className={`${PILL} ${OPS_BADGE[CICD_BADGE_KEY[cicd]]}`}>
            {delivery.inFlight ? (
              <svg
                className="w-2.5 h-2.5 animate-spin"
                viewBox="0 0 24 24"
                fill="none"
                aria-hidden="true"
              >
                <circle
                  cx="12"
                  cy="12"
                  r="10"
                  stroke="currentColor"
                  strokeWidth="4"
                  strokeDasharray="40 60"
                />
              </svg>
            ) : (
              <span aria-hidden="true">●</span>
            )}
            {delivery.label}
          </span>
          {isMcp ? (
            <span className={`${PILL} ${OPS_BADGE[gateway.deployed ? 'ready' : 'unknown']}`}>
              <span aria-hidden="true">●</span>
              {gateway.label}
            </span>
          ) : (
          <span
            className={`${PILL} ${OPS_BADGE[RUNTIME_BADGE_KEY[runtimeKey]]}`}
            title={runtimeStatusTitle(runtime, scope.note) ?? undefined}
          >
            <span aria-hidden="true">●</span>
            {agentRuntimeLabel(runtimeKey, representativeImageTag(repo))}
          </span>
          )}
        </span>
        {deliveryFailure && (
          <span className="block mt-1 text-[11px] text-rose-700">
            {deliveryFailure} ·{' '}
            <button
              type="button"
              onClick={() => setActiveKey('deployments')}
              className="font-medium underline underline-offset-2 hover:text-rose-800"
            >
              See Deployments
            </button>
          </span>
        )}
      </div>

      <div>
        <span className={FIELD_LABEL}>Governance</span>
        {agent ? (
          <span className={`${PILL} ${lifecycleBadge(agent.lifecycle_state).cls}`}>
            {lifecycleBadge(agent.lifecycle_state).label}
          </span>
        ) : mcp ? (
          <span className={`${PILL} ${lifecycleBadge(mcp.lifecycle_state).cls}`}>
            {lifecycleBadge(mcp.lifecycle_state).label}
          </span>
        ) : (
          <span className={`${PILL} ${OPS_BADGE.unknown}`}>Not reported</span>
        )}
      </div>
    </div>
  );
}

export function GatewayCard({
  repo,
  mcp,
  deployed,
  label,
}: {
  repo: Repository;
  mcp: McpServer | null;
  deployed: boolean;
  label: string;
}): JSX.Element {
  return (
    <div className={`${OPS_CARD} p-5 mb-6`}>
      <div className="flex items-center justify-between gap-3 mb-4">
        <h3 className="text-sm font-semibold text-slate-800">Gateway</h3>
        <span className={`${PILL} ${OPS_BADGE[deployed ? 'ready' : 'unknown']}`}>
          <span aria-hidden="true">●</span>
          {label}
        </span>
      </div>
      {deployed ? (
        <dl className="grid grid-cols-1 md:grid-cols-2 gap-x-8 gap-y-4">
          <CopyField label="Gateway URL" value={repo.gateway_url} />
          <CopyField label="Gateway ARN" value={repo.gateway_arn} />
          <Field label="Tools">{mcp ? mcp.available_tools.length : NO_VALUE}</Field>
        </dl>
      ) : (
        <p className="text-sm text-slate-500">
          No gateway yet. Build now creates it from this repository’s gateway.yaml.
        </p>
      )}
    </div>
  );
}
