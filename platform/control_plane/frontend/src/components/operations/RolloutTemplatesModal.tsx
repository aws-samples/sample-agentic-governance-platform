import { useCallback, useEffect, useMemo, useRef, useState } from 'react';

import {
  githubTemplatesApi,
  infraTemplatesApi,
  rolloutApi,
  type Connection,
  type InfraTemplateView,
  type Provider,
  type RolloutResultResponse,
  type TemplateView,
} from '../../api/client';
import { useLoad } from '../../api/useLoad';
import { ModalShell } from './ModalShell';
import {
  ADOPT_NAME_RULE_HINT,
  RECONCILE_ACTION_LABEL,
  SEED_CONSENT_PROMPT,
  adoptConfirm,
  batchConfirmLines,
  batchConfirmTitle,
  batchPlan,
  classifyRolloutError,
  deregisterConfirm,
  infraIntentFor,
  reconcileRows,
  rolloutActionLabel,
  toggleQueuedAction,
  wireFlagNote,
  type ClassifiedErrorView,
  type ReconcileAction,
  type ReconcileRowModel,
  type RolloutErrorView,
  type SingleRepoAction,
} from './reconcileView.ts';
import { classifyTemplateError } from './templatesView.ts';
import {
  BUILD_KIND_CHIP,
  PLATFORM_CHIP,
  buildKindLabel,
  ensuredInfraIds,
  infraRolloutRows,
  queuedTemplateInfra,
  withInfraSeeds,
  overwriteInfraConsent,
  overwriteInfraTargets,
} from './templateChain.ts';

const PROVIDER_LABEL: Record<Provider, string> = {
  github: 'GitHub',
  gitlab: 'GitLab',
};

const SECTION_LABEL = 'text-[11px] uppercase tracking-wide text-slate-400 font-medium';
const PILL = 'inline-flex items-center px-2 py-0.5 rounded-full text-[11px] font-semibold';
const ROW_BTN =
  'px-2 py-0.5 rounded-md bg-white border border-slate-300 text-slate-700 text-[11px] font-medium hover:bg-slate-50 transition-colors disabled:opacity-40';
const SECONDARY_BTN =
  'px-3.5 py-1.5 rounded-lg bg-white border border-slate-300 text-slate-700 text-sm font-medium hover:bg-slate-50 transition-colors disabled:opacity-40';
const FIELD_INPUT =
  'w-full px-3 py-2 text-sm rounded-lg border border-slate-300 focus:outline-none focus:ring-2 focus:ring-emerald-500/40 disabled:opacity-40';

type PendingConfirm = { action: SingleRepoAction; row: ReconcileRowModel } | null;

const RECONCILE_FALLBACK = 'Could not compare this org against AGP’s templates.';
const EMPTY_INFRA_CATALOG: readonly InfraTemplateView[] = [];
const EMPTY_AGENT_CATALOG: readonly TemplateView[] = [];

export default function RolloutTemplatesModal({
  connection,
  onClose,
  seedPrompt = false,
}: {
  connection: Connection;
  onClose: () => void;
  seedPrompt?: boolean;
}) {
  const viewLoad = useLoad(() => rolloutApi.reconcile(connection.id), [connection.id], RECONCILE_FALLBACK);
  const view = viewLoad.state.data;
  const loading = viewLoad.state.kind === 'loading';
  const error: RolloutErrorView | null =
    viewLoad.state.kind === 'error'
      ? classifyRolloutError(viewLoad.state.failure ?? viewLoad.state.message, RECONCILE_FALLBACK)
      : null;

  const infraCatalogLoad = useLoad(
    () => infraTemplatesApi.list(connection.id).catch(() => [] as InfraTemplateView[]),
    [connection.id],
  );
  const infraCatalog = infraCatalogLoad.state.data ?? EMPTY_INFRA_CATALOG;

  const agentCatalogLoad = useLoad(
    () =>
      githubTemplatesApi
        .list(connection.id)
        .then((rows) => ({ rows, ensuredUnknown: false }))
        .catch((err: unknown) => {
          console.warn('[RolloutTemplatesModal] agent template catalog unreadable; infra rows will under-claim what a roll-out ensures:', err);
          return { rows: [] as TemplateView[], ensuredUnknown: true };
        }),
    [connection.id],
  );
  const agentCatalog = agentCatalogLoad.state.data?.rows ?? EMPTY_AGENT_CATALOG;
  const ensuredUnknown = agentCatalogLoad.state.data?.ensuredUnknown ?? false;

  const [queued, setQueued] = useState<ReadonlyMap<string, ReconcileAction>>(new Map());
  const [overwriteInfra, setOverwriteInfra] = useState(false);
  const [overwriteFor, setOverwriteFor] = useState(view);
  if (overwriteFor !== view) {
    setOverwriteFor(view);
    setOverwriteInfra(false);
  }

  const [pending, setPending] = useState<PendingConfirm>(null);
  const [confirmingBatch, setConfirmingBatch] = useState(false);
  const [adoptDescription, setAdoptDescription] = useState('');

  const [actionPending, setActionPending] = useState(false);
  const [actionError, setActionError] = useState<ClassifiedErrorView | null>(null);
  const [result, setResult] = useState<RolloutResultResponse | null>(null);
  const [rowNotice, setRowNotice] = useState<{ name: string; text: string } | null>(null);

  const mountedRef = useRef(true);
  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
    };
  }, []);

  const reloadView = viewLoad.reload;
  const reloadInfraCatalog = infraCatalogLoad.reload;
  const reloadAgentCatalog = agentCatalogLoad.reload;
  const refetch = useCallback(() => {
    reloadView();
    reloadInfraCatalog();
    reloadAgentCatalog();
  }, [reloadView, reloadInfraCatalog, reloadAgentCatalog]);

  const rows = useMemo(() => (view ? reconcileRows(view.templates) : []), [view]);
  const infraRow = useMemo(
    () => (view ? reconcileRows([view.infra_repo])[0] : null),
    [view],
  );
  const summary = useMemo(() => reconcileRows.summarize(rows), [rows]);

  const toggleQueued = useCallback((row: ReconcileRowModel) => {
    setQueued((prev) => toggleQueuedAction(prev, row));
  }, []);

  const infraIntent = infraIntentFor(view?.infra_repo, overwriteInfra);
  const plan = useMemo(() => batchPlan(rows, queued, infraIntent), [rows, queued, infraIntent]);
  const canSubmit = !plan.empty || infraIntent !== 'none';

  const infraRows = useMemo(
    () =>
      infraRolloutRows(
        withInfraSeeds(infraCatalog, view?.infra_seeds ?? []),
        view?.infra_repo ?? null,
        ensuredInfraIds(agentCatalog, queuedTemplateInfra(view?.templates ?? [], plan.names)),
      ),
    [infraCatalog, view, agentCatalog, plan.names],
  );
  const overwriteTargets = useMemo(() => overwriteInfraTargets(infraRows), [infraRows]);

  const runRollout = useCallback(async () => {
    if (actionPending) return;
    setActionPending(true);
    setActionError(null);
    setRowNotice(null);
    try {
      const res = await rolloutApi.rollout(connection.id, {
        template_names: plan.names,
        overwrite: plan.overwrite,
        overwrite_infra: plan.overwriteInfra,
      });
      if (!mountedRef.current) return;
      setResult(res);
      setQueued(new Map());
      setOverwriteInfra(false);
      setPending(null);
    } catch (err: unknown) {
      if (mountedRef.current) {
        setActionError(classifyRolloutError(err, 'The rollout could not be completed.'));
        setPending(null);
      }
    } finally {
      if (mountedRef.current) setActionPending(false);
    }
  }, [actionPending, connection.id, plan]);

  const runAdopt = useCallback(
    async (row: ReconcileRowModel, description: string) => {
      if (actionPending) return;
      setActionPending(true);
      setActionError(null);
      setRowNotice(null);
      try {
        await rolloutApi.adopt(connection.id, {
          repo_name: row.item.name,
          ...(description.trim() ? { description: description.trim() } : {}),
        });
        if (!mountedRef.current) return;
        setRowNotice({
          name: row.item.name,
          text: `${rolloutActionLabel('adopted').label} — registered as a template, as-is.`,
        });
        setPending(null);
        setAdoptDescription('');
        refetch();
      } catch (err: unknown) {
        if (mountedRef.current) {
          setActionError(classifyRolloutError(err, 'That repository could not be adopted.'));
          setPending(null);
        }
      } finally {
        if (mountedRef.current) setActionPending(false);
      }
    },
    [actionPending, connection.id, refetch],
  );

  const runDeregister = useCallback(
    async (row: ReconcileRowModel) => {
      if (actionPending) return;
      setActionPending(true);
      setActionError(null);
      setRowNotice(null);
      try {
        await githubTemplatesApi.remove(connection.id, row.item.name);
        if (!mountedRef.current) return;
        setRowNotice({ name: row.item.name, text: 'Deregistered — the record is gone; no repository was touched.' });
        setPending(null);
        refetch();
      } catch (err: unknown) {
        if (mountedRef.current) {
          setActionError(classifyTemplateError(err, 'That template could not be deregistered.'));
          setPending(null);
        }
      } finally {
        if (mountedRef.current) setActionPending(false);
      }
    },
    [actionPending, connection.id, refetch],
  );

  const askConfirm = useCallback((action: SingleRepoAction, row: ReconcileRowModel) => {
    setActionError(null);
    setRowNotice(null);
    setAdoptDescription('');
    setPending({ action, row });
  }, []);

  const orgName = connection.org;
  const title = `Roll out templates — ${PROVIDER_LABEL[connection.provider]} · ${orgName}`;

  if (result) {
    return (
      <ModalShell
        title={title}
        description="Every repository’s outcome, in the platform’s own words."
        ariaLabel="Roll out templates — result"
        actionPending={false}
        onClose={onClose}
        footer={
          <button type="button" onClick={onClose} className="px-3.5 py-1.5 rounded-lg bg-emerald-600 text-white text-sm font-medium hover:bg-emerald-700 transition-colors">
            Done
          </button>
        }
      >
        {result.items.length === 0 ? (
          <p className="text-sm text-slate-400">Nothing was rolled out.</p>
        ) : (
          <ul className="space-y-1">
            {result.items.map((item) => {
              const style = rolloutActionLabel(item.action);
              return (
                <li key={item.name} className="flex items-start justify-between gap-3 px-2 py-1.5 rounded-lg bg-emerald-50/30">
                  <div className="min-w-0">
                    <span className="text-sm text-slate-700 font-medium truncate block" title={item.name}>
                      {item.name}
                    </span>
                    {item.reason && <span className="text-[11px] text-slate-500">{item.reason}</span>}
                  </div>
                  <span className={`shrink-0 ${PILL} ${style.cls}`}>{style.label}</span>
                </li>
              );
            })}
          </ul>
        )}
      </ModalShell>
    );
  }

  if (pending) {
    const { action, row } = pending;
    const text = action === 'adopt' ? adoptConfirm(row.item) : deregisterConfirm(row.item.name);
    const confirmLabel = RECONCILE_ACTION_LABEL[action];
    const onConfirm = () => {
      if (action === 'adopt') return void runAdopt(row, adoptDescription);
      return void runDeregister(row);
    };

    return (
      <ModalShell
        title={`${confirmLabel} — ${row.item.name}`}
        description={`${PROVIDER_LABEL[connection.provider]} · ${orgName}`}
        ariaLabel={`${confirmLabel} ${row.item.name}`}
        actionPending={actionPending}
        onClose={onClose}
        footer={
          <>
            <button type="button" onClick={() => setPending(null)} disabled={actionPending} className={SECONDARY_BTN}>
              Back
            </button>
            <button
              type="button"
              onClick={onConfirm}
              disabled={actionPending}
              className={
                action === 'deregister'
                  ? 'px-3.5 py-1.5 rounded-lg bg-rose-600 text-white text-sm font-medium hover:bg-rose-700 transition-colors disabled:opacity-40'
                  : 'px-3.5 py-1.5 rounded-lg bg-emerald-600 text-white text-sm font-medium hover:bg-emerald-700 transition-colors disabled:opacity-40'
              }
            >
              {actionPending ? 'Working…' : confirmLabel}
            </button>
          </>
        }
      >
        <p className="text-sm text-slate-600">{text}</p>

        {action === 'adopt' && (
          <div className="space-y-1.5">
            <label htmlFor="adopt-description" className={`block ${SECTION_LABEL}`}>
              Description (optional)
            </label>
            <input
              id="adopt-description"
              type="text"
              value={adoptDescription}
              onChange={(e) => setAdoptDescription(e.target.value)}
              disabled={actionPending}
              placeholder="What this template is for"
              className={FIELD_INPUT}
              autoComplete="off"
            />
            <p className="text-[11px] text-slate-400">
              Framework, AWS services and tags are editable on the template afterwards.
            </p>
          </div>
        )}

        {actionError && (
          <p className={`text-sm ${actionError.retryable ? 'text-amber-800' : 'text-rose-600'}`} role="alert">
            {actionError.message}
          </p>
        )}
      </ModalShell>
    );
  }

  if (confirmingBatch) {
    const lines = batchConfirmLines(rows, queued, infraIntent);
    const confirmTitle = batchConfirmTitle(plan, infraIntent, orgName);
    return (
      <ModalShell
        title={confirmTitle}
        description="Exactly what this writes. Nothing has happened yet."
        ariaLabel={`Confirm: ${confirmTitle}`}
        actionPending={actionPending}
        onClose={onClose}
        footer={
          <>
            <button
              type="button"
              onClick={() => setConfirmingBatch(false)}
              disabled={actionPending}
              className={SECONDARY_BTN}
            >
              Back
            </button>
            <button
              type="button"
              onClick={() => void runRollout()}
              disabled={actionPending}
              className="px-3.5 py-1.5 rounded-lg bg-emerald-600 text-white text-sm font-medium hover:bg-emerald-700 transition-colors disabled:opacity-40"
            >
              {actionPending ? 'Rolling out…' : 'Roll out'}
            </button>
          </>
        }
      >
        <ul className="space-y-2">
          {lines.map((line) => (
            <li key={line} className="text-sm text-slate-600 flex gap-2">
              <span className="text-slate-300 shrink-0" aria-hidden="true">
                •
              </span>
              <span>{line}</span>
            </li>
          ))}
        </ul>

        {wireFlagNote(plan) && <p className="text-[11px] text-amber-800">{wireFlagNote(plan)}</p>}

        {actionError && (
          <p className={`text-sm ${actionError.retryable ? 'text-amber-800' : 'text-rose-600'}`} role="alert">
            {actionError.message}
          </p>
        )}
      </ModalShell>
    );
  }

  return (
    <ModalShell
      title={title}
      description="What AGP has registered, compared against what is actually in the org. Nothing is written until you confirm an action."
      ariaLabel="Roll out templates"
      actionPending={actionPending}
      onClose={onClose}
      footer={
        <>
          <button type="button" onClick={onClose} disabled={actionPending} className={SECONDARY_BTN}>
            Close
          </button>
          <button
            type="button"
            onClick={() => setConfirmingBatch(true)}
            disabled={actionPending || loading || !!error || !canSubmit}
            className="px-3.5 py-1.5 rounded-lg bg-emerald-600 text-white text-sm font-medium hover:bg-emerald-700 transition-colors disabled:opacity-40"
          >
            {`Review roll-out${plan.names.length ? ` (${plan.names.length})` : ''}`}
          </button>
        </>
      }
    >
      {seedPrompt && (
        <div className="rounded-lg border border-emerald-200/70 bg-emerald-50/50 p-3">
          <p className="text-sm text-slate-700 font-medium">{SEED_CONSENT_PROMPT(orgName)}</p>
          <p className="text-[11px] text-slate-500 mt-1">
            Everything below is a comparison, not a queue. Select what to create, or adopt a
            repository AGP found.
          </p>
        </div>
      )}

      {loading ? (
        <p className="text-sm text-slate-400">Comparing this org against AGP’s registry…</p>
      ) : error ? (
        <div
          className={`rounded-lg border p-3 ${error.retryable ? 'border-amber-200/70 bg-amber-50/40' : 'border-rose-200/70 bg-rose-50/40'}`}
        >
          <h3 className={`text-sm font-semibold ${error.retryable ? 'text-amber-800' : 'text-rose-700'}`}>
            {error.retryable ? 'Couldn’t reach the provider' : 'Couldn’t reconcile this org'}
          </h3>
          <p className="text-sm text-slate-600 mt-1">{error.message}</p>
          {error.retryable && (
            <button type="button" onClick={refetch} className={`mt-2 ${ROW_BTN}`}>
              Retry
            </button>
          )}
        </div>
      ) : view ? (
        <>
          <div className="flex items-center justify-between gap-3">
            <span className="text-[11px] text-slate-500 tabular-nums">
              {summary.createCount} to create · {summary.adoptCount} found unregistered ·{' '}
              {summary.inSyncCount} in sync
              {summary.missingCount > 0 && ` · ${summary.missingCount} missing`}
            </span>
            <button
              type="button"
              onClick={refetch}
              disabled={actionPending}
              title="Re-read the org from the provider"
              className={ROW_BTN}
            >
              Refresh
            </button>
          </div>

          {rowNotice && (
            <p className="text-[11px] text-emerald-700" role="status">
              <span className="font-medium">{rowNotice.name}</span> — {rowNotice.text}
            </p>
          )}

          {plan.dropped.length > 0 && (
            <p className="text-[11px] text-amber-800" role="status">
              {plan.dropped.length === 1
                ? '1 selection was cleared'
                : `${plan.dropped.length} selections were cleared`}{' '}
              — the org changed under {plan.dropped.length === 1 ? 'it' : 'them'}:{' '}
              <span className="font-medium">{plan.dropped.join(', ')}</span>. Re-select what you still
              want, against the action each row offers now.
            </p>
          )}

          <div className="space-y-2">
            <span className={SECTION_LABEL}>Templates</span>
            {rows.length === 0 ? (
              <p className="text-sm text-slate-400">
                AGP ships no starter templates and this org has no unregistered repositories.
              </p>
            ) : (
              <ul className="divide-y divide-emerald-100/70">
                {rows.map((row) => (
                  <ReconcileRow
                    key={row.item.name}
                    row={row}
                    queued={plan.names.includes(row.item.name)}
                    disabled={actionPending}
                    onToggleQueued={() => toggleQueued(row)}
                    onAction={(action) => askConfirm(action, row)}
                  />
                ))}
              </ul>
            )}
          </div>

          {infraRows.length > 0 && (
            <div className="space-y-2">
              <span className={SECTION_LABEL}>Runtime infra repos</span>

              {ensuredUnknown && (
                <p className="text-[11px] text-amber-800" role="status">
                  The template catalog could not be read, so which mechanisms a roll-out ensures is only
                  partly known. Rows below may understate it — a roll-out still ensures every mechanism a
                  registered template deploys through.
                </p>
              )}

              {infraRows.map((infra) => (
                <div
                  key={infra.id}
                  className="rounded-lg border border-emerald-200/60 bg-emerald-50/40 px-3 py-2"
                >
                  <div className="flex items-center gap-3">
                    <span className="text-sm text-slate-700 font-medium truncate" title={infra.repoName}>
                      {infra.repoName}
                    </span>
                    {infra.platform && (
                      <span className={`shrink-0 text-[10px] font-medium px-1.5 py-0.5 rounded ${PLATFORM_CHIP}`}>
                        {infra.platform}
                      </span>
                    )}
                    {infra.buildKind && (
                      <span className={`shrink-0 text-[10px] font-medium px-1.5 py-0.5 rounded ${BUILD_KIND_CHIP}`}>
                        {buildKindLabel(infra.buildKind)}
                      </span>
                    )}
                    {infra.ensured ? (
                      <span className={`shrink-0 ml-auto ${PILL} bg-emerald-100 text-emerald-700`}>
                        Required
                      </span>
                    ) : (
                      <span
                        className={`shrink-0 ml-auto ${PILL} bg-slate-100 text-slate-500`}
                        title="No registered template deploys through this mechanism yet"
                      >
                        Not referenced
                      </span>
                    )}
                  </div>

                  {infra.reconcile && infraRow && (
                    <>
                      <p className="text-[11px] text-slate-500 mt-1">
                        {infraRow.provenance && !infraRow.unknown
                          ? `Present at ${infraRow.provenance}. A roll-out leaves it in place — AGP’s Terraform module is pushed into it only if you ask below, and never because of a re-push you selected above.`
                          : infraRow.provenance
                            ? `Present at ${infraRow.provenance}. ${infraRow.label}, so nothing will be written to it.`
                            : 'Not in the org yet. Every roll-out creates it and pushes AGP’s Terraform module — it is not optional, because a tenant runtime cannot deploy without it.'}
                      </p>

                      {infraRow.provenance && !infraRow.unknown && (
                        <label className="flex items-start gap-2 mt-2 cursor-pointer">
                          <input
                            type="checkbox"
                            checked={overwriteInfra}
                            onChange={(e) => setOverwriteInfra(e.target.checked)}
                            disabled={actionPending}
                            className="h-4 w-4 mt-0.5 shrink-0 rounded border-slate-300 text-emerald-600 focus:ring-emerald-500/40 disabled:opacity-40"
                          />
                          <span className="text-[11px] text-slate-600">
                            {overwriteInfraConsent(overwriteTargets)}
                          </span>
                        </label>
                      )}
                    </>
                  )}

                  {!infra.reconcile && (
                    <p className="text-[11px] text-slate-500 mt-1">
                      {infra.registered
                        ? 'Registered as an infra template.'
                        : 'Shipped with AGP; a roll-out adds it to this org’s infra catalog.'}{' '}
                      This comparison did not read it, so its state in
                      the org is not shown.{' '}
                      {infra.ensured
                        ? 'A roll-out ensures it either way — a registered template deploys through it.'
                        : 'No registered template deploys through it yet: AGP ensures an infra repo once one does — including in the same roll-out, if you register that template here.'}
                    </p>
                  )}
                </div>
              ))}
            </div>
          )}

          {actionError && (
            <p className={`text-sm ${actionError.retryable ? 'text-amber-800' : 'text-rose-600'}`} role="alert">
              {actionError.message}
            </p>
          )}
        </>
      ) : null}
    </ModalShell>
  );
}

function ReconcileRow({
  row,
  queued,
  disabled,
  onToggleQueued,
  onAction,
}: {
  row: ReconcileRowModel;
  queued: boolean;
  disabled: boolean;
  onToggleQueued: () => void;
  onAction: (action: SingleRepoAction) => void;
}) {
  const { item, label, tone, actions, queueAction, provenance, actionDisabled, note } = row;

  return (
    <li className="py-1.5">
      <div className="flex items-center gap-3">
        {queueAction ? (
          <input
            type="checkbox"
            checked={queued}
            onChange={onToggleQueued}
            disabled={disabled}
            aria-label={`${RECONCILE_ACTION_LABEL[queueAction]} ${item.name}`}
            className="h-4 w-4 rounded border-slate-300 text-emerald-600 focus:ring-emerald-500/40 disabled:opacity-40"
          />
        ) : (
          <span className="w-4 shrink-0" aria-hidden="true" />
        )}

        <div className="min-w-0 flex-1">
          <span className="text-sm text-slate-700 font-medium truncate block" title={item.name}>
            {item.name}
          </span>
          {provenance && (
            <span className="text-[11px] text-slate-400 tabular-nums" title="Default branch and head commit AGP read">
              {provenance}
            </span>
          )}
        </div>

        <span className={`shrink-0 ${PILL} ${tone}`}>{label}</span>

        <div className="shrink-0 flex items-center gap-1.5">
          {queueAction && (
            <span className={`${PILL} ${queued ? 'bg-amber-50 text-amber-700' : 'bg-slate-100 text-slate-500'}`}>
              {queued ? `Queued · ${RECONCILE_ACTION_LABEL[queueAction]}` : RECONCILE_ACTION_LABEL[queueAction]}
            </span>
          )}
          {actions
            .filter((a): a is SingleRepoAction => a === 'adopt' || a === 'deregister')
            .map((action) => (
              <button
                key={action}
                type="button"
                onClick={() => onAction(action)}
                disabled={disabled || actionDisabled}
                title={actionDisabled ? ADOPT_NAME_RULE_HINT : undefined}
                className={
                  action === 'deregister'
                    ? `${ROW_BTN} text-rose-700 hover:bg-rose-50`
                    : ROW_BTN
                }
              >
                {RECONCILE_ACTION_LABEL[action]}
              </button>
            ))}
        </div>
      </div>

      {note && (
        <p className="ml-7 mt-0.5 text-[11px] text-slate-500">{note}</p>
      )}
    </li>
  );
}
