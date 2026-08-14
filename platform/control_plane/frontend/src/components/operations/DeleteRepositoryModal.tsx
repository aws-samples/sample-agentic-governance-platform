// DeleteRepositoryModal — the repo teardown checklist (Epic 23 / Tasks 9, 12).
// Opened from the ProjectDetail repo table's per-row Delete. On open it runs a
// reachability PRE-CHECK (projectsApi.deletePreview) that probes what still exists,
// then renders the 5-item checklist (record, github, image, runtime, identity)
// GATED by that preview:
//   • state "present" | "unknown" → normal checkbox, CHECKED by default (unknown =
//     assume present, we still attempt teardown).
//   • state "gone"                → row DISABLED + unchecked + labelled "already
//     deleted / not found"; its selection value sends false.
// If the preview CALL itself fails we FALL BACK to the current all-checked behavior
// (never block a delete on a probe failure) + a small inline note.
//
// The Delete button calls projectsApi.deleteRepo(projectId, repo.id, selection). The
// backend tears down each selected artifact independently and returns a per-item
// outcome (deleted / failed / skipped) + record_removed; that result renders INSIDE
// the modal. On record_removed the parent refetches + closes (onDeleted); otherwise
// the modal stays open showing which items failed so the operator can re-run.
//
// House style: the ConnectionsAdmin ModalShell + opsUi tokens, the actionPending
// single-flight + mountedRef guard + inline <p role="alert"> idiom. The Delete
// action is rose-danger (opsUi's failed tint) — the only destructive button on
// the Ops surface.

import { useCallback, useEffect, useRef, useState, type JSX } from 'react';

import {
  projectsApi,
  type RepoDeleteResult,
  type RepoDeleteSelection,
  type Repository,
} from '../../api/client';
import { ModalShell } from './ConnectionsAdmin';
import { destructiveActionMessage } from './projectRoles';
// The retention statement (E28C/T7, D-C5), from the module that also owns the `repo deleted` marker
// the retained rows render. ONE source for both halves of one policy: this screen promises the
// history survives, and the deployments tab is where that promise is kept — two separately-worded
// versions is how a product comes to promise one thing and show another.
import { DEPLOYMENT_RETENTION_NOTE } from './repo-tabs/deploymentsTab';

// Checklist order + human labels. Keys mirror RepoDeleteSelection exactly.
const ITEMS: { key: keyof RepoDeleteSelection; label: string }[] = [
  { key: 'record', label: 'Internal record (platform tracking + governed agent)' },
  { key: 'github', label: 'GitHub repository' },
  { key: 'image', label: 'Container image(s)' },
  { key: 'runtime', label: 'AgentCore runtime + Terraform state' },
  { key: 'identity', label: 'Entra identity' },
];

/**
 * Human labels for every RESULT line-item, which is a SUPERSET of the checklist above.
 *
 * The cascade reports items an operator cannot individually select, because they RIDE another
 * selection: `langfuse` (E26/T7) rides `identity`, and `exec_role` (E28C/T5) rides `runtime` — an
 * operator keeping the runtime keeps the IAM role it needs to pull images and write logs. Both are
 * reported as their own non-blocking line-items precisely so a surviving resource is never silently
 * absent from the result, which is what made the exec-role leak invisible for six roles: the reclaim
 * always answered AccessDenied and the cascade always said "deleted".
 *
 * A KEY WITH NO LABEL HERE FALLS BACK TO THE RAW KEY (`LABEL_BY_KEY[it.item] ?? it.item`), so a new
 * backend item degrades to `exec_role` rather than to a blank row. That is honest but unreadable, and
 * an unreadable line on a teardown report is a line an operator skips — which is the failure mode
 * this whole item was added to fix.
 */
const LABEL_BY_KEY: Record<string, string> = {
  ...Object.fromEntries(ITEMS.map((i) => [i.key, i.label])),
  // Named in the checklist's idiom: the resource, then its system in parentheses. Plural because
  // the reclaim covers one role PER STAGE plus the legacy single-runtime name.
  exec_role: 'Runtime exec roles (IAM)',
  langfuse: 'Langfuse project + keys',
};

const ALL_SELECTED: RepoDeleteSelection = {
  record: true,
  github: true,
  image: true,
  runtime: true,
  identity: true,
};

// Per-item outcome tint — deleted = emerald (done), skipped = slate (no-op),
// failed = rose (attention). Mirrors the opsUi badge semantics.
function outcomeTint(outcome: string): string {
  const o = outcome.toLowerCase();
  if (o === 'deleted') return 'bg-emerald-50 text-emerald-700';
  if (o === 'failed' || o === 'error') return 'bg-rose-50 text-rose-700';
  return 'bg-slate-100 text-slate-500';
}

const DANGER_BTN =
  'px-3.5 py-1.5 rounded-lg bg-rose-600 text-white text-sm font-medium hover:bg-rose-700 transition-colors disabled:opacity-40';

export default function DeleteRepositoryModal({
  open,
  repo,
  projectId,
  onClose,
  onDeleted,
}: {
  open: boolean;
  repo: Repository;
  projectId: string;
  onClose: () => void;
  onDeleted: () => void;
}): JSX.Element | null {
  const [selection, setSelection] = useState<RepoDeleteSelection>(ALL_SELECTED);
  // Per-key reachability from the pre-check. "gone" keys are disabled + unchecked;
  // anything else (present / unknown / not reported) is selectable. Empty = the probe
  // hasn't resolved (or fell back), in which case every row is treated as selectable.
  const [stateByKey, setStateByKey] = useState<Partial<Record<string, string>>>({});
  const [previewLoading, setPreviewLoading] = useState(true);
  const [previewFailed, setPreviewFailed] = useState(false);

  const [result, setResult] = useState<RepoDeleteResult | null>(null);
  const [actionPending, setActionPending] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);

  // Unmount guard — deletePreview/deleteRepo resolve async and can land after
  // onDeleted closes the modal (record_removed path).
  const mountedRef = useRef(true);
  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
    };
  }, []);

  // Pre-check: on open, probe what still exists and seed the checklist from it.
  // present/unknown (or not reported) → checked + enabled; gone → unchecked + disabled.
  // A probe failure falls back to the all-checked behavior (don't block the delete).
  useEffect(() => {
    if (!open) return;
    let cancelled = false;
    setPreviewLoading(true);
    setPreviewFailed(false);
    setStateByKey({});
    setSelection(ALL_SELECTED);
    setResult(null);
    setActionError(null);
    (async () => {
      try {
        const preview = await projectsApi.deletePreview(projectId, repo.id);
        if (cancelled || !mountedRef.current) return;
        const map: Partial<Record<string, string>> = {};
        for (const it of preview.items) map[it.item] = it.state;
        setStateByKey(map);
        setSelection({
          record: map.record !== 'gone',
          github: map.github !== 'gone',
          image: map.image !== 'gone',
          runtime: map.runtime !== 'gone',
          identity: map.identity !== 'gone',
        });
      } catch {
        // Fall back to the current all-checked behavior — a preview failure must
        // never block a deletion.
        if (cancelled || !mountedRef.current) return;
        setPreviewFailed(true);
        setStateByKey({});
        setSelection(ALL_SELECTED);
      } finally {
        if (!cancelled && mountedRef.current) setPreviewLoading(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [open, projectId, repo.id]);

  const anySelected = ITEMS.some((i) => selection[i.key]);
  const canSubmit = anySelected && !actionPending && !previewLoading;

  const toggle = useCallback((key: keyof RepoDeleteSelection) => {
    setSelection((s) => ({ ...s, [key]: !s[key] }));
  }, []);

  const handleSubmit = useCallback(async () => {
    if (!anySelected || actionPending || previewLoading) return;
    setActionPending(true);
    setActionError(null);
    // Clear any prior result so a Retry doesn't briefly show stale per-item pills.
    setResult(null);
    try {
      const res = await projectsApi.deleteRepo(projectId, repo.id, selection);
      if (!mountedRef.current) return;
      setResult(res);
      // Failure is per-item, NOT record_removed: unchecking the record item is a
      // supported partial teardown (keep the tracking row), so record_removed can be
      // false by design with nothing failed. Only a real item failure keeps the modal
      // open for a re-run; otherwise the teardown did what the user asked → close.
      const failed = res.items.some((i) => i.outcome === 'failed');
      if (!failed) onDeleted();
    } catch (err: unknown) {
      if (mountedRef.current) {
        // The route is OWNER-gated (E27/T4). The button is role-gated upstream, so a 403
        // here means the caller's role changed since the page loaded — map it to a sentence
        // rather than surfacing the raw `insufficient project role` fragment.
        setActionError(
          destructiveActionMessage(
            err instanceof Error ? err.message : '',
            'repository',
            'Failed to delete the repository.',
          )
        );
      }
    } finally {
      if (mountedRef.current) setActionPending(false);
    }
  }, [anySelected, actionPending, previewLoading, projectId, repo.id, selection, onDeleted]);

  if (!open) return null;

  // Failure keys off per-item outcomes, not record_removed (which can be false by
  // design when the operator keeps the tracking row). After a real failure the record
  // survives; re-running with the same selection is safe (torn-down items report
  // skipped), so keep the button live as a Retry.
  const hadFailure = result !== null && result.items.some((i) => i.outcome === 'failed');
  const submitLabel = actionPending ? 'Deleting…' : hadFailure ? 'Retry' : 'Delete repository';

  return (
    <ModalShell
      title={`Delete ${repo.name}`}
      description="Tear down the selected artifacts for this repository. Unselected items are left in place."
      ariaLabel="Delete repository"
      actionPending={actionPending}
      onClose={onClose}
      footer={
        <>
          <button
            type="button"
            onClick={onClose}
            disabled={actionPending}
            className="px-3.5 py-1.5 rounded-lg bg-white border border-slate-300 text-slate-700 text-sm font-medium hover:bg-slate-50 transition-colors disabled:opacity-40"
          >
            {result && result.record_removed ? 'Done' : 'Cancel'}
          </button>
          <button
            type="button"
            onClick={handleSubmit}
            disabled={!canSubmit}
            className={DANGER_BTN}
          >
            {submitLabel}
          </button>
        </>
      }
    >
      <p className="text-sm text-slate-600">
        This permanently removes the selected artifacts. This cannot be undone.
      </p>

      {/* WHAT IS *NOT* TORN DOWN, stated up front (E28C/T7, D-C5).
          The `deployment` partition is APPEND-ONLY and is deliberately absent from the cascade —
          confirmed twice on live data, where ten rows survived a full teardown. Retention is the
          right decision: the record of what reached production is exactly what should outlive the
          repository that produced it. What was wrong is that nothing said so, on the one screen
          where an operator is being told what "permanently removes" covers. A checklist that
          enumerates five artifacts and stays silent about a sixth thing implies the sixth is gone
          too, so this states it rather than leaving the absence to be inferred.
          Deliberately NOT a checklist row: an unselectable checkbox would imply the choice exists. */}
      <p className="text-[11px] text-slate-500">
        {DEPLOYMENT_RETENTION_NOTE}
      </p>

      {/* Pre-check in flight — checklist is seeded from what still exists. */}
      {previewLoading && (
        <p className="text-sm text-slate-500" role="alert">
          Checking what still exists…
        </p>
      )}

      {/* Teardown checklist — seeded from the pre-check. present/unknown are checked;
          gone rows are disabled + shown as already-deleted. */}
      {!previewLoading && (
        <div className="space-y-2.5">
          {ITEMS.map(({ key, label }) => {
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
                </span>
              </label>
            );
          })}
        </div>
      )}

      {/* Pre-check couldn't run — we defaulted to all items selected. */}
      {!previewLoading && previewFailed && (
        <p className="text-[11px] text-amber-600" role="alert">
          Couldn't check current state; all items selected.
        </p>
      )}

      {/* Per-item outcome — rendered after the delete call returns. */}
      {result && (
        <div className="space-y-2">
          <span className="block text-[11px] uppercase tracking-wide text-slate-400 font-medium">
            Result
          </span>
          <ul className="space-y-1.5">
            {result.items.map((it, i) => (
              <li key={`${it.item}-${i}`} className="flex items-start justify-between gap-3 text-sm">
                <span className="text-slate-700">{LABEL_BY_KEY[it.item] ?? it.item}</span>
                <span className="flex flex-col items-end text-right">
                  <span
                    className={`inline-flex items-center px-2 py-0.5 rounded-full text-[11px] font-semibold capitalize ${outcomeTint(it.outcome)}`}
                  >
                    {it.outcome}
                  </span>
                  {it.reason && <span className="text-[11px] text-slate-400 mt-0.5">{it.reason}</span>}
                </span>
              </li>
            ))}
          </ul>
          {hadFailure && (
            <p className="text-[11px] text-rose-600" role="alert">
              Some steps failed — the repository was kept. Fix the underlying cause and re-run.
            </p>
          )}
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
