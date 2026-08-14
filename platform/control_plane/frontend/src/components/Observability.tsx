import { useState, useEffect } from 'react';
import { Link, useSearchParams } from 'react-router-dom';
import { observabilityApi } from '../api/client';
import type { ObservabilitySettings } from '../types';
import ObservabilityDashboard from './governance/observability/ObservabilityDashboard';

type TabId = 'dashboard' | 'langfuse';

export default function Observability() {
  const [searchParams] = useSearchParams();
  const tabParam = searchParams.get('tab');
  const [activeTab, setActiveTab] = useState<TabId>('dashboard');
  const [settings, setSettings] = useState<ObservabilitySettings | null>(null);
  const [loadingSettings, setLoadingSettings] = useState(true);
  const [iframeError, setIframeError] = useState(false);

  useEffect(() => {
    if (tabParam === 'langfuse' || tabParam === 'dashboard') setActiveTab(tabParam);
  }, [tabParam]);

  // Detection is now the observability settings endpoint (`configured` =
  // backend has a LANGFUSE_HOST) — Langfuse is base infra, not a per-user
  // deploy. Fail-silent: on error we degrade to a not-configured settings shape
  // so the page renders calm empty-states rather than crashing.
  useEffect(() => {
    let cancelled = false;
    observabilityApi
      .getSettings()
      .then((s) => { if (!cancelled) setSettings(s); })
      .catch(() => { if (!cancelled) setSettings({ langfuse_host: null, configured: false }); })
      .finally(() => { if (!cancelled) setLoadingSettings(false); });
    return () => { cancelled = true; };
  }, []);

  const configured = !!settings?.configured;
  const langfuseHost = settings?.langfuse_host ?? null;

  const tabs: { id: TabId; label: string }[] = [
    { id: 'dashboard', label: 'Dashboard' },
    { id: 'langfuse', label: 'Langfuse' },
  ];

  return (
    <div className="min-h-[calc(100vh-4rem)] relative">
      {/* Ombre gradient background */}
      <div className="absolute inset-0 pointer-events-none" style={{
        background: 'radial-gradient(ellipse 80% 70% at 20% 50%, rgba(219,234,254,0.8) 0%, transparent 60%), radial-gradient(ellipse 60% 80% at 80% 40%, rgba(221,214,254,0.6) 0%, transparent 55%), radial-gradient(ellipse 50% 60% at 50% 80%, rgba(252,231,243,0.5) 0%, transparent 50%)',
        animation: 'gradientDrift 20s ease-in-out infinite',
      }} />
      <div className="relative max-w-7xl mx-auto px-6 py-10">
        <div className="mb-8 animate-fade-in">
          <Link to="/" className="text-sm text-slate-400 hover:text-slate-600 transition-colors font-medium">← Back to Home</Link>
          <h1 className="text-3xl font-semibold text-slate-900 tracking-tight mt-3">Observability</h1>
          <p className="text-slate-500 mt-2 max-w-2xl">Monitor, trace, and evaluate your deployed AI agents — a platform dashboard drawn from Langfuse data, plus the embedded Langfuse UI.</p>
        </div>

        {/* Tabs */}
        <div className="flex gap-2 mb-8 animate-fade-in stagger-1">
          {tabs.map((tab) => (
            <button
              key={tab.id}
              onClick={() => setActiveTab(tab.id)}
              className={`px-5 py-2.5 rounded-lg text-sm font-medium transition-all ${
                activeTab === tab.id
                  ? 'bg-slate-800 text-white'
                  : 'bg-white text-slate-500 border border-slate-200 hover:border-slate-300 hover:text-slate-700'
              }`}
            >
              {tab.label}
            </button>
          ))}
        </div>

        {loadingSettings ? (
          <div className="card">
            <div className="flex items-center gap-3">
              <div className="w-5 h-5 border-2 border-slate-300 border-t-slate-600 rounded-full animate-spin"></div>
              <p className="text-sm text-slate-500">Loading observability…</p>
            </div>
          </div>
        ) : activeTab === 'dashboard' ? (
          <div className="animate-fade-in">
            <ObservabilityDashboard configured={configured} />
          </div>
        ) : (
          /* Langfuse Tab — the embedded Langfuse UI (base-infra host from settings) */
          <div className="space-y-6 animate-fade-in">
            <div className="card">
              <div className="flex items-start gap-4">
                <div className="w-12 h-12 rounded-xl bg-violet-50 flex items-center justify-center flex-shrink-0">
                  <svg className="w-6 h-6 text-violet-600" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={1.5}>
                    <path strokeLinecap="round" strokeLinejoin="round" d="M3.75 3v11.25A2.25 2.25 0 006 16.5h2.25M3.75 3h-1.5m1.5 0h16.5m0 0h1.5m-1.5 0v11.25A2.25 2.25 0 0118 16.5h-2.25m-7.5 0h7.5m-7.5 0l-1 3m8.5-3l1 3m0 0l.5 1.5m-.5-1.5h-9.5m0 0l-.5 1.5m.75-9l3-1.5M12 12.75l3 1.5M12 12.75V18" />
                  </svg>
                </div>
                <div>
                  <h2 className="text-xl font-semibold text-slate-900 mb-2">Langfuse</h2>
                  <p className="text-sm text-slate-500 leading-relaxed">
                    Open-source LLM observability platform for tracing, evaluating, and debugging your AI agent workflows. Provides end-to-end visibility into multi-agent orchestration.
                  </p>
                </div>
              </div>
            </div>

            {configured && langfuseHost ? (
              <>
                <div className="card border-violet-200 bg-violet-50/30">
                  <div className="flex items-start justify-between gap-4">
                    <div className="flex items-start gap-4 min-w-0">
                      <div className="w-10 h-10 rounded-xl bg-emerald-100 flex items-center justify-center flex-shrink-0">
                        <svg className="w-5 h-5 text-emerald-600" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
                          <path strokeLinecap="round" strokeLinejoin="round" d="M9 12.75L11.25 15 15 9.75M21 12a9 9 0 11-18 0 9 9 0 0118 0z" />
                        </svg>
                      </div>
                      <div className="min-w-0">
                        <div className="flex items-center gap-2 mb-1">
                          <h3 className="text-base font-semibold text-slate-900">Langfuse Server</h3>
                          <span className="inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-xs font-medium bg-emerald-100 text-emerald-700">
                            <span className="w-1.5 h-1.5 rounded-full bg-emerald-500"></span>
                            Active
                          </span>
                        </div>
                        <p className="text-sm text-slate-500 truncate">{langfuseHost}</p>
                      </div>
                    </div>
                    <a
                      href={langfuseHost}
                      target="_blank"
                      rel="noopener noreferrer"
                      className="inline-flex items-center gap-2 px-5 py-2.5 bg-violet-600 hover:bg-violet-700 text-white text-sm font-medium rounded-lg transition-colors flex-shrink-0"
                    >
                      Open in New Tab
                      <svg className="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
                        <path strokeLinecap="round" strokeLinejoin="round" d="M13.5 6H5.25A2.25 2.25 0 003 8.25v10.5A2.25 2.25 0 005.25 21h10.5A2.25 2.25 0 0018 18.75V10.5m-10.5 6L21 3m0 0h-5.25M21 3v5.25" />
                      </svg>
                    </a>
                  </div>
                </div>

                {/* Embedded Langfuse Dashboard */}
                {!iframeError ? (
                  <div className="rounded-xl border border-violet-200 overflow-hidden bg-white" style={{ height: 'calc(100vh - 20rem)' }}>
                    <iframe
                      src={langfuseHost}
                      className="w-full h-full border-0"
                      title="Langfuse Dashboard"
                      onError={() => setIframeError(true)}
                      onLoad={(e) => {
                        // Detect X-Frame-Options block (iframe loads but shows blank/error)
                        try {
                          const iframe = e.target as HTMLIFrameElement;
                          // If we can't access contentWindow, it's likely blocked
                          if (iframe.contentWindow?.location.href === 'about:blank') {
                            setIframeError(true);
                          }
                        } catch {
                          // Cross-origin access denied means iframe loaded successfully
                        }
                      }}
                    />
                  </div>
                ) : (
                  <div className="card border-amber-200 bg-amber-50/30">
                    <div className="flex items-start gap-3">
                      <svg className="w-5 h-5 text-amber-600 flex-shrink-0 mt-0.5" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={1.5}>
                        <path strokeLinecap="round" strokeLinejoin="round" d="M12 9v3.75m9-.75a9 9 0 11-18 0 9 9 0 0118 0zm-9 3.75h.008v.008H12v-.008z" />
                      </svg>
                      <div>
                        <p className="text-sm font-medium text-amber-900">Unable to embed Langfuse dashboard</p>
                        <p className="text-sm text-amber-700/80 mt-1">
                          The Langfuse server's security headers prevent embedding. Use the "Open in New Tab" button above to access the dashboard directly.
                        </p>
                      </div>
                    </div>
                  </div>
                )}
              </>
            ) : (
              <div className="card border-slate-200 bg-slate-50/50">
                <div className="flex items-start gap-4">
                  <div className="w-10 h-10 rounded-xl bg-slate-100 flex items-center justify-center flex-shrink-0">
                    <svg className="w-5 h-5 text-slate-400" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={1.5}>
                      <path strokeLinecap="round" strokeLinejoin="round" d="M12 9v3.75m9-.75a9 9 0 11-18 0 9 9 0 0118 0zm-9 3.75h.008v.008H12v-.008z" />
                    </svg>
                  </div>
                  <div>
                    <h3 className="text-base font-semibold text-slate-900 mb-1">Langfuse not configured</h3>
                    <p className="text-sm text-slate-500 max-w-2xl">
                      Langfuse ships as base infrastructure. Once the platform's Langfuse stack is deployed, its host will be wired in automatically and the dashboard will embed here.
                    </p>
                  </div>
                </div>
              </div>
            )}
          </div>
        )}
      </div>
    </div>
  );
}
