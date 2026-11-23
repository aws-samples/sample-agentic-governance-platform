import type { ProductCard } from '../../../api/client';

export type CardCta = 'subscribe' | 'pending' | 'approved' | 'deploy' | 'subscribed';
export function ctaFor(card: ProductCard): CardCta {
  if (!card.my_status) return 'subscribe';
  if (card.my_status === 'pending') return 'pending';
  if (card.my_status === 'approved') return card.product_type === 'agent' ? 'deploy' : 'subscribed';
  return 'subscribe';
}

export function buildSubscribeBody(card: ProductCard, opts: { agentId?: string; message?: string }) {
  return {
    product_type: card.product_type,
    product_id: card.product_id,
    agent_id: card.product_type === 'mcp' ? (opts.agentId ?? null) : null,
    message: opts.message ?? null,
  };
}

export function relativeTime(iso: string | null | undefined, now: Date = new Date()): string {
  if (!iso) return '';
  const then = new Date(iso);
  const t = then.getTime();
  if (Number.isNaN(t)) return '';

  const secs = Math.max(0, Math.floor((now.getTime() - t) / 1000));
  if (secs < 60) return 'just now';
  const mins = Math.floor(secs / 60);
  if (mins < 60) return `${mins}m ago`;
  const hours = Math.floor(mins / 60);
  if (hours < 24) return `${hours}h ago`;
  const days = Math.floor(hours / 24);
  if (days < 30) return `${days}d ago`;
  const months = Math.floor(days / 30);
  if (months < 12) return `${months}mo ago`;
  const years = Math.floor(days / 365);
  return `${years}y ago`;
}

export function declaredList(values: string[] | null | undefined): string[] {
  if (!values) return [];
  const out: string[] = [];
  for (const raw of values) {
    const v = (raw ?? '').trim();
    if (!v || out.includes(v)) continue;
    out.push(v);
  }
  return out;
}

export interface DatasheetChip {
  key: string;
  label: string;
  title: string;
}

export function datasheetChips(card: ProductCard): DatasheetChip[] {
  const chips: DatasheetChip[] = [];
  const sla = (card.sla_tier ?? '').trim();
  if (sla) chips.push({ key: 'sla', label: `SLA: ${sla}`, title: 'SLA tier (declared by the publisher)' });
  for (const c of declaredList(card.compliance)) {
    chips.push({ key: `compliance:${c}`, label: c, title: 'Compliance (declared by the publisher)' });
  }
  return chips;
}
