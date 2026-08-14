// marketplaceForm — pure helpers for the Marketplace pages (Epic 9, T7).
//
// Kept out of the page components so the CTA state machine, the subscribe POST
// body shaping, the eligible-agent filter and the declared-datasheet chip
// derivation (E33/T6) are unit-testable in vitest (the pages themselves are
// build-gated; only src/**/*.test.ts is picked up by the runner). No React, no
// axios — pure TS.

import type { Agent, ProductCard } from '../../../api/client';

// CTA state machine for a product card given the caller's status.
export type CardCta = 'subscribe' | 'pending' | 'approved' | 'deploy' | 'subscribed';
export function ctaFor(card: ProductCard): CardCta {
  if (!card.my_status) return 'subscribe';
  if (card.my_status === 'pending') return 'pending';
  if (card.my_status === 'approved') return card.product_type === 'agent' ? 'deploy' : 'subscribed';
  return 'subscribe'; // rejected/failed → can re-request
}

// Build the POST body. For MCP, agentId is required (caller-enforced before submit).
export function buildSubscribeBody(card: ProductCard, opts: { agentId?: string; message?: string }) {
  return {
    product_type: card.product_type,
    product_id: card.product_id,
    agent_id: card.product_type === 'mcp' ? (opts.agentId ?? null) : null,
    message: opts.message ?? null,
  };
}

// relativeTime — a tiny pure "Nd ago" formatter for the card's "Updated …" line
// (F3). `now` is injectable so the function is deterministic in vitest; it
// defaults to the wall clock in the UI. Invalid / empty input returns '' so the
// caller can omit the line entirely (no "—" placeholder). Future timestamps
// clamp to "just now" rather than rendering a negative age.
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

// declaredList — normalise a declared string list (compliance, guardrails) for
// rendering (E33/T6). The values come from a publisher-typed datasheet, so they can
// arrive padded, blank or repeated; every one of those renders badly (a chip of
// whitespace) or dangerously (duplicate React keys on a .map). Trims, drops empties
// and de-duplicates while preserving the declared order, so the caller can map
// straight over the result. A null/absent list yields [] — the caller omits the
// whole block rather than showing a "—" placeholder.
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

// A declared-datasheet chip for the agent card's trust strip.
export interface DatasheetChip {
  /** Stable React key. */
  key: string;
  /** Visible chip text. */
  label: string;
  /** Tooltip — always says the value is DECLARED, never measured. */
  title: string;
}

// datasheetChips — the agent card's declared-datasheet chip row (E33/T6): the SLA
// tier the publisher committed to, then each compliance framework they claim. Both
// are publisher DECLARATIONS approved by an admin (that is what makes them showable
// again after E31F removed the invented ones), so every tooltip names them as such.
// Absent/blank fields produce no chip at all — the omit-empty card idiom.
//
// The SLA chip carries a VISIBLE "SLA:" prefix, not just a tooltip: it shares the one
// neutral chip tint with the compliance chips, so without it a reader cannot tell a
// service-level commitment from a framework claim without hovering — and hover does
// not exist on touch and is unreliable for assistive tech. The row's shared "declared"
// qualifier is rendered on-screen by the card next to these chips for the same reason;
// the tooltips are the long-form restatement, never the only carrier of the disclosure.
export function datasheetChips(card: ProductCard): DatasheetChip[] {
  const chips: DatasheetChip[] = [];
  const sla = (card.sla_tier ?? '').trim();
  if (sla) chips.push({ key: 'sla', label: `SLA: ${sla}`, title: 'SLA tier (declared by the publisher)' });
  for (const c of declaredList(card.compliance)) {
    chips.push({ key: `compliance:${c}`, label: c, title: 'Compliance (declared by the publisher)' });
  }
  return chips;
}

// Which agents are eligible for the MCP picker (provisioned + the caller relates to).
// callerOid is part of the contract but unused here: the relationship filter
// (sponsor_oid===callerOid OR caller ∈ grants) is applied in the page where grants
// are fetched. Prefixed with _ to satisfy tsc's noUnusedParameters.
export function eligibleAgents(agents: Agent[], _callerOid: string | null): Agent[] {
  return agents.filter((a) => a.identity_status === 'provisioned' && !!a.entra_sp_id);
}
