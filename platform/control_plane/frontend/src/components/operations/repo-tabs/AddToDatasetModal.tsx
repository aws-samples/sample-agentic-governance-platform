import { useMemo, useState, type JSX } from 'react';

import { evalsApi, type EvalConfigOutput, type Repository, type TraceSummary } from '../../../api/client';
import { useLoad } from '../../../api/useLoad';
import { ModalShell } from '../ModalShell';
import { OPS_PRIMARY_BTN } from '../opsUi';
import { COMMIT_NOTE } from './evaluationsLogic';
import {
  DISCARD_CONFIRM,
  GOLDEN_ANSWER_LABEL,
  commitErrorText,
  datasetReadWarning,
  draftsFromTraces,
  matchModeNote,
  validateDrafts,
  whenText,
  type ItemDraft,
} from './tracesToDataset';

const FIELD_LABEL = 'block text-[11px] uppercase tracking-wide text-slate-400 font-medium mb-1';
const FIELD_INPUT =
  'w-full px-3 py-2 text-sm rounded-lg border border-slate-300 focus:outline-none focus:ring-2 focus:ring-emerald-500/40';
const SECONDARY_BTN =
  'px-3 py-1.5 rounded-lg border border-slate-300 bg-white text-sm font-medium text-slate-700 hover:bg-slate-50 transition-colors disabled:opacity-50 disabled:cursor-not-allowed';
const LINK_BTN = 'text-xs font-medium text-emerald-700 hover:text-emerald-800 disabled:opacity-50';

export default function AddToDatasetModal({
  repo,
  traces,
  onClose,
  onDone,
}: {
  repo: Pick<Repository, 'project_id' | 'id'>;
  traces: TraceSummary[];
  onClose: () => void;
  onDone: (count: number) => void;
}): JSX.Element {
  const [errors, setErrors] = useState<Record<string, string[]>>({});
  const [formErrors, setFormErrors] = useState<string[]>([]);
  const [pending, setPending] = useState(false);
  const [now] = useState(() => Date.now());
  const [initialTraces] = useState(traces);

  const filesLoad = useLoad(
    async (): Promise<{ ids: string[]; config: EvalConfigOutput | null; failed: boolean; fileErrors: number }> => {
      try {
        const files = await evalsApi.get(repo.project_id, repo.id);
        return { ids: files.items.map((i) => i.id), config: files.config, failed: false, fileErrors: files.errors.length };
      } catch {
        return { ids: [], config: null, failed: true, fileErrors: 0 };
      }
    },
    [repo.project_id, repo.id],
  );
  const read = filesLoad.state.kind === 'ready' ? filesLoad.state.data : null;
  const existingIds = useMemo(() => (read ? new Set(read.ids) : null), [read]);
  const config = read?.config ?? null;
  const readWarning = read ? datasetReadWarning({ failed: read.failed, errors: read.fileErrors }) : null;
  const initial = useMemo(() => (read ? draftsFromTraces(initialTraces, read.ids) : []), [read, initialTraces]);

  const [edits, setEdits] = useState<ItemDraft[] | null>(null);
  const drafts = edits ?? initial;

  const skipped = initialTraces.length - drafts.length;
  const note = useMemo(() => matchModeNote(config), [config]);

  const update = (key: string, patch: Partial<ItemDraft>) =>
    setEdits(drafts.map((d) => (d.key === key ? { ...d, ...patch } : d)));
  const remove = (key: string) => {
    setEdits(drafts.filter((d) => d.key !== key));
    setErrors((es) => {
      const next = { ...es };
      delete next[key];
      return next;
    });
  };

  const dirty = JSON.stringify(drafts) !== JSON.stringify(initial);
  const dismiss = () => {
    if (dirty && !window.confirm(DISCARD_CONFIRM)) return;
    onClose();
  };

  const submit = async () => {
    if (!existingIds) return;
    const v = validateDrafts(drafts, existingIds);
    setErrors(v.errors);
    setFormErrors(v.batchErrors);
    if (v.items.length === 0) return;
    setPending(true);
    try {
      const items = v.items;
      await evalsApi.addItems(repo.project_id, repo.id, items, 'append');
      onDone(items.length);
    } catch (e) {
      setFormErrors([commitErrorText(e)]);
      setPending(false);
    }
  };

  const errorCount = Object.keys(errors).length;
  const n = drafts.length;

  return (
    <ModalShell
      title="Add to dataset"
      description={`${n} item${n === 1 ? '' : 's'} from real traces. Edit each into what the agent should do, then commit.`}
      ariaLabel="Add traces to the evaluation dataset"
      actionPending={pending}
      onClose={dismiss}
      footer={
        <>
          <button type="button" className={SECONDARY_BTN} onClick={dismiss} disabled={pending}>
            Cancel
          </button>
          <button
            type="button"
            className={`${OPS_PRIMARY_BTN} disabled:opacity-50 disabled:cursor-not-allowed`}
            onClick={submit}
            disabled={pending || existingIds === null || n === 0}
          >
            {pending ? 'Committing…' : `Commit ${n} item${n === 1 ? '' : 's'}`}
          </button>
        </>
      }
    >
      {existingIds === null ? (
        <p className="text-sm text-slate-500" role="status">
          Reading the current dataset…
        </p>
      ) : (
        <>
          {readWarning && (
            <p role="alert" className="rounded-lg bg-amber-50 px-3 py-2 text-sm text-amber-800">
              {readWarning}
            </p>
          )}
          {skipped > 0 && (
            <p className="text-xs text-slate-500">
              {skipped} selected trace{skipped === 1 ? '' : 's'} recorded no input and {skipped === 1 ? 'was' : 'were'} left out.
            </p>
          )}
          {n === 0 ? (
            <p className="text-sm text-slate-500">Nothing left to add.</p>
          ) : (
            <ol className="max-h-[60vh] overflow-y-auto -mx-1 px-1 space-y-3">
              {drafts.map((d, i) => (
                <DraftCard
                  key={d.key}
                  index={i}
                  draft={d}
                  errors={errors[d.key] ?? []}
                  matchNote={note}
                  now={now}
                  disabled={pending}
                  onChange={(patch) => update(d.key, patch)}
                  onRemove={n > 1 ? () => remove(d.key) : null}
                />
              ))}
            </ol>
          )}
          {(formErrors.length > 0 || errorCount > 0) && (
            <div role="alert" className="rounded-lg bg-rose-50 px-3 py-2 text-sm text-rose-700 space-y-1">
              {errorCount > 0 && (
                <p>
                  {errorCount} item{errorCount === 1 ? ' needs' : 's need'} fixing before the commit.
                </p>
              )}
              {formErrors.map((e) => (
                <p key={e}>{e}</p>
              ))}
            </div>
          )}
          <p className="text-[11px] text-slate-400">{COMMIT_NOTE}</p>
        </>
      )}
    </ModalShell>
  );
}

function DraftCard({
  index,
  draft,
  errors,
  matchNote,
  now,
  disabled,
  onChange,
  onRemove,
}: {
  index: number;
  draft: ItemDraft;
  errors: string[];
  matchNote: string;
  now: number;
  disabled: boolean;
  onChange: (patch: Partial<ItemDraft>) => void;
  onRemove: (() => void) | null;
}): JSX.Element {
  const fid = (name: string) => `add-ds-${index}-${name}`;
  const when = whenText(draft.startedAt, now);
  const setTool = (i: number, patch: Partial<ItemDraft['tools'][number]>) =>
    onChange({ tools: draft.tools.map((t, j) => (j === i ? { ...t, ...patch } : t)) });
  const hasErrors = errors.length > 0;

  return (
    <li
      className={`rounded-xl border p-4 space-y-3 ${hasErrors ? 'border-rose-300 bg-rose-50/30' : 'border-emerald-100/70 bg-white'}`}
      aria-label={`Item ${index + 1}`}
    >
      <div className="flex items-baseline justify-between gap-2">
        <h3 className="text-xs font-semibold text-slate-700" title={`session ${draft.key}`}>
          Item {index + 1}
          <span className="ml-2 font-normal text-slate-400">from a trace{when ? `, ${when}` : ''}</span>
        </h3>
        {onRemove && (
          <button type="button" className={LINK_BTN} onClick={onRemove} disabled={disabled}>
            Remove
          </button>
        )}
      </div>

      <div>
        <label className={FIELD_LABEL} htmlFor={fid('id')}>ID</label>
        <input
          id={fid('id')}
          className={`${FIELD_INPUT} font-mono`}
          value={draft.id}
          disabled={disabled}
          onChange={(e) => onChange({ id: e.target.value })}
        />
      </div>

      <div>
        <label className={FIELD_LABEL} htmlFor={fid('input')}>Input</label>
        <textarea
          id={fid('input')}
          className={FIELD_INPUT}
          rows={2}
          value={draft.input}
          disabled={disabled}
          onChange={(e) => onChange({ input: e.target.value })}
        />
      </div>

      <div>
        <label className={FIELD_LABEL} htmlFor={fid('expected')}>{GOLDEN_ANSWER_LABEL}</label>
        <textarea
          id={fid('expected')}
          className={FIELD_INPUT}
          rows={3}
          value={draft.expectedOutput}
          placeholder="Leave blank to judge without a reference answer"
          disabled={disabled}
          onChange={(e) => onChange({ expectedOutput: e.target.value })}
        />
      </div>

      <div>
        <label className={FIELD_LABEL} htmlFor={fid('assertions')}>Assertions (optional, one per line)</label>
        <textarea
          id={fid('assertions')}
          className={FIELD_INPUT}
          rows={2}
          value={draft.assertions}
          placeholder="Opens a claim"
          disabled={disabled}
          onChange={(e) => onChange({ assertions: e.target.value })}
        />
      </div>

      <fieldset>
        <legend className={FIELD_LABEL}>Expected tools, in order</legend>
        {draft.tools.length === 0 && (
          <p className="text-xs text-slate-500 mb-2">The trace called no tools. Add one if it should have.</p>
        )}
        <div className="space-y-2">
          {draft.tools.map((t, i) => (
            <div key={i} className="flex gap-2 items-start">
              <span className="mt-2 w-5 shrink-0 text-right text-[11px] tabular-nums text-slate-400">{i + 1}.</span>
              <input
                aria-label={`Tool ${i + 1} name`}
                className={`${FIELD_INPUT} font-mono w-2/5`}
                value={t.name}
                placeholder="tool_name"
                disabled={disabled}
                onChange={(e) => setTool(i, { name: e.target.value })}
              />
              <input
                aria-label={`Tool ${i + 1} arguments (JSON)`}
                className={`${FIELD_INPUT} font-mono text-xs`}
                value={t.args}
                placeholder='{"key": "value"} or blank'
                disabled={disabled}
                onChange={(e) => setTool(i, { args: e.target.value })}
              />
              <button
                type="button"
                aria-label={`Remove tool ${i + 1}`}
                className="mt-1.5 shrink-0 h-7 w-7 rounded-md text-slate-400 hover:text-rose-600 hover:bg-rose-50 disabled:opacity-40"
                onClick={() => onChange({ tools: draft.tools.filter((_, j) => j !== i) })}
                disabled={disabled}
              >
                <span aria-hidden="true">×</span>
              </button>
            </div>
          ))}
        </div>
        <div className="mt-2 flex flex-wrap items-baseline justify-between gap-2">
          <button
            type="button"
            className={LINK_BTN}
            onClick={() => onChange({ tools: [...draft.tools, { name: '', args: '' }] })}
            disabled={disabled}
          >
            + Add tool
          </button>
          <span className="text-[11px] text-slate-400">{matchNote}</span>
        </div>
      </fieldset>

      {hasErrors && (
        <ul className="text-xs text-rose-700 list-disc pl-4 space-y-0.5">
          {errors.map((e) => (
            <li key={e}>{e}</li>
          ))}
        </ul>
      )}
    </li>
  );
}
