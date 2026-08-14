// ObsEmptyCard — the calm empty/error state shared by the observability surfaces
// (Epic 26). Lifted verbatim from the T8 ObservabilityDashboard's private
// EmptyCard so the platform dashboard and the per-agent Traces/Cost tabs render
// the identical "no data yet" / "couldn't load" affordance (DRY). `tone='amber'`
// is the soft-error variant; the default slate tone is the not-configured /
// no-data-in-window calm card.
export function ObsEmptyCard({ title, body, tone = 'slate' }: { title: string; body: string; tone?: 'slate' | 'amber' }) {
  const wrap = tone === 'amber' ? 'card border-amber-200 bg-amber-50/30' : 'card border-slate-200 bg-slate-50/50';
  const iconBg = tone === 'amber' ? 'bg-amber-100' : 'bg-slate-100';
  const iconFg = tone === 'amber' ? 'text-amber-600' : 'text-slate-400';
  const titleFg = tone === 'amber' ? 'text-amber-900' : 'text-slate-900';
  const bodyFg = tone === 'amber' ? 'text-amber-700/80' : 'text-slate-500';
  return (
    <div className={wrap}>
      <div className="flex items-start gap-4">
        <div className={`w-10 h-10 rounded-xl ${iconBg} flex items-center justify-center flex-shrink-0`}>
          <svg className={`w-5 h-5 ${iconFg}`} fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={1.5}>
            <path strokeLinecap="round" strokeLinejoin="round" d="M3 13.125C3 12.504 3.504 12 4.125 12h2.25c.621 0 1.125.504 1.125 1.125v6.75C7.5 20.496 6.996 21 6.375 21h-2.25A1.125 1.125 0 013 19.875v-6.75zM9.75 8.625c0-.621.504-1.125 1.125-1.125h2.25c.621 0 1.125.504 1.125 1.125v11.25c0 .621-.504 1.125-1.125 1.125h-2.25a1.125 1.125 0 01-1.125-1.125V8.625zM16.5 4.125c0-.621.504-1.125 1.125-1.125h2.25C20.496 3 21 3.504 21 4.125v15.75c0 .621-.504 1.125-1.125 1.125h-2.25a1.125 1.125 0 01-1.125-1.125V4.125z" />
          </svg>
        </div>
        <div>
          <h3 className={`text-base font-semibold ${titleFg} mb-1`}>{title}</h3>
          <p className={`text-sm ${bodyFg} max-w-2xl`}>{body}</p>
        </div>
      </div>
    </div>
  );
}
