import type { ContentFilterConfig, GuardrailFilterType, GuardrailFilterStrength } from '../../types';

const FILTER_TYPES: { type: GuardrailFilterType; label: string; description: string }[] = [
  { type: 'HATE', label: 'Hate Speech', description: 'Discriminatory or hateful content' },
  { type: 'INSULTS', label: 'Insults', description: 'Demeaning or offensive language' },
  { type: 'SEXUAL', label: 'Sexual Content', description: 'Sexually explicit material' },
  { type: 'VIOLENCE', label: 'Violence', description: 'Violent or graphic content' },
  { type: 'MISCONDUCT', label: 'Misconduct', description: 'Criminal or harmful activities' },
  { type: 'PROMPT_ATTACK', label: 'Prompt Injection', description: 'Attempts to override system instructions' },
];

const STRENGTHS: GuardrailFilterStrength[] = ['NONE', 'LOW', 'MEDIUM', 'HIGH'];

const strengthColors: Record<GuardrailFilterStrength, string> = {
  NONE: 'bg-slate-100 text-slate-600',
  LOW: 'bg-emerald-50 text-emerald-700 border-emerald-200',
  MEDIUM: 'bg-amber-50 text-amber-700 border-amber-200',
  HIGH: 'bg-red-50 text-red-700 border-red-200',
};

interface Props {
  filters: ContentFilterConfig[];
  onChange: (filters: ContentFilterConfig[]) => void;
}

export default function ContentFilterPanel({ filters, onChange }: Props) {
  const getFilter = (type: GuardrailFilterType): ContentFilterConfig => {
    return filters.find((f) => f.type === type) || { type, input_strength: 'NONE', output_strength: 'NONE' };
  };

  const updateFilter = (type: GuardrailFilterType, field: 'input_strength' | 'output_strength', value: GuardrailFilterStrength) => {
    const existing = filters.filter((f) => f.type !== type);
    const current = getFilter(type);
    const updated = { ...current, [field]: value };
    // Only include if at least one strength is set
    if (updated.input_strength === 'NONE' && updated.output_strength === 'NONE') {
      onChange(existing);
    } else {
      onChange([...existing, updated]);
    }
  };

  return (
    <div className="space-y-4">
      <div className="grid grid-cols-[1fr_120px_120px] gap-3 items-center mb-2">
        <span className="text-[10px] font-semibold text-slate-400 uppercase tracking-wider">Filter</span>
        <span className="text-[10px] font-semibold text-slate-400 uppercase tracking-wider text-center">Input</span>
        <span className="text-[10px] font-semibold text-slate-400 uppercase tracking-wider text-center">Output</span>
      </div>
      {FILTER_TYPES.map(({ type, label, description }) => {
        const filter = getFilter(type);
        const isPromptAttack = type === 'PROMPT_ATTACK';
        return (
          <div key={type} className="grid grid-cols-[1fr_120px_120px] gap-3 items-center py-2 border-b border-slate-100 last:border-0">
            <div>
              <p className="text-sm font-medium text-slate-800">{label}</p>
              <p className="text-xs text-slate-400">{description}</p>
            </div>
            <select
              value={filter.input_strength}
              onChange={(e) => updateFilter(type, 'input_strength', e.target.value as GuardrailFilterStrength)}
              className={`input-field text-xs text-center py-1.5 ${strengthColors[filter.input_strength]}`}
            >
              {STRENGTHS.map((s) => (
                <option key={s} value={s}>{s}</option>
              ))}
            </select>
            {isPromptAttack ? (
              <div className="text-xs text-center text-slate-400 py-1.5">N/A</div>
            ) : (
              <select
                value={filter.output_strength}
                onChange={(e) => updateFilter(type, 'output_strength', e.target.value as GuardrailFilterStrength)}
                className={`input-field text-xs text-center py-1.5 ${strengthColors[filter.output_strength]}`}
              >
                {STRENGTHS.map((s) => (
                  <option key={s} value={s}>{s}</option>
                ))}
              </select>
            )}
          </div>
        );
      })}
    </div>
  );
}
