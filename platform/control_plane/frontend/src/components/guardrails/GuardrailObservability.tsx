import { useState, useEffect } from 'react';
import { guardrailsApi } from '../../api/client';
import type { GuardrailTemplate, GuardrailMetrics } from '../../types';

export default function GuardrailObservability() {
  const [templates, setTemplates] = useState<GuardrailTemplate[]>([]);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [metrics, setMetrics] = useState<GuardrailMetrics | null>(null);
  const [loading, setLoading] = useState(true);
  const [metricsLoading, setMetricsLoading] = useState(false);
  const [hours, setHours] = useState(24);

  useEffect(() => {
    guardrailsApi.list('active').then((data) => {
      setTemplates(data);
      if (data.length > 0) setSelectedId(data[0].template_id);
    }).catch(() => {}).finally(() => setLoading(false));
  }, []);

  useEffect(() => {
    if (!selectedId) return;
    setMetricsLoading(true);
    guardrailsApi.getMetrics(selectedId, hours)
      .then(setMetrics)
      .catch(() => setMetrics(null))
      .finally(() => setMetricsLoading(false));
  }, [selectedId, hours]);

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
            <path strokeLinecap="round" strokeLinejoin="round" d="M3 13.125C3 12.504 3.504 12 4.125 12h2.25c.621 0 1.125.504 1.125 1.125v6.75C7.5 20.496 6.996 21 6.375 21h-2.25A1.125 1.125 0 013 19.875v-6.75z" />
          </svg>
        </div>
        <h3 className="text-lg font-semibold text-slate-900 mb-2">No Active Guardrails</h3>
        <p className="text-sm text-slate-500">Create and deploy guardrails to see observability data here.</p>
      </div>
    );
  }

  const maxInvocations = metrics?.time_series?.length
    ? Math.max(...metrics.time_series.map((d) => d.invocations), 1)
    : 1;

  return (
    <div className="space-y-6">
      {/* Controls */}
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-3">
          <select
            value={selectedId || ''}
            onChange={(e) => setSelectedId(e.target.value)}
            className="input-field text-sm py-2"
          >
            {templates.map((t) => (
              <option key={t.template_id} value={t.template_id}>{t.name}</option>
            ))}
          </select>
        </div>
        <div className="flex items-center gap-2">
          {[6, 24, 72, 168].map((h) => (
            <button
              key={h}
              onClick={() => setHours(h)}
              className={`px-3 py-1.5 text-xs font-medium rounded-lg transition-colors ${
                hours === h ? 'bg-blue-600 text-white' : 'bg-slate-100 text-slate-600 hover:bg-slate-200'
              }`}
            >
              {h < 24 ? `${h}h` : `${h / 24}d`}
            </button>
          ))}
        </div>
      </div>

      {metricsLoading ? (
        <div className="flex items-center justify-center py-12">
          <div className="w-5 h-5 border-2 border-slate-200 border-t-blue-600 rounded-full animate-spin" />
        </div>
      ) : metrics ? (
        <>
          {/* Metric cards */}
          <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
            <div className="card text-center">
              <p className="text-2xl font-bold text-slate-900">{metrics.total_invocations.toLocaleString()}</p>
              <p className="text-[10px] text-slate-500 uppercase mt-1">Total Invocations</p>
            </div>
            <div className="card text-center">
              <p className="text-2xl font-bold text-red-600">{metrics.blocked_count.toLocaleString()}</p>
              <p className="text-[10px] text-slate-500 uppercase mt-1">Blocked</p>
            </div>
            <div className="card text-center">
              <p className="text-2xl font-bold text-emerald-600">{metrics.allowed_count.toLocaleString()}</p>
              <p className="text-[10px] text-slate-500 uppercase mt-1">Allowed</p>
            </div>
            <div className="card text-center">
              <p className="text-2xl font-bold text-amber-600">{metrics.block_rate}%</p>
              <p className="text-[10px] text-slate-500 uppercase mt-1">Block Rate</p>
            </div>
          </div>

          {/* Time series chart (simple SVG bars) */}
          {metrics.time_series.length > 0 && (
            <div className="card">
              <h3 className="text-sm font-semibold text-slate-800 mb-4">Invocations Over Time</h3>
              <div className="flex items-end gap-1 h-32">
                {metrics.time_series.map((dp, i) => (
                  <div key={i} className="flex-1 flex flex-col items-center gap-1">
                    <div
                      className="w-full bg-blue-500/80 rounded-t transition-all hover:bg-blue-600"
                      style={{ height: `${(dp.invocations / maxInvocations) * 100}%`, minHeight: dp.invocations > 0 ? '4px' : '0' }}
                      title={`${dp.invocations} invocations`}
                    />
                  </div>
                ))}
              </div>
              <div className="flex justify-between mt-2">
                <span className="text-[9px] text-slate-400">
                  {metrics.time_series[0] ? new Date(metrics.time_series[0].timestamp).toLocaleString([], { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' }) : ''}
                </span>
                <span className="text-[9px] text-slate-400">
                  {metrics.time_series.length > 0 ? new Date(metrics.time_series[metrics.time_series.length - 1].timestamp).toLocaleString([], { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' }) : ''}
                </span>
              </div>
            </div>
          )}

          {/* Recent events */}
          {metrics.recent_events.length > 0 && (
            <div className="card">
              <h3 className="text-sm font-semibold text-slate-800 mb-4">Recent Events</h3>
              <div className="space-y-2">
                {metrics.recent_events.slice(0, 20).map((event, i) => (
                  <div key={i} className="flex items-center gap-3 py-2 border-b border-slate-50 last:border-0">
                    <span className={`w-2 h-2 rounded-full flex-shrink-0 ${
                      event.action === 'BLOCKED' ? 'bg-red-400' : event.action === 'ANONYMIZED' ? 'bg-amber-400' : 'bg-emerald-400'
                    }`} />
                    <span className="text-xs text-slate-500 font-mono w-36 flex-shrink-0">
                      {new Date(event.timestamp).toLocaleString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' })}
                    </span>
                    <span className={`text-xs font-semibold px-2 py-0.5 rounded ${
                      event.action === 'BLOCKED' ? 'bg-red-50 text-red-700' : event.action === 'ANONYMIZED' ? 'bg-amber-50 text-amber-700' : 'bg-emerald-50 text-emerald-700'
                    }`}>
                      {event.action}
                    </span>
                    {event.filter_type && <span className="text-xs text-slate-500">{event.filter_type}</span>}
                    {event.input_snippet && <span className="text-xs text-slate-400 truncate flex-1">{event.input_snippet}</span>}
                  </div>
                ))}
              </div>
            </div>
          )}

          {/* Empty state for metrics */}
          {metrics.total_invocations === 0 && (
            <div className="card text-center py-8">
              <p className="text-sm text-slate-500">No invocations recorded in the selected time window.</p>
              <p className="text-xs text-slate-400 mt-1">Data may be delayed up to 5 minutes.</p>
            </div>
          )}
        </>
      ) : (
        <div className="card text-center py-8">
          <p className="text-sm text-slate-500">Unable to load metrics. The guardrail may not have been invoked yet.</p>
        </div>
      )}
    </div>
  );
}
