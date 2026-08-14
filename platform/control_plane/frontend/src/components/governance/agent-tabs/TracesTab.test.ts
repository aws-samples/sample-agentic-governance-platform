import { describe, it, expect } from 'vitest';
import type { TraceRow } from '../../../types';
import {
  mapTraceRows,
  deriveTraceUrl,
  totalPages,
  EM_DASH,
} from './agentObservability';

// A normal, fully-populated trace row.
const FULL: TraceRow = {
  id: 't-1',
  timestamp: '2026-07-15T10:30:00Z',
  name: 'invoke-agent',
  user_id: 'user@acme.test',
  latency_ms: 1234.6,
  cost_usd: 0.0123,
};

// A row where Langfuse omitted the Optional fields (backend TraceRow emits these
// as null) — the Traces tab must render each as "—" and never crash.
const SPARSE: TraceRow = {
  id: 't-2',
  timestamp: null,
  name: null,
  user_id: null,
  latency_ms: null,
  cost_usd: 0,
};

describe('TracesTab · mapTraceRows', () => {
  it('maps a fully-populated trace into display cells (id preserved for keying)', () => {
    const [row] = mapTraceRows([FULL]);
    expect(row.id).toBe('t-1');
    expect(row.name).toBe('invoke-agent');
    expect(row.user).toBe('user@acme.test');
    expect(row.latency).toBe('1,235 ms'); // rounded, thousands-separated
    // Sub-dollar costs keep their significant digits: a fixed 2-decimal format
    // rendered every real per-trace cost (fractions of a cent) as "$0.00".
    expect(row.cost).toBe('$0.0123');
    expect(row.timestamp).not.toBe(EM_DASH); // formatted, not blank
    expect(row.raw).toBe(FULL); // original kept for drill-down
  });

  // Regression: a real Langfuse per-trace cost is fractions of a cent
  // ($0.004008 observed live). A fixed 2-decimal format showed "$0.00" for every
  // trace while Langfuse's own UI showed the real figure.
  it('does not round a sub-cent cost away to $0.00', () => {
    const [row] = mapTraceRows([{ ...FULL, cost_usd: 0.004008 }]);
    expect(row.cost).not.toBe('$0.00');
    expect(row.cost).toBe('$0.004008');
  });

  it('renders every null Optional field as "—" without crashing (the precondition fix)', () => {
    const [row] = mapTraceRows([SPARSE]);
    expect(row.timestamp).toBe(EM_DASH);
    expect(row.name).toBe(EM_DASH);
    expect(row.latency).toBe(EM_DASH);
    expect(row.user).toBe(EM_DASH);
    expect(row.cost).toBe('$0.00'); // cost_usd is non-null (defaults 0)
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
  it('computes page count from total rows and page size (always >= 1)', () => {
    expect(totalPages(0, 20)).toBe(1);
    expect(totalPages(20, 20)).toBe(1);
    expect(totalPages(21, 20)).toBe(2);
    expect(totalPages(41, 20)).toBe(3);
  });
});
