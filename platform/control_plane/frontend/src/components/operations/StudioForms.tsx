import { useRef, useState, type JSX, type ReactNode } from 'react';

import { OPS_BADGE } from './opsUi';
import {
  MCP_DESCRIPTION_MAX,
  SKILL_NO_FRONTMATTER,
  agentNameProblem,
  defaultExposed,
  mcpNameProblem,
  parseSkill,
  secretName,
  skillFile,
  validateSpec,
  withFrontmatter,
  type AgentDraft,
  type BlockKind,
  type McpDraft,
  type McpOption,
  type SkillFile,
  type SpecFile,
} from './studioModel';
import { ICON, KIND_TILE } from './studioStyles';

const FIELD_LABEL = 'text-[11px] uppercase tracking-wide text-slate-400 font-medium';
const FIELD_INPUT =
  'w-full px-3 py-2 text-sm rounded-lg border bg-white focus:outline-none focus:ring-2 focus:ring-emerald-500/40 disabled:opacity-40';
const PILL = 'inline-flex items-center gap-1.5 text-[11px] font-semibold px-2 py-0.5 rounded-full';
const LINK_BTN = 'text-xs font-medium text-emerald-700 hover:underline';
const SECTION = 'pt-5 mt-5 border-t border-emerald-100/70 first:pt-0 first:mt-0 first:border-t-0';

const inputTone = (error: boolean) => (error ? 'border-rose-300' : 'border-slate-300');

function Field(props: {
  label: string;
  htmlFor?: string;
  hint?: ReactNode;
  error?: string | null;
  aside?: ReactNode;
  children: ReactNode;
}): JSX.Element {
  return (
    <div>
      <div className="flex items-center justify-between gap-2 mb-1 min-h-[18px]">
        <label htmlFor={props.htmlFor} className={FIELD_LABEL}>
          {props.label}
        </label>
        {props.aside}
      </div>
      {props.children}
      {props.error ? (
        <p className="text-[11px] text-rose-600 mt-1">{props.error}</p>
      ) : (
        props.hint && <p className="text-[11px] text-slate-400 mt-1">{props.hint}</p>
      )}
    </div>
  );
}

function SectionHead(props: { title: string; blurb?: string; aside?: ReactNode }): JSX.Element {
  return (
    <div className="flex items-start justify-between gap-3 mb-3">
      <div>
        <h3 className="text-sm font-semibold text-slate-800">{props.title}</h3>
        {props.blurb && <p className="text-xs text-slate-500 mt-0.5 max-w-2xl">{props.blurb}</p>}
      </div>
      {props.aside}
    </div>
  );
}

export interface ReadFile {
  name: string;
  text: string;
  size: number;
}

export function DropZone(props: {
  label: string;
  accept: string;
  multiple?: boolean;
  compact?: boolean;
  onFiles: (files: ReadFile[]) => void;
  children?: ReactNode;
}): JSX.Element {
  const input = useRef<HTMLInputElement>(null);
  const [over, setOver] = useState(false);
  const read = async (list: FileList | null) => {
    if (!list || list.length === 0) return;
    const files = await Promise.all(
      [...list].map(async (f) => ({ name: f.name, text: await f.text(), size: f.size })),
    );
    props.onFiles(files);
  };
  return (
    <div
      onDragOver={(e) => {
        e.preventDefault();
        setOver(true);
      }}
      onDragLeave={() => setOver(false)}
      onDrop={(e) => {
        e.preventDefault();
        setOver(false);
        void read(e.dataTransfer.files);
      }}
      className={`rounded-lg border border-dashed transition-colors ${
        over ? 'border-emerald-400 bg-emerald-50/60' : 'border-slate-300 bg-white/60'
      } ${props.compact ? 'px-3 py-2' : 'px-4 py-4'}`}
    >
      <div className={`flex ${props.compact ? 'items-center' : 'flex-col items-center text-center'} gap-2`}>
        <svg className="w-5 h-5 text-slate-300 shrink-0" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={1.5} aria-hidden="true">
          <path strokeLinecap="round" strokeLinejoin="round" d="M3 16.5v2.25A2.25 2.25 0 005.25 21h13.5A2.25 2.25 0 0021 18.75V16.5M16.5 12L12 16.5m0 0L7.5 12m4.5 4.5V3" />
        </svg>
        <p className="text-xs text-slate-500">
          {props.label} or{' '}
          <button type="button" onClick={() => input.current?.click()} className={LINK_BTN}>
            browse
          </button>
        </p>
        {props.children && <div className={`flex flex-wrap gap-x-3 gap-y-1 ${props.compact ? 'ml-auto' : ''}`}>{props.children}</div>}
      </div>
      <input
        ref={input}
        type="file"
        accept={props.accept}
        multiple={props.multiple}
        className="hidden"
        aria-label={props.label}
        onChange={(e) => {
          void read(e.target.files);
          e.target.value = '';
        }}
      />
    </div>
  );
}

function CheckIcon({ ok }: { ok: boolean }): JSX.Element {
  return ok ? (
    <span className="shrink-0 inline-flex items-center justify-center w-4 h-4 rounded-full bg-emerald-100 text-emerald-700 mt-0.5">
      <svg className="w-2.5 h-2.5" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={4} aria-hidden="true">
        <path strokeLinecap="round" strokeLinejoin="round" d="M5 13l4 4L19 7" />
      </svg>
    </span>
  ) : (
    <span className="shrink-0 inline-flex items-center justify-center w-4 h-4 rounded-full bg-rose-100 text-rose-700 mt-0.5">
      <svg className="w-2.5 h-2.5" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={4} aria-hidden="true">
        <path strokeLinecap="round" strokeLinejoin="round" d="M6 18L18 6M6 6l12 12" />
      </svg>
    </span>
  );
}

const METHOD_TONE: Record<string, string> = {
  get: 'bg-sky-50 text-sky-700',
  post: 'bg-emerald-50 text-emerald-700',
  put: 'bg-amber-50 text-amber-700',
  patch: 'bg-amber-50 text-amber-700',
  delete: 'bg-rose-50 text-rose-700',
};

export function McpServerForm(props: { id: string; draft: McpDraft; onChange: (patch: Partial<McpDraft>) => void }): JSX.Element {
  const { id: bid, draft, onChange } = props;
  const report = draft.spec ? validateSpec(draft.spec.text, draft.spec.size, draft.spec.fileName) : null;
  const nameErr = draft.name ? mcpNameProblem(draft.name) : null;
  const tooLong = draft.description.length > MCP_DESCRIPTION_MAX;
  const exposable = report?.operations.filter((o) => o.id) ?? [];

  const setSpec = (spec: SpecFile) => {
    const ids = defaultExposed(spec);
    const keep = draft.exposed.filter((id) => ids.includes(id));
    onChange({ spec, exposed: keep.length ? keep : ids });
  };

  return (
    <div>
      <section className={SECTION}>
        <SectionHead title="Server" blurb="How the registry lists it." />
        <div className="grid grid-cols-1 md:grid-cols-[16rem_1fr] gap-4">
          <Field label="Name" htmlFor={`${bid}-name`} error={nameErr} hint="Also the repository name.">
            <input
              id={`${bid}-name`}
              value={draft.name}
              onChange={(e) => onChange({ name: e.target.value })}
              placeholder="fnol-claims"
              className={`${FIELD_INPUT} font-mono ${inputTone(Boolean(nameErr))}`}
            />
          </Field>
          <div>
            <Field
              label="Description"
              htmlFor={`${bid}-desc`}
              error={tooLong ? `The registry caps this at ${MCP_DESCRIPTION_MAX} characters.` : null}
              hint={`Max ${MCP_DESCRIPTION_MAX} characters. Shown on the registry and marketplace card.`}
              aside={
                <span className={`text-[11px] tabular-nums ${tooLong ? 'text-rose-600 font-semibold' : 'text-slate-400'}`}>
                  {draft.description.length}/{MCP_DESCRIPTION_MAX}
                </span>
              }
            >
              <input
                id={`${bid}-desc`}
                value={draft.description}
                onChange={(e) => onChange({ description: e.target.value })}
                placeholder="What the API does, in one line"
                className={`${FIELD_INPUT} ${inputTone(tooLong)}`}
              />
            </Field>
          </div>
        </div>
      </section>

      <section className={SECTION}>
        <SectionHead
          title="OpenAPI spec"
          blurb="The gateway turns each operation into a tool. YAML or JSON, checked here before anything is committed."
        />
        {!draft.spec ? (
          <DropZone
            label="Drop openapi.yaml or .json"
            accept=".yaml,.yml,.json"
            onFiles={([f]) => setSpec({ fileName: f.name, text: f.text, size: f.size })}
          />
        ) : (
          <div className="rounded-lg border border-slate-200 bg-white/70">
            <div className="flex items-center justify-between gap-3 px-3 py-2 border-b border-slate-100">
              <div className="flex items-center gap-2 min-w-0">
                <span className="font-mono text-xs text-slate-700 truncate">{draft.spec.fileName}</span>
                <span className="text-[11px] text-slate-400 tabular-nums">{(draft.spec.size / 1024).toFixed(1)} KB</span>
              </div>
              <div className="flex items-center gap-3">
                <button type="button" className={LINK_BTN} onClick={() => onChange({ spec: null, exposed: [] })}>
                  Replace
                </button>
              </div>
            </div>
            {report?.parseError ? (
              <p className="px-3 py-2 text-xs text-rose-700">Could not parse the file: {report.parseError}</p>
            ) : (
              <ul className="grid grid-cols-1 md:grid-cols-2 gap-x-6 gap-y-2 px-3 py-3" aria-label="Spec checks">
                {report?.checks.map((c) => (
                  <li key={c.key} className="flex items-start gap-2">
                    <CheckIcon ok={c.ok} />
                    <div className="min-w-0">
                      <p className={`text-xs ${c.ok ? 'text-slate-700' : 'text-rose-700 font-medium'}`}>{c.label}</p>
                      {c.detail && <p className={`text-[11px] break-words ${c.ok ? 'text-slate-400' : 'text-rose-600'}`}>{c.detail}</p>}
                    </div>
                  </li>
                ))}
              </ul>
            )}
          </div>
        )}
      </section>

      {report && !report.parseError && (
        <section className={SECTION}>
          <SectionHead
            title="Operations to expose"
            blurb="Unticked operations are removed from the spec before it is committed, so the gateway never offers them as tools. Expose only what the agent needs."
            aside={
              <span className="text-[11px] text-slate-400 tabular-nums shrink-0">
                {draft.exposed.length} of {exposable.length} exposed
              </span>
            }
          />
          <ul className="rounded-lg border border-slate-200 bg-white/70 divide-y divide-slate-100">
            {report.operations.map((o) => {
              const id = o.id;
              const checked = Boolean(id && draft.exposed.includes(id));
              return (
                <li key={`${o.method} ${o.path}`}>
                  <label className={`flex items-center gap-3 px-3 py-2 ${id ? 'cursor-pointer hover:bg-emerald-50/40' : 'opacity-60'}`}>
                    <input
                      type="checkbox"
                      disabled={!id}
                      checked={checked}
                      onChange={() =>
                        id &&
                        onChange({ exposed: checked ? draft.exposed.filter((x) => x !== id) : [...draft.exposed, id] })
                      }
                      className="h-4 w-4 accent-emerald-600"
                    />
                    <span className="font-mono text-xs font-semibold text-slate-800 w-36 truncate">{id ?? 'no operationId'}</span>
                    <span className={`${PILL} uppercase ${METHOD_TONE[o.method] ?? 'bg-slate-100 text-slate-600'}`}>{o.method}</span>
                    <span className="font-mono text-[11px] text-slate-500 truncate">{o.path}</span>
                    <span className="ml-auto text-xs text-slate-400 truncate hidden md:inline">{o.summary}</span>
                  </label>
                </li>
              );
            })}
          </ul>
        </section>
      )}

      <section className={SECTION}>
        <SectionHead
          title="Authentication"
          blurb="How the gateway authenticates to the API. A key is stored in AWS Secrets Manager and injected by the gateway, never written to the repository, never filled by the AI."
        />
        <div role="radiogroup" aria-label="Authentication" className="inline-flex rounded-lg border border-slate-200 bg-white p-0.5 mb-3">
          {(
            [
              ['none', 'None'],
              ['new', 'New API key'],
              ['existing', 'Use existing secret'],
            ] as const
          ).map(([mode, label]) => (
            <button
              key={mode}
              type="button"
              role="radio"
              aria-checked={draft.keyMode === mode}
              onClick={() => onChange({ keyMode: mode })}
              className={`px-3 py-1 text-xs font-medium rounded-md transition-colors ${
                draft.keyMode === mode ? 'bg-emerald-600 text-white' : 'text-slate-600 hover:bg-slate-50'
              }`}
            >
              {label}
            </button>
          ))}
        </div>
        <div className="max-w-md">
          {draft.keyMode === 'none' ? (
            <p role="note" className="flex items-start gap-2 rounded-lg bg-amber-50/70 border border-amber-200/70 px-3 py-2 text-xs text-amber-800">
              <svg className="w-4 h-4 shrink-0 text-amber-500" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={1.5} aria-hidden="true">
                <path strokeLinecap="round" strokeLinejoin="round" d="M11.25 11.25l.041-.02a.75.75 0 011.063.852l-.708 2.836a.75.75 0 001.063.853l.041-.021M21 12a9 9 0 11-18 0 9 9 0 0118 0zm-9-3.75h.008v.008H12V8.25z" />
              </svg>
              The gateway calls the API without credentials. Use only for public endpoints, not for production.
            </p>
          ) : draft.keyMode === 'new' ? (
            <Field label="API key" htmlFor={`${bid}-key`} hint={`Create stores it as ${secretName(draft.name)}. Kept in this tab only until then.`}>
              <input
                id={`${bid}-key`}
                type="password"
                autoComplete="off"
                value={draft.apiKey}
                onChange={(e) => onChange({ apiKey: e.target.value })}
                placeholder="Paste the key"
                className={`${FIELD_INPUT} font-mono ${inputTone(false)}`}
              />
            </Field>
          ) : (
            <Field
              label="Secret ARN"
              htmlFor={`${bid}-secret`}
              hint={`A Secrets Manager ARN under agp-mcp/${draft.name || '<name>'}/, with the key under api_key.`}
            >
              <input
                id={`${bid}-secret`}
                value={draft.secretArn}
                onChange={(e) => onChange({ secretArn: e.target.value.trim() })}
                placeholder={`arn:aws:secretsmanager:<region>:<account>:secret:agp-mcp/${draft.name || '<name>'}/api-key-…`}
                className={`${FIELD_INPUT} font-mono ${inputTone(false)}`}
              />
            </Field>
          )}
        </div>
      </section>
    </div>
  );
}

const SKILL_PILL: Record<string, { cls: string; label: string }> = {
  valid: { cls: OPS_BADGE.ready, label: 'valid' },
  missing_frontmatter: { cls: OPS_BADGE.warn, label: 'needs frontmatter' },
  invalid: { cls: OPS_BADGE.failed, label: 'invalid' },
};

function SkillRow(props: { skill: SkillFile; onFix: (text: string) => void; onRemove: () => void }): JSX.Element {
  const r = parseSkill(props.skill.text);
  const [name, setName] = useState(r.name);
  const [desc, setDesc] = useState(r.description);
  const pill = SKILL_PILL[r.status];
  return (
    <li className="px-3 py-2.5">
      <div className="flex items-start gap-3">
        <span className={`${PILL} ${pill.cls} shrink-0 mt-0.5`}>{pill.label}</span>
        <div className="min-w-0 flex-1">
          <p className="text-sm font-semibold text-slate-800 font-mono">{r.name || 'unnamed'}</p>
          <p className="text-xs text-slate-500">{r.description || r.problem}</p>
          <p className="text-[11px] text-slate-400 font-mono mt-0.5">skills/{r.name || '<name>'}/SKILL.md</p>
        </div>
        <button type="button" onClick={props.onRemove} aria-label={`Remove skill ${r.name || props.skill.fileName}`} className="text-slate-400 hover:text-slate-700 text-base leading-none px-1">
          ×
        </button>
      </div>
      {r.status !== 'valid' && (
        <div className="mt-2 ml-0 md:ml-[7.5rem] rounded-lg bg-amber-50/60 border border-amber-200/70 p-3">
          <p className="text-[11px] text-amber-800 mb-2">
            {r.problem} Agent Skills need a <span className="font-mono">name</span> (lowercase, hyphens) and a{' '}
            <span className="font-mono">description</span> in the frontmatter.
          </p>
          <div className="grid grid-cols-1 md:grid-cols-[12rem_1fr_auto] gap-2 items-end">
            <input aria-label="Skill name" value={name} onChange={(e) => setName(e.target.value)} placeholder="claimant-tone" className={`${FIELD_INPUT} border-slate-300 font-mono`} />
            <input aria-label="Skill description" value={desc} onChange={(e) => setDesc(e.target.value)} placeholder="When to use this skill" className={`${FIELD_INPUT} border-slate-300`} />
            <button
              type="button"
              disabled={!name || !desc}
              onClick={() => props.onFix(withFrontmatter(props.skill.text, name.trim(), desc.trim()))}
              className="px-3 py-2 rounded-lg bg-white border border-slate-300 text-slate-700 text-xs font-medium hover:bg-slate-50 disabled:opacity-40"
            >
              Add frontmatter
            </button>
          </div>
        </div>
      )}
    </li>
  );
}

export function AgentForm(props: {
  id: string;
  draft: AgentDraft;
  options: McpOption[];
  onChange: (patch: Partial<AgentDraft>) => void;
}): JSX.Element {
  const { id: bid, draft, options, onChange } = props;
  const nameErr = draft.name ? agentNameProblem(draft.name) : null;
  const suggestion = draft.name.replace(/-/g, '_');
  const addSkills = (files: ReadFile[]) => onChange({ skills: [...draft.skills, ...files.map((f) => skillFile(f.name, f.text))] });

  return (
    <div>
      <section className={SECTION}>
        <SectionHead title="Agent" blurb="A Strands agent, built from the Strands template in its own repository." />
        <div className="grid grid-cols-1 md:grid-cols-[16rem_auto] gap-4">
          <Field
            label="Name"
            htmlFor={`${bid}-name`}
            error={nameErr}
            hint="Underscores, not hyphens. It becomes the runtime and repository name."
            aside={
              nameErr && draft.name.includes('-') && !agentNameProblem(suggestion) ? (
                <button type="button" className={LINK_BTN} onClick={() => onChange({ name: suggestion })}>
                  Use {suggestion}
                </button>
              ) : undefined
            }
          >
            <input
              id={`${bid}-name`}
              value={draft.name}
              onChange={(e) => onChange({ name: e.target.value })}
              placeholder="fnol_intake_agent"
              className={`${FIELD_INPUT} font-mono ${inputTone(Boolean(nameErr))}`}
            />
          </Field>
          <Field label="Template" hint="Fixed. Studio composes Strands agents.">
            <div className="flex items-center h-[38px]">
              <span className={`${PILL} bg-slate-100 text-slate-700 border border-slate-200`}>
                <svg className="w-3 h-3 text-slate-400" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2} aria-hidden="true">
                  <path strokeLinecap="round" strokeLinejoin="round" d="M16.5 10.5V6.75a4.5 4.5 0 10-9 0v3.75m-.75 11.25h10.5a2.25 2.25 0 002.25-2.25v-6.75a2.25 2.25 0 00-2.25-2.25H6.75a2.25 2.25 0 00-2.25 2.25v6.75a2.25 2.25 0 002.25 2.25z" />
                </svg>
                Strands
              </span>
            </div>
          </Field>
        </div>
      </section>

      <section className={SECTION}>
        <SectionHead title="System prompt" blurb="Committed as prompt/system.md." />
        <textarea
          aria-label="System prompt"
          value={draft.prompt}
          onChange={(e) => onChange({ prompt: e.target.value })}
          rows={9}
          placeholder="You are…"
          className={`${FIELD_INPUT} font-mono text-[13px] leading-relaxed ${inputTone(false)}`}
        />
        <div className="mt-2">
          <DropZone compact label="Drop a prompt.md to replace the text" accept=".md,.txt" onFiles={([f]) => onChange({ prompt: f.text })} />
        </div>
      </section>

      <section className={SECTION}>
        <SectionHead
          title="Skills"
          blurb="Agent Skills format: each SKILL.md needs frontmatter with a name and a description, and lands at skills/<name>/SKILL.md."
        />
        {draft.skills.length > 0 && (
          <ul className="rounded-lg border border-slate-200 bg-white/70 divide-y divide-slate-100 mb-2">
            {draft.skills.map((s) => (
              <SkillRow
                key={s.id}
                skill={s}
                onFix={(text) => onChange({ skills: draft.skills.map((x) => (x.id === s.id ? { ...x, text } : x)) })}
                onRemove={() => onChange({ skills: draft.skills.filter((x) => x.id !== s.id) })}
              />
            ))}
          </ul>
        )}
        <DropZone compact multiple label="Drop one or more SKILL.md files" accept=".md" onFiles={addSkills}>
          <button type="button" className="text-xs font-medium text-slate-500 hover:underline" onClick={() => addSkills([{ name: 'SKILL.md', text: SKILL_NO_FRONTMATTER, size: 0 }])}>
            Add a sample without frontmatter
          </button>
        </DropZone>
      </section>

      <section className={SECTION}>
        <SectionHead title="MCP servers" blurb="Servers this agent is meant to call: the ones in this composition, or existing ones from the registry." />
        <div className="grid grid-cols-1 md:grid-cols-2 gap-2">
          {options.map((m) => {
            const on = draft.mcpIds.includes(m.id);
            return (
              <label
                key={m.id}
                className={`flex items-start gap-3 rounded-lg border p-3 cursor-pointer transition-colors ${
                  on ? 'border-emerald-400 bg-emerald-50/60' : 'border-slate-200 bg-white/70 hover:border-emerald-300'
                }`}
              >
                <input
                  type="checkbox"
                  checked={on}
                  onChange={() => onChange({ mcpIds: on ? draft.mcpIds.filter((x) => x !== m.id) : [...draft.mcpIds, m.id] })}
                  className="h-4 w-4 mt-0.5 accent-emerald-600"
                />
                <div className="min-w-0 flex-1">
                  <div className="flex items-center gap-2 flex-wrap">
                    <span className="font-mono text-sm font-semibold text-slate-800">{m.name || 'unnamed MCP server'}</span>
                    <span className={`${PILL} ${m.status === 'new' ? 'bg-sky-50 text-sky-700' : OPS_BADGE.ready}`}>
                      <span aria-hidden="true">●</span>
                      {m.status === 'new' ? 'will be created' : 'provisioned'}
                    </span>
                    {m.status === 'new' && <span className="text-[11px] text-slate-400">in this composition</span>}
                  </div>
                  <div className="flex flex-wrap gap-1 mt-1.5">
                    {m.tools.length === 0 ? (
                      <span className="text-[11px] text-slate-400">No tools exposed yet</span>
                    ) : (
                      m.tools.map((t) => (
                        <span key={t} className="font-mono text-[11px] px-1.5 py-0.5 rounded bg-slate-100 text-slate-600">
                          {t}
                        </span>
                      ))
                    )}
                  </div>
                </div>
              </label>
            );
          })}
        </div>
        <div className="mt-3 flex items-start gap-2 rounded-lg bg-slate-50 border border-slate-200 px-3 py-2">
          <svg className="w-4 h-4 text-slate-400 shrink-0 mt-0.5" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={1.5} aria-hidden="true">
            <path strokeLinecap="round" strokeLinejoin="round" d="M9 12.75L11.25 15 15 9.75m-3-7.036A11.959 11.959 0 013.598 6 11.99 11.99 0 003 9.749c0 5.592 3.824 10.29 9 11.623 5.176-1.332 9-6.03 9-11.622 0-1.31-.21-2.571-.598-3.751h-.152c-3.196 0-6.1-1.248-8.25-3.285z" />
          </svg>
          <p className="text-xs text-slate-600">
            Picking a server declares it on the agent, so the runtime is wired to it. The agent can call it only after access is
            granted in Governance. <span className="font-semibold">Studio never grants access.</span>
          </p>
        </div>
      </section>
    </div>
  );
}

export function Spinner({ className = 'w-3.5 h-3.5' }: { className?: string }): JSX.Element {
  return (
    <svg className={`${className} animate-spin`} viewBox="0 0 24 24" fill="none" aria-hidden="true">
      <circle cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="3" strokeDasharray="40 60" />
    </svg>
  );
}

export function KindIcon({ kind, size = 'w-8 h-8' }: { kind: BlockKind; size?: string }): JSX.Element {
  return (
    <span className={`shrink-0 inline-flex items-center justify-center rounded-lg ${size} ${KIND_TILE[kind]}`}>
      <svg className="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={1.5} aria-hidden="true">
        <path strokeLinecap="round" strokeLinejoin="round" d={ICON[kind]} />
      </svg>
    </span>
  );
}

export function FileChip(props: { name: string; onRemove?: () => void }): JSX.Element {
  return (
    <span className="inline-flex items-center gap-1.5 font-mono text-[11px] pl-2 pr-1 py-0.5 rounded-md bg-slate-100 text-slate-600">
      {props.name}
      {props.onRemove && (
        <button type="button" aria-label={`Remove ${props.name}`} onClick={props.onRemove} className="text-slate-400 hover:text-slate-700 px-0.5">
          ×
        </button>
      )}
    </span>
  );
}
