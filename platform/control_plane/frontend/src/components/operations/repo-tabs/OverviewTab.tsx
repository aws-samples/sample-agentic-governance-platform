import type { JSX } from 'react';

import type { Agent, McpServer, Repository } from '../../../api/client';
import { nextBadgeFromSteps, stepStatusText } from '../materializeSteps';
import { NO_VALUE } from '../opsLabels';
import { OPS_CARD } from '../opsUi';
import { ARTIFACT_TONE_CLS } from '../promoteConfirmModel';
import { Field } from '../RepoFields';
import { materializeSummary, promotionArtifact, recordStatusLabel, repoKindOf } from '../repositoryDetailTabs';
import { shortBuildTag } from './evaluationsLogic.ts';

export default function OverviewTab({
  repo,
  agent,
  mcp,
}: {
  repo: Repository;
  agent: Agent | null;
  mcp: McpServer | null;
}): JSX.Element {
  const isMcp = repoKindOf(repo) === 'mcp_server';
  const gov = isMcp ? mcp : agent;
  const materialize = materializeSummary(nextBadgeFromSteps(repo.steps), repo.steps.length);
  const artifact = promotionArtifact(repo);

  return (
    <div className="space-y-6">
      <div className={`${OPS_CARD} p-5`}>
        <h3 className="text-sm font-semibold text-slate-800 mb-4">Details</h3>
        <dl className="grid grid-cols-2 md:grid-cols-3 gap-x-8 gap-y-4">
          <Field label="Template">{repo.template_name}</Field>
          <Field label="Record status">{recordStatusLabel(repo.status) ?? NO_VALUE}</Field>
          <Field label="Created by">{repo.created_by || NO_VALUE}</Field>
          <Field label="Created">{repo.created_at.slice(0, 10)}</Field>
          <Field label={isMcp ? 'Description' : 'Purpose'}>
            {(isMcp ? mcp?.description : agent?.purpose) || NO_VALUE}
          </Field>
          {!isMcp && <Field label="Framework">{agent?.framework || NO_VALUE}</Field>}
          <Field label="Region">{gov?.region || NO_VALUE}</Field>
          <Field label="Business unit">{gov?.business_unit || NO_VALUE}</Field>
          <Field label="Data classification">{gov?.data_classification || NO_VALUE}</Field>
          <Field label="Source repository">
            {repo.repo_url ? (
              <a
                href={repo.repo_url}
                target="_blank"
                rel="noreferrer"
                className="text-emerald-700 hover:text-emerald-800 transition-colors break-all"
              >
                {repo.repo_url}
              </a>
            ) : (
              NO_VALUE
            )}
          </Field>
          {!isMcp && (
          <Field label="Endpoint">
            <span className="font-mono text-xs break-all">{agent?.endpoint_url || NO_VALUE}</span>
          </Field>
          )}
          {isMcp && <Field label="Tools">{mcp ? mcp.available_tools.length : NO_VALUE}</Field>}
          {!isMcp && (
          <Field label="Prod candidate">
            {repo.prod_candidate_at ? repo.prod_candidate_at.slice(0, 10) : NO_VALUE}
          </Field>
          )}
          {artifact.marker !== null && (
            <Field label="Approvable artifact">
              <span className="flex flex-col gap-1">
                <span
                  className={ARTIFACT_TONE_CLS[artifact.marker.tone]}
                  title={artifact.marker.note ?? undefined}
                >
                  {artifact.marker.text}
                </span>
                {artifact.imageTag !== null && (
                  <span className="font-mono text-[11px] text-slate-400 break-all" title={artifact.imageTag}>
                    {shortBuildTag(artifact.imageTag)}
                  </span>
                )}
              </span>
            </Field>
          )}
        </dl>
      </div>

      <div className={`${OPS_CARD} p-5`}>
        <div className="flex items-center justify-between gap-3 mb-4">
          <h3 className="text-sm font-semibold text-slate-800">Materialization run</h3>
          <span
            className={`text-[11px] ${
              materialize.succeeded ? 'text-emerald-700 font-medium' : 'text-slate-400'
            }`}
          >
            {materialize.label}
          </span>
        </div>
        <ol className="relative">
          {repo.steps.map((s) => (
            <li key={s.key} className="relative flex gap-3 pb-3 last:pb-0">
              <span
                aria-hidden="true"
                className={`mt-1.5 shrink-0 h-2 w-2 rounded-full ${
                  s.status === 'done'
                    ? 'bg-emerald-500'
                    : s.status === 'failed'
                      ? 'bg-rose-500'
                      : s.status === 'running'
                        ? 'bg-amber-500'
                        : 'bg-slate-300'
                }`}
              />
              <div className="min-w-0 flex-1">
                <div className="flex items-center justify-between gap-2">
                  <span
                    className={`text-sm truncate ${
                      s.status === 'failed'
                        ? 'text-rose-700 font-medium'
                        : s.status === 'done'
                          ? 'text-slate-700'
                          : 'text-slate-400'
                    }`}
                  >
                    {s.label}
                  </span>
                  <span className="shrink-0 text-[11px] capitalize text-slate-400">
                    {stepStatusText(s.status)}
                  </span>
                </div>
                {s.status === 'failed' && s.error && (
                  <p className="mt-1 rounded-md bg-rose-50 border border-rose-200/70 px-2 py-1 text-[11px] text-rose-700 break-words">
                    {s.error}
                  </p>
                )}
              </div>
            </li>
          ))}
        </ol>
      </div>
    </div>
  );
}
