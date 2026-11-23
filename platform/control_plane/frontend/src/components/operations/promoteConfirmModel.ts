import type { ArtifactMarker } from './repositoryDetailTabs';

export const ARTIFACT_TONE_CLS: Record<ArtifactMarker['tone'], string> = {
  pinned:
    'inline-flex w-fit items-center px-2 py-0.5 rounded-md bg-slate-100 border border-slate-200 font-mono text-xs text-slate-700 break-all',
  caution:
    'inline-flex w-fit items-center gap-1.5 px-2 py-0.5 rounded-md bg-amber-50 border border-amber-200 text-[11px] font-semibold text-amber-800',
};

export const PROMOTE_CONFIRM_BTN =
  'px-2.5 py-1 rounded-md bg-emerald-600 text-white text-xs font-medium hover:bg-emerald-700 transition-colors disabled:opacity-40';

export const PROMOTE_CANCEL_BTN =
  'px-2.5 py-1 rounded-md bg-white border border-slate-300 text-slate-700 text-xs font-medium hover:bg-slate-50 transition-colors disabled:opacity-40';
