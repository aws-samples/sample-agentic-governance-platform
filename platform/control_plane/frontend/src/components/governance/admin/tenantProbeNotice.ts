import type { TenantView, Verb } from '../../../api/client';
import { VERB_LABEL } from '../platformLabels';

export type ProbeOutcome =
  | { ok: true; tenant: Pick<TenantView, 'readiness'> }
  | { ok: false; message: string };

export function probeTime(at: Date): string {
  const pad = (n: number) => String(n).padStart(2, '0');
  return `${pad(at.getHours())}:${pad(at.getMinutes())}`;
}

export function probeNotice(at: Date, outcome: ProbeOutcome): string {
  const prefix = `Probed ${probeTime(at)} — `;
  if (!outcome.ok) return `${prefix}failed: ${outcome.message}`;
  const readiness = outcome.tenant.readiness;
  const verbs = (Object.keys(VERB_LABEL) as Verb[]).filter((v) => readiness[v] !== undefined);
  if (verbs.length === 0) return `${prefix}no readiness reported`;
  const unavailable = verbs.filter((v) => {
    const s = readiness[v]!.state;
    return s !== 'ready' && s !== 'not_applicable';
  });
  if (unavailable.length === 0) return `${prefix}all ready`;
  const names = unavailable.map((v) => {
    const stage = readiness[v]!.stage;
    return stage ? `${VERB_LABEL[v]} (${stage})` : VERB_LABEL[v];
  });
  return `${prefix}unavailable: ${names.join(', ')}`;
}
