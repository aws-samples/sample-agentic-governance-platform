import { useEffect, useState } from 'react';
import { observabilityApi } from '../../api/client';
import type { Agent } from '../../api/client';
import {
  AUTO_DELIVERY_NOTE,
  AUTO_NO_PROJECT_NOTE,
  FIELD_LABELS,
  MANUAL_DELIVERY_INTRO,
  MANUAL_NO_PROJECT_NOTE,
  OBSERVABILITY_HEADING,
  autoDeliveryHasProject,
  observabilityDelivery,
} from './observabilityDelivery';

const CARD = 'bg-white/70 backdrop-blur rounded-xl border border-slate-200/60 shadow-sm';

function WiringRow({ label, value }: { label: string; value: string }) {
  return (
    <div>
      <dt className="text-[11px] uppercase tracking-wide text-slate-400 font-medium">{label}</dt>
      <dd className="mt-0.5 text-sm font-mono text-slate-700 break-all">{value}</dd>
    </div>
  );
}

export default function ObservabilityPanel({ agent }: { agent: Agent }) {
  const delivery = observabilityDelivery(agent.platform, agent.trace_delivery);
  const [langfuseHost, setLangfuseHost] = useState<string | null>(null);
  useEffect(() => {
    if (delivery !== 'manual') return;
    let cancelled = false;
    observabilityApi
      .getSettings()
      .then((s) => {
        if (!cancelled) setLangfuseHost(s.langfuse_host);
      })
      .catch(() => {});
    return () => {
      cancelled = true;
    };
  }, [delivery]);

  if (delivery === 'auto') {
    if (!autoDeliveryHasProject(agent.langfuse_project_id)) {
      return (
        <div className={`${CARD} p-5`}>
          <h2 className="text-sm font-semibold text-slate-800 mb-1">{OBSERVABILITY_HEADING}</h2>
          <p className="text-sm text-slate-700">{AUTO_NO_PROJECT_NOTE}</p>
        </div>
      );
    }
    return (
      <div className={`${CARD} p-5`}>
        <h2 className="text-sm font-semibold text-slate-800 mb-1">{OBSERVABILITY_HEADING}</h2>
        <div className="flex items-start gap-2">
          <span aria-hidden="true" className="mt-1.5 h-1.5 w-1.5 shrink-0 rounded-full bg-emerald-500" />
          <p className="text-sm text-slate-700">{AUTO_DELIVERY_NOTE}</p>
        </div>
      </div>
    );
  }

  const projectId = agent.langfuse_project_id ?? null;
  const secretName = agent.langfuse_key_secret_name ?? null;

  return (
    <div className={`${CARD} p-5`}>
      <h2 className="text-sm font-semibold text-slate-800 mb-1">{OBSERVABILITY_HEADING}</h2>
      {projectId ? (
        <>
          <p className="text-sm text-slate-700">{MANUAL_DELIVERY_INTRO}</p>
          <dl className="mt-4 space-y-3">
            {langfuseHost && <WiringRow label={FIELD_LABELS.host} value={langfuseHost} />}
            <WiringRow label={FIELD_LABELS.project} value={projectId} />
            {secretName && <WiringRow label={FIELD_LABELS.secret} value={secretName} />}
          </dl>
        </>
      ) : (
        <p className="text-sm text-slate-700">{MANUAL_NO_PROJECT_NOTE}</p>
      )}
    </div>
  );
}
