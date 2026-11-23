// Rationale: frontend/README.md "The honesty copy is pinned and frozen", "Tests are node-environment only, by design" (honesty disclosure)
import { type JSX } from 'react';

import {
  COMING_SOON_BODY,
  COMING_SOON_TITLE,
  SAMPLE_BADGE_LABEL,
  SOON_TAG_LABEL,
} from './comingSoonCopy';

/** The chrome the component sits on: governance white or the Operations dark slate. */
export type HonestyTone = 'light' | 'dark';

const BANNER_SHELL: Record<HonestyTone, string> = {
  light: 'border-amber-200 bg-amber-50',
  dark: 'border-amber-300/30 bg-amber-400/15',
};

const BANNER_ICON: Record<HonestyTone, string> = {
  light: 'text-amber-600',
  dark: 'text-amber-300',
};

const BANNER_TITLE: Record<HonestyTone, string> = {
  light: 'text-amber-900',
  dark: 'text-amber-100',
};

const BANNER_BODY: Record<HonestyTone, string> = {
  light: 'text-amber-700',
  dark: 'text-amber-200/90',
};

const BADGE: Record<HonestyTone, string> = {
  light: 'bg-amber-50 text-amber-700 border-amber-200/70',
  dark: 'bg-amber-400/15 text-amber-200 border-amber-300/30',
};

const BADGE_DOT: Record<HonestyTone, string> = {
  light: 'bg-amber-400',
  dark: 'bg-amber-300',
};

const TAG: Record<HonestyTone, string> = {
  light: 'bg-white border-slate-200 text-slate-500',
  dark: 'bg-white/10 border-white/20 text-slate-300',
};

const INFO_ICON_PATH =
  'M11.25 11.25l.041-.02a.75.75 0 011.063.852l-.708 2.836a.75.75 0 001.063.853l.041-.021M21 12a9 9 0 11-18 0 9 9 0 0118 0zm-9-3.75h.008v.008H12V8.25z';

export function ComingSoonBanner(props: { tone?: HonestyTone }): JSX.Element {
  return <HonestyBanner tone={props.tone} title={COMING_SOON_TITLE} body={COMING_SOON_BODY} />;
}

function HonestyBanner(props: { tone?: HonestyTone; title: string; body: string }): JSX.Element {
  const tone = props.tone ?? 'light';
  return (
    <div
      role="status"
      className={`w-full flex items-start gap-3 rounded-lg border px-4 py-3.5 mb-6 ${BANNER_SHELL[tone]}`}
    >
      <svg
        className={`w-5 h-5 flex-shrink-0 ${BANNER_ICON[tone]}`}
        fill="none"
        viewBox="0 0 24 24"
        stroke="currentColor"
        strokeWidth={1.5}
        aria-hidden="true"
      >
        <path strokeLinecap="round" strokeLinejoin="round" d={INFO_ICON_PATH} />
      </svg>
      <div className="min-w-0">
        <p className={`text-sm font-semibold ${BANNER_TITLE[tone]}`}>{props.title}</p>
        <p className={`text-sm mt-0.5 ${BANNER_BODY[tone]}`}>{props.body}</p>
      </div>
    </div>
  );
}

export function SampleBadge(props: { tone?: HonestyTone }): JSX.Element {
  const tone = props.tone ?? 'light';
  return (
    <span
      className={`inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-[11px] font-medium border ${BADGE[tone]}`}
    >
      <span aria-hidden="true" className={`h-1.5 w-1.5 rounded-full ${BADGE_DOT[tone]}`} />
      {SAMPLE_BADGE_LABEL}
    </span>
  );
}

export function ComingSoonBadge(props: { tone?: HonestyTone }): JSX.Element {
  const tone = props.tone ?? 'light';
  return (
    <span
      className={`inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-[11px] font-medium border ${BADGE[tone]}`}
    >
      <span aria-hidden="true" className={`h-1.5 w-1.5 rounded-full ${BADGE_DOT[tone]}`} />
      {COMING_SOON_TITLE}
    </span>
  );
}

export function SoonTag(props: { tone?: HonestyTone }): JSX.Element {
  const tone = props.tone ?? 'light';
  return (
    <span
      className={`inline-flex items-center border px-1.5 py-0.5 rounded-[5px] text-[10px] leading-none font-medium uppercase tracking-wide ${TAG[tone]}`}
    >
      {SOON_TAG_LABEL}
    </span>
  );
}
