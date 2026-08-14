import { type JSX, type ReactNode } from 'react';
import { Link } from 'react-router-dom';
import { OPS_HEADING, OPS_SUBTITLE } from './opsUi';

/**
 * Shared Operations page frame (Epic 18). Mirrors the wrapper idiom every Ops
 * page used to inline (min-h calc → centered max-w-7xl container → optional
 * back link → heading row → body) but driven by props and the
 * Task-1 content-identity tokens. Every later Operations page renders inside
 * this instead of repeating the frame.
 *
 * THE BACK LINK NAMES A PLACE, NOT A DESTINATION CLASS (E28C/T7, D-C4b). It used to hardcode
 * "← Operations" for every caller, which was true of the 18 pages whose parent IS the Operations
 * overview and false of the one page whose parent is a PROJECT: a repository has exactly one
 * parent project, and a back link that walked past it dropped the operator two levels up from
 * where they were. `backLabel` is therefore OPTIONAL and defaults to the old string — every
 * existing call site keeps its exact wording, and only a caller that can name a truer parent
 * overrides it.
 */
export const OPS_BACK_LABEL = '← Operations';

export default function OpsPage(props: {
  title: string;
  subtitle?: string;
  /** When set, render a back Link to it (omit on the /ops Overview). */
  backTo?: string;
  /**
   * The back link's text. Defaults to `OPS_BACK_LABEL` — pass a parent's own name (already
   * arrow-prefixed) only when the destination is NOT the Operations overview.
   */
  backLabel?: string;
  /** Optional right-aligned button/control in the heading row. */
  action?: ReactNode;
  children: ReactNode;
}): JSX.Element {
  const { title, subtitle, backTo, backLabel = OPS_BACK_LABEL, action, children } = props;
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
