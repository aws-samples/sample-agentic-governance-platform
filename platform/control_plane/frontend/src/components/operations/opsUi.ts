export const OPS_CARD =
  'bg-white/80 backdrop-blur rounded-xl border border-emerald-200/50 shadow-sm';

export const OPS_HEADING = 'text-3xl font-semibold tracking-tight text-slate-900';

export const OPS_SUBTITLE = 'text-slate-500 mt-1 max-w-2xl';

// Decision: docs/adr/frontend-two-status-machines.md
export const OPS_BADGE: Record<
  'ready' | 'provisioning' | 'ok' | 'warn' | 'failed' | 'running' | 'pending' | 'unknown',
  string
> = {
  ready: 'bg-emerald-50 text-emerald-700',
  provisioning: 'bg-amber-50 text-amber-700',
  ok: 'bg-emerald-50 text-emerald-700',
  warn: 'bg-amber-50 text-amber-700',
  failed: 'bg-rose-50 text-rose-700',
  running: 'bg-emerald-50 text-emerald-700',
  pending: 'bg-amber-50 text-amber-700',
  unknown: 'bg-slate-100 text-slate-500',
};

export const OPS_PRIMARY_BTN =
  'px-3.5 py-1.5 rounded-lg bg-emerald-600 text-white text-sm font-medium hover:bg-emerald-700 transition-colors';

export const OPS_TABLE_HEAD = 'bg-emerald-50/60 text-slate-500 text-xs uppercase tracking-wide';

export const OPS_TABLE_DIVIDE = 'divide-y divide-emerald-100/70';

export function formatDate(iso?: string | null): string {
  if (!iso) return '—';
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return d.toLocaleString(undefined, {
    year: 'numeric',
    month: 'short',
    day: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
  });
}
