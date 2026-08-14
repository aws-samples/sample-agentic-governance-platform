// TabStrip — THE accessible Operations tab bar, extracted (E28/T11).
//
// ---------------------------------------------------------------------------
// WHY THIS COMPONENT EXISTS
//
// `ProjectDetail.tsx:318-346` earned the app's first real WAI-ARIA tab bar (the repo had ZERO
// `role="tab"` occurrences before it). `Settings.tsx` then became a second implementation of
// the same contract, and its fix round showed the cost: suppressing the strip at one tab
// nearly left a `tabpanel` pointing at an `aria-labelledby` id that no longer existed. This
// page would have been the THIRD copy of the same twelve lines — so the bar is a component
// now, and the next surface composes it instead of re-deriving it.
//
// The two LIVE call sites are deliberately NOT migrated onto this. Both are working, reviewed,
// committed code; extracting the component and re-pointing two call sites are two changes, and
// only the first belongs to this task. Whoever migrates them should expect a diff of zero
// rendered output — the class strings below are `ProjectDetail.tsx:344`'s verbatim, so the
// three surfaces stay one visual system.
//
// ---------------------------------------------------------------------------
// THE CONTRACT THIS PRESERVES
//
//   • `role="tablist"` / `role="tab"` / `aria-selected` / `aria-controls`, with the panels
//     carrying `role="tabpanel"` + `aria-labelledby` (the PAGE owns the panels — see below),
//     so the controls announce as "tab, selected, 2 of 6" and the tab↔panel relationship is
//     expressed rather than implied by proximity.
//   • ONE TAB STOP for the whole bar (roving tabindex): Tab reaches the selected tab and then
//     moves on into the panel; Left/Right move between tabs, with wraparound, and Home/End
//     jump to the ends. Plain buttons would put every tab in the tab sequence, which is not
//     the model a screen-reader user expects from a tablist.
//   • The page-navigation "current" attribute is deliberately ABSENT. It is defined for
//     navigation within a set of PAGES; on a client-side tab it announces a current page the
//     URL flatly contradicts (WCAG 4.1.2 name/role/value). Not spelled out here because the
//     guard asserting its absence reads this file's raw source and does not skip comments —
//     a guard that has to decide what is "only a comment" is one a comment can defeat.
//
// The arrow-key arithmetic is `projectDetailTabs.nextTabKey`, IMPORTED rather than re-derived.
// Index-with-wraparound would be a fourth copy of the same six lines, and — being logic inside
// a `.tsx` — a copy no test could reach (vitest collects only `src/**/*.test.ts`). Callers pass
// their own `tabId`/`tabPanelId` derivations for the same reason: three surfaces derive ids,
// and the derivation is the caller's so the ids cannot collide.
//
// This file makes NO decisions. Which tabs exist, which are selectable, and which is active
// are all the caller's; this renders them. House style: emerald-on-glass Ops tokens, Tailwind
// v4 utility strings, 2-space indent.

import { useCallback, useRef, type JSX } from 'react';

import { nextTabKey } from './projectDetailTabs';

export interface TabStripItem {
  key: string;
  label: string;
}

export interface TabStripProps {
  /**
   * The tabs to render, in order. The caller passes only the SELECTABLE ones — a tab whose
   * body does not exist must not appear here, or the keyboard model will hand focus to a tab
   * that opens onto an empty panel.
   */
  tabs: readonly TabStripItem[];
  activeKey: string;
  onSelect: (key: string) => void;
  /** Names the bar for a screen reader, e.g. "Repository sections". */
  ariaLabel: string;
  /** The caller's id derivations, so this component cannot collide two surfaces' ids. */
  tabId: (key: string) => string;
  tabPanelId: (key: string) => string;
}

export default function TabStrip({
  tabs,
  activeKey,
  onSelect,
  ariaLabel,
  tabId,
  tabPanelId,
}: TabStripProps): JSX.Element | null {
  // Roving-tabindex focus management. Only the SELECTED tab is in the tab sequence, so an
  // arrow-key selection must move DOM focus itself — otherwise focus would stay on a button
  // that just became `tabIndex={-1}`, which strands the keyboard user.
  const tabRefs = useRef<Record<string, HTMLButtonElement | null>>({});
  const handleKeyDown = useCallback(
    (e: React.KeyboardEvent<HTMLButtonElement>) => {
      const next = nextTabKey(
        tabs.map((t) => t.key),
        e.currentTarget.dataset.tabKey ?? '',
        e.key,
      );
      if (!next) return; // not part of the tablist model — leave the press alone
      e.preventDefault();
      onSelect(next);
      tabRefs.current[next]?.focus();
    },
    [tabs, onSelect],
  );

  // A tablist offering ONE tab announces a choice that does not exist ("tab, selected, 1 of
  // 1"), and with none there is nothing to announce at all. Both render nothing — the caller's
  // panels are unaffected either way, which is the same call `settingsSections.showTabStrip`
  // makes. Note the caller must then not attach `aria-labelledby` to a panel, since there is
  // no `role="tab"` for it to point at (the Settings fix round's near-miss).
  if (tabs.length < 2) return null;

  return (
    <div
      role="tablist"
      aria-label={ariaLabel}
      className="flex items-center gap-1 p-1 mb-4 bg-emerald-50/60 rounded-xl w-fit overflow-x-auto"
    >
      {tabs.map((tab) => {
        const selected = activeKey === tab.key;
        return (
          <button
            key={tab.key}
            ref={(el) => {
              tabRefs.current[tab.key] = el;
            }}
            type="button"
            role="tab"
            id={tabId(tab.key)}
            data-tab-key={tab.key}
            aria-selected={selected}
            aria-controls={tabPanelId(tab.key)}
            tabIndex={selected ? 0 : -1}
            onClick={() => onSelect(tab.key)}
            onKeyDown={handleKeyDown}
            className={`px-3.5 py-1.5 rounded-lg text-sm font-medium transition-colors whitespace-nowrap ${selected ? 'bg-white text-slate-900 shadow-sm' : 'text-slate-500 hover:text-slate-700'}`}
          >
            {tab.label}
          </button>
        );
      })}
    </div>
  );
}
