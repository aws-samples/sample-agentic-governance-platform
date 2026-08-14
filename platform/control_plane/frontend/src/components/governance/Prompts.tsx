import { ComingSoonBanner } from '../shared/comingSoon';

const SAMPLE_PROMPTS = [
  { name: 'claims-intake-system',  owner: 'platform-team', usedBy: 5, version: 'v3', updated: '2026-05-22' },
  { name: 'fraud-triage-rubric',   owner: 'risk-team',     usedBy: 2, version: 'v7', updated: '2026-05-28' },
  { name: 'broker-tone-guide',     owner: 'cx-team',       usedBy: 4, version: 'v2', updated: '2026-05-19' },
];

export default function Prompts() {
  return (
    <div className="min-h-[calc(100vh-4rem)] relative">
      <div className="relative max-w-7xl mx-auto px-6 py-10">
        <ComingSoonBanner />
        <h1 className="text-3xl font-semibold text-slate-900 tracking-tight animate-fade-in">Prompts</h1>
        <p className="text-sm text-slate-500 mt-1 mb-6">The governed prompt library — versioned, owned, and reused across agents.</p>

        <div className="bg-white/70 backdrop-blur rounded-xl border border-slate-200/60 overflow-hidden shadow-sm">
          <table className="w-full text-sm">
            <thead className="bg-slate-50/80 text-slate-500 text-xs uppercase tracking-wide">
              <tr>
                <th className="text-left font-medium px-4 py-3">Prompt</th>
                <th className="text-left font-medium px-4 py-3">Owner</th>
                <th className="text-left font-medium px-4 py-3">Used by</th>
                <th className="text-left font-medium px-4 py-3">Version</th>
                <th className="text-left font-medium px-4 py-3">Updated</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-slate-100">
              {SAMPLE_PROMPTS.map((p) => (
                <tr key={p.name} className="hover:bg-blue-50/40 transition-colors">
                  <td className="px-4 py-3 font-medium text-slate-800 font-mono text-xs">{p.name}</td>
                  <td className="px-4 py-3 text-slate-600">{p.owner}</td>
                  <td className="px-4 py-3 text-slate-600">{p.usedBy} agents</td>
                  <td className="px-4 py-3 text-slate-600">{p.version}</td>
                  <td className="px-4 py-3 text-slate-600">{p.updated}</td>
                </tr>
              ))}
            </tbody>
          </table>
          <div className="px-4 py-3 text-xs text-slate-400 border-t border-slate-100">Prompt library with CRUD and versioning.</div>
        </div>
      </div>
    </div>
  );
}
