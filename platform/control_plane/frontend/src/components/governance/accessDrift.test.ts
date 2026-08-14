// Tests for accessDrift — the Access tab's platform-ACL drift decisions (E29/T13e, §3A).
//
// Pure-function tests only, for the reason platformLabels.test.ts states: vitest collects
// `src/**/*.test.ts` in a node environment, so the drift panel's DECISIONS are exported from a
// `.ts` module and pinned here, while `AccessTab.tsx` only binds and renders them.
import { describe, it, expect } from 'vitest';
import {
  driftEntriesOf,
  driftPanelApplies,
  driftRows,
  driftSummary,
} from './accessDrift';
import type { DriftPanelSource } from './accessDrift';
import type { AccessDriftEntry } from '../../api/client';
import {
  DRIFT_CLEAN_NOTE,
  DRIFT_DIRECTION_NOTE,
  DRIFT_KIND_LABEL,
  DRIFT_PANEL_NOTE,
  DRIFT_UNAVAILABLE_NOTE,
  DRIFT_UNKNOWN_DIRECTION_NOTE,
} from './platformLabels';

const governed: DriftPanelSource = {
  platform: 'databricks',
  auth_type: 'entra',
  runtime_handle: 'https://agp-agent-1234.aws.databricksapps.com',
};

const entry = (over: Partial<AccessDriftEntry> = {}): AccessDriftEntry => ({
  principal: 'lars.svensson@example.com',
  kind: 'user',
  level: 'CAN_USE',
  direction: 'unauthorized_acl',
  ...over,
});

describe('driftPanelApplies', () => {
  it('is true for a Databricks-governed agent (all three legs present)', () => {
    expect(driftPanelApplies(governed)).toBe(true);
  });

  it('is false without a runtime handle — an inert metadata-only Databricks record', () => {
    // No app exists, so there is no ACL to drift from. Mirrors the backend gate exactly:
    // `is_databricks_governed` requires the handle for this reason.
    expect(driftPanelApplies({ ...governed, runtime_handle: null })).toBe(false);
    expect(driftPanelApplies({ ...governed, runtime_handle: '' })).toBe(false);
  });

  it('is false for a non-Entra auth type — no grants to mirror', () => {
    expect(driftPanelApplies({ ...governed, auth_type: 'api_key' })).toBe(false);
    expect(driftPanelApplies({ ...governed, auth_type: 'none' })).toBe(false);
  });

  it('is false on every other platform, and on an absent platform', () => {
    expect(driftPanelApplies({ ...governed, platform: 'aws_bedrock' })).toBe(false);
    expect(driftPanelApplies({ ...governed, platform: 'azure' })).toBe(false);
    expect(driftPanelApplies({ ...governed, platform: null })).toBe(false);
    expect(driftPanelApplies({ ...governed, platform: undefined })).toBe(false);
  });
});

describe('driftRows', () => {
  it('maps unauthorized_acl to its verbatim copy and the fails-OPEN severity', () => {
    const [row] = driftRows([entry()]);
    expect(row.note).toBe(DRIFT_DIRECTION_NOTE.unauthorized_acl);
    expect(row.note).toBe('Has platform access without an AGP grant');
    expect(row.severity).toBe('warning');
    expect(row.tint).toContain('amber');
  });

  it('maps missing_acl to its verbatim copy and the fails-CLOSED severity', () => {
    const [row] = driftRows([entry({ direction: 'missing_acl' })]);
    expect(row.note).toBe(DRIFT_DIRECTION_NOTE.missing_acl);
    expect(row.note).toBe('AGP grant not enforced on the platform');
    expect(row.severity).toBe('info');
    expect(row.tint).not.toContain('amber');
  });

  it('passes the principal and the platform level through unchanged', () => {
    const [row] = driftRows([entry({ principal: 'data-eng', kind: 'group', level: 'CAN_MANAGE' })]);
    expect(row.principal).toBe('data-eng');
    expect(row.level).toBe('CAN_MANAGE');
    expect(row.kindLabel).toBe(DRIFT_KIND_LABEL.group);
  });

  it('labels all three principal kinds', () => {
    const rows = driftRows([
      entry({ kind: 'user' }),
      entry({ kind: 'group' }),
      entry({ kind: 'service_principal' }),
    ]);
    expect(rows.map((r) => r.kindLabel)).toEqual(['User', 'Group', 'Service principal']);
  });

  it('keeps a row whose direction this build does not know, with the neutral note', () => {
    // The backend may be newer than the bundle. Dropping the row would hide a principal;
    // printing the raw wire word would teach a vocabulary the product does not have.
    const rows = driftRows([entry({ direction: 'weird_new_direction' as AccessDriftEntry['direction'] })]);
    expect(rows).toHaveLength(1);
    expect(rows[0].note).toBe(DRIFT_UNKNOWN_DIRECTION_NOTE);
    expect(rows[0].severity).toBe('warning');
  });

  it('falls back on an unrecognised principal kind rather than rendering the raw wire value', () => {
    const rows = driftRows([entry({ kind: 'toaster' as AccessDriftEntry['kind'] })]);
    expect(rows[0].kindLabel).toBe('Principal');
  });

  it('survives a prototype-chain key in either field (the platformHostLabel bug class)', () => {
    // `in`/bare-index lookups walk the prototype chain, so 'constructor' would read a
    // non-string off Object and hand it to JSX. Both lookups must be own-property tests.
    const rows = driftRows([
      entry({ direction: 'constructor' as AccessDriftEntry['direction'], kind: 'toString' as AccessDriftEntry['kind'] }),
    ]);
    expect(typeof rows[0].note).toBe('string');
    expect(rows[0].note).toBe(DRIFT_UNKNOWN_DIRECTION_NOTE);
    expect(rows[0].kindLabel).toBe('Principal');
  });

  it('gives each row a key that distinguishes the two directions for one principal', () => {
    // A principal can legitimately appear in BOTH directions at different levels; React keys
    // must not collide or one row silently vanishes.
    const rows = driftRows([
      entry({ direction: 'unauthorized_acl' }),
      entry({ direction: 'missing_acl' }),
    ]);
    expect(new Set(rows.map((r) => r.key)).size).toBe(2);
  });

  it('preserves the server ordering and returns [] for no entries', () => {
    const rows = driftRows([entry({ principal: 'b' }), entry({ principal: 'a' })]);
    expect(rows.map((r) => r.principal)).toEqual(['b', 'a']);
    expect(driftRows([])).toEqual([]);
  });
});

describe('driftEntriesOf', () => {
  it('passes a real entries array through', () => {
    const entries = [entry()];
    expect(driftEntriesOf({ entries })).toEqual(entries);
    expect(driftEntriesOf({ entries: [] })).toEqual([]);
  });

  it('maps a body WITHOUT entries to null, never to the positive "clean" answer', () => {
    // A proxy answering `{}`, a route-shape change, an error page served with a 200: none of
    // those are evidence that the platform matches AGP's grants.
    expect(driftEntriesOf({})).toBeNull();
    expect(driftSummary(driftEntriesOf({})).state).toBe('unavailable');
    expect(driftSummary(driftEntriesOf({})).note).not.toBe(DRIFT_CLEAN_NOTE);
  });

  it('maps a non-array entries to null instead of letting it reach driftRows', () => {
    // Left un-guarded this survives a `?? []`, reports 'drifted' with an undefined count, and
    // then throws inside `.map` during render.
    expect(driftEntriesOf({ entries: {} })).toBeNull();
    expect(driftEntriesOf({ entries: 'CAN_USE' })).toBeNull();
    expect(driftEntriesOf({ entries: null })).toBeNull();
  });

  it('maps a non-object body to null', () => {
    expect(driftEntriesOf(null)).toBeNull();
    expect(driftEntriesOf(undefined)).toBeNull();
    expect(driftEntriesOf('')).toBeNull();
  });
});

describe('driftSummary', () => {
  it('reports drift and its count when entries exist', () => {
    const s = driftSummary([entry(), entry({ direction: 'missing_acl' })]);
    expect(s.state).toBe('drifted');
    expect(s.count).toBe(2);
    expect(s.note).toBe(DRIFT_PANEL_NOTE);
  });

  it('reports clean for an empty entry list — the post-re-assert success state', () => {
    const s = driftSummary([]);
    expect(s.state).toBe('clean');
    expect(s.count).toBe(0);
    expect(s.note).toBe(DRIFT_CLEAN_NOTE);
  });

  it('reports unavailable for null (the drift read failed) and NEVER claims the ACL is fine', () => {
    const s = driftSummary(null);
    expect(s.state).toBe('unavailable');
    expect(s.count).toBe(0);
    expect(s.note).toBe(DRIFT_UNAVAILABLE_NOTE);
    expect(s.note).not.toBe(DRIFT_CLEAN_NOTE);
  });
});
