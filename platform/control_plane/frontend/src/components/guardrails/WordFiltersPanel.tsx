import { useState } from 'react';
import type { WordFilterConfig } from '../../types';

interface Props {
  config: WordFilterConfig;
  onChange: (config: WordFilterConfig) => void;
}

export default function WordFiltersPanel({ config, onChange }: Props) {
  const [newWord, setNewWord] = useState('');

  const addWord = () => {
    const word = newWord.trim().toLowerCase();
    if (!word || config.blocked_words.includes(word)) return;
    onChange({ ...config, blocked_words: [...config.blocked_words, word] });
    setNewWord('');
  };

  const removeWord = (word: string) => {
    onChange({ ...config, blocked_words: config.blocked_words.filter((w) => w !== word) });
  };

  return (
    <div className="space-y-5">
      {/* Profanity toggle */}
      <div className="flex items-center justify-between p-4 bg-slate-50 rounded-xl">
        <div>
          <p className="text-sm font-medium text-slate-800">Managed Profanity Filter</p>
          <p className="text-xs text-slate-500 mt-0.5">AWS-managed list of profane words and phrases</p>
        </div>
        <button
          onClick={() => onChange({ ...config, enable_profanity: !config.enable_profanity })}
          className={`relative w-11 h-6 rounded-full transition-colors ${
            config.enable_profanity ? 'bg-blue-600' : 'bg-slate-200'
          }`}
        >
          <span
            className={`absolute top-0.5 left-0.5 w-5 h-5 bg-white rounded-full shadow transition-transform ${
              config.enable_profanity ? 'translate-x-5' : 'translate-x-0'
            }`}
          />
        </button>
      </div>

      {/* Custom blocked words */}
      <div>
        <p className="text-sm font-medium text-slate-800 mb-2">Custom Blocked Words</p>
        <p className="text-xs text-slate-500 mb-3">Add specific words or phrases that should be blocked</p>

        <div className="flex gap-2 mb-3">
          <input
            type="text"
            value={newWord}
            onChange={(e) => setNewWord(e.target.value)}
            onKeyDown={(e) => e.key === 'Enter' && addWord()}
            placeholder="Enter a word or phrase..."
            className="input-field text-sm flex-1"
          />
          <button onClick={addWord} className="btn-primary text-sm px-4">
            Add
          </button>
        </div>

        {config.blocked_words.length > 0 && (
          <div className="flex flex-wrap gap-2">
            {config.blocked_words.map((word) => (
              <span key={word} className="inline-flex items-center gap-1.5 px-3 py-1.5 bg-red-50 border border-red-200 text-red-700 text-xs font-medium rounded-lg">
                {word}
                <button onClick={() => removeWord(word)} className="text-red-400 hover:text-red-600">
                  <svg className="w-3 h-3" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
                    <path strokeLinecap="round" strokeLinejoin="round" d="M6 18L18 6M6 6l12 12" />
                  </svg>
                </button>
              </span>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}
