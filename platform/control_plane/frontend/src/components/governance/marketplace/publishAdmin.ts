import type { ProductType, PublishRequest, SubscriptionStatus } from '../../../api/client';
import { warnOnce } from '../../operations/warnOnce';

export interface DatasheetRow {
  label: string;
  value: string;
}

export function productKey(productType: ProductType, productId: string): string {
  return `${productType}#${productId}`;
}

function joinList(values: string[] | null | undefined): string {
  if (!values) return '';
  const seen = new Set<string>();
  for (const raw of values) {
    const v = raw.trim();
    if (v) seen.add(v);
  }
  return [...seen].join(', ');
}

export function datasheetRows(req: PublishRequest): DatasheetRow[] {
  const d = req.datasheet;
  const candidates: DatasheetRow[] = [
    { label: 'Owner team', value: (d.owner_team ?? '').trim() },
    { label: 'Support contact', value: (d.support_contact ?? '').trim() },
    { label: 'Data classification', value: (d.data_classification ?? '').trim() },
    { label: 'SLA tier', value: (d.sla_tier ?? '').trim() },
    { label: 'Compliance', value: joinList(d.compliance) },
    { label: 'Support hours', value: (d.support_hours ?? '').trim() },
    { label: 'Version', value: (d.version ?? '').trim() },
    { label: 'Region', value: (d.region ?? '').trim() },
    { label: 'Guardrails', value: joinList(d.guardrails) },
    { label: 'Pitch', value: (d.pitch ?? '').trim() },
  ];
  return candidates.filter((row) => row.value !== '');
}

export function publishBadge(req: PublishRequest): { cls: string; label: string } {
  if (req.status === 'approved') return { cls: 'bg-emerald-50 text-emerald-700', label: 'Approved' };
  if (req.status === 'rejected') return { cls: 'bg-red-50 text-red-700', label: 'Rejected' };
  if ((req.error ?? '').trim()) return { cls: 'bg-red-50 text-red-700', label: 'Failed' };
  return { cls: 'bg-amber-50 text-amber-700', label: 'Pending' };
}

export interface PublishQueueCounts {
  total: number;
  pending: number;
  failed: number;
  approved: number;
  rejected: number;
}

export function publishQueueCounts(reqs: PublishRequest[]): PublishQueueCounts {
  const counts: PublishQueueCounts = { total: reqs.length, pending: 0, failed: 0, approved: 0, rejected: 0 };
  for (const r of reqs) {
    const label = publishBadge(r).label;
    if (label === 'Failed') counts.failed += 1;
    else if (label === 'Approved') counts.approved += 1;
    else if (label === 'Rejected') counts.rejected += 1;
    else counts.pending += 1;
  }
  return counts;
}

const STATUS_BADGE: Record<SubscriptionStatus, { cls: string; label: string }> = {
  pending: { cls: 'bg-amber-50 text-amber-700', label: 'Pending' },
  approved: { cls: 'bg-emerald-50 text-emerald-700', label: 'Approved' },
  rejected: { cls: 'bg-red-50 text-red-700', label: 'Rejected' },
  failed: { cls: 'bg-red-50 text-red-700', label: 'Failed' },
  revoked: { cls: 'bg-slate-100 text-slate-600', label: 'Revoked' },
};

export function subscriptionStatusBadge(status: SubscriptionStatus): { cls: string; label: string } {
  if (Object.prototype.hasOwnProperty.call(STATUS_BADGE, status)) return STATUS_BADGE[status];
  warnOnce('[publishAdmin] subscription status this frontend does not know; shown as sent:', status);
  return { cls: 'bg-slate-100 text-slate-500', label: status };
}

export function productTypeBadge(req: PublishRequest): { cls: string; label: string } {
  return {
    cls: 'bg-slate-100 text-slate-600',
    label: req.product_type === 'mcp' ? 'MCP' : 'Agent',
  };
}

export function unpublishStateFor(
  req: PublishRequest,
  publishedKeys: Set<string>,
): 'unpublish' | 'unpublished' | 'none' {
  if (publishedKeys.has(productKey(req.product_type, req.product_id))) return 'unpublish';
  return req.status === 'approved' ? 'unpublished' : 'none';
}
