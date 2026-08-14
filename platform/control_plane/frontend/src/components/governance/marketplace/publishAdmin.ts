// publishAdmin — pure view logic for the MarketplaceAdmin publish-requests queue (Epic 33, T8).
//
// Lives beside the component because the frontend test harness is vitest in a NODE
// environment (no DOM render): anything worth testing has to be a plain .ts module.
//
// Everything a datasheet declares is publisher-ASSERTED, never measured. The rendering
// rule is therefore "omit what was not declared": a field the publisher left blank
// produces NO row at all, rather than a "—" that would read like a measured absence.

import type { MarketplaceProductType, MarketplacePublishRequest } from '../../../api/client';

export interface DatasheetRow {
  label: string;
  value: string;
}

// The identity of a marketplace product is the (product_type, product_id) PAIR, never the id
// alone: agents and MCP servers live in two registries with independent id spaces, so the same
// id can legitimately name one of each. ONE key builder, used both to BUILD the live-listing set
// and to probe it, so the two sides cannot drift into a mismatch that would offer Unpublish on
// the wrong product.
export function productKey(productType: MarketplaceProductType, productId: string): string {
  return `${productType}#${productId}`;
}

// Trim + drop blanks + de-duplicate, then join. Duplicates are dropped because a datasheet
// list is a set of declarations ("GDPR" twice says nothing twice).
function joinList(values: string[] | null | undefined): string {
  if (!values) return '';
  const seen = new Set<string>();
  for (const raw of values) {
    const v = raw.trim();
    if (v) seen.add(v);
  }
  return [...seen].join(', ');
}

// The declared datasheet as ordered label/value rows, in the order the backend model
// declares its fields (mandatory three first, then the optionals). Blank values are
// omitted — including the mandatory three, defensively: the backend guarantees they are
// non-empty, but an empty row would render as a dangling label.
export function datasheetRows(req: MarketplacePublishRequest): DatasheetRow[] {
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

// Status badge for a publish request. Same semantic tints as the subscriptions table
// (emerald=approved, amber=pending, red=rejected/failed); local because the request
// status enum is its own three-value enum, not MarketplaceStatus.
//
// The FAILED-ish state is not a status: when an approve fails to write the agent
// envelope the backend keeps the request PENDING and persists a safe `error` literal, so
// the row stays actionable (Approve again = retry). We surface that as "Failed" so the
// admin sees it needs another attempt, while the actions keep treating it as pending.
export function publishBadge(req: MarketplacePublishRequest): { cls: string; label: string } {
  if (req.status === 'approved') return { cls: 'bg-emerald-50 text-emerald-700', label: 'Approved' };
  if (req.status === 'rejected') return { cls: 'bg-red-50 text-red-700', label: 'Rejected' };
  if ((req.error ?? '').trim()) return { cls: 'bg-red-50 text-red-700', label: 'Failed' };
  return { cls: 'bg-amber-50 text-amber-700', label: 'Pending' };
}

// Which registry the queued product lives in (Amendment 1: publish is the only door for BOTH
// product types, so the queue mixes them). Neutral slate on purpose — a product type is a
// classification, not a state, so it must not compete with the status badge beside it. The label
// is explicit rather than a CSS `capitalize`, which would render "mcp" as "Mcp".
export function productTypeBadge(req: MarketplacePublishRequest): { cls: string; label: string } {
  return {
    cls: 'bg-slate-100 text-slate-600',
    label: req.product_type === 'mcp' ? 'MCP' : 'Agent',
  };
}

// Whether a queue row may offer Unpublish, given the live set of listed product KEYS
// (`listAgentProducts` + `listMcpProducts` return published products only, so membership IS
// "listed right now"). Keys are pairs — see productKey: an id alone would let an unpublished
// agent inherit the Unpublish affordance of a published MCP that happens to share its id.
//
// The decisive input is that set, NOT the request status. There is ONE publish record per
// product, so a re-publish OVERWRITES the approved record back to PENDING — and a REJECTED
// re-publish leaves a rejected record — while the earlier approval keeps the product live in
// the catalog. Gating on `status === 'approved'` would therefore strand a live listing with
// no way to delist it.
//
// 'unpublished' is the muted label for the mirror case: an approved request whose product is
// no longer listed (already delisted). Anything else has no unpublish affordance at all.
export function unpublishStateFor(
  req: MarketplacePublishRequest,
  publishedKeys: Set<string>,
): 'unpublish' | 'unpublished' | 'none' {
  if (publishedKeys.has(productKey(req.product_type, req.product_id))) return 'unpublish';
  return req.status === 'approved' ? 'unpublished' : 'none';
}
