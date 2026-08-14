// Settings — `/ops/settings` (E28/T8). Replaces the Operations Admin console, which held
// templates and org connections behind ONE admin gate even though only org connections
// needed it. Templates left for its own top-level page (T9); what remains is split by
// AUDIENCE instead of lumped behind an admin door: General is anyone's, Admin is ADMIN's.
//
// This file is LAYOUT ONLY. Which sections exist, who sees each, which tab each lands on,
// and which tab a deep link opens all live in `settingsSections.ts` — partly because only
// `src/**/*.test.ts` is collected by vitest (a decision here could never be pinned), and
// partly because that is what makes an 11th section a config entry instead of an edit here.
// The tab strip is DERIVED (`visibleTabs`): there is no tab list in this file to fall out of
// step with the registry, and a tab with no visible sections cannot render.
//
// Layout: within a tab, sections are flat BANDS in one column — a heading, a one-line
// purpose, then the body, separated by hairline rules. Not cards, because both bodies
// already bring their own cards and tables (a card per section would frame a frame); not an
// accordion, because collapsed settings hide the very thing someone is scanning for. With
// 11 sections the single column just grows, and the heading + purpose pair is what keeps a
// long page legible — so the tab strip carries the audience split and the bands carry the
// scan. `scroll-mt` on each band keeps a `#fragment` landing below the app header.
//
// A11y: the tab strip follows `ProjectDetail.tsx:318-349` — the app's only accessible one —
// re-using `nextTabKey` for the roving-tabindex arrow model rather than re-deriving the
// index arithmetic. Real tab/tablist/tabpanel roles, `aria-selected`, `aria-controls` +
// `aria-labelledby`, and one tab stop for the whole bar. The page-navigation "current"
// attribute is deliberately absent: it is defined for navigation between PAGES, so on a
// client-side tab it announces a current page the URL flatly contradicts (WCAG 4.1.2).
//
// House style: emerald-on-glass Ops tokens (`opsUi.ts`), the `OpsPage` frame, Tailwind v4
// utility strings, inline SVG, 2-space indent.

import { type JSX, useCallback, useMemo, useRef, useState } from 'react';

import { useUser } from '../../contexts/UserContext';
import OpsPage from './OpsPage';
import { OPS_CARD } from './opsUi';
import { nextTabKey } from './projectDetailTabs';
import {
  type SettingsTabKey,
  resolveInitialTab,
  sectionAnchorId,
  settingsSubtitle,
  showTabStrip,
  tabId,
  tabPanelId,
  visibleSections,
  visibleTabs,
} from './settingsSections';

export default function Settings(): JSX.Element {
  const { user } = useUser();
  // The registry owns the gate — this page only passes the level in. Open-coding the
  // admin-level comparison here would make Settings a second authority on who is an admin.
  const roleLevel = user?.role_level ?? 0;

  const tabs = useMemo(() => visibleTabs(roleLevel), [roleLevel]);
  const sections = useMemo(() => visibleSections(roleLevel), [roleLevel]);
  // A one-tab strip is a choice that doesn't exist (a viewer's case today), and the subtitle
  // must not promise an Admin tab they cannot see. Both are DERIVED — see settingsSections.ts.
  const withStrip = showTabStrip(roleLevel);

  // Read the fragment ONCE, at mount. A deep link is an opening instruction, not a binding:
  // re-reading it would drag the user back to the linked tab every time they left it.
  const [activeKey, setActiveKey] = useState<SettingsTabKey>(() =>
    resolveInitialTab(roleLevel, typeof window === 'undefined' ? '' : window.location.hash),
  );

  const tabRefs = useRef<Record<string, HTMLButtonElement | null>>({});
  const handleTabKeyDown = useCallback(
    (e: React.KeyboardEvent<HTMLButtonElement>) => {
      const next = nextTabKey(
        tabs.map((t) => t.key),
        e.currentTarget.dataset.tabKey ?? '',
        e.key,
      );
      if (!next) return; // not part of the tablist model — leave the press alone
      e.preventDefault();
      setActiveKey(next as SettingsTabKey);
      tabRefs.current[next]?.focus();
    },
    [tabs],
  );

  // `user` is still loading, or the registry is empty for this caller. Never a bare page.
  if (tabs.length === 0) {
    return (
      <OpsPage backTo="/ops" title="Settings">
        <div className={`${OPS_CARD} p-8 text-center text-slate-400 text-sm`}>Loading settings…</div>
      </OpsPage>
    );
  }

  // Guard against a stale selection (a role refresh can retract the Admin tab mid-visit).
  const selectedKey = tabs.some((t) => t.key === activeKey) ? activeKey : tabs[0].key;
  const shown = sections.filter((s) => s.tab === selectedKey);

  return (
    <OpsPage
      backTo="/ops"
      title="Settings"
      subtitle={settingsSubtitle(roleLevel)}
    >
      {/* Pill tab bar — OperationsAdmin's class strings verbatim, with the real tab
          semantics ProjectDetail established: one tab stop (roving tabindex), Left/Right to
          move, and the tab↔panel relationship expressed via aria-controls/aria-labelledby.
          Suppressed entirely at one tab: a tablist reading "tab, selected, 1 of 1" offers a
          choice that does not exist. The sections render identically either way. */}
      {withStrip && (
        <div
          role="tablist"
          aria-label="Settings audiences"
          className="flex items-center gap-1 p-1 mb-4 bg-emerald-50/60 rounded-xl w-fit overflow-x-auto"
        >
          {tabs.map((tab) => {
            const selected = selectedKey === tab.key;
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
                onClick={() => setActiveKey(tab.key)}
                onKeyDown={handleTabKeyDown}
                className={`px-3.5 py-1.5 rounded-lg text-sm font-medium transition-colors whitespace-nowrap ${selected ? 'bg-white text-slate-900 shadow-sm' : 'text-slate-500 hover:text-slate-700'}`}
              >
                {tab.label}
              </button>
            );
          })}
        </div>
      )}

      {/* The active tab's sections, as bands. `key` remounts on a tab change so a section
          body starts from a clean load rather than showing the previous tab's in-flight state.
          The TAB SEMANTICS are conditional on the strip: with no tablist rendered there is no
          `role="tab"` for `aria-labelledby` to point at, and a `tabpanel` whose label id does
          not resolve — or which has no owning tablist — is broken ARIA that reads worse than
          no ARIA. So at one tab this is a plain container, and the sections' own labelled
          <section> elements carry the structure. `tabIndex={0}` likewise only earns its place
          alongside a roving-tabindex bar that needs somewhere to hand focus. */}
      <div
        key={selectedKey}
        {...(withStrip
          ? {
              role: 'tabpanel',
              id: tabPanelId(selectedKey),
              'aria-labelledby': tabId(selectedKey),
              tabIndex: 0,
            }
          : {})}
      >
        {shown.map((section, i) => (
          <section
            key={section.id}
            id={sectionAnchorId(section.id)}
            aria-labelledby={`${sectionAnchorId(section.id)}-heading`}
            // A fragment link would otherwise land the heading under the sticky app header.
            className={`scroll-mt-24 ${i > 0 ? 'mt-8 pt-8 border-t border-emerald-100/70' : ''}`}
          >
            <div className="mb-3 max-w-3xl">
              <div className="flex items-center gap-2">
                <h2
                  id={`${sectionAnchorId(section.id)}-heading`}
                  className="text-base font-semibold text-slate-900"
                >
                  {section.title}
                </h2>
                {/* Admin sections are marked in place. With both audiences on one page, an
                    admin needs to see WHICH settings they are changing for everybody else —
                    the tab label alone stops being visible once you have scrolled. */}
                {section.gate === 'admin' && (
                  <span className="inline-flex items-center gap-1 text-[11px] font-semibold px-2 py-0.5 rounded-full bg-emerald-50 text-emerald-700">
                    <svg
                      className="w-3 h-3"
                      fill="none"
                      viewBox="0 0 24 24"
                      stroke="currentColor"
                      strokeWidth={2}
                      aria-hidden="true"
                    >
                      <path
                        strokeLinecap="round"
                        strokeLinejoin="round"
                        d="M12 3l7.5 3v5.25c0 4.28-3.05 8.3-7.5 9.75-4.45-1.45-7.5-5.47-7.5-9.75V6L12 3z"
                      />
                    </svg>
                    Affects everyone
                  </span>
                )}
              </div>
              <p className="text-sm text-slate-500 mt-1 leading-relaxed">{section.purpose}</p>
            </div>
            <section.Component />
          </section>
        ))}
      </div>
    </OpsPage>
  );
}
