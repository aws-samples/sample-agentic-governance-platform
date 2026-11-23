import { useCallback, useEffect, useRef, useState, type JSX } from 'react';

import {
  projectsApi,
  type RepoDeleteResult,
  type RepoDeleteSelection,
  type Repository,
} from '../../api/client';
import { useLoad } from '../../api/useLoad';
import { ModalShell } from './ModalShell';
import { deleteResultView, outcomeLabel, selectionFromPreview, type DeleteTone } from './deleteRepoResult';
import { destructiveActionMessage } from './projectRoles';
import { DEPLOYMENT_RETENTION_NOTE } from './repo-tabs/deploymentsTabModel';

const ITEMS: { key: keyof RepoDeleteSelection; label: string }[] = [
  { key: 'record', label: 'Internal record (platform tracking + governed agent)' },
  { key: 'github', label: 'GitHub repository' },
  { key: 'image', label: 'Container image(s)' },
  { key: 'runtime', label: 'AgentCore runtime + Terraform state' },
  { key: 'identity', label: 'Entra identity' },
];

const LABEL_BY_KEY: Record<string, string> = {
  ...Object.fromEntries(ITEMS.map((i) => [i.key, i.label])),
  exec_role: 'Runtime exec roles (IAM)',
  langfuse: 'Langfuse project + keys',
};

const MCP_ITEMS: { key: keyof RepoDeleteSelection; label: string }[] = [
  { key: 'record', label: 'Internal record (platform tracking + MCP registry entry)' },
  { key: 'github', label: 'GitHub repository' },
  { key: 'identity', label: 'Entra MCP app' },
];
const MCP_GATEWAY_LABEL = 'AgentCore gateway (Terraform-managed)';
const MCP_LABEL_BY_KEY: Record<string, string> = {
  ...LABEL_BY_KEY,
  ...Object.fromEntries(MCP_ITEMS.map((i) => [i.key, i.label])),
  runtime: MCP_GATEWAY_LABEL,
  policy_engine: 'Cedar policy engine (AgentCore)',
};

const ALL_SELECTED: Required<RepoDeleteSelection> = {
  record: true,
  github: true,
  image: true,
  runtime: true,
  identity: true,
};
const MCP_ALL_SELECTED: Required<RepoDeleteSelection> = { ...ALL_SELECTED, image: false, runtime: false };

const PILL_TINT: Record<DeleteTone, string> = {
  done: 'bg-emerald-50 text-emerald-700',
  kept: 'bg-slate-100 text-slate-500',
  maybeRemains: 'bg-amber-50 text-amber-700',
  attention: 'bg-rose-50 text-rose-700',
};
const SUMMARY_TINT: Record<DeleteTone, string> = {
  done: 'border-emerald-200 bg-emerald-50 text-emerald-800',
  kept: 'border-slate-200 bg-slate-50 text-slate-700',
  maybeRemains: 'border-amber-200 bg-amber-50 text-amber-800',
  attention: 'border-rose-200 bg-rose-50 text-rose-800',
};

function cedarPlan(state: string | undefined, note: string | undefined, identitySelected: boolean): string {
  if (state === 'gone') return note ? `Cedar policy engine: ${note}` : 'No Cedar policy engine on record';
  if (!identitySelected) return 'Cedar policy engine: left in place — the Entra MCP app is not being removed';
  if (state === 'present') return `Cedar policy engine: ${note ?? 'removed with the Entra MCP app'}`;
  return 'Cedar policy engine, if any: removed with the Entra MCP app';
}

const DANGER_BTN =
  'px-3.5 py-1.5 rounded-lg bg-rose-600 text-white text-sm font-medium hover:bg-rose-700 transition-colors disabled:opacity-40';
const EMPTY_BY_KEY: Partial<Record<string, string>> = {};
type PreviewMaps = { stateByKey: Partial<Record<string, string>>; noteByKey: Partial<Record<string, string>> };

export default function DeleteRepositoryModal({
  repo,
  projectId,
  onClose,
  onDeleted,
}: {
  repo: Repository;
  projectId: string;
  onClose: () => void;
  onDeleted: () => void;
}): JSX.Element {
  const isMcp = repo.artifact_kind === 'mcp_server';
  const allSelected = isMcp ? MCP_ALL_SELECTED : ALL_SELECTED;

  const preview = useLoad(
    () =>
      projectsApi.deletePreview(projectId, repo.id).then((p): PreviewMaps => {
        const stateByKey: Partial<Record<string, string>> = {};
        const noteByKey: Partial<Record<string, string>> = {};
        for (const it of p.items) {
          stateByKey[it.item] = it.state;
          if (it.note) noteByKey[it.item] = it.note;
        }
        return { stateByKey, noteByKey };
      }),
    [projectId, repo.id],
  );
  const previewLoading = preview.state.kind === 'loading';
  const previewFailed = preview.state.kind === 'error';
  const stateByKey = preview.state.kind === 'ready' ? preview.state.data.stateByKey : EMPTY_BY_KEY;
  const noteByKey = preview.state.kind === 'ready' ? preview.state.data.noteByKey : EMPTY_BY_KEY;

  const [toggled, setToggled] = useState<{ against: PreviewMaps | null; selection: Required<RepoDeleteSelection> } | null>(null);
  const previewData = preview.state.data;
  const userSelection = toggled !== null && toggled.against === previewData ? toggled.selection : null;
  const selection =
    userSelection ?? (preview.state.kind === 'ready' ? selectionFromPreview(stateByKey, isMcp) : allSelected);

  const [result, setResult] = useState<RepoDeleteResult | null>(null);
  const [submitted, setSubmitted] = useState<Required<RepoDeleteSelection>>(ALL_SELECTED);
  const [actionPending, setActionPending] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);

  const mountedRef = useRef(true);
  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
    };
  }, []);

  const items = isMcp ? MCP_ITEMS : ITEMS;
  const labelByKey = isMcp ? MCP_LABEL_BY_KEY : LABEL_BY_KEY;
  const anySelected = items.some((i) => selection[i.key]);
  const canSubmit = anySelected && !actionPending && !previewLoading;

  const toggle = (key: keyof RepoDeleteSelection) =>
    setToggled({ against: previewData, selection: { ...selection, [key]: !selection[key] } });

  const handleSubmit = useCallback(async () => {
    if (!anySelected || actionPending || previewLoading) return;
    setActionPending(true);
    setActionError(null);
    setResult(null);
    try {
      const res = await projectsApi.deleteRepo(projectId, repo.id, selection);
      if (!mountedRef.current) return;
      setSubmitted(selection);
      setResult(res);
    } catch (err: unknown) {
      if (mountedRef.current) {
        setActionError(
          destructiveActionMessage(err, 'repository', 'Failed to delete the repository.')
        );
      }
    } finally {
      if (mountedRef.current) setActionPending(false);
    }
  }, [anySelected, actionPending, previewLoading, projectId, repo.id, selection]);

  const view = result ? deleteResultView(result, submitted) : null;

  const handleDone = useCallback(() => {
    if (view?.navigateAway) onDeleted();
    else onClose();
  }, [view?.navigateAway, onDeleted, onClose]);

  const showForm = view === null || view.canRetry;
  const submitLabel = actionPending ? 'Deleting…' : view?.canRetry ? 'Retry' : 'Delete repository';

  return (
    <ModalShell
      title={`Delete ${repo.name}`}
      description="Tear down the selected artifacts for this repository. Unselected items are left in place."
      ariaLabel="Delete repository"
      actionPending={actionPending}
      onClose={result ? handleDone : onClose}
      footer={
        <>
          <button
            type="button"
            onClick={result ? handleDone : onClose}
            disabled={actionPending}
            className="px-3.5 py-1.5 rounded-lg bg-white border border-slate-300 text-slate-700 text-sm font-medium hover:bg-slate-50 transition-colors disabled:opacity-40"
          >
            {result ? 'Done' : 'Cancel'}
          </button>
          {showForm && (
            <button
              type="button"
              onClick={handleSubmit}
              disabled={!canSubmit}
              className={DANGER_BTN}
            >
              {submitLabel}
            </button>
          )}
        </>
      }
    >
      {showForm && (
        <>
          <p className="text-sm text-slate-600">
            This permanently removes the selected artifacts. This cannot be undone.
          </p>

          <p className="text-[11px] text-slate-500">
            {DEPLOYMENT_RETENTION_NOTE}
          </p>

          {previewLoading && (
            <p className="text-sm text-slate-500" role="alert">
              Checking what still exists…
            </p>
          )}

          {!previewLoading && (
            <div className="space-y-2.5">
              {items.map(({ key, label }) => {
                const gone = stateByKey[key] === 'gone';
                return (
                  <label
                    key={key}
                    className={`flex items-start gap-2.5 text-sm select-none ${
                      gone ? 'text-slate-400 cursor-not-allowed' : 'text-slate-700 cursor-pointer'
                    }`}
                  >
                    <input
                      type="checkbox"
                      checked={!gone && selection[key]}
                      onChange={() => toggle(key)}
                      disabled={actionPending || gone}
                      className="mt-0.5 h-4 w-4 rounded border-slate-300 text-emerald-600 focus:ring-2 focus:ring-emerald-500/40 disabled:opacity-40"
                    />
                    <span className="flex flex-col">
                      <span>{label}</span>
                      {gone && (
                        <span className="text-[11px] text-slate-400">already deleted / not found</span>
                      )}
                      {isMcp && key === 'identity' && (
                        <span className="text-[11px] text-slate-500">{cedarPlan(stateByKey.policy_engine, noteByKey.policy_engine, !gone && selection.identity)}</span>
                      )}
                    </span>
                  </label>
                );
              })}
            </div>
          )}

          {isMcp && !previewLoading && (
            <div className="rounded-lg border border-slate-200 bg-slate-50 px-3 py-2">
              <p className="text-sm text-slate-700">{MCP_GATEWAY_LABEL}</p>
              <p
                className={`text-[11px] mt-0.5 ${
                  stateByKey.runtime === 'gone' ? 'text-slate-500' : 'text-amber-700'
                }`}
              >
                {noteByKey.runtime ?? 'Not removed by AGP — Terraform-managed. Delete it by hand.'}
              </p>
            </div>
          )}

          {!previewLoading && previewFailed && (
            <p className="text-[11px] text-amber-600" role="alert">
              Couldn't check current state; all items selected.
            </p>
          )}
        </>
      )}

      {view && (
        <div className="space-y-2" aria-live="polite">
          <p
            className={`rounded-lg border px-3 py-2 text-sm font-medium ${SUMMARY_TINT[view.summary.tone]}`}
            role={view.summary.tone === 'attention' || view.summary.tone === 'maybeRemains' ? 'alert' : 'status'}
          >
            {view.summary.text}
          </p>
          <ul className="divide-y divide-slate-100">
            {view.rows.map((it, i) => (
              <li key={`${it.item}-${i}`} className="flex items-start justify-between gap-3 py-1.5 text-sm">
                <span className="text-slate-700">{labelByKey[it.item] ?? it.item}</span>
                <span className="flex flex-col items-end text-right">
                  <span
                    className={`inline-flex items-center px-2 py-0.5 rounded-full text-[11px] font-semibold ${PILL_TINT[it.tone]}`}
                  >
                    {outcomeLabel(it.outcome)}
                  </span>
                  {it.reason && (
                    <span className={`text-[11px] mt-0.5 ${it.tone === 'maybeRemains' ? 'text-amber-700' : 'text-slate-500'}`}>
                      {it.reason}
                    </span>
                  )}
                </span>
              </li>
            ))}
          </ul>
        </div>
      )}

      {actionError && (
        <p className="text-sm text-red-600" role="alert">
          {actionError}
        </p>
      )}
    </ModalShell>
  );
}
