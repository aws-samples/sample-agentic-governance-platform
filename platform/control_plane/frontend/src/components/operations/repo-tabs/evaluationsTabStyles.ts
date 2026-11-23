import type { EvalRun } from '../../../api/client';
import { OPS_BADGE } from '../opsUi';
import { warnOnce } from '../warnOnce';
import type { GateCardState, ScoreTone, ToolDiffStatus } from './evaluationsLogic';

export const PILL = 'inline-flex items-center gap-1.5 text-[11px] font-semibold px-2 py-0.5 rounded-full';
export const FIELD_LABEL = 'block text-[11px] uppercase tracking-wide text-slate-400 font-medium mb-1';
export const FIELD_INPUT =
  'w-full px-3 py-2 text-sm rounded-lg border border-slate-300 focus:outline-none focus:ring-2 focus:ring-emerald-500/40';
export const SECONDARY_BTN =
  'px-3 py-1.5 rounded-lg border border-slate-300 bg-white text-sm font-medium text-slate-700 hover:bg-slate-50 transition-colors disabled:opacity-50 disabled:cursor-not-allowed';
export const SECTION_TITLE = 'text-sm font-semibold text-slate-800';

export const GATE_STYLE: Record<GateCardState, { rail: string; pill: string; label: string; glyph: string }> = {
  passed: { rail: 'bg-emerald-500', pill: OPS_BADGE.ok, label: 'Passed', glyph: '✓' },
  failed: { rail: 'bg-rose-500', pill: OPS_BADGE.failed, label: 'Blocked', glyph: '✗' },
  running: { rail: 'bg-amber-400', pill: OPS_BADGE.pending, label: 'Running', glyph: '◌' },
  none: { rail: 'bg-slate-300', pill: OPS_BADGE.unknown, label: 'Not evaluated', glyph: '–' },
};

const RUN_STATUS_PILL: Record<EvalRun['status'], { cls: string; label: string }> = {
  passed: { cls: OPS_BADGE.ok, label: 'Passed' },
  failed: { cls: OPS_BADGE.failed, label: 'Failed' },
  error: { cls: OPS_BADGE.failed, label: 'Error' },
  running: { cls: OPS_BADGE.pending, label: 'Running' },
};

export function runStatusPill(status: EvalRun['status']): { cls: string; label: string } {
  if (Object.prototype.hasOwnProperty.call(RUN_STATUS_PILL, status)) return RUN_STATUS_PILL[status];
  warnOnce('[evaluationsTabStyles] eval run status this frontend does not know; shown as sent:', status);
  return { cls: OPS_BADGE.unknown, label: status };
}

export const TONE_CLS: Record<ScoreTone, string> = {
  ok: 'text-emerald-700',
  fail: 'text-rose-700',
  error: 'text-amber-700',
  neutral: 'text-slate-400',
};

export const DIFF_STYLE: Record<ToolDiffStatus, { cls: string; label: string }> = {
  match: { cls: 'text-emerald-700', label: 'match' },
  'args-differ': { cls: 'text-rose-700', label: 'arguments differ' },
  missing: { cls: 'text-rose-700', label: 'not called' },
  extra: { cls: 'text-slate-500', label: 'extra call' },
  'no-data': { cls: 'text-amber-700', label: 'no tool-call data' },
};
