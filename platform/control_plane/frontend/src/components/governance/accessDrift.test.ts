import { describe, it, expect } from 'vitest';
import {
  driftEntriesOf,
  driftPanelApplies,
  driftRows,
  driftSummary,
} from './accessDrift';
import type { DriftPanelSource } from './accessDrift';
import type { DriftEntry } from '../../api/client';
import {
  DRIFT_CLEAN_NOTE,
  DRIFT_DIRECTION_NOTE,
  DRIFT_KIND_LABEL,
  DRIFT_PANEL_NOTE,
  DRIFT_UNAVAILABLE_NOTE,
} from './platformLabels';

const governed: DriftPanelSource = {
  platform: 'databricks',
  auth_type: 'entra',
  runtime_handle: 'https://agp-agent-1234.aws.databricksapps.com',
};

function driftEntry(overrides: Partial<DriftEntry> = {}): DriftEntry {
  return {
    principal: 'lars.svensson@example.com',
    kind: 'user',
    level: 'CAN_USE',
    direction: 'unauthorized_acl',
    ...overrides,
  };
}

describe('driftPanelApplies', () => {
  it('is true for a Databricks-governed agent (all three legs present)', () => {
    expect(driftPanelApplies(governed)).toBe(true);
  });

  it('is false without a runtime handle: an inert metadata-only Databricks record has no app, so no ACL to drift from, and the backend gate requires the handle too', () => {
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

  it('never reads binding_mode: an sp_secret record carries the same asserted ACL, and a record whose mode never resolved still gets the panel', () => {
    const withMode: DriftPanelSource & { binding_mode: string } = { ...governed, binding_mode: 'sp_secret' };
    expect(driftPanelApplies(withMode)).toBe(true);
    expect(driftPanelApplies({ ...governed })).toBe(true);
  });
});

describe('driftRows', () => {
  it('maps unauthorized_acl to its verbatim copy and the fails-OPEN severity', () => {
    const [row] = driftRows([driftEntry()]);
    expect(row.note).toBe(DRIFT_DIRECTION_NOTE.unauthorized_acl);
    expect(row.note).toBe('Has platform access without an AGP grant');
    expect(row.severity).toBe('warning');
    expect(row.tint).toContain('amber');
  });

  it('maps missing_acl to its verbatim copy and the fails-CLOSED severity', () => {
    const [row] = driftRows([driftEntry({ direction: 'missing_acl' })]);
    expect(row.note).toBe(DRIFT_DIRECTION_NOTE.missing_acl);
    expect(row.note).toBe('AGP grant not enforced on the platform');
    expect(row.severity).toBe('info');
    expect(row.tint).not.toContain('amber');
  });

  it('passes the principal and the platform level through unchanged', () => {
    const [row] = driftRows([driftEntry({ principal: 'data-eng', kind: 'group', level: 'CAN_MANAGE' })]);
    expect(row.principal).toBe('data-eng');
    expect(row.level).toBe('CAN_MANAGE');
    expect(row.kindLabel).toBe(DRIFT_KIND_LABEL.group);
  });

  it('labels all three principal kinds', () => {
    const rows = driftRows([
      driftEntry({ kind: 'user' }),
      driftEntry({ kind: 'group' }),
      driftEntry({ kind: 'service_principal' }),
    ]);
    expect(rows.map((r) => r.kindLabel)).toEqual(['User', 'Group', 'Service principal']);
  });

  it('gives each row a distinct key when one principal appears in both directions, and when two entries differ only by level', () => {
    const rows = driftRows([
      driftEntry({ direction: 'unauthorized_acl' }),
      driftEntry({ direction: 'missing_acl' }),
      driftEntry({ direction: 'missing_acl', level: 'CAN_MANAGE' }),
    ]);
    expect(new Set(rows.map((r) => r.key)).size).toBe(3);
  });

  it('preserves the server ordering, since re-sorting a diff would make two reads of unchanged state look like a change, and returns [] for no entries', () => {
    const rows = driftRows([driftEntry({ principal: 'b' }), driftEntry({ principal: 'a' })]);
    expect(rows.map((r) => r.principal)).toEqual(['b', 'a']);
    expect(driftRows([])).toEqual([]);
  });
});

describe('driftEntriesOf', () => {
  it('passes a real entries array through', () => {
    const entries = [driftEntry()];
    expect(driftEntriesOf({ entries })).toEqual(entries);
    expect(driftEntriesOf({ entries: [] })).toEqual([]);
  });

  it('maps a body without entries to null, never to the positive "clean" answer: a proxy answering {}, a route-shape change or an error page served with a 200 is no evidence of a match', () => {
    expect(driftEntriesOf({})).toBeNull();
    expect(driftSummary(driftEntriesOf({})).state).toBe('unavailable');
    expect(driftSummary(driftEntriesOf({})).note).not.toBe(DRIFT_CLEAN_NOTE);
  });

  it('maps a non-array entries to null instead of letting it reach driftRows, where .map would throw during render', () => {
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
    const s = driftSummary([driftEntry(), driftEntry({ direction: 'missing_acl' })]);
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
