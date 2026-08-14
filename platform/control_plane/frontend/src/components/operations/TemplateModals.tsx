// TemplateModals — the upload + edit dialogs for the template catalog (E28/T9b).
//
// Moved here VERBATIM from `TemplatesAdmin.tsx`, which is now deleted. That file was the
// Ops Admin console's "Templates" tab body; T8 removed the console, and T9 gave templates
// their own left-nav page, so the tab body's only remaining job was housing these two
// dialogs. Keeping it alive just to export them would have left the catalog reachable at
// two paths with two different chromes — the defect T9 was written to close.
//
// `Templates.tsx` is the sole call site. These stay in their own module rather than moving
// into the page because they are ~410 lines of form, and a 750-line page file is how the
// original grew a tab body it could not test.
//
// Mechanics preserved exactly (they were live-tested): the `mountedRef` guard so a resolved
// promise cannot setState on an unmounted dialog, `actionPending` single-flight, `canSubmit`,
// the inline `<p role="alert">` error, and autofocus on open. The parent closes the dialog
// and refetches on success — these never do.
//
// House style: the shared `ModalShell` (ConnectionsAdmin) + opsUi tokens, Tailwind v4
// utility strings, 2-space indent.

import { useCallback, useEffect, useRef, useState } from 'react';

import type { TemplatePatch, TemplateView } from '../../api/client';
// Explicit `.tsx`, load-bearing: with `allowImportingTsExtensions` on a case-insensitive
// filesystem, an extensionless `./ConnectionsAdmin` can resolve to a sibling whose name
// differs only in casing and import as `undefined` with no error (see
// `githubLinkApi.test.ts:18-20`).
import { ModalShell } from './ConnectionsAdmin.tsx';
// The chip tints come from the card grid, not a local copy: these dialogs EDIT the very
// chips the cards render, so a second `TAG_PILL` would let the editor and the card disagree.
import { SERVICE_CHIP, TAG_PILL } from './TemplateCardGrid.tsx';

// Templates are Strands agent scaffolds; the framework is fixed at upload time.
const FRAMEWORK_DEFAULT = 'strands';

const FIELD_LABEL = 'block text-[11px] uppercase tracking-wide text-slate-400 font-medium mb-1';
const FIELD_INPUT =
  'w-full px-3 py-2 text-sm rounded-lg border border-slate-300 focus:outline-none focus:ring-2 focus:ring-emerald-500/40';

// --- Chip input -------------------------------------------------------------
// A controlled string[] editor: type a value and press Enter or comma to commit
// it as a removable chip; Backspace on an empty field removes the last chip.
// Used for both aws_services and tags — the client encodes them (repeated form
// fields on upload, JSON arrays on patch), so this only produces string[].
function ChipInput({
  id,
  values,
  onChange,
  disabled,
  placeholder,
  chipClass,
}: {
  id: string;
  values: string[];
  onChange: (next: string[]) => void;
  disabled: boolean;
  placeholder: string;
  chipClass: string;
}) {
  const [draft, setDraft] = useState('');

  const commit = useCallback(
    (raw: string) => {
      const token = raw.trim();
      if (!token) return;
      // Dedupe case-insensitively; keep the first-seen casing.
      if (!values.some((v) => v.toLowerCase() === token.toLowerCase())) {
        onChange([...values, token]);
      }
      setDraft('');
    },
    [values, onChange],
  );

  const handleKeyDown = useCallback(
    (e: React.KeyboardEvent<HTMLInputElement>) => {
      if (e.key === 'Enter' || e.key === ',') {
        e.preventDefault();
        commit(draft);
      } else if (e.key === 'Backspace' && draft.length === 0 && values.length > 0) {
        onChange(values.slice(0, -1));
      }
    },
    [commit, draft, values, onChange],
  );

  return (
    <div
      className={`w-full min-h-[2.5rem] flex flex-wrap items-center gap-1.5 px-2 py-1.5 rounded-lg border border-slate-300 focus-within:ring-2 focus-within:ring-emerald-500/40 ${
        disabled ? 'opacity-40' : ''
      }`}
    >
      {values.map((v) => (
        <span
          key={v}
          className={`inline-flex items-center gap-1 text-[11px] font-medium px-2 py-0.5 rounded-full ${chipClass}`}
        >
          {v}
          <button
            type="button"
            onClick={() => onChange(values.filter((x) => x !== v))}
            disabled={disabled}
            aria-label={`Remove ${v}`}
            className="leading-none hover:text-rose-600 transition-colors"
          >
            ×
          </button>
        </span>
      ))}
      <input
        id={id}
        type="text"
        value={draft}
        onChange={(e) => setDraft(e.target.value)}
        onKeyDown={handleKeyDown}
        onBlur={() => commit(draft)}
        disabled={disabled}
        placeholder={values.length === 0 ? placeholder : ''}
        className="flex-1 min-w-[6rem] text-sm bg-transparent focus:outline-none"
        autoComplete="off"
      />
    </div>
  );
}

// --- Upload-template modal --------------------------------------------------
// Collects the `.zip` + name + description + (read-only) framework + aws_services
// + tags, then POSTs via githubTemplatesApi.upload. Mirrors AddConnectionModal
// mechanics (mountedRef guard, actionPending, canSubmit, inline <p role="alert">).
// Exported for the standalone Templates page (E28/T9) — one upload dialog, both call
// sites. Unchanged otherwise.
export function UploadTemplateModal({
  orgLabel,
  onSubmit,
  onClose,
}: {
  orgLabel: string;
  onSubmit: (
    file: File,
    meta: { name: string; framework: string; description?: string; aws_services?: string[]; tags?: string[] },
  ) => Promise<void>;
  onClose: () => void;
}) {
  const [file, setFile] = useState<File | null>(null);
  const [name, setName] = useState('');
  const [description, setDescription] = useState('');
  const [awsServices, setAwsServices] = useState<string[]>([]);
  const [tags, setTags] = useState<string[]>([]);

  const [actionPending, setActionPending] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);

  const nameRef = useRef<HTMLInputElement>(null);

  const mountedRef = useRef(true);
  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
    };
  }, []);

  useEffect(() => {
    nameRef.current?.focus();
  }, []);

  const canSubmit = name.trim().length > 0 && file !== null;

  const handleSubmit = useCallback(async () => {
    if (actionPending || !canSubmit || !file) return;
    setActionPending(true);
    setActionError(null);
    try {
      await onSubmit(file, {
        name: name.trim(),
        framework: FRAMEWORK_DEFAULT,
        description: description.trim(),
        aws_services: awsServices,
        tags,
      });
      // Parent closes the modal + refetches on success.
    } catch (err: unknown) {
      if (mountedRef.current) {
        setActionError(err instanceof Error ? err.message : 'Upload failed.');
      }
    } finally {
      if (mountedRef.current) setActionPending(false);
    }
  }, [actionPending, canSubmit, file, name, description, awsServices, tags, onSubmit]);

  return (
    <ModalShell
      title="Upload template"
      description={`Create a new GitHub template repository in ${orgLabel}. The uploaded .zip becomes the repo's contents.`}
      ariaLabel="Upload template"
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
            Cancel
          </button>
          <button
            type="button"
            onClick={handleSubmit}
            disabled={actionPending || !canSubmit}
            className="px-3.5 py-1.5 rounded-lg bg-emerald-600 text-white text-sm font-medium hover:bg-emerald-700 transition-colors disabled:opacity-40"
          >
            {actionPending ? 'Uploading…' : 'Upload template'}
          </button>
        </>
      }
    >
      <div>
        <label htmlFor="upload-tmpl-name" className={FIELD_LABEL}>
          Name
        </label>
        <input
          id="upload-tmpl-name"
          ref={nameRef}
          type="text"
          value={name}
          onChange={(e) => setName(e.target.value)}
          disabled={actionPending}
          placeholder="my-agent-template"
          className={`${FIELD_INPUT} disabled:opacity-40`}
          autoComplete="off"
        />
        <p className="text-[11px] text-slate-400 mt-1">
          Becomes the repository name in the org — it can’t be changed later.
        </p>
      </div>

      <div>
        <label htmlFor="upload-tmpl-file" className={FIELD_LABEL}>
          Scaffold archive (.zip)
        </label>
        <input
          id="upload-tmpl-file"
          type="file"
          accept=".zip"
          onChange={(e) => setFile(e.target.files?.[0] ?? null)}
          disabled={actionPending}
          className="w-full text-sm text-slate-600 file:mr-3 file:py-1.5 file:px-3 file:rounded-lg file:border-0 file:text-sm file:font-medium file:bg-emerald-50 file:text-emerald-700 hover:file:bg-emerald-100 disabled:opacity-40"
        />
        {file && <p className="text-[11px] text-slate-400 mt-1 truncate">{file.name}</p>}
      </div>

      <div>
        <label htmlFor="upload-tmpl-framework" className={FIELD_LABEL}>
          Framework
        </label>
        <input
          id="upload-tmpl-framework"
          type="text"
          value={FRAMEWORK_DEFAULT}
          readOnly
          disabled
          className={`${FIELD_INPUT} bg-slate-50 text-slate-500`}
        />
      </div>

      <div>
        <label htmlFor="upload-tmpl-desc" className={FIELD_LABEL}>
          Description
        </label>
        <textarea
          id="upload-tmpl-desc"
          value={description}
          onChange={(e) => setDescription(e.target.value)}
          disabled={actionPending}
          rows={3}
          className={`${FIELD_INPUT} disabled:opacity-40`}
        />
      </div>

      <div>
        <label htmlFor="upload-tmpl-services" className={FIELD_LABEL}>
          AWS services <span className="text-slate-300 normal-case">(Enter to add)</span>
        </label>
        <ChipInput
          id="upload-tmpl-services"
          values={awsServices}
          onChange={setAwsServices}
          disabled={actionPending}
          placeholder="Bedrock, Lambda, DynamoDB"
          chipClass={SERVICE_CHIP}
        />
      </div>

      <div>
        <label htmlFor="upload-tmpl-tags" className={FIELD_LABEL}>
          Tags <span className="text-slate-300 normal-case">(Enter to add)</span>
        </label>
        <ChipInput
          id="upload-tmpl-tags"
          values={tags}
          onChange={setTags}
          disabled={actionPending}
          placeholder="starter, chat, rag"
          chipClass={TAG_PILL}
        />
      </div>

      {actionError && (
        <p className="text-sm text-rose-600" role="alert">
          {actionError}
        </p>
      )}
    </ModalShell>
  );
}

// --- Edit-template modal ----------------------------------------------------
// Edits the mutable display metadata (description / aws_services / tags) via
// githubTemplatesApi.patch. The name and framework are fixed at upload time, so
// they're shown read-only. Same mechanics as UploadTemplateModal.
// Exported for the standalone Templates page (E28/T9) — one edit dialog, both call sites.
export function EditTemplateModal({
  template,
  onSubmit,
  onClose,
}: {
  template: TemplateView;
  onSubmit: (patch: TemplatePatch) => Promise<void>;
  onClose: () => void;
}) {
  const [description, setDescription] = useState(template.description);
  const [awsServices, setAwsServices] = useState<string[]>(template.aws_services);
  const [tags, setTags] = useState<string[]>(template.tags);

  const [actionPending, setActionPending] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);

  const descRef = useRef<HTMLTextAreaElement>(null);

  const mountedRef = useRef(true);
  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
    };
  }, []);

  useEffect(() => {
    descRef.current?.focus();
  }, []);

  const handleSubmit = useCallback(async () => {
    if (actionPending) return;
    setActionPending(true);
    setActionError(null);
    try {
      await onSubmit({
        description: description.trim(),
        aws_services: awsServices,
        tags,
      });
      // Parent closes the modal + refetches on success.
    } catch (err: unknown) {
      if (mountedRef.current) {
        setActionError(err instanceof Error ? err.message : 'Failed to save template.');
      }
    } finally {
      if (mountedRef.current) setActionPending(false);
    }
  }, [actionPending, description, awsServices, tags, onSubmit]);

  return (
    <ModalShell
      title={`Edit template — ${template.name}`}
      description="Edit the template's display metadata. The repository name and framework are fixed at upload time."
      ariaLabel="Edit template"
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
            Cancel
          </button>
          <button
            type="button"
            onClick={handleSubmit}
            disabled={actionPending}
            className="px-3.5 py-1.5 rounded-lg bg-emerald-600 text-white text-sm font-medium hover:bg-emerald-700 transition-colors disabled:opacity-40"
          >
            {actionPending ? 'Saving…' : 'Save changes'}
          </button>
        </>
      }
    >
      <div>
        <label htmlFor="edit-tmpl-desc" className={FIELD_LABEL}>
          Description
        </label>
        <textarea
          id="edit-tmpl-desc"
          ref={descRef}
          value={description}
          onChange={(e) => setDescription(e.target.value)}
          disabled={actionPending}
          rows={3}
          className={`${FIELD_INPUT} disabled:opacity-40`}
        />
      </div>

      <div>
        <label htmlFor="edit-tmpl-services" className={FIELD_LABEL}>
          AWS services <span className="text-slate-300 normal-case">(Enter to add)</span>
        </label>
        <ChipInput
          id="edit-tmpl-services"
          values={awsServices}
          onChange={setAwsServices}
          disabled={actionPending}
          placeholder="Bedrock, Lambda, DynamoDB"
          chipClass={SERVICE_CHIP}
        />
      </div>

      <div>
        <label htmlFor="edit-tmpl-tags" className={FIELD_LABEL}>
          Tags <span className="text-slate-300 normal-case">(Enter to add)</span>
        </label>
        <ChipInput
          id="edit-tmpl-tags"
          values={tags}
          onChange={setTags}
          disabled={actionPending}
          placeholder="starter, chat, rag"
          chipClass={TAG_PILL}
        />
      </div>

      {actionError && (
        <p className="text-sm text-rose-600" role="alert">
          {actionError}
        </p>
      )}
    </ModalShell>
  );
}
