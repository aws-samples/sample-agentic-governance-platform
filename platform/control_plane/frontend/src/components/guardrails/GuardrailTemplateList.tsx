import { useState, useEffect } from 'react';
import { guardrailsApi } from '../../api/client';
import type { GuardrailTemplate, GuardrailStatus } from '../../types';

interface Props {
  onCreateNew: () => void;
}

const statusStyles: Record<GuardrailStatus, { bg: string; text: string; dot: string }> = {
  draft: { bg: 'bg-slate-100', text: 'text-slate-700', dot: 'bg-slate-400' },
  creating: { bg: 'bg-blue-50', text: 'text-blue-700', dot: 'bg-blue-400' },
  active: { bg: 'bg-emerald-50', text: 'text-emerald-700', dot: 'bg-emerald-400' },
  updating: { bg: 'bg-amber-50', text: 'text-amber-700', dot: 'bg-amber-400' },
  failed: { bg: 'bg-red-50', text: 'text-red-700', dot: 'bg-red-400' },
  deleting: { bg: 'bg-orange-50', text: 'text-orange-700', dot: 'bg-orange-400' },
  deleted: { bg: 'bg-slate-50', text: 'text-slate-500', dot: 'bg-slate-300' },
};

export default function GuardrailTemplateList({ onCreateNew }: Props) {
  const [templates, setTemplates] = useState<GuardrailTemplate[]>([]);
  const [loading, setLoading] = useState(true);
  const [copiedId, setCopiedId] = useState<string | null>(null);

  useEffect(() => {
    loadTemplates();
  }, []);

  const loadTemplates = async () => {
    setLoading(true);
    try {
      const data = await guardrailsApi.list();
      setTemplates(data.filter((t) => t.status !== 'deleted'));
    } catch {
      // API might not be deployed yet — show empty state
    } finally {
      setLoading(false);
    }
  };

  const copyId = (id: string) => {
    navigator.clipboard.writeText(id);
    setCopiedId(id);
    setTimeout(() => setCopiedId(null), 2000);
  };

  const handleDelete = async (templateId: string) => {
    if (!confirm('Delete this guardrail template? This will also remove the Bedrock resource.')) return;
    try {
      await guardrailsApi.delete(templateId);
      loadTemplates();
    } catch {}
  };

  const featureSummary = (t: GuardrailTemplate): string[] => {
    const features: string[] = [];
    if (t.content_filters?.length > 0) features.push('Content');
    if (t.pii_entities?.length > 0) features.push('PII');
    if (t.denied_topics?.length > 0) features.push('Topics');
    if (t.word_filter?.enable_profanity || (t.word_filter?.blocked_words?.length ?? 0) > 0) features.push('Words');
    if (t.contextual_grounding?.enabled) features.push('Grounding');
    return features;
  };

  if (loading) {
    return (
      <div className="flex items-center justify-center py-20">
        <div className="w-6 h-6 border-2 border-slate-200 border-t-blue-600 rounded-full animate-spin" />
      </div>
    );
  }

  if (templates.length === 0) {
    return (
      <div className="text-center py-16">
        <div className="w-16 h-16 mx-auto mb-4 bg-slate-100 rounded-2xl flex items-center justify-center">
          <svg className="w-8 h-8 text-slate-400" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={1.5}>
            <path strokeLinecap="round" strokeLinejoin="round" d="M12 9v3.75m0-10.036A11.959 11.959 0 013.598 6 11.99 11.99 0 003 9.749c0 5.592 3.824 10.29 9 11.623 5.176-1.332 9-6.03 9-11.622 0-1.31-.21-2.571-.598-3.751h-.152c-3.196 0-6.1-1.248-8.25-3.285z" />
          </svg>
        </div>
        <h3 className="text-lg font-semibold text-slate-900 mb-2">No Guardrail Templates</h3>
        <p className="text-sm text-slate-500 mb-6 max-w-md mx-auto">
          Create your first guardrail template to protect your AI agents with content filtering, PII detection, and more.
        </p>
        <button onClick={onCreateNew} className="btn-primary text-sm px-5 py-2.5">
          Create Your First Guardrail
        </button>
      </div>
    );
  }

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <p className="text-sm text-slate-500">{templates.length} template{templates.length !== 1 ? 's' : ''}</p>
        <button onClick={onCreateNew} className="btn-primary text-sm px-4 py-2">
          + Create New
        </button>
      </div>

      <div className="space-y-3">
        {templates.map((template) => {
          const style = statusStyles[template.status] || statusStyles.draft;
          const features = featureSummary(template);

          return (
            <div key={template.template_id} className="card hover:shadow-md transition-shadow">
              <div className="flex items-start justify-between gap-4">
                <div className="flex-1 min-w-0">
                  <div className="flex items-center gap-3 mb-1">
                    <h3 className="text-sm font-semibold text-slate-900 truncate">{template.name}</h3>
                    <span className={`inline-flex items-center gap-1.5 px-2 py-0.5 rounded-full text-[10px] font-semibold ${style.bg} ${style.text}`}>
                      <span className={`w-1.5 h-1.5 rounded-full ${style.dot}`} />
                      {template.status}
                    </span>
                  </div>
                  {template.description && (
                    <p className="text-xs text-slate-500 mb-2 truncate">{template.description}</p>
                  )}
                  <div className="flex items-center gap-3 flex-wrap">
                    {features.map((f) => (
                      <span key={f} className="px-2 py-0.5 text-[10px] font-medium bg-slate-100 text-slate-600 rounded-full">{f}</span>
                    ))}
                    {template.guardrail_id && (
                      <button
                        onClick={() => copyId(template.guardrail_id!)}
                        className="inline-flex items-center gap-1 text-[10px] font-mono text-blue-600 hover:text-blue-800 transition-colors"
                      >
                        <svg className="w-3 h-3" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
                          <path strokeLinecap="round" strokeLinejoin="round" d="M15.666 3.888A2.25 2.25 0 0013.5 2.25h-3c-1.03 0-1.9.693-2.166 1.638m7.332 0c.055.194.084.4.084.612v0a.75.75 0 01-.75.75H9.75a.75.75 0 01-.75-.75v0c0-.212.03-.418.084-.612m7.332 0c.646.049 1.288.11 1.927.184 1.1.128 1.907 1.077 1.907 2.185V19.5a2.25 2.25 0 01-2.25 2.25H6.75A2.25 2.25 0 014.5 19.5V6.257c0-1.108.806-2.057 1.907-2.185a48.208 48.208 0 011.927-.184" />
                        </svg>
                        {copiedId === template.guardrail_id ? 'Copied!' : template.guardrail_id}
                      </button>
                    )}
                  </div>
                </div>
                <div className="flex items-center gap-1 flex-shrink-0">
                  <button
                    onClick={() => handleDelete(template.template_id)}
                    className="p-2 text-slate-400 hover:text-red-500 rounded-lg hover:bg-red-50 transition-colors"
                    title="Delete"
                  >
                    <svg className="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={1.5}>
                      <path strokeLinecap="round" strokeLinejoin="round" d="M14.74 9l-.346 9m-4.788 0L9.26 9m9.968-3.21c.342.052.682.107 1.022.166m-1.022-.165L18.16 19.673a2.25 2.25 0 01-2.244 2.077H8.084a2.25 2.25 0 01-2.244-2.077L4.772 5.79m14.456 0a48.108 48.108 0 00-3.478-.397m-12 .562c.34-.059.68-.114 1.022-.165m0 0a48.11 48.11 0 013.478-.397m7.5 0v-.916c0-1.18-.91-2.164-2.09-2.201a51.964 51.964 0 00-3.32 0c-1.18.037-2.09 1.022-2.09 2.201v.916m7.5 0a48.667 48.667 0 00-7.5 0" />
                    </svg>
                  </button>
                </div>
              </div>
              <div className="flex items-center gap-4 mt-3 pt-3 border-t border-slate-100 text-[10px] text-slate-400">
                <span>Created {new Date(template.created_at).toLocaleDateString()}</span>
                {template.guardrail_version && <span>Version {template.guardrail_version}</span>}
                {template.created_by && <span>by {template.created_by}</span>}
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
}
