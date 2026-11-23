// Rationale: frontend/README.md "Tests are node-environment only, by design"

import type {
  DatasetItemInput,
  EvalConfigOutput,
  InvokeResult,
  TraceSummary,
} from '../../../api/client';
import {
  isApiFailure,
} from '../../../api/failure';
import {
  ARGS_MODE_OPTIONS,
  ITEM_ID_RE,
  MAX_ITEMS,
  TOOL_MODE_OPTIONS,
  classifyCommitFailure,
  itemFromForm,
  latencyText,
  type ItemForm,
} from './evaluationsLogic';

export const TRACE_STAGES = [
  { value: 'dev', label: 'Dev' },
  { value: 'prod', label: 'Prod' },
] as const;
export type TraceStage = (typeof TRACE_STAGES)[number]['value'];
export const DEFAULT_TRACE_STAGE: TraceStage = TRACE_STAGES[0].value;
export const TRACE_LIMIT = 50;
const INPUT_PREVIEW_CHARS = 140;

export function selectable(trace: TraceSummary): boolean {
  return Boolean(trace.input?.trim());
}

export function whenText(iso: string | null, now: number): string | null {
  if (!iso) return null;
  const t = Date.parse(iso);
  if (Number.isNaN(t)) return null;
  const mins = Math.floor((now - t) / 60000);
  if (mins < 1) return 'just now';
  if (mins < 60) return `${mins} min ago`;
  if (mins < 24 * 60) return `${Math.floor(mins / 60)} h ago`;
  return new Date(t).toLocaleString(undefined, { day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit' });
}

function oneLine(text: string): string {
  return text.replace(/\s+/g, ' ').trim();
}

export function truncate(text: string, max: number): string {
  return text.length <= max ? text : `${text.slice(0, max - 1).trimEnd()}…`;
}

export interface TraceRowView {
  when: string | null;
  title: string;
  input: string | null;
  tools: string[] | null;
  latency: string | null;
}

export function traceRowView(trace: TraceSummary, now: number): TraceRowView {
  const started = trace.started_at ? new Date(trace.started_at) : null;
  const at = started && !Number.isNaN(started.getTime()) ? started.toLocaleString() : 'start time unknown';
  return {
    when: whenText(trace.started_at, now),
    title: `${at} · session ${trace.session_id}`,
    input: trace.input?.trim() ? truncate(oneLine(trace.input), INPUT_PREVIEW_CHARS) : null,
    tools: trace.tool_calls === null ? null : trace.tool_calls.map((c) => c.name),
    latency: latencyText(trace.latency_ms),
  };
}

export interface InvokeRun {
  prompt: string;
  response: unknown;
  toolCalls: InvokeResult['tool_calls'] | undefined;
  sessionId: string | null | undefined;
}

const isPlainObject = (v: unknown): v is Record<string, unknown> =>
  typeof v === 'object' && v !== null && !Array.isArray(v);

export function traceFromInvoke(run: InvokeRun, startedAt: string): TraceSummary | null {
  if (!run.sessionId || !run.prompt.trim()) return null;
  return {
    session_id: run.sessionId,
    trace_id: null,
    started_at: startedAt,
    input: run.prompt.trim(),
    output: typeof run.response === 'string' ? run.response : null,
    tool_calls: Array.isArray(run.toolCalls)
      ? run.toolCalls.map((c) => ({ name: String(c.name), args: isPlainObject(c.args) ? c.args : {} }))
      : null,
    latency_ms: null,
  };
}

export const INVOKE_ADD_COPY = {
  button: 'Add to eval dataset',
  hint: 'Saving starts a new build + eval run.',
};

export interface ItemDraft extends ItemForm {
  key: string;
  sourceTraceId: string;
  startedAt: string | null;
}

export function slugify(input: string, max = 40): string {
  const words = input
    .normalize('NFKD')
    .replace(/[\u0300-\u036f]/g, '')
    .toLowerCase()
    .split(/[^a-z0-9]+/)
    .filter(Boolean);
  let slug = '';
  for (const w of words) {
    const next = slug ? `${slug}-${w}` : w;
    if (next.length > max) break;
    slug = next;
  }
  return slug || (words[0] ?? 'trace').slice(0, max);
}

export function shortSuffix(sessionId: string): string {
  const alnum = sessionId.toLowerCase().replace(/[^a-z0-9]/g, '');
  return alnum.slice(-4) || '0000';
}

export function draftId(trace: TraceSummary, taken: ReadonlySet<string>): string {
  const base = `${slugify(trace.input ?? '')}-${shortSuffix(trace.session_id)}`.slice(0, 60);
  let id = base;
  for (let n = 2; taken.has(id); n++) id = `${base}-${n}`;
  return id;
}

/** Gateway/MCP tools are named `<label>__<target>___<tool>`; helper tools carry no `__`. */
const isGatewayTool = (name: string): boolean => name.includes('__');

const ID_KEY_RE = /(^id|_id|Id)$/;

function idArgs(args: Record<string, unknown> | null | undefined): string {
  const kept = Object.entries(args ?? {}).filter(
    ([k, v]) => ID_KEY_RE.test(k) && (typeof v === 'string' || typeof v === 'number'),
  );
  return kept.length > 0 ? JSON.stringify(Object.fromEntries(kept)) : '';
}

export function toolDisplayName(name: string): string {
  const afterLabel = name.slice(name.indexOf('__') + 2);
  const i = afterLabel.lastIndexOf('___');
  return i >= 0 ? afterLabel.slice(i + 3) : afterLabel;
}

export function genericGoldenAnswer(toolNames: readonly string[]): string {
  const names = toolNames.map(toolDisplayName);
  const list = names.length === 1 ? names[0] : `${names.slice(0, -1).join(', ')} and ${names[names.length - 1]}`;
  return `Uses ${list} and answers from ${names.length === 1 ? 'its' : 'their'} results.`;
}

export function draftFromTrace(trace: TraceSummary, taken: ReadonlySet<string>): ItemDraft {
  const calls = (trace.tool_calls ?? [])
    .filter((c) => isGatewayTool(c.name))
    .filter((c, i, all) => i === 0 || all[i - 1].name !== c.name);
  return {
    key: trace.session_id,
    sourceTraceId: trace.trace_id ?? trace.session_id,
    startedAt: trace.started_at,
    id: draftId(trace, taken),
    input: trace.input?.trim() ?? '',
    expectedOutput: calls.length > 0 ? genericGoldenAnswer(calls.map((c) => c.name)) : (trace.output?.trim() ?? ''),
    assertions: '',
    tools: calls.map((c) => ({ name: c.name, args: idArgs(c.args) })),
  };
}

export function draftsFromTraces(traces: readonly TraceSummary[], existingIds: Iterable<string>): ItemDraft[] {
  const taken = new Set(existingIds);
  const drafts: ItemDraft[] = [];
  for (const t of traces) {
    if (!selectable(t)) continue;
    const d = draftFromTrace(t, taken);
    taken.add(d.id);
    drafts.push(d);
  }
  return drafts;
}

export interface DraftValidation {
  items: DatasetItemInput[];
  errors: Record<string, string[]>;
  batchErrors: string[];
}

export function validateDrafts(drafts: readonly ItemDraft[], existingIds: ReadonlySet<string>): DraftValidation {
  const errors: Record<string, string[]> = {};
  const batchErrors: string[] = [];
  const items: DatasetItemInput[] = [];
  const counts = new Map<string, number>();
  for (const d of drafts) counts.set(d.id.trim(), (counts.get(d.id.trim()) ?? 0) + 1);
  for (const d of drafts) {
    const { item, errors: errs } = itemFromForm(d, existingIds);
    const id = d.id.trim();
    if (ITEM_ID_RE.test(id) && (counts.get(id) ?? 0) > 1) errs.push(`ID: "${id}" is used by more than one item here`);
    if (errs.length > 0) errors[d.key] = errs;
    else if (item) items.push({ ...item, source_trace_id: d.sourceTraceId });
  }
  const total = existingIds.size + drafts.length;
  if (total > MAX_ITEMS) {
    batchErrors.push(
      `A dataset holds at most ${MAX_ITEMS} items; it has ${existingIds.size}, so add at most ${Math.max(0, MAX_ITEMS - existingIds.size)}.`,
    );
  }
  if (Object.keys(errors).length > 0 || batchErrors.length > 0) return { items: [], errors, batchErrors };
  return { items, errors, batchErrors };
}

export function matchModeNote(config: EvalConfigOutput | null): string {
  if (!config) return 'How tools and arguments are matched is set in Evaluations → Settings.';
  const tools = TOOL_MODE_OPTIONS.find((o) => o.value === config.checks.tools)?.label ?? config.checks.tools;
  const args = ARGS_MODE_OPTIONS.find((o) => o.value === config.checks.args)?.label ?? config.checks.args;
  return `Matched by Settings: tools ${tools.toLowerCase()}, arguments ${args.toLowerCase()}. Change it in Evaluations → Settings.`;
}

export const GOLDEN_ANSWER_LABEL = 'Golden answer — edit to what it should have said';

export function committedFlash(n: number): string {
  return `Committed ${n} item${n === 1 ? '' : 's'} to the dataset. A dev build has started, and evals run on it once it is deployed.`;
}

export function commitErrorText(err: unknown): string {
  if (isApiFailure(err) && classifyCommitFailure(err) === 'conflict') {
    return 'The branch changed while you were editing. Press Commit again — your edits are kept.';
  }
  return err instanceof Error ? err.message : 'Something went wrong';
}

export function datasetReadWarning(read: { failed: boolean; errors: number }): string | null {
  if (read.failed) {
    return 'Could not read the current dataset, so the ids here are not checked against it. The commit may be refused.';
  }
  if (read.errors > 0) {
    return `The current dataset has ${read.errors} error${read.errors === 1 ? '' : 's'}. The commit may be refused until ${read.errors === 1 ? 'it is' : 'they are'} fixed on the Evaluations tab.`;
  }
  return null;
}

export const DISCARD_CONFIRM = 'Discard your edits to these dataset items?';

export const TRACES_COPY = {
  title: 'Traces',
  subtitle: 'Recent conversations with this agent. Pick the ones worth keeping and add them to the evaluation dataset.',
  emptyNone: {
    headline: 'No trace source for this stage',
    detail:
      'Traces are read from CloudWatch (AgentCore observability spans), and none are available for this agent at this stage yet. You can still add dataset items by hand on the Evaluations tab.',
  },
  emptyList: {
    headline: 'No traces yet',
    detail:
      'Nothing has reached CloudWatch for this agent at this stage recently. Use the agent, then refresh — or add dataset items by hand on the Evaluations tab.',
  },
  noInput: 'This trace recorded no input, so it cannot become a dataset item.',
  noToolData: 'No tool-call data',
  noTools: 'No tools',
};
