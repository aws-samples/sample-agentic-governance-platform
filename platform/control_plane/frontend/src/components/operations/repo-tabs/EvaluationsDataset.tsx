import { useMemo, useState, type JSX } from 'react';

import {
  evalsApi,
  type DatasetItemInput,
  type DatasetItemOutput,
  type EvalFiles,
  type Repository,
} from '../../../api/client';
import { ModalShell } from '../ModalShell';
import { NO_VALUE } from '../opsLabels';
import { OPS_CARD, OPS_PRIMARY_BTN, OPS_TABLE_DIVIDE, OPS_TABLE_HEAD } from '../opsUi';
import {
  EMPTY_DATASET_COPY,
  commitErrorText,
  errText,
  githubFileUrl,
  itemFromForm,
  previewJsonl,
  suggestItemId,
  type ItemForm,
} from './evaluationsLogic';
import { FIELD_INPUT, FIELD_LABEL, SECONDARY_BTN, SECTION_TITLE } from './evaluationsTabStyles';

export function DatasetSection({
  repo,
  files,
  error,
  mayWrite,
  onAdd,
  onUpload,
}: {
  repo: Repository;
  files: EvalFiles | null;
  error: string | null;
  mayWrite: boolean;
  onAdd: () => void;
  onUpload: () => void;
}): JSX.Element {
  const path = files?.config.dataset ?? 'evals/dataset.jsonl';
  const gh = githubFileUrl(repo.repo_url, path);
  const items = files?.items ?? [];
  const writeTitle = mayWrite ? undefined : 'Needs the Maintainer role on this project';
  return (
    <section aria-label="Dataset" className={`${OPS_CARD} overflow-hidden`}>
      <div className="flex flex-wrap items-center justify-between gap-3 px-5 pt-5 pb-3">
        <div>
          <h3 className={SECTION_TITLE}>
            Dataset <span className="font-normal text-slate-400">· {items.length} {items.length === 1 ? 'item' : 'items'}</span>
          </h3>
          <p className="text-[11px] text-slate-400 font-mono">{path}</p>
        </div>
        <div className="flex flex-wrap gap-2">
          <button type="button" className={SECONDARY_BTN} onClick={onAdd} disabled={!mayWrite || !files} title={writeTitle}>
            Add item
          </button>
          <button type="button" className={SECONDARY_BTN} onClick={onUpload} disabled={!mayWrite || !files} title={writeTitle}>
            Upload file
          </button>
          {gh && (
            <a href={gh} target="_blank" rel="noreferrer" className={SECONDARY_BTN}>
              View in GitHub ↗
            </a>
          )}
        </div>
      </div>
      {error && files === null ? (
        <p className="px-5 pb-5 text-sm text-rose-700">The eval files could not be read: {error}</p>
      ) : items.length === 0 ? (
        <EmptyDataset />
      ) : (
        <table className="w-full text-sm">
          <thead className={OPS_TABLE_HEAD}>
            <tr>
              <th scope="col" className="text-left font-medium px-5 py-2">ID</th>
              <th scope="col" className="text-left font-medium px-3 py-2">Input</th>
              <th scope="col" className="text-left font-medium px-3 py-2">Expected tools</th>
              <th scope="col" className="text-left font-medium px-3 py-2 hidden md:table-cell">For judges</th>
              <th scope="col" className="text-left font-medium px-3 py-2 hidden lg:table-cell">Source</th>
            </tr>
          </thead>
          <tbody className={OPS_TABLE_DIVIDE}>
            {items.map((it) => (
              <tr key={it.id} className="align-top">
                <td className="px-5 py-2.5 font-mono text-xs text-slate-800 whitespace-nowrap">{it.id}</td>
                <td className="px-3 py-2.5 text-slate-700 max-w-md">
                  <span className="line-clamp-2 break-words" title={it.input}>{it.input}</span>
                </td>
                <td className="px-3 py-2.5 font-mono text-xs text-slate-600">
                  {(it.expected_tools ?? []).length === 0 ? <span className="text-slate-300">{NO_VALUE}</span> : (it.expected_tools ?? []).map((t) => t.name).join(' → ')}
                </td>
                <td className="px-3 py-2.5 text-slate-500 hidden md:table-cell whitespace-nowrap">
                  {[it.expected_output ? 'expected answer' : null, (it.assertions ?? []).length ? `${it.assertions!.length} ${it.assertions!.length === 1 ? 'assertion' : 'assertions'}` : null]
                    .filter(Boolean)
                    .join(' · ') || NO_VALUE}
                </td>
                <td className="px-3 py-2.5 text-slate-500 hidden lg:table-cell">{it.source_trace_id ? 'from a trace' : 'written'}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </section>
  );
}

function EmptyDataset(): JSX.Element {
  return (
    <div className="px-5 pb-5">
      <div className="rounded-lg border border-dashed border-emerald-200 bg-emerald-50/30 p-4">
        <p className="text-sm font-semibold text-slate-800">{EMPTY_DATASET_COPY.headline}</p>
        <p className="text-sm text-slate-500 mt-1 max-w-2xl">{EMPTY_DATASET_COPY.detail}</p>
        <ol className="mt-3 grid gap-3 sm:grid-cols-3">
          {EMPTY_DATASET_COPY.ways.map((w, i) => (
            <li key={w.title} className="rounded-lg bg-white/80 border border-emerald-100 px-3 py-2.5">
              <span className="text-[11px] font-semibold text-emerald-700 tabular-nums">{i + 1}</span>
              <span className="block text-sm font-medium text-slate-800">{w.title}</span>
              <span className="block text-xs text-slate-500 mt-0.5">{w.detail}</span>
            </li>
          ))}
        </ol>
        <p className="text-xs text-slate-500 mt-3">{EMPTY_DATASET_COPY.gateNote}</p>
      </div>
    </div>
  );
}

export function AddItemModal({
  repo,
  existing,
  onClose,
  onDone,
}: {
  repo: Repository;
  existing: DatasetItemOutput[];
  onClose: () => void;
  onDone: (id: string) => void;
}): JSX.Element {
  const ids = useMemo(() => new Set(existing.map((i) => i.id)), [existing]);
  const [form, setForm] = useState<ItemForm>(() => ({
    id: suggestItemId(ids),
    input: '',
    expectedOutput: '',
    assertions: '',
    tools: [{ name: '', args: '' }],
  }));
  const [errors, setErrors] = useState<string[]>([]);
  const [pending, setPending] = useState(false);
  const set = (patch: Partial<ItemForm>) => setForm((f) => ({ ...f, ...patch }));
  const setTool = (i: number, patch: Partial<ItemForm['tools'][number]>) =>
    setForm((f) => ({ ...f, tools: f.tools.map((t, j) => (j === i ? { ...t, ...patch } : t)) }));

  const submit = async () => {
    const { item, errors: errs } = itemFromForm(form, ids);
    setErrors(errs);
    if (!item) return;
    setPending(true);
    try {
      await evalsApi.addItems(repo.project_id, repo.id, [item], 'append');
      onDone(item.id);
    } catch (e) {
      setErrors([commitErrorText(e, 'Add item')]);
      setPending(false);
    }
  };

  return (
    <ModalShell
      title="Add a dataset item"
      description="Committed to the repository's dataset file. Only the ID and the input are required."
      ariaLabel="Add a dataset item"
      actionPending={pending}
      onClose={onClose}
      footer={
        <>
          <button type="button" className={SECONDARY_BTN} onClick={onClose} disabled={pending}>Cancel</button>
          <button type="button" className={`${OPS_PRIMARY_BTN} disabled:opacity-50`} onClick={() => void submit()} disabled={pending}>
            {pending ? 'Committing…' : 'Add item'}
          </button>
        </>
      }
    >
      <div className="max-h-[60vh] overflow-y-auto space-y-3 pr-1">
        <label className="block">
          <span className={FIELD_LABEL}>ID</span>
          <input className={`${FIELD_INPUT} font-mono`} value={form.id} onChange={(e) => set({ id: e.target.value })} />
        </label>
        <label className="block">
          <span className={FIELD_LABEL}>Input — what the user says</span>
          <textarea className={FIELD_INPUT} rows={3} value={form.input} onChange={(e) => set({ input: e.target.value })} />
        </label>
        <fieldset className="space-y-2">
          <legend className={FIELD_LABEL}>Expected tools, in order (checked deterministically)</legend>
          {form.tools.map((t, i) => (
            <div key={i} className="flex gap-2">
              <input
                aria-label={`Tool ${i + 1} name`}
                placeholder="tool name"
                className={`${FIELD_INPUT} font-mono w-2/5`}
                value={t.name}
                onChange={(e) => setTool(i, { name: e.target.value })}
              />
              <input
                aria-label={`Tool ${i + 1} arguments`}
                placeholder='{"policy_id": "P-1001"} (optional)'
                className={`${FIELD_INPUT} font-mono`}
                value={t.args}
                onChange={(e) => setTool(i, { args: e.target.value })}
              />
              <button
                type="button"
                aria-label={`Remove tool ${i + 1}`}
                className="px-2 text-slate-400 hover:text-rose-600"
                onClick={() => set({ tools: form.tools.filter((_, j) => j !== i) })}
              >
                ×
              </button>
            </div>
          ))}
          <button type="button" className="text-xs font-medium text-emerald-700 hover:text-emerald-800" onClick={() => set({ tools: [...form.tools, { name: '', args: '' }] })}>
            + Add a tool
          </button>
        </fieldset>
        <label className="block">
          <span className={FIELD_LABEL}>Expected output (for the Correctness judge)</span>
          <textarea className={FIELD_INPUT} rows={2} value={form.expectedOutput} onChange={(e) => set({ expectedOutput: e.target.value })} />
        </label>
        <label className="block">
          <span className={FIELD_LABEL}>Assertions, one per line (for the Goal success judge)</span>
          <textarea className={FIELD_INPUT} rows={2} value={form.assertions} onChange={(e) => set({ assertions: e.target.value })} />
        </label>
        {errors.length > 0 && (
          <ul role="alert" className="text-sm text-rose-700 list-disc pl-5 space-y-0.5">
            {errors.map((e, i) => <li key={i}>{e}</li>)}
          </ul>
        )}
      </div>
    </ModalShell>
  );
}

export function UploadModal({
  repo,
  current,
  path,
  onClose,
  onDone,
}: {
  repo: Repository;
  current: number;
  path: string;
  onClose: () => void;
  onDone: (count: number) => void;
}): JSX.Element {
  const [fileName, setFileName] = useState<string | null>(null);
  const [items, setItems] = useState<DatasetItemInput[] | null>(null);
  const [errors, setErrors] = useState<string[]>([]);
  const [checking, setChecking] = useState(false);
  const [pending, setPending] = useState(false);

  const read = async (file: File) => {
    setFileName(file.name);
    setItems(null);
    setErrors([]);
    const text = await file.text();
    const preview = previewJsonl(text);
    if (preview.errors.length > 0) {
      setErrors(preview.errors);
      return;
    }
    setChecking(true);
    try {
      const res = await evalsApi.validate(repo.project_id, repo.id, { dataset_text: text });
      if (!res.ok) setErrors(res.errors);
      else setItems(preview.items);
    } catch (e) {
      setErrors([errText(e)]);
    } finally {
      setChecking(false);
    }
  };

  const confirm = async () => {
    if (!items) return;
    setPending(true);
    try {
      await evalsApi.addItems(repo.project_id, repo.id, items, 'replace');
      onDone(items.length);
    } catch (e) {
      setErrors([commitErrorText(e, 'Replace')]);
      setPending(false);
    }
  };

  return (
    <ModalShell
      title="Upload a dataset file"
      description={`A .jsonl file, one item per line. It replaces ${path} in the repository.`}
      ariaLabel="Upload a dataset file"
      actionPending={pending}
      onClose={onClose}
      footer={
        <>
          <button type="button" className={SECONDARY_BTN} onClick={onClose} disabled={pending}>Cancel</button>
          <button type="button" className={`${OPS_PRIMARY_BTN} disabled:opacity-50`} onClick={() => void confirm()} disabled={!items || pending}>
            {pending ? 'Committing…' : items ? `Replace with ${items.length} ${items.length === 1 ? 'item' : 'items'}` : 'Replace dataset'}
          </button>
        </>
      }
    >
      <label className="block">
        <span className={FIELD_LABEL}>File</span>
        <input
          type="file"
          accept=".jsonl,application/jsonl,application/x-ndjson,text/plain"
          className="block w-full text-sm text-slate-600 file:mr-3 file:rounded-lg file:border-0 file:bg-emerald-50 file:px-3 file:py-1.5 file:text-sm file:font-medium file:text-emerald-700 hover:file:bg-emerald-100"
          onChange={(e) => {
            const f = e.target.files?.[0];
            if (f) void read(f);
          }}
        />
      </label>
      <p className="text-xs text-slate-500 font-mono break-all">
        {'{"id":"fnol-001","input":"…","expected_tools":[{"name":"get_policy","args":{"policy_id":"P-1001"}}],"assertions":["Opens a claim"]}'}
      </p>
      {checking && <p className="text-sm text-slate-500">Validating {fileName}…</p>}
      {items && (
        <p className="text-sm text-emerald-800 rounded-lg bg-emerald-50/70 border border-emerald-200 px-3 py-2">
          {fileName} is valid: {items.length} {items.length === 1 ? 'item' : 'items'}. This replaces the current {current} {current === 1 ? 'item' : 'items'}.
        </p>
      )}
      {errors.length > 0 && (
        <div role="alert" className="rounded-lg border border-rose-200 bg-rose-50/60 px-3 py-2">
          <p className="text-sm font-medium text-rose-800">{fileName ?? 'The file'} has {errors.length} {errors.length === 1 ? 'error' : 'errors'}. Nothing was committed.</p>
          <ul className="mt-1 max-h-40 overflow-auto list-disc pl-5 font-mono text-xs text-rose-700 space-y-0.5">
            {errors.map((e, i) => <li key={i}>{e}</li>)}
          </ul>
        </div>
      )}
    </ModalShell>
  );
}
