import { useCallback, useRef, type JSX } from 'react';

import { nextTabKey } from './projectDetailTabs';

export interface TabStripItem {
  key: string;
  label: string;
}

export interface TabStripProps {
  /** Only the selectable tabs: a tab whose body does not exist must not be listed, or the arrow keys hand focus to an empty panel. */
  tabs: readonly TabStripItem[];
  activeKey: string;
  onSelect: (key: string) => void;
  ariaLabel: string;
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
  const tabRefs = useRef<Record<string, HTMLButtonElement | null>>({});
  const handleKeyDown = useCallback(
    (e: React.KeyboardEvent<HTMLButtonElement>) => {
      const next = nextTabKey(
        tabs.map((t) => t.key),
        e.currentTarget.dataset.tabKey ?? '',
        e.key,
      );
      if (!next) return;
      e.preventDefault();
      onSelect(next);
      tabRefs.current[next]?.focus();
    },
    [tabs, onSelect],
  );

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
