import { describe, expect, it } from 'vitest';

import type { EvalConfigOutput, TraceSummary } from '../../../api/client';
import { ApiFailure } from '../../../api/failure';
import {
  DEFAULT_TRACE_STAGE,
  TRACES_COPY,
  TRACE_STAGES,
  commitErrorText,
  committedFlash,
  datasetReadWarning,
  draftFromTrace,
  draftsFromTraces,
  matchModeNote,
  selectable,
  shortSuffix,
  slugify,
  traceFromInvoke,
  traceRowView,
  validateDrafts,
  whenText,
} from './tracesToDataset';
import observabilityTabRaw from './ObservabilityTab.tsx?raw';
import modalRaw from './AddToDatasetModal.tsx?raw';
import invokePanelRaw from '../../governance/InvokePanel.tsx?raw';
import agentDetailRaw from '../../governance/AgentDetail.tsx?raw';
import { stripComments } from '../../../rawSource';

const observabilityTabSrc = stripComments(observabilityTabRaw);
const modalSrc = stripComments(modalRaw);
const invokePanelSrc = stripComments(invokePanelRaw);
const agentDetailSrc = stripComments(agentDetailRaw);

function trace(over: Partial<TraceSummary> = {}): TraceSummary {
  return {
    session_id: 'sess-0f3a-9b2c',
    trace_id: 'trace-1',
    started_at: '2026-10-06T10:00:00Z',
    input: 'My car was hit on the A40, policy P-1001',
    output: 'A claim is opened: C-77.',
    tool_calls: [
      { name: 'skills', args: { name: 'fnol' } },
      { name: 'claims__api___getPolicy', args: { policyId: 'P-1001' } },
      { name: 'claims__api___openClaim', args: { policyId: 'P-1001', description: 'Rear-ended on the A40' } },
    ],
    latency_ms: 2400,
    ...over,
  };
}

const NOW = Date.parse('2026-10-06T10:30:00Z');

describe('the traces list', () => {
  it('offers both stages and defaults to the first', () => {
    expect(TRACE_STAGES.map((s) => s.value)).toHaveLength(2);
    expect(DEFAULT_TRACE_STAGE).toBe(TRACE_STAGES[0].value);
  });

  it('shows when, the input, the tools in order and the latency — the session id only as hover text', () => {
    const v = traceRowView(trace(), NOW);
    expect(v.when).toBe('30 min ago');
    expect(v.input).toBe('My car was hit on the A40, policy P-1001');
    expect(v.tools).toEqual(['skills', 'claims__api___getPolicy', 'claims__api___openClaim']);
    expect(v.latency).toBe('2.4 s');
    expect(v.title).toContain('sess-0f3a-9b2c');
    expect(v.input).not.toContain('sess-');
  });

  it('truncates a long input to one line', () => {
    const v = traceRowView(trace({ input: `line one\n\n${'x'.repeat(400)}` }), NOW);
    expect(v.input?.length).toBeLessThanOrEqual(140);
    expect(v.input).not.toContain('\n');
    expect(v.input?.endsWith('…')).toBe(true);
  });

  it('keeps "no tool-call data" apart from "no tools"', () => {
    expect(traceRowView(trace({ tool_calls: null }), NOW).tools).toBeNull();
    expect(traceRowView(trace({ tool_calls: [] }), NOW).tools).toEqual([]);
  });

  it('reads relative times and survives a missing or bad start time', () => {
    expect(whenText('2026-10-06T10:29:40Z', NOW)).toBe('just now');
    expect(whenText('2026-10-06T07:00:00Z', NOW)).toBe('3 h ago');
    expect(whenText(null, NOW)).toBeNull();
    expect(whenText('not a date', NOW)).toBeNull();
    expect(traceRowView(trace({ started_at: null }), NOW).title).toContain('start time unknown');
  });

  it('a trace without an input cannot be selected', () => {
    expect(selectable(trace())).toBe(true);
    expect(selectable(trace({ input: null }))).toBe(false);
    expect(selectable(trace({ input: '   ' }))).toBe(false);
  });
});

describe('ids', () => {
  it('slugs the first words of the input', () => {
    expect(slugify('My car was hit on the A40, policy P-1001')).toBe('my-car-was-hit-on-the-a40-policy-p-1001');
    expect(slugify('Ünïcödé wörds')).toBe('unicode-words');
    expect(slugify('???')).toBe('trace');
    expect(slugify('a'.repeat(80)).length).toBe(40);
  });

  it('suffixes with the end of the session id', () => {
    expect(shortSuffix('sess-0f3a-9b2c')).toBe('9b2c');
    expect(shortSuffix('---')).toBe('0000');
  });

  it('is always a valid item id and unique against the dataset and the batch', () => {
    const t = trace({ input: 'x'.repeat(200), session_id: 'S'.repeat(80) });
    const d = draftFromTrace(t, new Set());
    expect(d.id).toMatch(/^[A-Za-z0-9._-]{1,64}$/);
    const twin = trace();
    const drafts = draftsFromTraces([twin, twin], [draftFromTrace(twin, new Set()).id]);
    expect(new Set([...drafts.map((x) => x.id), draftFromTrace(twin, new Set()).id]).size).toBe(3);
    expect(drafts[0].id.endsWith('-2')).toBe(true);
  });
});

describe('a draft', () => {
  it('pre-fills only what the next run must repeat: gateway tools, their id args, a generic answer', () => {
    const d = draftFromTrace(trace(), new Set());
    expect(d.input).toBe('My car was hit on the A40, policy P-1001');
    expect(d.expectedOutput).toBe('Uses getPolicy and openClaim and answers from their results.');
    expect(d.tools).toEqual([
      { name: 'claims__api___getPolicy', args: '{"policyId":"P-1001"}' },
      { name: 'claims__api___openClaim', args: '{"policyId":"P-1001"}' },
    ]);
    expect(d.sourceTraceId).toBe('trace-1');
  });

  it('collapses consecutive repeats, keeps the call order, drops non-id and non-scalar args', () => {
    const d = draftFromTrace(
      trace({
        tool_calls: [
          { name: 'claims__api___getPolicy', args: { policy_id: 'P-1', note: 'x' } },
          { name: 'get_current_datetime', args: {} },
          { name: 'claims__api___getPolicy', args: { policy_id: 'P-1' } },
          { name: 'claims__api___openClaim', args: { claimId: { nested: 1 }, id: 7 } },
        ],
      }),
      new Set(),
    );
    expect(d.tools).toEqual([
      { name: 'claims__api___getPolicy', args: '{"policy_id":"P-1"}' },
      { name: 'claims__api___openClaim', args: '{"id":7}' },
    ]);
    expect(d.expectedOutput).toBe('Uses getPolicy and openClaim and answers from their results.');
  });

  it('one gateway tool reads "its results"; no gateway tool keeps the reply as the golden answer', () => {
    expect(draftFromTrace(trace({ tool_calls: [{ name: 'claims__api___getPolicy', args: {} }] }), new Set()).expectedOutput).toBe(
      'Uses getPolicy and answers from its results.',
    );
    const none = draftFromTrace(trace({ tool_calls: [{ name: 'skills', args: {} }] }), new Set());
    expect(none.tools).toEqual([]);
    expect(none.expectedOutput).toBe('A claim is opened: C-77.');
  });

  it('falls back to the session id as the source trace id', () => {
    expect(draftFromTrace(trace({ trace_id: null }), new Set()).sourceTraceId).toBe('sess-0f3a-9b2c');
  });

  it('skips traces that recorded no input', () => {
    expect(draftsFromTraces([trace({ input: null }), trace()], [])).toHaveLength(1);
  });
});

describe('validateDrafts', () => {
  it('turns clean drafts into dataset items carrying their source trace', () => {
    const drafts = draftsFromTraces([trace()], []);
    const v = validateDrafts(drafts, new Set());
    expect(v.errors).toEqual({});
    expect(v.items).toEqual([
      {
        id: drafts[0].id,
        input: 'My car was hit on the A40, policy P-1001',
        expected_output: 'Uses getPolicy and openClaim and answers from their results.',
        expected_tools: [
          { name: 'claims__api___getPolicy', args: { policyId: 'P-1001' } },
          { name: 'claims__api___openClaim', args: { policyId: 'P-1001' } },
        ],
        source_trace_id: 'trace-1',
      },
    ]);
  });

  it('refuses ids used twice in the batch, ids already in the dataset, and bad args JSON', () => {
    const [a, b] = draftsFromTraces([trace(), trace({ session_id: 'other-1111' })], []);
    const dup = { ...b, id: a.id };
    const v1 = validateDrafts([a, dup], new Set());
    expect(v1.items).toEqual([]);
    expect(v1.errors[a.key].join()).toMatch(/more than one item/);

    const v2 = validateDrafts([a], new Set([a.id]));
    expect(v2.errors[a.key].join()).toMatch(/already in the dataset/);

    const bad = { ...a, tools: [{ name: 'claims__api___getPolicy', args: '{not json' }] };
    expect(validateDrafts([bad], new Set()).errors[a.key].join()).toMatch(/JSON object/);
  });

  it('refuses a batch that would push the dataset past 50 items', () => {
    const existing = new Set(Array.from({ length: 50 }, (_, i) => `e-${i}`));
    const v = validateDrafts(draftsFromTraces([trace()], existing), existing);
    expect(v.items).toEqual([]);
    expect(v.batchErrors[0]).toMatch(/at most 50/);
  });
});

describe('copy', () => {
  it('says the commit started a dev build', () => {
    expect(committedFlash(1)).toMatch(/^Committed 1 item to/);
    expect(committedFlash(3)).toMatch(/3 items.*dev build has started/);
  });

  it('turns the eval.conflict 409 into press-Commit-again (edits kept), and passes anything else through as sent', () => {
    const moved = 'the branch moved; reload and retry';
    const text = commitErrorText(new ApiFailure(409, 'eval.conflict', moved, moved));
    expect(text).toMatch(/branch changed while you were editing/);
    expect(text).toMatch(/Press Commit again/);
    expect(text).toMatch(/edits are kept/);
    expect(commitErrorText(new ApiFailure(409, 'eval.conflict', 'reworded', 'reworded'))).toMatch(/Press Commit again/);
    expect(commitErrorText(new ApiFailure(409, null, moved, moved))).toBe(moved);
    expect(commitErrorText(new ApiFailure(409, 'eval.trunk_mismatch', 'not the trunk', 'not the trunk'))).toBe('not the trunk');
    expect(commitErrorText(new Error('Needs the Maintainer role'))).toBe('Needs the Maintainer role');
    expect(commitErrorText('boom')).toBe('Something went wrong');
  });

  it('warns before editing when the current dataset was unreadable or has errors', () => {
    expect(datasetReadWarning({ failed: false, errors: 0 })).toBeNull();
    expect(datasetReadWarning({ failed: true, errors: 0 })).toMatch(/Could not read.*may be refused/);
    expect(datasetReadWarning({ failed: false, errors: 2 })).toMatch(/2 errors.*may be refused/);
  });

  it('names the match modes from Settings when known', () => {
    const cfg = { checks: { tools: 'in_order', args: 'subset', no_error: true } } as EvalConfigOutput;
    expect(matchModeNote(cfg)).toMatch(/tools in order, arguments subset/);
    expect(matchModeNote(null)).toMatch(/Settings/);
  });

  it('both empty states point at CloudWatch or the Evaluations tab', () => {
    expect(TRACES_COPY.emptyNone.detail).toMatch(/CloudWatch/);
    expect(TRACES_COPY.emptyNone.detail).toMatch(/Evaluations tab/);
    expect(TRACES_COPY.emptyList.detail).toMatch(/Evaluations tab/);
  });
});

describe('wiring (source guards)', () => {
  it('the tab reads traces and the modal commits with append', () => {
    expect(observabilityTabSrc.length).toBeGreaterThan(0);
    expect(observabilityTabSrc).toMatch(/evalsApi\s*\.traces\(/);
    expect(observabilityTabSrc).toContain('AddToDatasetModal');
    expect(modalSrc).toContain("evalsApi.addItems(repo.project_id, repo.id, items, 'append')");
    expect(modalSrc).toContain('validateDrafts');
  });

  it('the tab never renders the session id as row text', () => {
    expect(observabilityTabSrc).not.toMatch(/>\s*\{\s*t(race)?\.session_id\s*\}/);
  });
});

describe('an invoke as a trace', () => {
  const run = {
    prompt: '  My car was hit on the A40, policy P-1001 ',
    response: 'A claim is opened: C-77.',
    toolCalls: [
      { name: 'skills', args: { name: 'fnol' } },
      { name: 'claims__api___getPolicy', args: { policyId: 'P-1001' } },
      { name: 'claims__api___openClaim', args: null },
    ],
    sessionId: 'sess-0f3a-9b2c',
  };

  it('pre-fills input = the prompt, the same generic answer and gateway tools as a trace', () => {
    const t = traceFromInvoke(run, '2026-10-06T10:00:00Z');
    expect(t).not.toBeNull();
    const d = draftFromTrace(t!, new Set());
    expect(d.input).toBe('My car was hit on the A40, policy P-1001');
    expect(d.expectedOutput).toBe('Uses getPolicy and openClaim and answers from their results.');
    expect(d.tools).toEqual([
      { name: 'claims__api___getPolicy', args: '{"policyId":"P-1001"}' },
      { name: 'claims__api___openClaim', args: '' },
    ]);
    expect(d.sourceTraceId).toBe('sess-0f3a-9b2c');
  });

  it('a structured reply leaves the golden answer blank; no tool source gives no expected tools', () => {
    const t = traceFromInvoke({ ...run, response: { a: 1 }, toolCalls: null }, '2026-10-06T10:00:00Z');
    const d = draftFromTrace(t!, new Set());
    expect(d.expectedOutput).toBe('');
    expect(d.tools).toEqual([]);
  });

  it('offers nothing without a session id or a prompt', () => {
    expect(traceFromInvoke({ ...run, sessionId: null }, '2026-10-06T10:00:00Z')).toBeNull();
    expect(traceFromInvoke({ ...run, prompt: '   ' }, '2026-10-06T10:00:00Z')).toBeNull();
  });

  it('the saved item validates and carries the session id as its source', () => {
    const t = traceFromInvoke(run, '2026-10-06T10:00:00Z')!;
    const v = validateDrafts([draftFromTrace(t, new Set())], new Set());
    expect(v.items).toHaveLength(1);
    expect(v.items[0].source_trace_id).toBe('sess-0f3a-9b2c');
    expect(v.items[0].expected_tools?.map((x) => x.name)).toEqual(['claims__api___getPolicy', 'claims__api___openClaim']);
  });

  it('the invoke panel opens the same modal (same save path), says saving starts a build, and is gated like the save route: platform Operator and project Maintainer', () => {
    expect(invokePanelSrc).toContain('traceFromInvoke(');
    expect(invokePanelSrc).toContain('<AddToDatasetModal');
    expect(invokePanelSrc).toContain('INVOKE_ADD_COPY.button');
    expect(invokePanelSrc).toContain('INVOKE_ADD_COPY.hint');
    expect(invokePanelSrc).toMatch(/datasetRepo && result\.trace &&/);
    expect(agentDetailSrc).toContain('datasetRepo={isOperatorOrHigher && mayMaintainOpsProject ? opsRepo ?? null : null}');
    expect(agentDetailSrc).toMatch(/projectsApi\s*\.get\(opsProjectId\)/);
    expect(agentDetailSrc).toContain('mayMaintainProject(effectiveRole(detail), roleLevel, detail.ungoverned)');
    expect(agentDetailSrc).not.toContain('datasetRepo={roleLevel >= 1');
  });

  it('the invoke panel offers the button once per invoke: gone after a commit, back on the next Run', () => {
    expect(invokePanelSrc).toMatch(/addedFlash === null && \(\s*<button/);
    expect(invokePanelSrc).toMatch(/setResult\(null\);\s*setAddedFlash\(null\);/);
  });
});
