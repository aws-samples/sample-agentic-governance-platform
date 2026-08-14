import React from 'react';
import { Link, useNavigate } from 'react-router-dom';
import GuardrailBuilder from './guardrails/GuardrailBuilder';
import GuardrailTemplateList from './guardrails/GuardrailTemplateList';
import GuardrailObservability from './guardrails/GuardrailObservability';

type Tab = 'builder' | 'templates' | 'observability';

export default function Guardrails({ initialTab }: { initialTab?: Tab }) {
  const navigate = useNavigate();
  const activeTab: Tab = initialTab || 'templates';

  const tabs: { id: Tab; label: string; path: string; icon: React.ReactNode }[] = [
    {
      id: 'templates',
      label: 'My Guardrails',
      path: '/secure/guardrails',
      icon: (
        <svg className="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={1.5}>
          <path strokeLinecap="round" strokeLinejoin="round" d="M3.75 12h16.5m-16.5 3.75h16.5M3.75 19.5h16.5M5.625 4.5h12.75a1.875 1.875 0 010 3.75H5.625a1.875 1.875 0 010-3.75z" />
        </svg>
      ),
    },
    {
      id: 'builder',
      label: 'Create Guardrail',
      path: '/secure/guardrails/create',
      icon: (
        <svg className="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={1.5}>
          <path strokeLinecap="round" strokeLinejoin="round" d="M12 4.5v15m7.5-7.5h-15" />
        </svg>
      ),
    },
    {
      id: 'observability',
      label: 'Observability',
      path: '/secure/guardrails/observability',
      icon: (
        <svg className="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={1.5}>
          <path strokeLinecap="round" strokeLinejoin="round" d="M3 13.125C3 12.504 3.504 12 4.125 12h2.25c.621 0 1.125.504 1.125 1.125v6.75C7.5 20.496 6.996 21 6.375 21h-2.25A1.125 1.125 0 013 19.875v-6.75zM9.75 8.625c0-.621.504-1.125 1.125-1.125h2.25c.621 0 1.125.504 1.125 1.125v11.25c0 .621-.504 1.125-1.125 1.125h-2.25a1.125 1.125 0 01-1.125-1.125V8.625zM16.5 4.125c0-.621.504-1.125 1.125-1.125h2.25C20.496 3 21 3.504 21 4.125v15.75c0 .621-.504 1.125-1.125 1.125h-2.25a1.125 1.125 0 01-1.125-1.125V4.125z" />
        </svg>
      ),
    },
  ];

  return (
    <div className="min-h-[calc(100vh-4rem)] relative">
      <div className="relative max-w-7xl mx-auto px-6 py-10">
        {/* Header */}
        <div className="mb-8 animate-fade-in">
          <Link to="/" className="text-sm text-slate-400 hover:text-slate-600 transition-colors font-medium">
            &larr; Back to Home
          </Link>
          <h1 className="text-3xl font-semibold text-slate-900 tracking-tight mt-3">Guardrails</h1>
          <p className="text-slate-500 mt-2 max-w-2xl">
            Configure safety controls for your AI agents — content filtering, PII detection, prompt injection protection, and more.
          </p>
        </div>

        {/* Tab Navigation */}
        <div className="flex items-center gap-1 mb-8 p-1 bg-slate-100/80 rounded-xl w-fit animate-fade-in stagger-1">
          {tabs.map((tab) => (
            <button
              key={tab.id}
              onClick={() => navigate(tab.path)}
              className={`flex items-center gap-2 px-4 py-2 rounded-lg text-sm font-medium transition-all ${
                activeTab === tab.id
                  ? 'bg-white text-slate-900 shadow-sm'
                  : 'text-slate-500 hover:text-slate-700'
              }`}
            >
              {tab.icon}
              {tab.label}
            </button>
          ))}
        </div>

        {/* Tab Content */}
        <div className="animate-fade-in stagger-2">
          {activeTab === 'templates' && <GuardrailTemplateList onCreateNew={() => navigate('/secure/guardrails/create')} />}
          {activeTab === 'builder' && <GuardrailBuilder onComplete={() => navigate('/secure/guardrails')} />}
          {activeTab === 'observability' && <GuardrailObservability />}
        </div>
      </div>
    </div>
  );
}
