// Rationale: frontend/README.md "Tests are node-environment only, by design"

import type {
  DatasetItemInput,
  DatasetItemOutput,
  EvalRun,
  ExpectedToolInput,
  ExpectedToolOutput,
  ItemResult,
  Repository,
  RunSummary,
  Score,
  ToolCall,
} from '../../../api/client';
import {
  type ApiFailure,
  type ErrorCode,
  isApiFailure,
} from '../../../api/failure';
import { refusalKind } from '../../../api/refusalKind';
import { NO_VALUE } from '../opsLabels';
import { shortDigest } from '../repoRowModel';

/** The AgentCore built-in judges the GA `Evaluate` API offers. */
export const JUDGE_CHOICES: readonly { id: string; label: string; hint: string }[] = [
  { id: 'Builtin.GoalSuccessRate', label: 'Goal success', hint: 'Did the agent achieve what the assertions describe?' },
  { id: 'Builtin.Correctness', label: 'Correctness', hint: 'Does the answer agree with the expected output?' },
  { id: 'Builtin.Helpfulness', label: 'Helpfulness', hint: 'Is the answer useful to the person asking?' },
  { id: 'Builtin.ToolSelectionAccuracy', label: 'Tool selection', hint: 'Did the agent pick the right tools?' },
];

export const CHECK_LABEL: Record<string, string> = {
  tools: 'Tool names',
  args: 'Tool arguments',
  no_error: 'No error',
};
const CHECK_ORDER = ['tools', 'args', 'no_error'];

export const TOOL_MODE_OPTIONS = [
  { value: 'in_order', label: 'In order', hint: 'Expected tools appear in this order; extras allowed' },
  { value: 'exact', label: 'Exact', hint: 'The same tools, in the same order, nothing else' },
  { value: 'any_order', label: 'Any order', hint: 'Every expected tool appears somewhere' },
  { value: 'off', label: 'Off', hint: 'Tool names are not checked' },
] as const;

export const ARGS_MODE_OPTIONS = [
  { value: 'subset', label: 'Subset', hint: 'Every listed argument equals the actual value' },
  { value: 'exact', label: 'Exact', hint: 'The arguments are exactly the listed ones' },
  { value: 'off', label: 'Off', hint: 'Tool arguments are not checked' },
] as const;

export function judgeLabel(id: string): string {
  return JUDGE_CHOICES.find((j) => j.id === id)?.label ?? id.replace(/^Builtin\./, '');
}

export function evaluatorLabel(score: Pick<Score, 'evaluator' | 'kind'>): string {
  return score.kind === 'check' ? (CHECK_LABEL[score.evaluator] ?? score.evaluator) : judgeLabel(score.evaluator);
}

export function evalsTabVisible(repo: { agent_id?: string | null }): boolean {
  return Boolean(repo.agent_id);
}

export function currentBuild(runs: { current_digest?: string | null; current_build_tag?: string | null } | null | undefined): string | null {
  return runs?.current_digest || runs?.current_build_tag || null;
}

export function shortBuildTag(tag: string | null | undefined): string | null {
  const t = tag?.trim();
  if (!t) return null;
  if (/^[0-9a-f]{40}$/.test(t)) return t.slice(0, 7);
  const dash = t.lastIndexOf('-');
  return dash >= 0 && dash < t.length - 1 ? t.slice(dash + 1) : t;
}

function shortIdentity(identity: string | null | undefined): string | null {
  if (!identity) return null;
  return identity.startsWith('sha256:') ? shortDigest(identity) : shortBuildTag(identity);
}

export function buildLabel(
  run: { git_sha?: string | null; digest?: string | null; build_tag?: string | null } | null,
  current?: string | null,
): string | null {
  const d =
    shortBuildTag(run?.build_tag) ?? shortDigest(run?.digest) ?? run?.git_sha?.slice(0, 7) ?? shortIdentity(current);
  return d ? `build ${d}` : null;
}

export function checksText(summary: RunSummary | null | undefined): string | null {
  if (!summary) return null;
  const total = summary.checks_passed + summary.checks_failed;
  if (total === 0) return 'no checks scored';
  return `${summary.checks_passed}/${total} checks passed`;
}

const fmt2 = (n: number) => n.toFixed(2);

export function judgesText(
  summary: RunSummary | null | undefined,
  threshold: number,
): { text: string; ok: boolean } | null {
  if (!summary) return null;
  const entries = Object.entries(summary.judge_means);
  if (entries.length === 0) return null;
  const failing = entries.filter(([, m]) => m === null || m < threshold);
  if (failing.length > 0) {
    const [id, mean] = failing[0];
    const head = mean === null ? `${judgeLabel(id)} not scored` : `${judgeLabel(id)} ${fmt2(mean)} < ${fmt2(threshold)}`;
    const more = failing.length > 1 ? ` (+${failing.length - 1} more)` : '';
    return { text: head + more, ok: false };
  }
  const [lowId, low] = entries.reduce((a, b) => ((b[1] as number) < (a[1] as number) ? b : a));
  const name = entries.length === 1 ? judgeLabel(lowId) : 'judges';
  return { text: `${name} ${fmt2(low as number)} ≥ ${fmt2(threshold)}`, ok: true };
}

export function runSummaryText(run: EvalRun): string {
  if (run.status === 'running') return 'Running…';
  const parts = [checksText(run.summary), judgesText(run.summary, run.config.gate.judge_min_mean)?.text];
  const text = parts.filter(Boolean).join(' · ');
  return text || (run.status === 'error' ? 'The run errored before scoring' : 'Nothing scored');
}

export const POLL_MS = 5000;
export const STALE_RUNNING_MS = 2 * 60 * 60 * 1000;

export function isRunActive(run: Pick<EvalRun, 'status' | 'started_at'> | null | undefined, now: number): boolean {
  if (!run || run.status !== 'running') return false;
  const started = Date.parse(run.started_at);
  return Number.isNaN(started) || now - started < STALE_RUNNING_MS;
}

export function shouldPoll(runs: readonly EvalRun[], current: EvalRun | null, now: number): boolean {
  return isRunActive(current, now) || runs.some((r) => isRunActive(r, now));
}

export function shouldAutoRun(input: {
  currentBuild: string | null | undefined;
  currentRun: EvalRun | null | undefined;
  itemCount: number;
  fileErrors: number;
  mayWrite: boolean;
  anyActive: boolean;
  requested: ReadonlySet<string>;
}): boolean {
  const { currentBuild, currentRun, itemCount, fileErrors, mayWrite, anyActive, requested } = input;
  return Boolean(currentBuild) && !currentRun && !anyActive && itemCount > 0 && fileErrors === 0 && mayWrite
    && !requested.has(currentBuild as string);
}

export type DevEvalState = 'none' | 'running' | 'passed' | 'failed' | 'inconclusive';

export function devEvalState(
  runs: { current_digest?: string | null; current_build_tag?: string | null; current_run?: EvalRun | null } | null | undefined,
  now: number,
): DevEvalState | null {
  if (!runs || !currentBuild(runs)) return null;
  const run = runs.current_run;
  if (!run) return 'none';
  if (run.status === 'running') return isRunActive(run, now) ? 'running' : 'none';
  if (run.status === 'error') return 'inconclusive';
  return run.status;
}

export type GateCardState = NonNullable<Repository['eval_gate']>;

export interface GateCard {
  state: GateCardState;
  headline: string;
  facts: string[];
  detail: string | null;
  reasons: string[];
  regressions: string[];
}

export function gateCard(input: {
  currentBuild: string | null | undefined;
  currentRun: EvalRun | null | undefined;
  itemCount: number;
  now: number;
}): GateCard {
  const { currentBuild, currentRun: run, itemCount, now } = input;
  const build = buildLabel(run ?? null, currentBuild);
  const withBuild = (facts: (string | null | undefined)[]) => [...facts, build].filter(Boolean) as string[];
  if (!currentBuild) {
    return {
      state: 'none',
      headline: 'No dev build to evaluate yet',
      facts: [],
      detail: 'Deploy to dev first. Prod promotion needs an override until a run for that build passes.',
      reasons: [],
      regressions: [],
    };
  }
  if (!run || (run.status === 'running' && !isRunActive(run, now))) {
    return {
      state: 'none',
      headline: run ? 'The last run stopped responding' : 'Not evaluated yet',
      facts: withBuild([]),
      detail:
        itemCount === 0
          ? 'Add dataset items below, then run evals. Until a run passes, prod promotion needs an override with a reason.'
          : 'Run evals for this build. Until a run passes, prod promotion needs an override with a reason.',
      reasons: [],
      regressions: [],
    };
  }
  if (run.status === 'running') {
    const n = run.item_count ?? itemCount;
    return {
      state: 'running',
      headline: 'Evaluating',
      facts: withBuild([n > 0 ? `${n} ${n === 1 ? 'item' : 'items'}` : null]),
      detail: 'Each item is invoked on dev, checked, then judged. This page refreshes every 5 seconds.',
      reasons: [],
      regressions: [],
    };
  }
  const threshold = run.config.gate.judge_min_mean;
  const facts = withBuild([checksText(run.summary), judgesText(run.summary, threshold)?.text]);
  const regressions = run.regressions ?? [];
  if (run.gate?.passed) {
    return { state: 'passed', headline: 'This build can be promoted', facts, detail: null, reasons: [], regressions };
  }
  return {
    state: 'failed',
    headline: run.status === 'error' ? 'The run errored — promotion is blocked' : 'Promotion is blocked',
    facts,
    detail: 'Fix the agent or the dataset and run again, or promote with an override and a reason.',
    reasons: run.gate?.reasons ?? [],
    regressions,
  };
}

export function gateSentence(card: GateCard): string {
  return [card.headline, ...card.facts].join(' · ');
}

const RUN_LABEL_MINUTE: Intl.DateTimeFormatOptions = { day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit' };
const RUN_LABEL_SECOND: Intl.DateTimeFormatOptions = { ...RUN_LABEL_MINUTE, second: '2-digit' };

export function runLabels(
  runs: readonly Pick<EvalRun, 'run_id' | 'started_at'>[],
  locale?: string,
): Map<string, string> {
  const fmt = (iso: string, opts: Intl.DateTimeFormatOptions) => {
    const d = new Date(iso);
    return Number.isNaN(d.getTime()) ? 'Run' : d.toLocaleString(locale, opts);
  };
  const minute = runs.map((r) => fmt(r.started_at, RUN_LABEL_MINUTE));
  const count = new Map<string, number>();
  for (const m of minute) count.set(m, (count.get(m) ?? 0) + 1);
  return new Map(
    runs.map((r, i) => [r.run_id, (count.get(minute[i]) ?? 0) > 1 ? fmt(r.started_at, RUN_LABEL_SECOND) : minute[i]]),
  );
}

export function orderScores(scores: readonly Score[]): Score[] {
  const rank = (s: Score) => (s.kind === 'check' ? (CHECK_ORDER.indexOf(s.evaluator) + 1 || CHECK_ORDER.length + 1) : 100);
  return scores.map((s, i) => ({ s, i })).sort((a, b) => rank(a.s) - rank(b.s) || a.i - b.i).map((x) => x.s);
}

export type ScoreTone = 'ok' | 'fail' | 'error' | 'neutral';

export function scoreView(score: Score, threshold: number): { text: string; tone: ScoreTone; title: string | null } {
  const title = score.error ?? score.explanation ?? null;
  if (score.error) return { text: 'error', tone: 'error', title };
  if (score.kind === 'check') {
    return score.passed ? { text: '✓', tone: 'ok', title } : { text: '✗', tone: 'fail', title };
  }
  if (score.value === null) return { text: 'not scored', tone: 'neutral', title };
  return { text: fmt2(score.value), tone: score.value >= threshold ? 'ok' : 'fail', title };
}

const REFERENCE_FIELD: Record<string, string> = { expectedResponse: 'expected response', assertions: 'assertions' };

export function judgeExplanation(explanation: string | null | undefined): { text: string | null; unused: string | null } {
  const m = /\s*\(ignored reference inputs: ([^)]*)\)\s*$/.exec(explanation ?? '');
  if (!m) return { text: explanation || null, unused: null };
  const fields = m[1].split(',').map((f) => f.trim()).filter(Boolean)
    .map((f) => REFERENCE_FIELD[f] ?? f.replace(/([a-z])([A-Z])/g, '$1 $2').toLowerCase());
  return { text: (explanation ?? '').slice(0, m.index).trim() || null, unused: `Not used by this judge: ${fields.join(', ')}` };
}

export function itemPassed(result: ItemResult): boolean {
  return !result.error && !result.scores.some((s) => s.kind === 'check' && s.passed === false);
}

export function itemsText(results: readonly ItemResult[]): string {
  const ok = results.filter(itemPassed).length;
  return `${ok}/${results.length} items pass their checks`;
}

export function latencyText(ms: number | null | undefined): string | null {
  if (ms === null || ms === undefined) return null;
  return ms < 1000 ? `${ms} ms` : `${(ms / 1000).toFixed(1)} s`;
}

export type ToolDiffStatus = 'match' | 'args-differ' | 'missing' | 'extra' | 'no-data';

export interface ToolDiffRow {
  expected: ExpectedToolOutput | null;
  actual: ToolCall | null;
  status: ToolDiffStatus;
}

function deepEqual(a: unknown, b: unknown): boolean {
  if (a === b) return true;
  if (typeof a !== 'object' || typeof b !== 'object' || a === null || b === null) return false;
  if (Array.isArray(a) !== Array.isArray(b)) return false;
  const ka = Object.keys(a as object);
  const kb = Object.keys(b as object);
  if (ka.length !== kb.length) return false;
  return ka.every((k) => deepEqual((a as Record<string, unknown>)[k], (b as Record<string, unknown>)[k]));
}

export function argsMatch(expected: ExpectedToolOutput, actual: ToolCall): boolean {
  if (!expected.args) return true;
  return Object.entries(expected.args).every(([k, v]) => deepEqual(v, actual.args?.[k]));
}

/** Mirrors the backend's `tool_name_matches`. */
export function toolNameMatches(expected: string, actual: string): boolean {
  if (actual === expected || actual.endsWith(`__${expected}`)) return true;
  return !expected.includes('__') && actual.endsWith(`___${expected}`);
}

export function toolDiffRows(
  expected: readonly ExpectedToolOutput[],
  actual: readonly ToolCall[] | null | undefined,
): ToolDiffRow[] {
  if (actual === null || actual === undefined) {
    return expected.map((e) => ({ expected: e, actual: null, status: 'no-data' as const }));
  }
  const rows: ToolDiffRow[] = [];
  let j = 0;
  for (const e of expected) {
    let k = j;
    while (k < actual.length && !toolNameMatches(e.name, actual[k].name)) k++;
    if (k === actual.length) {
      rows.push({ expected: e, actual: null, status: 'missing' });
      continue;
    }
    for (; j < k; j++) rows.push({ expected: null, actual: actual[j], status: 'extra' });
    rows.push({ expected: e, actual: actual[k], status: argsMatch(e, actual[k]) ? 'match' : 'args-differ' });
    j = k + 1;
  }
  for (; j < actual.length; j++) rows.push({ expected: null, actual: actual[j], status: 'extra' });
  return rows;
}

export const ITEM_ID_RE = /^[A-Za-z0-9._-]{1,64}$/;
export const MAX_INPUT = 8000;
export const MAX_ITEMS = 50;
const ITEM_KEYS = new Set(['id', 'input', 'expected_output', 'assertions', 'expected_tools', 'source_trace_id', 'tags']);

export interface ItemFormTool {
  name: string;
  args: string;
}
export interface ItemForm {
  id: string;
  input: string;
  expectedOutput: string;
  assertions: string;
  tools: ItemFormTool[];
}

export function suggestItemId(existing: Iterable<string>): string {
  const taken = new Set(existing);
  for (let n = taken.size + 1; ; n++) {
    const id = `item-${String(n).padStart(3, '0')}`;
    if (!taken.has(id)) return id;
  }
}

function isPlainObject(v: unknown): v is Record<string, unknown> {
  return typeof v === 'object' && v !== null && !Array.isArray(v);
}

export function itemFromForm(form: ItemForm, existingIds: ReadonlySet<string>): { item: DatasetItemInput | null; errors: string[] } {
  const errors: string[] = [];
  const id = form.id.trim();
  if (!ITEM_ID_RE.test(id)) errors.push('ID: 1–64 letters, digits, dot, dash or underscore');
  else if (existingIds.has(id)) errors.push(`ID: "${id}" is already in the dataset`);
  const input = form.input.trim();
  if (!input) errors.push('Input is required');
  else if (input.length > MAX_INPUT) errors.push(`Input: at most ${MAX_INPUT} characters`);
  const tools: ExpectedToolInput[] = [];
  form.tools.forEach((t, i) => {
    const name = t.name.trim();
    const argsText = t.args.trim();
    if (!name && !argsText) return;
    if (!name) {
      errors.push(`Tool ${i + 1}: a name is required`);
      return;
    }
    if (!argsText) {
      tools.push({ name });
      return;
    }
    try {
      const args: unknown = JSON.parse(argsText);
      if (!isPlainObject(args)) throw new Error('not an object');
      tools.push({ name, args });
    } catch {
      errors.push(`Tool ${i + 1} arguments: must be a JSON object, e.g. {"policy_id": "P-1001"}`);
    }
  });
  if (errors.length > 0) return { item: null, errors };
  const assertions = form.assertions.split('\n').map((a) => a.trim()).filter(Boolean);
  const item: DatasetItemInput = { id, input };
  if (form.expectedOutput.trim()) item.expected_output = form.expectedOutput.trim();
  if (assertions.length > 0) item.assertions = assertions;
  if (tools.length > 0) item.expected_tools = tools;
  return { item, errors: [] };
}

export function previewJsonl(text: string): { items: DatasetItemInput[]; errors: string[] } {
  const items: DatasetItemInput[] = [];
  const errors: string[] = [];
  const seen = new Set<string>();
  text.split(/\r?\n/).forEach((raw, i) => {
    const line = raw.trim();
    if (!line) return;
    const at = `line ${i + 1}`;
    let obj: unknown;
    try {
      obj = JSON.parse(line);
    } catch {
      errors.push(`${at}: not valid JSON`);
      return;
    }
    if (!isPlainObject(obj)) {
      errors.push(`${at}: each line must be one JSON object`);
      return;
    }
    const before = errors.length;
    for (const k of Object.keys(obj)) if (!ITEM_KEYS.has(k)) errors.push(`${at}: unknown field "${k}"`);
    const id = obj.id;
    if (typeof id !== 'string' || !ITEM_ID_RE.test(id)) errors.push(`${at}: "id" must be 1–64 letters, digits, dot, dash or underscore`);
    else if (seen.has(id)) errors.push(`${at}: duplicate id "${id}"`);
    else seen.add(id);
    const input = obj.input;
    if (typeof input !== 'string' || !input.trim()) errors.push(`${at}: "input" is required`);
    else if (input.length > MAX_INPUT) errors.push(`${at}: "input" is longer than ${MAX_INPUT} characters`);
    const tools = obj.expected_tools;
    if (tools !== undefined && !(Array.isArray(tools) && tools.every((t) => isPlainObject(t) && typeof t.name === 'string'))) {
      errors.push(`${at}: "expected_tools" must be a list of {"name", "args"?}`);
    }
    if (errors.length === before) items.push(obj as unknown as DatasetItemInput);
  });
  if (seen.size > MAX_ITEMS) errors.push(`at most ${MAX_ITEMS} items per dataset (found ${seen.size})`);
  if (seen.size === 0 && errors.length === 0) errors.push('the file has no items');
  return { items, errors };
}

export function parseThreshold(text: string): number | null {
  if (!text.trim()) return null;
  const n = Number(text);
  return Number.isFinite(n) && n >= 0 && n <= 1 ? n : null;
}

export function githubFileUrl(repoUrl: string | null | undefined, path: string): string | null {
  if (!repoUrl) return null;
  return `${repoUrl.replace(/\.git$/, '').replace(/\/+$/, '')}/blob/HEAD/${path.replace(/^\/+/, '')}`;
}

export const EMPTY_DATASET_COPY = {
  headline: 'No dataset yet',
  detail:
    'A dataset is a list of inputs with what the agent should do: the expected tools, an expected answer, and assertions. Add items in any of three ways:',
  ways: [
    { title: 'From traces', detail: 'On the Observability tab, pick real conversations and add them to the dataset.' },
    { title: 'Add item', detail: 'Write one item by hand with the form.' },
    { title: 'Upload file', detail: 'Upload an evals/dataset.jsonl, one JSON object per line.' },
  ],
  gateNote: 'Until a run passes, promoting this agent to prod needs an override with a reason.',
};

export const COMING_SOON: readonly { title: string; detail: string }[] = [
  { title: 'Judge calibration', detail: 'Compare judge scores with human labels.' },
  { title: 'Human review queue', detail: 'Route low-scoring items to a reviewer.' },
  { title: 'Simulated users', detail: 'Multi-turn conversations driven by a persona.' },
  { title: 'Online monitoring', detail: 'Score a sample of production traffic continuously.' },
];

export const COMMIT_NOTE = 'Saved as a commit to the default branch. That starts a dev build, and the new build is evaluated once it is deployed.';

export type CommitRefusalKind = 'conflict' | 'trunk_mismatch';

export const COMMIT_REFUSAL_CODES = {
  conflict: ['eval.conflict'],
  trunk_mismatch: ['eval.trunk_mismatch'],
} as const satisfies Record<CommitRefusalKind, readonly ErrorCode[]>;

export function classifyCommitFailure(failure: ApiFailure): CommitRefusalKind | null {
  return refusalKind(COMMIT_REFUSAL_CODES, failure);
}

export function commitErrorText(err: unknown, action: string): string {
  const kind = isApiFailure(err) ? classifyCommitFailure(err) : null;
  if (kind === 'conflict') {
    return `The branch changed while you were editing — press ${action} again; your edits are kept.`;
  }
  if (kind === 'trunk_mismatch') {
    return 'This repository’s default branch on GitHub is not the project’s trunk branch, so AGP will not commit eval files to it. Make the trunk the default branch on GitHub (or ask the project owner to), then try again.';
  }
  return err instanceof Error ? err.message : 'Something went wrong';
}

export function expectedItemFor(
  itemId: string,
  snapshot: readonly DatasetItemOutput[] | null | undefined,
  current: ReadonlyMap<string, DatasetItemOutput>,
): { item: DatasetItemOutput | undefined; fromSnapshot: boolean } {
  const snap = snapshot && snapshot.length > 0 ? snapshot.find((i) => i.id === itemId) : undefined;
  if (snap) return { item: snap, fromSnapshot: true };
  return { item: current.get(itemId), fromSnapshot: false };
}

export function runBlockedReason(input: {
  mayWrite: boolean;
  itemCount: number;
  fileErrors: number;
  currentBuild: string | null | undefined;
  active: boolean;
}): string | null {
  if (input.active) return 'A run is in progress';
  if (!input.mayWrite) return 'Needs the Maintainer role on this project';
  if (!input.currentBuild) return 'Deploy to dev first';
  if (input.fileErrors > 0) return 'Fix the eval file errors first';
  if (input.itemCount === 0) return 'Add dataset items first';
  return null;
}

export const when = (iso: string | null | undefined): string => (iso ? new Date(iso).toLocaleString() : NO_VALUE);
export const json = (v: unknown): string => JSON.stringify(v);
export const errText = (e: unknown): string => (e instanceof Error ? e.message : 'Something went wrong');
