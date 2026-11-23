import { useEffect, useRef, useState, type JSX, type RefObject } from 'react';

import { OPS_BADGE, OPS_PRIMARY_BTN } from './opsUi';
import { AgentForm, DropZone, FileChip, KindIcon, McpServerForm, Spinner } from './StudioForms.tsx';
import {
  KIND_LABEL,
  assignTargets,
  attachment,
  blockName,
  blockSummary,
  draftFilesProblem,
  mcpOptions,
  treeRows,
  type Attachment,
  type Block,
  type BlockKind,
  type RegistryMcp,
  type RepoFile,
} from './studioModel';
import { KIND_BLURB, PILL, SECONDARY_BTN } from './studioStyles';

export function AiBox(props: {
  useCase: string;
  setUseCase: (v: string) => void;
  files: Attachment[];
  setFiles: (f: Attachment[]) => void;
  error: string | null;
  manualRef: RefObject<HTMLButtonElement | null>;
  onDraft: () => void;
  onManual: () => void;
}): JSX.Element {
  const filesProblem = draftFilesProblem(props.files);
  return (
    <div className="max-w-3xl mx-auto py-2">
      <div className="flex items-end justify-between gap-3 mb-2">
        <label htmlFor="studio-usecase" className="text-base font-semibold text-slate-900">
          What do you want to build?
        </label>
      </div>
      <div className="rounded-xl border border-slate-300 bg-white focus-within:ring-2 focus-within:ring-emerald-500/40 focus-within:border-emerald-400 transition-shadow">
        <textarea
          id="studio-usecase"
          rows={5}
          maxLength={4000}
          value={props.useCase}
          onChange={(e) => props.setUseCase(e.target.value)}
          placeholder="Who uses it, what it should do, which systems it talks to, what it must never do. Refer to attached files by name to say where they go."
          className="w-full px-4 pt-3 pb-2 text-sm rounded-t-xl bg-transparent resize-none focus:outline-none"
        />
        <div className="px-3 pb-3">
          <DropZone
            compact
            multiple
            label="Attach files: OpenAPI specs, SKILL.md, a prompt (up to 10, 1 MB each)"
            accept=""
            onFiles={(fs) => props.setFiles([...props.files, ...fs.map(attachment)])}
          />
          {props.files.length > 0 && (
            <div className="flex flex-wrap gap-1.5 mt-2" aria-label="Attached files">
              {props.files.map((f) => (
                <FileChip key={f.id} name={f.name} onRemove={() => props.setFiles(props.files.filter((x) => x.id !== f.id))} />
              ))}
            </div>
          )}
          {filesProblem && <p className="text-[11px] text-rose-600 mt-1.5">{filesProblem}</p>}
        </div>
      </div>
      {props.error && (
        <div role="alert" className="mt-3 rounded-lg bg-rose-50 border border-rose-200/70 px-3 py-2 text-xs text-rose-800">
          {props.error}
        </div>
      )}
      <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-3 mt-3">
        <p className="text-xs text-slate-500">
          The AI drafts the components and places each file. The API key is never sent to it.{' '}
          <span className="font-semibold text-slate-700">Nothing is created until you click Create.</span>
        </p>
        <button
          type="button"
          onClick={props.onDraft}
          disabled={!props.useCase.trim() || Boolean(filesProblem)}
          className={`${OPS_PRIMARY_BTN} shrink-0 disabled:opacity-40`}
        >
          Compose
        </button>
      </div>
      <div className="flex items-center gap-3 mt-8">
        <span className="h-px flex-1 bg-emerald-100" />
        <button
          ref={props.manualRef}
          type="button"
          onClick={props.onManual}
          className={`px-3 py-1 rounded-md border text-xs font-medium focus:outline-none focus-visible:ring-2 focus-visible:ring-emerald-500/50 ${
            props.error ? 'border-emerald-400 bg-white text-emerald-800' : 'border-slate-200 bg-white/70 text-slate-600 hover:bg-white hover:text-slate-800'
          }`}
        >
          Compose manually
        </button>
        <span className="h-px flex-1 bg-emerald-100" />
      </div>
    </div>
  );
}

export function DraftingSkeleton(): JSX.Element {
  return (
    <div aria-live="polite">
      <p className="text-xs text-slate-500 flex items-center gap-2 mb-3">
        <Spinner className="w-3.5 h-3.5 text-emerald-600" />
        Drafting components from your description…
      </p>
      <div className="space-y-3">
        {['mcp', 'agent'].map((k) => (
          <div key={k} className="rounded-xl border border-slate-200 bg-white/70 p-4 animate-pulse">
            <div className="flex items-center gap-3">
              <div className="w-8 h-8 rounded-lg bg-emerald-100/70" />
              <div className="flex-1 space-y-2">
                <div className="h-3 w-40 rounded bg-emerald-100/70" />
                <div className="h-2.5 w-64 rounded bg-slate-100" />
              </div>
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}

export function AddComponent(props: { onAdd: (k: BlockKind) => void; primary?: boolean }): JSX.Element {
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!open) return;
    const close = (e: MouseEvent | KeyboardEvent) => {
      if (e instanceof KeyboardEvent ? e.key === 'Escape' : !ref.current?.contains(e.target as Node)) setOpen(false);
    };
    document.addEventListener('mousedown', close);
    document.addEventListener('keydown', close);
    return () => {
      document.removeEventListener('mousedown', close);
      document.removeEventListener('keydown', close);
    };
  }, [open]);
  return (
    <div ref={ref} className="relative inline-block">
      <button
        type="button"
        aria-haspopup="menu"
        aria-expanded={open}
        onClick={() => setOpen((o) => !o)}
        className={`${props.primary ? OPS_PRIMARY_BTN : SECONDARY_BTN} inline-flex items-center gap-1.5`}
      >
        <span aria-hidden="true" className="text-base leading-none">+</span> Add component
      </button>
      {open && (
        <div role="menu" className="absolute z-20 left-0 mt-1.5 w-80 rounded-xl border border-slate-200 bg-white shadow-lg p-1.5">
          {(['mcp', 'agent'] as const).map((k) => (
            <button
              key={k}
              type="button"
              role="menuitem"
              onClick={() => {
                props.onAdd(k);
                setOpen(false);
              }}
              className="w-full flex items-start gap-3 rounded-lg px-2.5 py-2 text-left hover:bg-emerald-50/70 focus:outline-none focus-visible:bg-emerald-50"
            >
              <KindIcon kind={k} />
              <span>
                <span className="block text-sm font-semibold text-slate-800">{KIND_LABEL[k]}</span>
                <span className="block text-xs text-slate-500">{KIND_BLURB[k]}</span>
              </span>
            </button>
          ))}
        </div>
      )}
    </div>
  );
}

export function BlockCard(props: {
  block: Block;
  blocks: Block[];
  registry: RegistryMcp[];
  open: boolean;
  issues: number;
  onToggle: () => void;
  onChange: (b: Block) => void;
  onRemove: () => void;
}): JSX.Element {
  const { block: b } = props;
  const [confirm, setConfirm] = useState(false);
  const bodyId = `${b.id}-body`;
  return (
    <section id={b.id} className="scroll-mt-6 rounded-xl border border-slate-200 bg-white/80 backdrop-blur shadow-sm">
      <div className="flex items-center gap-3 px-4 py-3">
        <button type="button" onClick={props.onToggle} aria-expanded={props.open} aria-controls={bodyId} className="flex items-center gap-3 min-w-0 flex-1 text-left">
          <KindIcon kind={b.kind} />
          <span className="min-w-0">
            <span className="flex items-center gap-2">
              <span className="text-[10px] uppercase tracking-wide font-semibold text-slate-400">{KIND_LABEL[b.kind]}</span>
              <span className={`font-mono text-sm font-semibold truncate ${b.draft.name ? 'text-slate-900' : 'text-slate-400'}`}>{blockName(b)}</span>
            </span>
            <span className="block text-xs text-slate-500 truncate">{blockSummary(b, props.blocks, props.registry)}</span>
          </span>
        </button>
        {props.issues > 0 ? (
          <span className={`${PILL} bg-rose-50 text-rose-700 shrink-0`}>{props.issues} to fix</span>
        ) : (
          <span className={`${PILL} ${OPS_BADGE.ready} shrink-0`}>
            <svg className="w-3 h-3" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={3} aria-hidden="true">
              <path strokeLinecap="round" strokeLinejoin="round" d="M5 13l4 4L19 7" />
            </svg>
            ready
          </span>
        )}
        {confirm ? (
          <span className="flex items-center gap-1.5 shrink-0">
            <button type="button" onClick={props.onRemove} className="px-2 py-1 rounded-md bg-rose-600 text-white text-[11px] font-semibold hover:bg-rose-700">
              Remove
            </button>
            <button type="button" onClick={() => setConfirm(false)} className="px-2 py-1 rounded-md text-[11px] font-medium text-slate-500 hover:bg-slate-100">
              Keep
            </button>
          </span>
        ) : (
          <button
            type="button"
            onClick={() => setConfirm(true)}
            aria-label={`Remove ${blockName(b)}`}
            title="Remove this component"
            className="shrink-0 p-1.5 rounded-md text-slate-400 hover:text-rose-600 hover:bg-rose-50"
          >
            <svg className="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={1.5} aria-hidden="true">
              <path strokeLinecap="round" strokeLinejoin="round" d="M14.74 9l-.346 9m-4.788 0L9.26 9m9.968-3.21c.342.052.682.107 1.022.166m-1.022-.165L18.16 19.673a2.25 2.25 0 01-2.244 2.077H8.084a2.25 2.25 0 01-2.244-2.077L4.772 5.79m14.456 0a48.108 48.108 0 00-3.478-.397m-12 .562c.34-.059.68-.114 1.022-.165m0 0a48.11 48.11 0 013.478-.397m7.5 0v-.916c0-1.18-.91-2.164-2.09-2.201a51.964 51.964 0 00-3.32 0c-1.18.037-2.09 1.022-2.09 2.201v.916m7.5 0a48.667 48.667 0 00-7.5 0" />
            </svg>
          </button>
        )}
        <button type="button" onClick={props.onToggle} aria-label={props.open ? 'Collapse' : 'Expand'} className="shrink-0 p-1 text-slate-400 hover:text-slate-700">
          <svg className={`w-4 h-4 transition-transform ${props.open ? 'rotate-180' : ''}`} fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2} aria-hidden="true">
            <path strokeLinecap="round" strokeLinejoin="round" d="M19.5 8.25l-7.5 7.5-7.5-7.5" />
          </svg>
        </button>
      </div>
      {props.open && (
        <div id={bodyId} className="px-5 pb-5 pt-4 border-t border-emerald-100/70">
          {b.kind === 'mcp' ? (
            <McpServerForm id={b.id} draft={b.draft} onChange={(p) => props.onChange({ ...b, draft: { ...b.draft, ...p } })} />
          ) : (
            <AgentForm id={b.id} draft={b.draft} options={mcpOptions(props.blocks, props.registry)} onChange={(p) => props.onChange({ ...b, draft: { ...b.draft, ...p } })} />
          )}
        </div>
      )}
    </section>
  );
}

export function UnusedFiles(props: { files: Attachment[]; blocks: Block[]; onAssign: (f: Attachment, target: string) => void; onRemove: (f: Attachment) => void }): JSX.Element {
  const targets = assignTargets(props.blocks);
  return (
    <div className="rounded-xl border border-dashed border-amber-300/80 bg-amber-50/40 px-4 py-3">
      <p className="text-xs font-semibold text-slate-700">Not used</p>
      <p className="text-[11px] text-slate-500 mb-2">The AI did not place these files. Assign one by hand, or remove it. Files left here are not committed.</p>
      <ul className="space-y-1.5">
        {props.files.map((f) => (
          <li key={f.id} className="flex flex-wrap items-center gap-2">
            <FileChip name={f.name} />
            <select
              aria-label={`Assign ${f.name}`}
              value=""
              onChange={(e) => e.target.value && props.onAssign(f, e.target.value)}
              className="px-2 py-1 text-xs rounded-md border border-slate-300 bg-white focus:outline-none focus:ring-2 focus:ring-emerald-500/40"
            >
              <option value="">Assign to…</option>
              {targets.map((t) => (
                <option key={t.key} value={t.key}>
                  {t.label}
                </option>
              ))}
            </select>
            <button type="button" onClick={() => props.onRemove(f)} className="text-[11px] font-medium text-slate-500 hover:text-rose-700 hover:underline">
              Remove
            </button>
          </li>
        ))}
      </ul>
    </div>
  );
}

export function RepoTree(props: {
  repo: string;
  kind: string;
  branch: string;
  files: RepoFile[];
  selected: string;
  onSelect: (key: string) => void;
  footer?: string;
}): JSX.Element {
  return (
    <div className="rounded-lg border border-slate-200 bg-white/70">
      <div className="px-3 py-2 border-b border-slate-100">
        <p className="font-mono text-xs font-semibold text-slate-800 truncate">{props.repo}</p>
        <p className="text-[11px] text-slate-400">
          {props.kind} · branch <span className="font-mono">{props.branch}</span>
        </p>
      </div>
      <ul className="py-1">
        {treeRows(props.files).map((r) => {
          const key = r.file ? `${props.repo}:${r.file.path}` : '';
          const active = key === props.selected;
          return (
            <li key={`${r.depth}-${r.label}-${r.file?.path ?? 'dir'}`}>
              {r.file ? (
                <button
                  type="button"
                  onClick={() => props.onSelect(key)}
                  style={{ paddingLeft: `${0.75 + r.depth * 0.9}rem` }}
                  className={`w-full flex items-center justify-between gap-2 pr-3 py-1 text-left font-mono text-xs transition-colors ${
                    active ? 'bg-emerald-50 text-emerald-800' : 'text-slate-700 hover:bg-slate-50'
                  }`}
                >
                  <span className="truncate">{r.label}</span>
                  {r.file.note && (
                    <span className={`shrink-0 font-sans text-[10px] ${r.file.note === 'needs fix' ? 'text-amber-700' : 'text-slate-400'}`}>{r.file.note}</span>
                  )}
                </button>
              ) : (
                <span style={{ paddingLeft: `${0.75 + r.depth * 0.9}rem` }} className="block py-1 font-mono text-xs text-slate-400">
                  {r.label}
                </span>
              )}
            </li>
          );
        })}
      </ul>
      {props.footer && <p className="px-3 py-2 border-t border-slate-100 text-[11px] text-slate-500">{props.footer}</p>}
    </div>
  );
}
