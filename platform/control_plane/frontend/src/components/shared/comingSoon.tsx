/**
 * Shared honesty components (Epic 31F, Task 1) — the platform's three ways of
 * saying "this isn't real yet", so that every page says it the same way.
 *
 * Before this module, each page invented its own disclosure: AgentsOverview grew a
 * local amber `SampleBadge`, OperationsOverview a slate `SAMPLE_BADGE` class string,
 * and most mock-up pages said nothing at all. Divergent disclosure is barely better
 * than none — an operator can't learn one visual language for "not live" if each
 * screen picks a different one. T2–T6 and T8 replace their local variants with these.
 *
 * Three granularities, deliberately distinct so they never compete on a page:
 *  - `ComingSoonBanner` — the WHOLE PAGE is a mock-up. Top of the content area.
 *  - `SampleBadge`      — ONE widget on an otherwise-live page is illustrative.
 *  - `SoonTag`          — a nav row leads somewhere not built yet.
 *
 * Every export takes `tone`, default `'light'`. `light` is the governance white
 * chrome; `dark` is for the Operations dark-slate surfaces, whose token family
 * (see operationsTheme.ts: `emerald-400/20` fills, `text-*-200`, `border-*-300/30`)
 * is what the dark amber values here mirror. Amber, not rose: a not-yet-live
 * feature is a caveat, not a failure — rose is reserved for real breakage.
 *
 * Styling is Tailwind utilities with inline SVG paths, matching the tree (the repo
 * carries no icon library). No test file: vitest here runs node-env over
 * `src/**\/*.test.ts` only and cannot render react — the pinned strings are frozen
 * in `comingSoonCopy.test.ts` and `tsc -b` + `vite build` cover these components.
 */

import { type JSX } from 'react';

import {
  COMING_SOON_BODY,
  COMING_SOON_TITLE,
  SAMPLE_BADGE_LABEL,
  SOON_TAG_LABEL,
} from './comingSoonCopy';

/** Which chrome the component is sitting on: governance white vs Operations dark slate. */
export type HonestyTone = 'light' | 'dark';

// Per-tone class sets. Kept as plain records rather than conditionals inline in the
// JSX so a reader can diff the two tones against each other at a glance.

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

// Body sits a step warmer/lighter than the title so the two lines read as lead +
// detail rather than one block of bold amber.
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

// A bordered chip, not a filled blob: on white nav chrome the thin slate hairline is
// what makes the tag read as a discrete object at 10px, where a bg-only fill goes soft.
// The dark tone keeps the exact same shape in the operations aside's translucent-white
// idiom (`white/10` fills, `text-slate-300` — see operationsTheme.ts navInactive).
const TAG: Record<HonestyTone, string> = {
  light: 'bg-white border-slate-200 text-slate-500',
  dark: 'bg-white/10 border-white/20 text-slate-300',
};

/** Heroicons-style outline "information circle" — the tree draws icons as inline paths. */
const INFO_ICON_PATH =
  'M11.25 11.25l.041-.02a.75.75 0 011.063.852l-.708 2.836a.75.75 0 001.063.853l.041-.021M21 12a9 9 0 11-18 0 9 9 0 0118 0zm-9-3.75h.008v.008H12V8.25z';

/**
 * Full-width tinted band declaring that the page below is a mock-up. Rendered at
 * the TOP of a page's content area, above the heading's own content.
 *
 * Non-dismissible on purpose: a dismiss control would let the one fact the page
 * most needs to convey be the first thing an operator clicks away, and the
 * dismissal would then have to persist (or nag), neither of which serves honesty.
 * `role="status"` (polite) rather than `role="alert"`: assistive tech should hear
 * it without having the page interrupted — unmissable, never blocking.
 */
export function ComingSoonBanner(props: { tone?: HonestyTone }): JSX.Element {
  const tone = props.tone ?? 'light';
  return (
    <div
      role="status"
      className={`w-full flex items-start gap-3 rounded-lg border px-4 py-3.5 mb-6 ${BANNER_SHELL[tone]}`}
    >
      {/* 20px glyph against the title's 20px line box — no nudge needed to sit level
          with the lead line, which is where the mock anchors it. */}
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
        <p className={`text-sm font-semibold ${BANNER_TITLE[tone]}`}>{COMING_SOON_TITLE}</p>
        <p className={`text-sm mt-0.5 ${BANNER_BODY[tone]}`}>{COMING_SOON_BODY}</p>
      </div>
    </div>
  );
}

/**
 * Small inline pill marking ONE illustrative widget on a page that is otherwise
 * live — the case where a whole-page banner would overclaim. A visual twin of the
 * local badge at `governance/AgentsOverview.tsx:363` (same pill shape, same amber
 * family, same leading dot), so adopting this module changes no pixels there.
 */
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

/**
 * Pill twin of `SampleBadge` reading "Coming soon" — for an illustrative widget
 * whose real feature is on the roadmap (vs. `SampleBadge`'s "this data is fake"):
 * same shape/palette so the two disclosures sit on one visual system.
 */
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

/**
 * Tiny uppercase muted CHIP for nav rows whose destination isn't built yet. Slate,
 * not amber: a nav row is scanned in a dense list, and amber there would read as a
 * warning about the row rather than a quiet note about its destination — the same
 * reasoning behind the muted `SAMPLE_BADGE` pill in `OperationsOverview.tsx:38`,
 * whose tracking/size this matches.
 *
 * Deliberately a rounded RECTANGLE (`rounded-[5px]`), not the `rounded-full` pill of
 * `SampleBadge`: the two tags land on the same screens, and shape is the cheapest way
 * to keep "this row isn't built" distinct from "this widget is illustrative" without
 * spending a second colour on it. `leading-none` keeps the box tight around 10px caps
 * so the chip doesn't outgrow the 14px nav label it trails.
 */
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
