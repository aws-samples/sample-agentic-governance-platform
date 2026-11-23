import type { DriftEntry, AgentAuthType, Platform } from '../../api/client';
import {
  DRIFT_CLEAN_NOTE,
  DRIFT_DIRECTION_NOTE,
  DRIFT_KIND_LABEL,
  DRIFT_PANEL_NOTE,
  DRIFT_UNAVAILABLE_NOTE,
} from './platformLabels';

export interface DriftPanelSource {
  platform?: Platform | null;
  auth_type?: AgentAuthType | null;
  runtime_handle?: string | null;
}

export function driftPanelApplies(agent: DriftPanelSource): boolean {
  return (
    !!agent.runtime_handle &&
    agent.auth_type === 'entra' &&
    agent.platform === 'databricks'
  );
}

export function driftEntriesOf(res: unknown): DriftEntry[] | null {
  if (typeof res !== 'object' || res === null) return null;
  const entries = (res as { entries?: unknown }).entries;
  return Array.isArray(entries) ? (entries as DriftEntry[]) : null;
}

export type DriftSeverity = 'warning' | 'info';

export interface DriftRow {
  key: string;
  principal: string;
  kindLabel: string;
  level: string;
  note: string;
  severity: DriftSeverity;
  tint: string;
}

const SEVERITY_TINT: Record<DriftSeverity, string> = {
  warning: 'bg-amber-50 text-amber-700',
  info: 'bg-slate-100 text-slate-600',
};

function directionView(direction: DriftEntry['direction']): { note: string; severity: DriftSeverity } {
  return {
    note: DRIFT_DIRECTION_NOTE[direction],
    severity: direction === 'missing_acl' ? 'info' : 'warning',
  };
}

export function driftRows(entries: DriftEntry[]): DriftRow[] {
  return entries.map((e, i) => {
    const { note, severity } = directionView(e.direction);
    return {
      key: `${e.direction}:${e.principal}:${i}`,
      principal: e.principal,
      kindLabel: DRIFT_KIND_LABEL[e.kind],
      level: e.level,
      note,
      severity,
      tint: SEVERITY_TINT[severity],
    };
  });
}

export interface DriftSummary {
  state: 'clean' | 'drifted' | 'unavailable';
  count: number;
  note: string;
}

export function driftSummary(entries: DriftEntry[] | null): DriftSummary {
  if (entries === null) return { state: 'unavailable', count: 0, note: DRIFT_UNAVAILABLE_NOTE };
  if (entries.length === 0) return { state: 'clean', count: 0, note: DRIFT_CLEAN_NOTE };
  return { state: 'drifted', count: entries.length, note: DRIFT_PANEL_NOTE };
}
