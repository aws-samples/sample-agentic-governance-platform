import { type JSX, type ReactNode } from 'react';
import { Link } from 'react-router-dom';
import { OPS_BACK_LABEL } from './opsLabels';
import { OPS_HEADING, OPS_SUBTITLE } from './opsUi';

export default function OpsPage(props: {
  title: string;
  subtitle?: string;
  backTo?: string;
  backLabel?: string;
  eyebrow?: ReactNode;
  action?: ReactNode;
  children: ReactNode;
}): JSX.Element {
  const { title, subtitle, backTo, backLabel = OPS_BACK_LABEL, eyebrow, action, children } = props;
  return (
    <div className="min-h-[calc(100vh-4rem)] relative">
      <div className="relative max-w-7xl mx-auto px-6 py-10">
        {backTo && (
          <Link
            to={backTo}
            className="text-sm text-slate-400 hover:text-slate-600 transition-colors font-medium"
          >
            {backLabel}
          </Link>
        )}

        <div className={`flex items-end justify-between gap-4 ${backTo ? 'mt-3' : ''} mb-6`}>
          <div>
            {eyebrow && (
              <div className="text-[11px] uppercase tracking-wide font-semibold text-emerald-700 mb-1">{eyebrow}</div>
            )}
            <h1 className={OPS_HEADING}>{title}</h1>
            {subtitle && <p className={OPS_SUBTITLE}>{subtitle}</p>}
          </div>
          {action && <div className="shrink-0">{action}</div>}
        </div>

        {children}
      </div>
    </div>
  );
}
