import { describe, expect, expectTypeOf, it } from 'vitest';

import { ApiFailure, type ErrorCode, formatApiErrorDetail } from '../../../api/failure';
import type {
  EvalConfigOutput,
  EvalRun,
  ExpectedToolOutput,
  GateResult,
  ItemResult,
  RunSummary,
  Score,
  ToolCall,
} from '../../../api/openapi';
import {
  COMING_SOON,
  COMMIT_REFUSAL_CODES,
  EMPTY_DATASET_COPY,
  JUDGE_CHOICES,
  STALE_RUNNING_MS,
  buildLabel,
  checksText,
  classifyCommitFailure,
  commitErrorText,
  currentBuild,
  devEvalState,
  errText,
  expectedItemFor,
  evalsTabVisible,
  gateCard,
  gateSentence,
  githubFileUrl,
  isRunActive,
  itemFromForm,
  itemPassed,
  json,
  judgesText,
  orderScores,
  parseThreshold,
  previewJsonl,
  runBlockedReason,
  runLabels,
  runSummaryText,
  scoreView,
  judgeExplanation,
  shouldAutoRun,
  shortBuildTag,
  shouldPoll,
  suggestItemId,
  toolDiffRows,
  toolNameMatches,
  when,
  type CommitRefusalKind,
} from './evaluationsLogic';
import tabRaw from './EvaluationsTab.tsx?raw';
import datasetRaw from './EvaluationsDataset.tsx?raw';
import runsRaw from './EvaluationsRuns.tsx?raw';
import stylesRaw from './evaluationsTabStyles.ts?raw';
import hookRaw from './useRepoEvals.ts?raw';
import pageRaw from '../RepositoryDetail.tsx?raw';
import overviewTabRaw from './OverviewTab.tsx?raw';
import accessTabRaw from './AccessTab.tsx?raw';
import repoSummaryCardRaw from '../RepoSummaryCard.tsx?raw';
import repoFieldsRaw from '../RepoFields.tsx?raw';
import useRepositoryDetailRaw from '../useRepositoryDetail.ts?raw';
import { NO_VALUE } from '../opsLabels';
import { stripComments } from '../../../rawSource';
import { datasetItem } from '../../../test/wire';

const tabSrc = [tabRaw, runsRaw, datasetRaw, stylesRaw].map(stripComments).join('\n');
const hookSrc = stripComments(hookRaw);
const pageSrc = [pageRaw, overviewTabRaw, accessTabRaw, repoSummaryCardRaw, repoFieldsRaw, useRepositoryDetailRaw].map(stripComments).join('\n');

const NOW = Date.parse('2026-10-06T12:00:00Z');
const SHA = 'abc1234def5678';

function evalConfigOutput(overrides: Partial<EvalConfigOutput> = {}): EvalConfigOutput {
  return {
    checks: { args: 'subset', no_error: true, tools: 'in_order' },
    dataset: 'evals/dataset.jsonl',
    gate: { checks: 'all_pass', judge_min_mean: 0.8 },
    judges: ['Builtin.GoalSuccessRate', 'Builtin.Correctness'],
    version: 1,
    ...overrides,
  };
}

function runSummary(overrides: Partial<RunSummary> = {}): RunSummary {
  return {
    checks_failed: 0,
    checks_passed: 12,
    items: 4,
    judge_means: { 'Builtin.GoalSuccessRate': 0.9, 'Builtin.Correctness': 0.86 },
    ...overrides,
  };
}

function gateResult(overrides: Partial<GateResult> = {}): GateResult {
  return { passed: true, reasons: [], ...overrides };
}

function evalRun(overrides: Partial<EvalRun> = {}): EvalRun {
  return {
    agent_id: 'a1',
    baseline: null,
    build_tag: null,
    config: evalConfigOutput(),
    digest: 'sha256:' + 'f'.repeat(64),
    finished_at: '2026-10-06T11:58:00Z',
    gate: gateResult(),
    git_sha: SHA,
    item_count: null,
    project_id: 'p1',
    regressions: [],
    repo_id: 'r1',
    run_id: '6f1c2b8e-1111-4a4a-9b9b-000000000001',
    stage: 'dev',
    started_at: '2026-10-06T11:55:00Z',
    started_by: 'lars@example.com',
    status: 'passed',
    summary: runSummary(),
    trigger: 'manual',
    ...overrides,
  };
}

function score(overrides: Partial<Score> = {}): Score {
  return {
    error: null,
    evaluator: 'tools',
    explanation: null,
    kind: 'check',
    label: null,
    passed: true,
    value: 1,
    ...overrides,
  };
}

function itemResult(overrides: Partial<ItemResult> = {}): ItemResult {
  return {
    error: null,
    item_id: 'x',
    latency_ms: null,
    output: null,
    scores: [],
    session_id: null,
    tool_calls: null,
    ...overrides,
  };
}

function expectedToolOutput(overrides: Partial<ExpectedToolOutput> = {}): ExpectedToolOutput {
  return { args: null, name: 'get_policy', ...overrides };
}

function toolCall(overrides: Partial<ToolCall> = {}): ToolCall {
  return { args: {}, name: 'get_policy', ...overrides };
}

describe('the tab is hidden for repos without an agent', () => {
  it('needs an agent_id', () => {
    expect(evalsTabVisible({ agent_id: 'a1' })).toBe(true);
    expect(evalsTabVisible({ agent_id: null })).toBe(false);
    expect(evalsTabVisible({ agent_id: '' })).toBe(false);
  });
});

describe('summaries: counts with totals, checks before judges', () => {
  it('checks are a count over a total, never a percentage', () => {
    expect(checksText(runSummary())).toBe('12/12 checks passed');
    expect(checksText(runSummary({ checks_passed: 10, checks_failed: 2 }))).toBe('10/12 checks passed');
    expect(checksText(runSummary({ checks_passed: 0, checks_failed: 0 }))).toBe('no checks scored');
    expect(checksText(null)).toBeNull();
  });

  it('judges quote the deciding mean, and name a failing judge', () => {
    expect(judgesText(runSummary(), 0.8)).toEqual({ text: 'judges 0.86 ≥ 0.80', ok: true });
    const single = runSummary({ judge_means: { 'Builtin.Correctness': 0.86 } });
    expect(judgesText(single, 0.8)?.text).toBe('Correctness 0.86 ≥ 0.80');
    const failing = runSummary({ judge_means: { 'Builtin.GoalSuccessRate': 0.9, 'Builtin.Correctness': 0.62 } });
    expect(judgesText(failing, 0.8)).toEqual({ text: 'Correctness 0.62 < 0.80', ok: false });
  });

  it('a judge with no scores fails, as it does in the gate', () => {
    const none = runSummary({ judge_means: { 'Builtin.Correctness': null, 'Builtin.Helpfulness': 0.1 } });
    expect(judgesText(none, 0.8)).toEqual({ text: 'Correctness not scored (+1 more)', ok: false });
  });

  it('checks-only runs carry no judge text', () => {
    expect(judgesText(runSummary({ judge_means: {} }), 0.8)).toBeNull();
    expect(runSummaryText(evalRun({ summary: runSummary({ judge_means: {} }) }))).toBe('12/12 checks passed');
  });

  it('the run row reads checks first', () => {
    expect(runSummaryText(evalRun())).toBe('12/12 checks passed · judges 0.86 ≥ 0.80');
    expect(runSummaryText(evalRun({ status: 'running', summary: null }))).toBe('Running…');
    expect(runSummaryText(evalRun({ status: 'error', summary: null }))).toBe('The run errored before scoring');
  });
});

describe('the gate card', () => {
  const base = { currentBuild: 'sha256:' + 'f'.repeat(64), itemCount: 4, now: NOW };

  it('passed: one sentence with the verdict, the evidence and the build', () => {
    const card = gateCard({ ...base, currentRun: evalRun() });
    expect(card.state).toBe('passed');
    expect(gateSentence(card)).toBe('This build can be promoted · 12/12 checks passed · judges 0.86 ≥ 0.80 · build sha256:fffffff…');
  });

  it('failed: blocked, with the gate reasons verbatim', () => {
    const r = evalRun({
      status: 'failed',
      summary: runSummary({ checks_passed: 10, checks_failed: 2 }),
      gate: gateResult({ passed: false, reasons: ['fnol-001: tools failed — expected open_claim'] }),
    });
    const card = gateCard({ ...base, currentRun: r });
    expect(card.state).toBe('failed');
    expect(gateSentence(card)).toBe('Promotion is blocked · 10/12 checks passed · judges 0.86 ≥ 0.80 · build sha256:fffffff…');
    expect(card.reasons).toEqual(['fnol-001: tools failed — expected open_claim']);
    expect(card.detail).toMatch(/override/);
  });

  it('regressions vs prod ride along on a finished run, never change the verdict, and are empty for runs from before baselines', () => {
    const regressions = ['Judges 0.62, down from 0.86 on prod build b1a0bca'];
    const card = gateCard({ ...base, currentRun: evalRun({ regressions }) });
    expect(card.state).toBe('passed');
    expect(card.regressions).toEqual(regressions);
    expect(gateCard({ ...base, currentRun: evalRun() }).regressions).toEqual([]);
    expect(gateCard({ ...base, currentRun: null }).regressions).toEqual([]);
  });

  it('error counts as blocked', () => {
    const card = gateCard({ ...base, currentRun: evalRun({ status: 'error', summary: null, gate: gateResult({ passed: false, reasons: ['x'] }) }) });
    expect(card.state).toBe('failed');
    expect(card.headline).toMatch(/errored/);
  });

  it('running: the run’s own item count, not the file’s', () => {
    const card = gateCard({ ...base, itemCount: 5, currentRun: evalRun({ status: 'running', item_count: 4, summary: null, gate: null, finished_at: null }) });
    expect(card.state).toBe('running');
    expect(card.facts).toEqual(['4 items', 'build sha256:fffffff…']);
  });

  it('a running run older than 2 h is not live and reads as not evaluated', () => {
    const stale = evalRun({ status: 'running', started_at: new Date(NOW - STALE_RUNNING_MS - 1).toISOString(), summary: null, gate: null });
    const card = gateCard({ ...base, currentRun: stale });
    expect(card.state).toBe('none');
    expect(card.headline).toMatch(/stopped responding/);
  });

  it('none: says promotion needs an override, and how to get there; without a run the short digest identifies the build', () => {
    const card = gateCard({ ...base, currentRun: null });
    expect(card.state).toBe('none');
    expect(card.detail).toMatch(/override/);
    expect(gateCard({ ...base, itemCount: 0, currentRun: null }).detail).toMatch(/Add dataset items/);
    expect(card.facts[0]).toMatch(/^build sha256:/);
  });

  it('no dev build at all', () => {
    const card = gateCard({ ...base, currentBuild: null, currentRun: null });
    expect(card.state).toBe('none');
    expect(card.headline).toBe('No dev build to evaluate yet');
  });

  it('a tag-only (harness) build is a build: not evaluated yet, labelled by its short tag', () => {
    const card = gateCard({ ...base, currentBuild: 'claims-abc1234', currentRun: null });
    expect(card.headline).toBe('Not evaluated yet');
    expect(card.facts).toEqual(['build abc1234']);
  });
});

describe('liveness and auto-run', () => {
  it('only a fresh running run is active', () => {
    expect(isRunActive(evalRun({ status: 'running' }), NOW)).toBe(true);
    expect(isRunActive(evalRun({ status: 'running', started_at: '2026-10-06T09:00:00Z' }), NOW)).toBe(false);
    expect(isRunActive(evalRun(), NOW)).toBe(false);
    expect(isRunActive(null, NOW)).toBe(false);
  });

  it('polls while anything is live', () => {
    expect(shouldPoll([evalRun()], null, NOW)).toBe(false);
    expect(shouldPoll([evalRun(), evalRun({ status: 'running' })], null, NOW)).toBe(true);
    expect(shouldPoll([], evalRun({ status: 'running' }), NOW)).toBe(true);
  });

  const auto = { currentBuild: 'sha256:d1', currentRun: null, itemCount: 3, fileErrors: 0, mayWrite: true, anyActive: false, requested: new Set<string>() };
  it('runs once per digest when there is a dataset and no run for the build', () => {
    expect(shouldAutoRun(auto)).toBe(true);
    expect(shouldAutoRun({ ...auto, requested: new Set(['sha256:d1']) })).toBe(false);
    expect(shouldAutoRun({ ...auto, requested: new Set(['sha256:old']) })).toBe(true);
  });
  it('never with a run, no dataset, broken files, no build, or a viewer', () => {
    expect(shouldAutoRun({ ...auto, currentRun: evalRun() })).toBe(false);
    expect(shouldAutoRun({ ...auto, itemCount: 0 })).toBe(false);
    expect(shouldAutoRun({ ...auto, fileErrors: 1 })).toBe(false);
    expect(shouldAutoRun({ ...auto, currentBuild: null })).toBe(false);
    expect(shouldAutoRun({ ...auto, mayWrite: false })).toBe(false);
  });
  it('waits while any run is active instead of burning the build; the same build still qualifies once that run finishes', () => {
    expect(shouldAutoRun({ ...auto, anyActive: true })).toBe(false);
    expect(shouldAutoRun({ ...auto, anyActive: false })).toBe(true);
  });
  it('a tag-only build auto-runs once per tag', () => {
    expect(shouldAutoRun({ ...auto, currentBuild: 'claims-abc1234' })).toBe(true);
    expect(shouldAutoRun({ ...auto, currentBuild: 'claims-abc1234', requested: new Set(['claims-abc1234']) })).toBe(false);
  });
});

describe('runs and results', () => {
  it('labels runs by start time, so no UUID is primary text and dropping an older run never renames a newer one', () => {
    const runs = [
      { run_id: 'u-new', started_at: '2026-10-06T11:00:00Z' },
      { run_id: 'u-old', started_at: '2026-10-05T11:00:00Z' },
    ];
    const labels = runLabels(runs, 'en-GB');
    expect(labels.get('u-old')).toMatch(/5 Oct/);
    expect(labels.get('u-new')).toMatch(/6 Oct/);
    expect(labels.get('u-new')).not.toMatch(/Run/);
    expect(runLabels(runs.slice(0, 1), 'en-GB').get('u-new')).toBe(labels.get('u-new'));
  });

  it('runs in the same minute get seconds, so labels stay distinct', () => {
    const labels = runLabels(
      [
        { run_id: 'a', started_at: '2026-10-06T11:00:05Z' },
        { run_id: 'b', started_at: '2026-10-06T11:00:40Z' },
      ],
      'en-GB',
    );
    expect(labels.get('a')).not.toBe(labels.get('b'));
  });

  it('orders checks (tools, args, no_error) before judges', () => {
    const ordered = orderScores([
      score({ evaluator: 'Builtin.Correctness', kind: 'judge' }),
      score({ evaluator: 'no_error', kind: 'check' }),
      score({ evaluator: 'Builtin.GoalSuccessRate', kind: 'judge' }),
      score({ evaluator: 'tools', kind: 'check' }),
      score({ evaluator: 'args', kind: 'check' }),
    ]);
    expect(ordered.map((x) => x.evaluator)).toEqual(['tools', 'args', 'no_error', 'Builtin.Correctness', 'Builtin.GoalSuccessRate']);
  });

  it('renders scores: ✓/✗ for checks, two decimals for judges, errors as errors', () => {
    expect(scoreView(score({ evaluator: 'tools', kind: 'check', value: 1, passed: true }), 0.8).tone).toBe('ok');
    expect(scoreView(score({ evaluator: 'tools', kind: 'check', value: 0, passed: false, explanation: 'why' }), 0.8)).toEqual({ text: '✗', tone: 'fail', title: 'why' });
    expect(scoreView(score({ evaluator: 'tools', kind: 'check', value: null, passed: false, error: 'no tool-call data' }), 0.8)).toEqual({ text: 'error', tone: 'error', title: 'no tool-call data' });
    expect(scoreView(score({ evaluator: 'Builtin.Correctness', kind: 'judge', value: 0.75, passed: null }), 0.8)).toMatchObject({ text: '0.75', tone: 'fail' });
    expect(scoreView(score({ evaluator: 'Builtin.Correctness', kind: 'judge', value: 0.9, passed: null }), 0.8).tone).toBe('ok');
  });

  it('an item passes on its checks; a judge alone never fails it', () => {
    const r = itemResult({ scores: [score({ evaluator: 'Builtin.Correctness', kind: 'judge', value: 0.1, passed: null })] });
    expect(itemPassed(r)).toBe(true);
    expect(itemPassed({ ...r, error: 'boom' })).toBe(false);
    expect(itemPassed({ ...r, scores: [score({ evaluator: 'tools', kind: 'check', value: 0, passed: false })] })).toBe(false);
  });
});

describe('expected vs actual tools', () => {
  it('pairs in order, marks extras and missing', () => {
    const rows = toolDiffRows(
      [expectedToolOutput({ name: 'get_policy', args: { policy_id: 'P-1' } }), expectedToolOutput({ name: 'open_claim' })],
      [toolCall({ name: 'lookup' }), toolCall({ name: 'get_policy', args: { policy_id: 'P-1', extra: 1 } })],
    );
    expect(rows.map((r) => r.status)).toEqual(['extra', 'match', 'missing']);
  });

  it('flags differing arguments', () => {
    const rows = toolDiffRows(
      [expectedToolOutput({ name: 'get_policy', args: { policy_id: 'P-1' } })],
      [toolCall({ name: 'get_policy', args: { policy_id: 'P-2' } })],
    );
    expect(rows[0].status).toBe('args-differ');
  });

  it('no tool-call data is no-data, never "missing"', () => {
    expect(toolDiffRows([expectedToolOutput({ name: 'a' })], null).map((r) => r.status)).toEqual(['no-data']);
  });

  it('trailing extras are listed', () => {
    expect(toolDiffRows([], [toolCall({ name: 'a' })]).map((r) => r.status)).toEqual(['extra']);
  });

  it('a bare expected name matches the MCP-prefixed actual call, as the gate does', () => {
    const rows = toolDiffRows([expectedToolOutput({ name: 'getPolicy' })], [toolCall({ name: 'claims_walk__api___getPolicy' })]);
    expect(rows.map((r) => r.status)).toEqual(['match']);
  });
});

describe('toolNameMatches — the backend tool_name_matches rule', () => {
  it('equal names match', () => {
    expect(toolNameMatches('getPolicy', 'getPolicy')).toBe(true);
  });
  it('actual ending in "__" + expected matches', () => {
    expect(toolNameMatches('api___getPolicy', 'claims_walk__api___getPolicy')).toBe(true);
    expect(toolNameMatches('getPolicy', 'claims__getPolicy')).toBe(true);
  });
  it('bare expected matches actual ending in "___" + expected', () => {
    expect(toolNameMatches('getPolicy', 'claims_walk__api___getPolicy')).toBe(true);
  });
  it('an expected name with "__" still needs the whole name as the suffix', () => {
    expect(toolNameMatches('x__getPolicy', 'srv___x__getPolicyz')).toBe(false);
    expect(toolNameMatches('a__b', 'q___a__bb')).toBe(false);
  });
  it('a mere suffix without the separator does not match', () => {
    expect(toolNameMatches('Policy', 'getPolicy')).toBe(false);
    expect(toolNameMatches('getPolicy', 'api_getPolicy')).toBe(false);
  });
});

describe('the add-item form', () => {
  const form = { id: 'fnol-001', input: 'My car was hit', expectedOutput: '', assertions: 'Opens a claim\n\n Quotes the number ', tools: [{ name: 'get_policy', args: '{"policy_id":"P-1"}' }, { name: '', args: '' }] };

  it('builds a dataset item, dropping blanks', () => {
    expect(itemFromForm(form, new Set())).toEqual({
      item: {
        id: 'fnol-001',
        input: 'My car was hit',
        assertions: ['Opens a claim', 'Quotes the number'],
        expected_tools: [{ name: 'get_policy', args: { policy_id: 'P-1' } }],
      },
      errors: [],
    });
  });

  it('refuses a bad id, a duplicate, an empty input and non-object args', () => {
    expect(itemFromForm({ ...form, id: 'bad id' }, new Set()).errors[0]).toMatch(/^ID/);
    expect(itemFromForm(form, new Set(['fnol-001'])).errors[0]).toMatch(/already/);
    expect(itemFromForm({ ...form, input: '  ' }, new Set()).errors).toContain('Input is required');
    expect(itemFromForm({ ...form, tools: [{ name: 'x', args: '[1]' }] }, new Set()).errors[0]).toMatch(/JSON object/);
    expect(itemFromForm({ ...form, tools: [{ name: '', args: '{}' }] }, new Set()).errors[0]).toMatch(/name is required/);
  });

  it('suggests the next free id', () => {
    expect(suggestItemId([])).toBe('item-001');
    expect(suggestItemId(['a', 'item-002'])).toBe('item-003');
  });
});

describe('the upload preview', () => {
  it('accepts the documented format and skips blank lines', () => {
    const text = '{"id":"a","input":"hi","expected_tools":[{"name":"t","args":{"k":1}}]}\n\n{"id":"b","input":"yo"}\n';
    const out = previewJsonl(text);
    expect(out.errors).toEqual([]);
    expect(out.items.map((i) => i.id)).toEqual(['a', 'b']);
  });

  it('collects every error with its line number', () => {
    const text = ['not json', '{"id":"a","input":"x"}', '{"id":"a","input":"y"}', '{"id":"b"}', '{"id":"c","input":"z","colour":1}'].join('\n');
    expect(previewJsonl(text).errors).toEqual([
      'line 1: not valid JSON',
      'line 3: duplicate id "a"',
      'line 4: "input" is required',
      'line 5: unknown field "colour"',
    ]);
  });

  it('caps the dataset and refuses an empty file', () => {
    const many = Array.from({ length: 51 }, (_, i) => JSON.stringify({ id: `i${i}`, input: 'x' })).join('\n');
    expect(previewJsonl(many).errors.at(-1)).toMatch(/at most 50 items/);
    expect(previewJsonl('\n\n').errors).toEqual(['the file has no items']);
  });
});

describe('settings and links', () => {
  it('threshold is 0..1', () => {
    expect(parseThreshold('0.8')).toBe(0.8);
    expect(parseThreshold('1')).toBe(1);
    expect(parseThreshold('1.2')).toBeNull();
    expect(parseThreshold('')).toBeNull();
    expect(parseThreshold('abc')).toBeNull();
  });

  it('offers exactly the four GA built-in judges', () => {
    expect(JUDGE_CHOICES.map((j) => j.id)).toEqual([
      'Builtin.GoalSuccessRate',
      'Builtin.Correctness',
      'Builtin.Helpfulness',
      'Builtin.ToolSelectionAccuracy',
    ]);
  });

  it('links the dataset file on GitHub', () => {
    expect(githubFileUrl('https://github.com/acme/agent.git', 'evals/dataset.jsonl')).toBe('https://github.com/acme/agent/blob/HEAD/evals/dataset.jsonl');
    expect(githubFileUrl(null, 'evals/dataset.jsonl')).toBeNull();
  });

  it('the empty state names all three ways in and the override rule', () => {
    expect(EMPTY_DATASET_COPY.ways.map((w) => w.title)).toEqual(['From traces', 'Add item', 'Upload file']);
    expect(EMPTY_DATASET_COPY.gateNote).toMatch(/override/);
    expect(COMING_SOON.map((c) => c.title)).toEqual(['Judge calibration', 'Human review queue', 'Simulated users', 'Online monitoring']);
  });
});

describe('formatApiErrorDetail — the eval-file 422', () => {
  it('lists file errors after the message', () => {
    const detail = { message: 'the dataset is invalid', errors: ['evals/dataset.jsonl:3: duplicate id'] };
    expect(formatApiErrorDetail(detail, 'fallback')).toBe('the dataset is invalid: evals/dataset.jsonl:3: duplicate id');
  });
  it('still reads plain strings and FastAPI lists', () => {
    expect(formatApiErrorDetail('the dataset is empty', 'f')).toBe('the dataset is empty');
    expect(formatApiErrorDetail([{ loc: ['body', 'trigger'], msg: 'bad', type: 'x' }], 'f')).toBe('trigger: bad');
  });
});

describe('wiring guards', () => {
  it('the page mounts the tab through panelProps and hides it without an agent', () => {
    expect(pageSrc).toContain("{...panelProps('evaluations')}");
    expect(pageSrc).toContain('evalsTabVisible(repo)');
  });

  it('the page-level hook polls on the shared interval and dedupes auto-runs through the pure decision; the page owns the one read and hands its state to the tab', () => {
    expect(hookSrc).toContain('POLL_MS');
    expect(hookSrc).toContain('shouldAutoRun(');
    expect(hookSrc).toContain("startRun(projectId, repoId, 'auto')");
    expect(pageSrc).toContain('useRepoEvals(');
    expect(tabSrc).not.toContain('evalsApi.listRuns(');
  });

  it('a refused auto start stays quiet; the gate card already says what is missing', () => {
    expect(hookSrc).toMatch(/startRun\(projectId, repoId, 'auto'\)\s*\.then\(\(\) => loadRuns\(\)\)\s*\.catch\(\(\) => undefined\)/);
  });

  it('uploads validate server-side before a replace commit', () => {
    expect(tabSrc).toContain('evalsApi.validate(');
    expect(tabSrc).toContain("'replace'");
  });
});

describe('Run evals: disabled with a reason', () => {
  const ok = { mayWrite: true, itemCount: 2, fileErrors: 0, currentBuild: 'sha256:d', active: false };
  it('says why, most pressing first', () => {
    expect(runBlockedReason(ok)).toBeNull();
    expect(runBlockedReason({ ...ok, active: true })).toBe('A run is in progress');
    expect(runBlockedReason({ ...ok, mayWrite: false })).toMatch(/Maintainer/);
    expect(runBlockedReason({ ...ok, currentBuild: null })).toBe('Deploy to dev first');
    expect(runBlockedReason({ ...ok, currentBuild: 'claims-abc1234' })).toBeNull();
    expect(runBlockedReason({ ...ok, fileErrors: 2 })).toMatch(/errors/);
    expect(runBlockedReason({ ...ok, itemCount: 0 })).toBe('Add dataset items first');
  });
});

describe('build identity: digest, else tag (harness repos have only a tag)', () => {
  it('currentBuild prefers the digest, falls back to the tag', () => {
    expect(currentBuild({ current_digest: 'sha256:d', current_build_tag: 't-abc1234' })).toBe('sha256:d');
    expect(currentBuild({ current_digest: null, current_build_tag: 't-abc1234' })).toBe('t-abc1234');
    expect(currentBuild({ current_digest: null, current_build_tag: null })).toBeNull();
    expect(currentBuild({ current_digest: null })).toBeNull();
    expect(currentBuild(null)).toBeNull();
  });
  it('shortBuildTag: commit sha → 7, container tag → the sha after the last dash', () => {
    expect(shortBuildTag('a'.repeat(40))).toBe('aaaaaaa');
    expect(shortBuildTag('claims_agent-abc1234')).toBe('abc1234');
    expect(shortBuildTag('plain')).toBe('plain');
    expect(shortBuildTag(null)).toBeNull();
  });
  it('buildLabel: build tag, else digest, else commit sha, else the current identity', () => {
    expect(buildLabel({ git_sha: SHA, build_tag: 'x-1' })).toBe('build 1');
    expect(buildLabel({ git_sha: SHA, build_tag: 'b'.repeat(40) })).toBe('build bbbbbbb');
    expect(buildLabel({ git_sha: SHA })).toBe('build abc1234');
    expect(buildLabel({ digest: null, build_tag: 'claims-def5678' })).toBe('build def5678');
    expect(buildLabel(null, 'claims-def5678')).toBe('build def5678');
    expect(buildLabel(null, 'sha256:' + 'f'.repeat(64))).toMatch(/^build sha256:f+…$/);
    expect(buildLabel(null, null)).toBeNull();
  });
});

function apiFailure({ status, code, detail = '', message = detail }: { status: number; code: ErrorCode | null; detail?: string; message?: string }): ApiFailure {
  return new ApiFailure(status, code, detail, message);
}

describe('COMMIT_REFUSAL_CODES — typed on the generated ErrorCode', () => {
  it('satisfies Record<CommitRefusalKind, readonly ErrorCode[]>, so a renamed backend code fails tsc where this table names it', () => {
    expectTypeOf(COMMIT_REFUSAL_CODES).toExtend<Record<CommitRefusalKind, readonly ErrorCode[]>>();
  });
  it('classifies by code alone: the detail under no code is unrecognised, a reworded detail under the code is not', () => {
    expect(classifyCommitFailure(apiFailure({ status: 409, code: 'eval.conflict', detail: 'reworded' }))).toBe('conflict');
    expect(classifyCommitFailure(apiFailure({ status: 409, code: 'eval.trunk_mismatch' }))).toBe('trunk_mismatch');
    expect(classifyCommitFailure(apiFailure({ status: 409, code: null, detail: 'the branch moved; reload and retry' }))).toBeNull();
    expect(classifyCommitFailure(apiFailure({ status: 422, code: 'eval.empty', detail: 'the dataset is empty' }))).toBeNull();
  });
});

describe('commit errors: say what to do', () => {
  const conflict = apiFailure({ status: 409, code: 'eval.conflict', detail: 'the branch moved; reload and retry' });
  it('a branch race: press the same button again, edits are kept', () => {
    const t = commitErrorText(conflict, 'Save');
    expect(t).toBe('The branch changed while you were editing — press Save again; your edits are kept.');
    expect(commitErrorText(conflict, 'Replace')).toMatch(/press Replace again/);
  });
  it('a default branch that is not the trunk is explained plainly', () => {
    const t = commitErrorText(apiFailure({ status: 409, code: 'eval.trunk_mismatch', detail: "the repository's default branch is not the project trunk" }), 'Save');
    expect(t).toMatch(/default branch on GitHub is not the project’s trunk/);
    expect(t).not.toMatch(/'s default branch is not the project trunk/);
  });
  it('anything else passes through as sent, and a rejection with no message gets the generic line', () => {
    expect(commitErrorText(apiFailure({ status: 422, code: 'eval.empty', detail: 'the dataset is empty' }), 'Save')).toBe('the dataset is empty');
    expect(commitErrorText(new Error('the dataset is empty'), 'Save')).toBe('the dataset is empty');
    expect(commitErrorText(undefined, 'Save')).toBe('Something went wrong');
  });
});

describe('run detail: expected tools from the run’s own dataset', () => {
  const current = new Map([['i1', datasetItem({ id: 'i1', input: 'now', expected_tools: [expectedToolOutput({ name: 'new_tool' })] })]]);
  const snapshot = [datasetItem({ id: 'i1', input: 'then', expected_tools: [expectedToolOutput({ name: 'old_tool' })] })];
  it('prefers the snapshot the run used', () => {
    expect(expectedItemFor('i1', snapshot, current)).toEqual({ item: snapshot[0], fromSnapshot: true });
  });
  it('falls back to the current dataset when the run saved none', () => {
    expect(expectedItemFor('i1', [], current)).toEqual({ item: current.get('i1'), fromSnapshot: false });
    expect(expectedItemFor('i1', undefined, current).item?.input).toBe('now');
    expect(expectedItemFor('gone', [], current).item).toBeUndefined();
  });
});

describe('judgeExplanation', () => {
  it('splits the "ignored reference inputs" suffix into a readable note', () => {
    expect(judgeExplanation('The answer opens a claim. (ignored reference inputs: assertions, expectedResponse)')).toEqual({
      text: 'The answer opens a claim.',
      unused: 'Not used by this judge: assertions, expected response',
    });
    expect(judgeExplanation('(ignored reference inputs: expectedResponse)')).toEqual({
      text: null,
      unused: 'Not used by this judge: expected response',
    });
  });
  it('leaves other text alone', () => {
    expect(judgeExplanation('Fine.')).toEqual({ text: 'Fine.', unused: null });
    expect(judgeExplanation(null)).toEqual({ text: null, unused: null });
  });
});

describe('devEvalState — the DEV build\'s eval state, for the header', () => {
  const B = 'sha256:' + 'f'.repeat(64);
  it('null until the runs are read, or with no dev build', () => {
    expect(devEvalState(null, NOW)).toBeNull();
    expect(devEvalState({ current_digest: null, current_build_tag: null, current_run: null }, NOW)).toBeNull();
  });
  it('none without a run, or when the run stopped responding', () => {
    expect(devEvalState({ current_digest: B, current_run: null }, NOW)).toBe('none');
    const stale = evalRun({ status: 'running', started_at: new Date(NOW - STALE_RUNNING_MS - 1).toISOString(), finished_at: null });
    expect(devEvalState({ current_digest: B, current_run: stale }, NOW)).toBe('none');
  });
  it('maps the run status: error is inconclusive', () => {
    expect(devEvalState({ current_digest: B, current_run: evalRun() }, NOW)).toBe('passed');
    expect(devEvalState({ current_digest: B, current_run: evalRun({ status: 'failed' }) }, NOW)).toBe('failed');
    expect(devEvalState({ current_digest: B, current_run: evalRun({ status: 'error' }) }, NOW)).toBe('inconclusive');
    const live = evalRun({ status: 'running', started_at: new Date(NOW - 1000).toISOString(), finished_at: null });
    expect(devEvalState({ current_build_tag: 'x-abc', current_run: live }, NOW)).toBe('running');
  });
});

describe('when, json, errText — the text the run and dataset sections render', () => {
  it('when shows a timestamp as a date that keeps the year and is neither the mark nor the raw string, and a missing one as the no-value mark', () => {
    const iso = '2026-10-06T11:55:00Z';
    expect(when(iso)).not.toBe(NO_VALUE);
    expect(when(iso)).not.toBe(iso);
    expect(when(iso)).toContain('2026');
    expect(when(null)).toBe(NO_VALUE);
    expect(when(undefined)).toBe(NO_VALUE);
    expect(when('')).toBe(NO_VALUE);
  });
  it('json writes tool arguments as compact JSON', () => {
    expect(json({ policy_id: 'P-1001' })).toBe('{"policy_id":"P-1001"}');
    expect(json(null)).toBe('null');
  });
  it('errText reads the message of an Error and gives the generic line for anything else', () => {
    expect(errText(new Error('the run could not be read'))).toBe('the run could not be read');
    expect(errText(apiFailure({ status: 500, code: null, detail: 'boom' }))).toBe('boom');
    expect(errText('boom')).toBe('Something went wrong');
    expect(errText(undefined)).toBe('Something went wrong');
  });
});
