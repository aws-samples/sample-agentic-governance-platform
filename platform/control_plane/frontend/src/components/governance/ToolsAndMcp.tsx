import { useState } from 'react';
import McpServerCatalog from './McpServerCatalog';
import { SoonTag } from '../shared/comingSoon';

type Tab = 'tools' | 'knowledge';

export default function ToolsAndMcp() {
  const [tab, setTab] = useState<Tab>('tools');
  // `soon` marks a tab whose panel is still a stub. Only the Knowledge tab is —
  // the Tools / MCP servers tab is the real, live catalog — so this page gets the
  // small per-tab tag rather than a whole-page "coming soon" banner.
  const tabs: { id: Tab; label: string; soon?: boolean }[] = [
    { id: 'tools', label: 'Tools / MCP servers' },
    { id: 'knowledge', label: 'Knowledge', soon: true },
  ];

  return (
    <div className="min-h-[calc(100vh-4rem)] relative">
      <div className="relative max-w-7xl mx-auto px-6 py-10">
        <h1 className="text-2xl font-semibold text-slate-900">Tools &amp; MCP</h1>
        <p className="text-sm text-slate-500 mt-1 mb-6">Everything agents consume — governed once, independently, because a shared MCP server is a fleet-wide supply-chain risk.</p>

        <div className="flex items-center gap-1 mb-8 p-1 bg-slate-100/80 rounded-xl w-fit">
          {tabs.map((t) => (
            <button key={t.id} onClick={() => setTab(t.id)} className={`inline-flex items-center gap-1.5 px-4 py-2 rounded-lg text-sm font-medium transition-all ${tab === t.id ? 'bg-white text-slate-900 shadow-sm' : 'text-slate-500 hover:text-slate-700'}`}>
              {t.label}
              {t.soon && <SoonTag />}
            </button>
          ))}
        </div>

        {tab === 'tools' && <McpServerCatalog />}

        {tab === 'knowledge' && (
          <div className="bg-white/60 backdrop-blur rounded-xl border border-slate-200/60 p-8 text-center text-slate-400 text-sm">Knowledge sources / RAG.</div>
        )}
      </div>
    </div>
  );
}
