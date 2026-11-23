export const FRAMEWORK_PILL = 'bg-emerald-50 text-emerald-700';
export const TAG_PILL = 'bg-emerald-50/70 text-emerald-700';
export const SERVICE_CHIP = 'bg-slate-100 text-slate-500';
export const KIND_CHIP = { mcp_server: 'bg-sky-50 text-sky-700', agent: 'bg-slate-100 text-slate-600' } as const;

export function formatTemplateDate(iso: string): string {
  if (!iso) return '—';
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return d.toLocaleDateString(undefined, { year: 'numeric', month: 'short', day: 'numeric' });
}
