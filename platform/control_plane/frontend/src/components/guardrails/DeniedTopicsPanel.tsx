import { useState } from 'react';
import type { DeniedTopic } from '../../types';

interface Props {
  topics: DeniedTopic[];
  onChange: (topics: DeniedTopic[]) => void;
}

const SUGGESTIONS = [
  { name: 'Insider Trading Advice', definition: 'Any advice related to trading based on material non-public information' },
  { name: 'Unauthorized Financial Advice', definition: 'Specific investment recommendations without proper licensing' },
  { name: 'Market Manipulation', definition: 'Strategies for artificially influencing security prices' },
];

export default function DeniedTopicsPanel({ topics, onChange }: Props) {
  const [newExample, setNewExample] = useState<Record<number, string>>({});

  const addTopic = () => {
    onChange([...topics, { name: '', definition: '', examples: [] }]);
  };

  const removeTopic = (index: number) => {
    onChange(topics.filter((_, i) => i !== index));
  };

  const updateTopic = (index: number, field: keyof DeniedTopic, value: string | string[]) => {
    onChange(topics.map((t, i) => (i === index ? { ...t, [field]: value } : t)));
  };

  const addExample = (index: number) => {
    const ex = newExample[index]?.trim();
    if (!ex) return;
    const updated = [...topics[index].examples, ex];
    updateTopic(index, 'examples', updated);
    setNewExample({ ...newExample, [index]: '' });
  };

  const removeExample = (topicIndex: number, exIndex: number) => {
    const updated = topics[topicIndex].examples.filter((_, i) => i !== exIndex);
    updateTopic(topicIndex, 'examples', updated);
  };

  const addSuggestion = (suggestion: { name: string; definition: string }) => {
    if (topics.some((t) => t.name === suggestion.name)) return;
    onChange([...topics, { ...suggestion, examples: [] }]);
  };

  return (
    <div className="space-y-4">
      {/* Suggestions */}
      {topics.length === 0 && (
        <div className="p-4 bg-blue-50/50 border border-blue-100 rounded-xl">
          <p className="text-xs font-medium text-blue-700 mb-2">Quick Add (FSI Suggestions)</p>
          <div className="flex flex-wrap gap-2">
            {SUGGESTIONS.map((s) => (
              <button
                key={s.name}
                onClick={() => addSuggestion(s)}
                className="px-3 py-1.5 text-xs font-medium bg-white border border-blue-200 text-blue-700 rounded-lg hover:bg-blue-50 transition-colors"
              >
                + {s.name}
              </button>
            ))}
          </div>
        </div>
      )}

      {/* Topic list */}
      {topics.map((topic, index) => (
        <div key={index} className="border border-slate-200 rounded-xl p-4 space-y-3">
          <div className="flex items-start justify-between gap-3">
            <div className="flex-1 space-y-3">
              <input
                type="text"
                value={topic.name}
                onChange={(e) => updateTopic(index, 'name', e.target.value)}
                placeholder="Topic name (e.g., Insider Trading Advice)"
                className="input-field text-sm w-full"
              />
              <textarea
                value={topic.definition}
                onChange={(e) => updateTopic(index, 'definition', e.target.value)}
                placeholder="Definition — describe what this topic covers..."
                className="input-field text-sm w-full resize-none"
                rows={2}
              />
            </div>
            <button
              onClick={() => removeTopic(index)}
              className="p-1.5 text-slate-400 hover:text-red-500 transition-colors"
            >
              <svg className="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
                <path strokeLinecap="round" strokeLinejoin="round" d="M6 18L18 6M6 6l12 12" />
              </svg>
            </button>
          </div>

          {/* Examples */}
          <div>
            <p className="text-xs text-slate-500 mb-1.5">Examples (optional, max 5)</p>
            <div className="flex flex-wrap gap-1.5 mb-2">
              {topic.examples.map((ex, exIdx) => (
                <span key={exIdx} className="inline-flex items-center gap-1 px-2 py-1 bg-slate-100 text-slate-700 text-xs rounded-lg">
                  {ex}
                  <button onClick={() => removeExample(index, exIdx)} className="text-slate-400 hover:text-red-500">
                    <svg className="w-3 h-3" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
                      <path strokeLinecap="round" strokeLinejoin="round" d="M6 18L18 6M6 6l12 12" />
                    </svg>
                  </button>
                </span>
              ))}
            </div>
            {topic.examples.length < 5 && (
              <div className="flex gap-2">
                <input
                  type="text"
                  value={newExample[index] || ''}
                  onChange={(e) => setNewExample({ ...newExample, [index]: e.target.value })}
                  onKeyDown={(e) => e.key === 'Enter' && addExample(index)}
                  placeholder="Add example..."
                  className="input-field text-xs flex-1 py-1.5"
                />
                <button onClick={() => addExample(index)} className="btn-secondary text-xs px-3 py-1.5">
                  Add
                </button>
              </div>
            )}
          </div>
        </div>
      ))}

      <button onClick={addTopic} className="btn-secondary w-full text-sm py-2.5">
        + Add Denied Topic
      </button>
    </div>
  );
}
