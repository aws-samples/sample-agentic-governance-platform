import { COMING_SOON_TITLE } from '../../shared/comingSoonCopy';

export const DEPLOY_SOON_LABEL = `Deploy Agent — ${COMING_SOON_TITLE.toLowerCase()}`;
export const DEPLOY_SOON_TITLE =
  'Agent deployment from the marketplace is coming in a future release';

export const DISABLED_CTA =
  'px-3.5 py-1.5 rounded-lg bg-slate-100 text-slate-400 text-sm font-medium ' +
  'border border-slate-200 cursor-not-allowed';

export const META_CHIP =
  'inline-flex items-center gap-1 px-2 py-0.5 rounded-md text-xs font-medium bg-slate-100 text-slate-600';

export function classificationChipCls(value: string): string {
  switch (value.toLowerCase()) {
    case 'restricted':
      return 'bg-rose-50 text-rose-700';
    case 'confidential':
      return 'bg-amber-50 text-amber-700';
    case 'internal':
      return 'bg-sky-50 text-sky-700';
    case 'public':
      return 'bg-emerald-50 text-emerald-700';
    default:
      return 'bg-slate-100 text-slate-600';
  }
}
