import type { ContextualGroundingConfig } from '../../types';

interface Props {
  config: ContextualGroundingConfig;
  onChange: (config: ContextualGroundingConfig) => void;
}

export default function ContextualGroundingPanel({ config, onChange }: Props) {
  return (
    <div className="space-y-5">
      {/* Enable toggle */}
      <div className="flex items-center justify-between p-4 bg-slate-50 rounded-xl">
        <div>
          <p className="text-sm font-medium text-slate-800">Enable Contextual Grounding</p>
          <p className="text-xs text-slate-500 mt-0.5">Verify that model responses are grounded in source material and relevant to the query</p>
        </div>
        <button
          onClick={() => onChange({ ...config, enabled: !config.enabled })}
          className={`relative w-11 h-6 rounded-full transition-colors ${
            config.enabled ? 'bg-blue-600' : 'bg-slate-200'
          }`}
        >
          <span
            className={`absolute top-0.5 left-0.5 w-5 h-5 bg-white rounded-full shadow transition-transform ${
              config.enabled ? 'translate-x-5' : 'translate-x-0'
            }`}
          />
        </button>
      </div>

      {config.enabled && (
        <div className="space-y-5 pl-1">
          {/* Grounding threshold */}
          <div>
            <div className="flex items-center justify-between mb-2">
              <div>
                <p className="text-sm font-medium text-slate-800">Grounding Threshold</p>
                <p className="text-xs text-slate-500">How closely the response must align with provided context</p>
              </div>
              <span className="text-sm font-mono font-semibold text-blue-600">{config.grounding_threshold.toFixed(2)}</span>
            </div>
            <input
              type="range"
              min="0"
              max="1"
              step="0.05"
              value={config.grounding_threshold}
              onChange={(e) => onChange({ ...config, grounding_threshold: parseFloat(e.target.value) })}
              className="w-full h-2 bg-slate-200 rounded-full appearance-none cursor-pointer accent-blue-600"
            />
            <div className="flex justify-between mt-1">
              <span className="text-[10px] text-slate-400">Lenient (0.0)</span>
              <span className="text-[10px] text-slate-400">Strict (1.0)</span>
            </div>
          </div>

          {/* Relevance threshold */}
          <div>
            <div className="flex items-center justify-between mb-2">
              <div>
                <p className="text-sm font-medium text-slate-800">Relevance Threshold</p>
                <p className="text-xs text-slate-500">How relevant the response must be to the user query</p>
              </div>
              <span className="text-sm font-mono font-semibold text-blue-600">{config.relevance_threshold.toFixed(2)}</span>
            </div>
            <input
              type="range"
              min="0"
              max="1"
              step="0.05"
              value={config.relevance_threshold}
              onChange={(e) => onChange({ ...config, relevance_threshold: parseFloat(e.target.value) })}
              className="w-full h-2 bg-slate-200 rounded-full appearance-none cursor-pointer accent-blue-600"
            />
            <div className="flex justify-between mt-1">
              <span className="text-[10px] text-slate-400">Lenient (0.0)</span>
              <span className="text-[10px] text-slate-400">Strict (1.0)</span>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
