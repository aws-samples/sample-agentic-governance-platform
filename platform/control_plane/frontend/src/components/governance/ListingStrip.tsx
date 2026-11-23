import { Link } from 'react-router-dom';
import type { ListingStep, NextAction, StepStatus } from './listingSteps';

const DOT: Record<StepStatus, string> = {
  done: 'bg-emerald-600 text-white',
  current: 'bg-white text-blue-700 ring-2 ring-inset ring-blue-500',
  blocked: 'bg-red-50 text-red-700 ring-1 ring-inset ring-red-300',
  upcoming: 'bg-slate-100 text-slate-400',
};

const TEXT: Record<StepStatus, string> = {
  done: 'text-slate-700',
  current: 'text-slate-900 font-medium',
  blocked: 'text-red-700 font-medium',
  upcoming: 'text-slate-400',
};

const SR: Record<StepStatus, string> = {
  done: 'done',
  current: 'current step',
  blocked: 'blocked',
  upcoming: 'not yet',
};

export default function ListingStrip({
  steps,
  next,
  onCta,
  isAdmin,
}: {
  steps: ListingStep[];
  next: NextAction;
  onCta: () => void;
  isAdmin: boolean;
}) {
  return (
    <div className="mt-3">
      <ol className="flex flex-wrap items-center gap-x-2 gap-y-1.5 text-xs" aria-label="Marketplace listing steps">
        {steps.map((s, i) => (
          <li key={s.label} className="flex items-center gap-2" aria-current={s.status === 'current' ? 'step' : undefined}>
            {i > 0 && (
              <span
                aria-hidden="true"
                className={`h-px w-6 ${steps[i - 1].status === 'done' ? 'bg-emerald-300' : 'bg-slate-200'}`}
              />
            )}
            <span
              aria-hidden="true"
              className={`inline-flex h-5 w-5 shrink-0 items-center justify-center rounded-full text-[11px] font-semibold ${DOT[s.status]}`}
            >
              {s.status === 'done' ? (
                <svg viewBox="0 0 16 16" className="h-3 w-3" fill="none" stroke="currentColor" strokeWidth="2.5">
                  <path d="M3.5 8.5l3 3 6-7" strokeLinecap="round" strokeLinejoin="round" />
                </svg>
              ) : s.status === 'blocked' ? (
                '!'
              ) : (
                i + 1
              )}
            </span>
            <span className={TEXT[s.status]}>
              {s.label}
              <span className="sr-only"> ({SR[s.status]})</span>
            </span>
          </li>
        ))}
      </ol>

      {next.kind !== 'none' && (
        <div className="mt-2.5 flex flex-wrap items-center gap-x-3 gap-y-1.5 text-sm">
          <span className="text-[11px] uppercase tracking-wide text-slate-400 font-medium">Next</span>
          {next.kind === 'cta' ? (
            <button
              type="button"
              onClick={onCta}
              className="px-3 py-1 rounded-lg bg-blue-600 text-white text-xs font-medium hover:bg-blue-700 transition-colors"
            >
              {next.text}
            </button>
          ) : (
            <span className="text-slate-600">{next.text}</span>
          )}
          {next.kind === 'review' && isAdmin && (
            <Link to="/admin" className="text-xs font-medium text-blue-700 hover:underline">
              Review in Admin →
            </Link>
          )}
        </div>
      )}
    </div>
  );
}
