import { describe, it, expect } from 'vitest';
import type { TraceRow } from '../../../api/client';
import {
  mapTraceRows,
  deriveTraceUrl,
  totalPages,
  EM_DASH,
  OPS_OBSERVABILITY_TAB,
  isHarnessAgent,
  tracesEmptyState,
} from './agentObservability';

const FULL: TraceRow = {
  id: 't-1',
  timestamp: '2026-07-15T10:30:00Z',
  name: 'invoke-agent',
  user_id: 'user@acme.test',
  latency_ms: 1234.6,
  cost_usd: 0.0123,
};

const SPARSE: TraceRow = {
  id: 't-2',
  timestamp: null,
  name: null,
  user_id: null,
  latency_ms: null,
  cost_usd: 0,
};

describe('TracesTab · mapTraceRows', () => {
  it('maps a fully-populated trace into display cells: id kept for keying, latency rounded and thousands-separated, a sub-dollar cost keeping its digits, the timestamp formatted, the raw row kept for drill-down', () => {
    const [row] = mapTraceRows([FULL]);
    expect(row.id).toBe('t-1');
    expect(row.name).toBe('invoke-agent');
    expect(row.user).toBe('user@acme.test');
    expect(row.latency).toBe('1,235 ms');
    expect(row.cost).toBe('$0.0123');
    expect(row.timestamp).not.toBe(EM_DASH);
    expect(row.raw).toBe(FULL);
  });

  it('does not round a sub-cent cost away to $0.00: a real Langfuse per-trace cost is fractions of a cent, so the precision scales with the magnitude', () => {
    const [row] = mapTraceRows([{ ...FULL, cost_usd: 0.004008 }]);
    expect(row.cost).not.toBe('$0.00');
    expect(row.cost).toBe('$0.004008');
  });

  it('renders every Optional field Langfuse omitted as "—" without crashing; a zero cost renders as $0.00', () => {
    const [row] = mapTraceRows([SPARSE]);
    expect(row.timestamp).toBe(EM_DASH);
    expect(row.name).toBe(EM_DASH);
    expect(row.latency).toBe(EM_DASH);
    expect(row.user).toBe(EM_DASH);
    expect(row.cost).toBe('$0.00');
  });

  it('empty-state: [] maps to [] (the caller renders the calm no-traces card)', () => {
    expect(mapTraceRows([])).toEqual([]);
  });

  it('tolerates an unparseable timestamp by showing the raw string, not a crash', () => {
    const [row] = mapTraceRows([{ ...FULL, timestamp: 'not-a-date' }]);
    expect(row.timestamp).toBe('not-a-date');
  });
});

describe('TracesTab · deriveTraceUrl', () => {
  it('builds a Langfuse deep link from the settings host (trailing slash trimmed)', () => {
    expect(deriveTraceUrl('https://lf.acme.test/', 't-1')).toBe('https://lf.acme.test/trace/t-1');
    expect(deriveTraceUrl('https://lf.acme.test', 't-1')).toBe('https://lf.acme.test/trace/t-1');
  });

  it('returns null when the host is unknown (Langfuse not wired in) → detail-only affordance', () => {
    expect(deriveTraceUrl(null, 't-1')).toBeNull();
    expect(deriveTraceUrl(undefined, 't-1')).toBeNull();
    expect(deriveTraceUrl('', 't-1')).toBeNull();
  });
});

describe('TracesTab · totalPages', () => {
  it('computes the page count, at least 1 so the footer never reads "of 0", even for a non-positive page size', () => {
    expect(totalPages(0, 20)).toBe(1);
    expect(totalPages(5, 0)).toBe(1);
    expect(totalPages(20, 20)).toBe(1);
    expect(totalPages(21, 20)).toBe(2);
    expect(totalPages(41, 20)).toBe(3);
  });
});

const RUNTIME_ARN = 'arn:aws:bedrock-agentcore:eu-west-1:000000000000:runtime/fnol-abc123';
const HARNESS_ARN = 'arn:aws:bedrock-agentcore:eu-west-1:000000000000:harness/fnol-h-abc123';

describe('TracesTab · isHarnessAgent: the ARN resource segment starts with harness/, in the same slots a runtime ARN uses', () => {
  it('true when the agent_arn scalar names a harness', () => {
    expect(isHarnessAgent({ agent_arn: HARNESS_ARN })).toBe(true);
  });

  it('true when any per-stage ARN names a harness (a legacy record may lack the scalar)', () => {
    expect(isHarnessAgent({ agent_arn: null, agent_arns: { a: RUNTIME_ARN, b: HARNESS_ARN } })).toBe(true);
  });

  it('false for a runtime ARN, an absent ARN, a malformed string, and "harness" outside the resource segment', () => {
    expect(isHarnessAgent({ agent_arn: RUNTIME_ARN })).toBe(false);
    expect(isHarnessAgent({ agent_arn: RUNTIME_ARN, agent_arns: { a: RUNTIME_ARN } })).toBe(false);
    expect(isHarnessAgent({})).toBe(false);
    expect(isHarnessAgent({ agent_arn: null, agent_arns: null })).toBe(false);
    expect(isHarnessAgent({ agent_arn: '', agent_arns: {} })).toBe(false);
    expect(isHarnessAgent({ agent_arn: 'arn:aws:bedrock-agentcore:eu-west-1:000000000000:runtime/harness-ish' })).toBe(false);
    expect(isHarnessAgent({ agent_arn: 'harness/not-an-arn' })).toBe(false);
  });
});

describe('TracesTab · tracesEmptyState: a harness traces to CloudWatch, never Langfuse, so its empty list is explained, not promised away', () => {
  const REPO = { repo_id: 'r-1', name: 'fnol-e40' };

  it('a non-harness agent keeps the Langfuse copy, with no Ops link, whatever the repo edge says', () => {
    for (const repo of [REPO, null, undefined]) {
      const s = tracesEmptyState({ agent_arn: RUNTIME_ARN }, repo);
      expect(s.variant).toBe('langfuse');
      expect(s.title).toBe('No traces recorded yet');
      expect(s.body).toContain('Langfuse');
      expect(s.opsLink).toBeNull();
    }
  });

  it('a harness with a resolved Ops repository points at that repository\'s Observability tab through the Ops object route', () => {
    const s = tracesEmptyState({ agent_arn: HARNESS_ARN }, REPO);
    expect(s.variant).toBe('harness');
    expect(s.title).toBe('Traces for this agent live in CloudWatch');
    expect(s.body).toContain('CloudWatch');
    expect(s.body).toContain('not Langfuse');
    expect(s.body).toContain('Observability tab');
    expect(s.opsLink).toEqual({
      name: 'fnol-e40',
      to: `/ops/agents/r-1?tab=${OPS_OBSERVABILITY_TAB}`,
    });
  });

  it('a harness with no Ops repository (none, or still resolving) says where traces are, without a link and without promising a repository it could not find', () => {
    for (const repo of [null, undefined]) {
      const s = tracesEmptyState({ agent_arn: HARNESS_ARN }, repo);
      expect(s.variant).toBe('harness');
      expect(s.title).toBe('Traces for this agent live in CloudWatch');
      expect(s.body).toContain('CloudWatch');
      expect(s.opsLink).toBeNull();
      expect(s.body).not.toMatch(/Open the Observability tab/);
    }
  });

  it('never blames Langfuse for a harness', () => {
    for (const repo of [REPO, null]) {
      const s = tracesEmptyState({ agent_arn: HARNESS_ARN }, repo);
      expect(s.body).not.toContain('has no Langfuse traces');
      expect(s.body).not.toContain('will appear here');
    }
  });
});
