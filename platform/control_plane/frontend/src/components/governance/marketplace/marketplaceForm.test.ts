import { describe, it, expect } from 'vitest';
import {
  ctaFor,
  buildSubscribeBody,
  datasheetChips,
  declaredList,
  eligibleAgents,
  relativeTime,
} from './marketplaceForm';
import type { Agent, ProductCard } from '../../../api/client';

const card = (over: Partial<ProductCard> = {}): ProductCard => ({
  product_type: 'agent',
  product_id: 'bp-fnol',
  name: 'FNOL Agent',
  capabilities: [],
  available: true,
  auto_approve: false,
  my_status: null,
  ...over,
});

const agent = (over: Partial<Agent> = {}): Agent => ({
  id: 'agent-1',
  name: 'Contact Center Agent',
  mcp_server_ids: [],
  origin: 'Registered',
  identity_status: 'provisioned',
  entra_sp_id: 'sp-a',
  lifecycle_state: 'approved',
  auth_type: 'entra',
  created_at: '2026-06-07T00:00:00Z',
  updated_at: '2026-06-07T00:00:00Z',
  ...over,
});

describe('ctaFor', () => {
  it('no status → subscribe', () => {
    expect(ctaFor(card({ my_status: null }))).toBe('subscribe');
  });

  it('pending → pending', () => {
    expect(ctaFor(card({ my_status: 'pending' }))).toBe('pending');
  });

  it('agent + approved → deploy', () => {
    expect(ctaFor(card({ product_type: 'agent', my_status: 'approved' }))).toBe('deploy');
  });

  it('mcp + approved → subscribed', () => {
    expect(ctaFor(card({ product_type: 'mcp', my_status: 'approved' }))).toBe('subscribed');
  });

  it('rejected → subscribe (can re-request)', () => {
    expect(ctaFor(card({ my_status: 'rejected' }))).toBe('subscribe');
  });

  it('failed → subscribe (can re-request)', () => {
    expect(ctaFor(card({ my_status: 'failed' }))).toBe('subscribe');
  });

  it('revoked → subscribe (can re-subscribe after an admin revoke)', () => {
    expect(ctaFor(card({ my_status: 'revoked' }))).toBe('subscribe');
  });
});

describe('buildSubscribeBody', () => {
  it('agent card sends agent_id null', () => {
    const body = buildSubscribeBody(card({ product_type: 'agent', product_id: 'bp-fnol' }), {
      agentId: 'agent-1',
      message: 'please',
    });
    expect(body).toEqual({
      product_type: 'agent',
      product_id: 'bp-fnol',
      agent_id: null,
      message: 'please',
    });
  });

  it('mcp card sends the passed agentId and message', () => {
    const body = buildSubscribeBody(card({ product_type: 'mcp', product_id: 'm1' }), {
      agentId: 'agent-9',
      message: 'on behalf of',
    });
    expect(body).toEqual({
      product_type: 'mcp',
      product_id: 'm1',
      agent_id: 'agent-9',
      message: 'on behalf of',
    });
  });

  it('mcp card without agentId sends agent_id null; missing message → null', () => {
    const body = buildSubscribeBody(card({ product_type: 'mcp', product_id: 'm1' }), {});
    expect(body.agent_id).toBeNull();
    expect(body.message).toBeNull();
  });
});

describe('eligibleAgents', () => {
  it('keeps only provisioned agents with an entra_sp_id', () => {
    const provisioned = agent({ id: 'a1', identity_status: 'provisioned', entra_sp_id: 'sp-a' });
    const notProvisioned = agent({ id: 'a2', identity_status: 'pending', entra_sp_id: null });
    const noSpId = agent({ id: 'a3', identity_status: 'provisioned', entra_sp_id: null });

    const result = eligibleAgents([provisioned, notProvisioned, noSpId], 'caller-oid');
    expect(result.map((a) => a.id)).toEqual(['a1']);
  });
});

describe('declaredList', () => {
  it('absent / empty list → []', () => {
    expect(declaredList(null)).toEqual([]);
    expect(declaredList(undefined)).toEqual([]);
    expect(declaredList([])).toEqual([]);
  });

  it('trims, drops blanks and keeps the declared order', () => {
    expect(declaredList([' GDPR ', '', '   ', 'SOC 2'])).toEqual(['GDPR', 'SOC 2']);
  });

  it('de-duplicates (duplicate chips would collide on their React key)', () => {
    expect(declaredList(['GDPR', 'GDPR ', 'BaFin'])).toEqual(['GDPR', 'BaFin']);
  });
});

describe('datasheetChips', () => {
  it('a card with no declared sla/compliance → no chips (omit-empty, no "—")', () => {
    expect(datasheetChips(card())).toEqual([]);
    expect(datasheetChips(card({ sla_tier: '  ', compliance: [] }))).toEqual([]);
  });

  it('sla tier first, then one chip per compliance framework', () => {
    const chips = datasheetChips(card({ sla_tier: 'Gold', compliance: ['GDPR', 'SOC 2'] }));
    expect(chips.map((c) => c.label)).toEqual(['SLA: Gold', 'GDPR', 'SOC 2']);
    expect(chips.map((c) => c.key)).toEqual(['sla', 'compliance:GDPR', 'compliance:SOC 2']);
  });

  it('the sla chip label visibly says it is an SLA, not only its tooltip', () => {
    // The chips share one neutral tint, and `title` is unavailable on touch and
    // unreliable for assistive tech — so the distinction must be in the label.
    const chips = datasheetChips(card({ sla_tier: 'Gold', compliance: ['Gold'] }));
    expect(chips[0].label).toBe('SLA: Gold');
    expect(chips[1].label).toBe('Gold');
  });

  it('every chip tooltip discloses the value as declared, never measured', () => {
    const chips = datasheetChips(card({ sla_tier: 'Gold', compliance: ['GDPR'] }));
    expect(chips.every((c) => c.title.includes('declared'))).toBe(true);
  });

  it('compliance chips inherit declaredList normalisation (trim/blank/dupe)', () => {
    const chips = datasheetChips(card({ compliance: [' GDPR', 'GDPR', '', 'BaFin'] }));
    expect(chips.map((c) => c.label)).toEqual(['GDPR', 'BaFin']);
  });

  it('sla tier alone renders without any compliance chips', () => {
    const chips = datasheetChips(card({ sla_tier: 'Silver', compliance: null }));
    expect(chips).toEqual([
      { key: 'sla', label: 'SLA: Silver', title: 'SLA tier (declared by the publisher)' },
    ]);
  });

  // Amendment 1: publication is the only door for MCP servers too, so an MCP card carries a
  // declared datasheet exactly like an agent card and MarketplaceCard renders these chips for
  // both. The derivation must therefore be product-type AGNOSTIC — it was the CARD that used to
  // gate the row on `product_type === 'mcp'`, throwing an approved declaration away.
  const mcpCard = (over: Partial<ProductCard> = {}): ProductCard =>
    card({ product_type: 'mcp', product_id: 'mcp-1', name: 'Claims MCP', kind: 'gateway', ...over });

  it('an MCP card yields the SAME chips as an agent card with the same declaration', () => {
    const declaration = { sla_tier: 'Gold', compliance: ['GDPR', 'SOC 2'] };
    expect(datasheetChips(mcpCard(declaration))).toEqual(datasheetChips(card(declaration)));
    expect(datasheetChips(mcpCard(declaration)).map((c) => c.label)).toEqual([
      'SLA: Gold',
      'GDPR',
      'SOC 2',
    ]);
  });

  it('an MCP card with NO declared sla/compliance yields [] — no chip row, no dangling "Declared" label', () => {
    // The card renders the row (and its visible "Declared" prefix) only when this list is
    // non-empty, so [] is what keeps an undeclared card free of an empty labelled row.
    expect(datasheetChips(mcpCard())).toEqual([]);
    expect(datasheetChips(mcpCard({ sla_tier: '   ', compliance: [] }))).toEqual([]);
    expect(datasheetChips(mcpCard({ sla_tier: null, compliance: null }))).toEqual([]);
  });

  it('declared fields the row does not own never invent a chip (owner_team / support_contact / declared_at / guardrails)', () => {
    // These are projected onto both card types but belong to the header (owner_team) or the
    // detail views — surfacing them here would produce a "Declared" row asserting more than
    // the row is meant to carry.
    expect(
      datasheetChips(
        mcpCard({
          owner_team: 'Claims Platform',
          support_contact: 'claims@example.com',
          declared_at: '2026-08-11T09:00:00Z',
          guardrails: ['PII redaction'],
          support_hours: '9x5 CET',
        }),
      ),
    ).toEqual([]);
  });
});

describe('relativeTime', () => {
  // `now` is injected so the helper is deterministic in tests.
  const now = new Date('2026-06-08T12:00:00Z');

  it('under a minute → "just now"', () => {
    expect(relativeTime('2026-06-08T11:59:30Z', now)).toBe('just now');
    expect(relativeTime('2026-06-08T12:00:00Z', now)).toBe('just now');
  });

  it('minutes → "Nm ago"', () => {
    expect(relativeTime('2026-06-08T11:55:00Z', now)).toBe('5m ago');
    expect(relativeTime('2026-06-08T11:01:00Z', now)).toBe('59m ago');
  });

  it('hours → "Nh ago"', () => {
    expect(relativeTime('2026-06-08T10:00:00Z', now)).toBe('2h ago');
    expect(relativeTime('2026-06-07T13:00:00Z', now)).toBe('23h ago');
  });

  it('days → "Nd ago"', () => {
    expect(relativeTime('2026-06-05T12:00:00Z', now)).toBe('3d ago');
    expect(relativeTime('2026-05-10T12:00:00Z', now)).toBe('29d ago');
  });

  it('months → "Nmo ago", years → "Ny ago"', () => {
    expect(relativeTime('2026-04-08T12:00:00Z', now)).toBe('2mo ago');
    expect(relativeTime('2024-06-08T12:00:00Z', now)).toBe('2y ago');
  });

  it('empty / invalid input → empty string (caller omits the line)', () => {
    expect(relativeTime('', now)).toBe('');
    expect(relativeTime('not-a-date', now)).toBe('');
    expect(relativeTime(null, now)).toBe('');
  });

  it('future timestamps clamp to "just now" (no negative ages)', () => {
    expect(relativeTime('2026-06-08T12:05:00Z', now)).toBe('just now');
  });
});
