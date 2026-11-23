import { type JSX, useCallback, useMemo, useRef, useState } from 'react';

import { useUser } from '../../contexts/useUser';
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
  const roleLevel = user?.role_level ?? 0;

  const tabs = useMemo(() => visibleTabs(roleLevel), [roleLevel]);
  const sections = useMemo(() => visibleSections(roleLevel), [roleLevel]);
  const withStrip = showTabStrip(roleLevel);

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
      if (!next) return;
      e.preventDefault();
      setActiveKey(next as SettingsTabKey);
      tabRefs.current[next]?.focus();
    },
    [tabs],
  );

  if (tabs.length === 0) {
    return (
      <OpsPage backTo="/ops" title="Settings">
        <div className={`${OPS_CARD} p-8 text-center text-slate-400 text-sm`}>Loading settings…</div>
      </OpsPage>
    );
  }

  const selectedKey = tabs.some((t) => t.key === activeKey) ? activeKey : tabs[0].key;
  const shown = sections.filter((s) => s.tab === selectedKey);

  return (
    <OpsPage
      backTo="/ops"
      title="Settings"
      subtitle={settingsSubtitle(roleLevel)}
    >
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
