import { useEffect, useState } from 'react';
import type { ContentFilterConfig, PiiEntityConfig, DeniedTopic, WordFilterConfig, ContextualGroundingConfig } from '../../types';

interface Props {
  contentFilters: ContentFilterConfig[];
  piiEntities: PiiEntityConfig[];
  deniedTopics: DeniedTopic[];
  wordFilter?: WordFilterConfig;
  contextualGrounding?: ContextualGroundingConfig;
}

interface Stage {
  id: string;
  label: string;
  shortLabel: string;
  active: boolean;
  color: string;
}

export default function DataFlowVisualizer({ contentFilters, piiEntities, deniedTopics, wordFilter, contextualGrounding }: Props) {
  const [particlePos, setParticlePos] = useState(0);
  const [animating, setAnimating] = useState(true);

  const stages: Stage[] = [
    {
      id: 'content',
      label: 'Content Filter',
      shortLabel: 'Content',
      active: contentFilters.some((f) => f.input_strength !== 'NONE'),
      color: '#DC2626',
    },
    {
      id: 'pii',
      label: 'PII Detection',
      shortLabel: 'PII',
      active: piiEntities.length > 0,
      color: '#F59E0B',
    },
    {
      id: 'topics',
      label: 'Topic Check',
      shortLabel: 'Topics',
      active: deniedTopics.length > 0,
      color: '#8B5CF6',
    },
    {
      id: 'words',
      label: 'Word Filter',
      shortLabel: 'Words',
      active: !!(wordFilter?.enable_profanity || (wordFilter?.blocked_words?.length ?? 0) > 0),
      color: '#EC4899',
    },
    {
      id: 'grounding',
      label: 'Grounding',
      shortLabel: 'Ground',
      active: !!(contextualGrounding?.enabled),
      color: '#0EA5E9',
    },
  ];

  const activeStages = stages.filter((s) => s.active);
  const totalStages = activeStages.length;

  useEffect(() => {
    if (!animating || totalStages === 0) return;
    const interval = setInterval(() => {
      setParticlePos((prev) => (prev + 1) % ((totalStages + 2) * 10));
    }, 80);
    return () => clearInterval(interval);
  }, [animating, totalStages]);

  if (totalStages === 0) {
    return (
      <div className="p-6 bg-slate-50 rounded-xl border border-slate-200 text-center">
        <svg className="w-8 h-8 text-slate-300 mx-auto mb-2" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={1.5}>
          <path strokeLinecap="round" strokeLinejoin="round" d="M12 9v3.75m0-10.036A11.959 11.959 0 013.598 6 11.99 11.99 0 003 9.75c0 5.592 3.824 10.29 9 11.622 5.176-1.332 9-6.03 9-11.622 0-1.31-.21-2.571-.598-3.75h-.152c-3.196 0-6.1-1.248-8.25-3.285z" />
        </svg>
        <p className="text-sm text-slate-500">Enable guardrail features to see the data flow</p>
      </div>
    );
  }

  // Calculate which stage the particle is at
  const segmentSize = 10; // frames per segment
  const particleSegment = Math.floor(particlePos / segmentSize);
  const segmentProgress = (particlePos % segmentSize) / segmentSize;

  return (
    <div className="p-5 bg-gradient-to-r from-slate-50 to-blue-50/30 rounded-xl border border-slate-200">
      <div className="flex items-center justify-between mb-3">
        <p className="text-xs font-semibold text-slate-500 uppercase tracking-wider">Data Flow Preview</p>
        <button
          onClick={() => setAnimating(!animating)}
          className="text-xs text-slate-400 hover:text-slate-600 transition-colors"
        >
          {animating ? 'Pause' : 'Play'}
        </button>
      </div>

      {/* Pipeline visualization */}
      <div className="relative flex items-center gap-0 overflow-hidden py-4">
        {/* Input node */}
        <div className="flex-shrink-0 flex flex-col items-center gap-1">
          <div className={`w-10 h-10 rounded-xl flex items-center justify-center border-2 transition-all ${
            particleSegment === 0 ? 'border-blue-400 bg-blue-50 shadow-sm shadow-blue-200' : 'border-slate-200 bg-white'
          }`}>
            <svg className="w-4 h-4 text-blue-600" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
              <path strokeLinecap="round" strokeLinejoin="round" d="M7.5 8.25h9m-9 3H12m-9.75 1.51c0 1.6 1.123 2.994 2.707 3.227 1.129.166 2.27.293 3.423.379.35.026.67.21.865.501L12 21l2.755-4.133a1.14 1.14 0 01.865-.501 48.172 48.172 0 003.423-.379c1.584-.233 2.707-1.626 2.707-3.228V6.741c0-1.602-1.123-2.995-2.707-3.228A48.394 48.394 0 0012 3c-2.392 0-4.744.175-7.043.513C3.373 3.746 2.25 5.14 2.25 6.741v6.018z" />
            </svg>
          </div>
          <span className="text-[9px] text-slate-500 font-medium">Input</span>
        </div>

        {/* Connector + stages */}
        {activeStages.map((stage, idx) => {
          const stageSegment = idx + 1;
          const isActive = particleSegment === stageSegment;
          const isPassed = particleSegment > stageSegment;

          return (
            <div key={stage.id} className="flex items-center flex-1 min-w-0">
              {/* Connector line */}
              <div className="flex-1 h-0.5 relative mx-1">
                <div className="absolute inset-0 bg-slate-200 rounded-full" />
                <div
                  className="absolute inset-y-0 left-0 rounded-full transition-all duration-100"
                  style={{
                    width: isPassed ? '100%' : isActive ? '50%' : particleSegment === stageSegment - 1 ? `${segmentProgress * 100}%` : '0%',
                    backgroundColor: stage.color,
                    opacity: 0.6,
                  }}
                />
              </div>

              {/* Stage node */}
              <div className="flex-shrink-0 flex flex-col items-center gap-1">
                <div
                  className={`w-10 h-10 rounded-xl flex items-center justify-center border-2 transition-all duration-200 ${
                    isActive ? 'scale-110 shadow-md' : ''
                  }`}
                  style={{
                    borderColor: isActive ? stage.color : isPassed ? `${stage.color}60` : '#e2e8f0',
                    backgroundColor: isActive ? `${stage.color}10` : isPassed ? `${stage.color}05` : 'white',
                    boxShadow: isActive ? `0 4px 12px ${stage.color}30` : 'none',
                  }}
                >
                  <div
                    className="w-3 h-3 rounded-full transition-all"
                    style={{
                      backgroundColor: isActive || isPassed ? stage.color : '#cbd5e1',
                      boxShadow: isActive ? `0 0 8px ${stage.color}` : 'none',
                    }}
                  />
                </div>
                <span className="text-[9px] text-slate-500 font-medium whitespace-nowrap">{stage.shortLabel}</span>
              </div>
            </div>
          );
        })}

        {/* Final connector + output */}
        <div className="flex items-center flex-shrink-0">
          <div className="w-6 h-0.5 mx-1 relative">
            <div className="absolute inset-0 bg-slate-200 rounded-full" />
            <div
              className="absolute inset-y-0 left-0 bg-emerald-500 rounded-full transition-all duration-100"
              style={{ width: particleSegment > totalStages ? '100%' : '0%', opacity: 0.6 }}
            />
          </div>
          <div className="flex flex-col items-center gap-1">
            <div className={`w-10 h-10 rounded-xl flex items-center justify-center border-2 transition-all ${
              particleSegment > totalStages ? 'border-emerald-400 bg-emerald-50 shadow-sm shadow-emerald-200' : 'border-slate-200 bg-white'
            }`}>
              <svg className="w-4 h-4 text-emerald-600" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
                <path strokeLinecap="round" strokeLinejoin="round" d="M9 12.75L11.25 15 15 9.75M21 12a9 9 0 11-18 0 9 9 0 0118 0z" />
              </svg>
            </div>
            <span className="text-[9px] text-slate-500 font-medium">Output</span>
          </div>
        </div>
      </div>

      {/* Legend */}
      <div className="flex flex-wrap gap-3 mt-3 pt-3 border-t border-slate-200/60">
        {activeStages.map((stage) => (
          <div key={stage.id} className="flex items-center gap-1.5">
            <div className="w-2 h-2 rounded-full" style={{ backgroundColor: stage.color }} />
            <span className="text-[10px] text-slate-500">{stage.label}</span>
          </div>
        ))}
      </div>
    </div>
  );
}
