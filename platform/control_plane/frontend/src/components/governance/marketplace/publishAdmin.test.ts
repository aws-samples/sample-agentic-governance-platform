import { describe, it, expect } from 'vitest';
import {
  datasheetRows,
  productKey,
  productTypeBadge,
  publishBadge,
  unpublishStateFor,
} from './publishAdmin';
import type { MarketplacePublishRequest, PublishDatasheet } from '../../../api/client';

const datasheet = (over: Partial<PublishDatasheet> = {}): PublishDatasheet => ({
  owner_team: 'Claims Platform',
  support_contact: 'claims-support@example.com',
  data_classification: 'Internal',
  ...over,
});

const req = (over: Partial<MarketplacePublishRequest> = {}): MarketplacePublishRequest => ({
  id: 'pub-abc1234567',
  product_type: 'agent',
  product_id: 'agent-1',
  product_name: 'FNOL Agent',
  tenant_id: 'tenant-a',
  datasheet: datasheet(),
  status: 'pending',
  requested_by: 'oid-publisher',
  requested_by_email: 'publisher@example.com',
  created_at: '2026-08-10T09:00:00Z',
  updated_at: '2026-08-10T09:00:00Z',
  ...over,
});

describe('datasheetRows', () => {
  it('mandatory-only datasheet → exactly the three mandatory rows, in declaration order', () => {
    expect(datasheetRows(req())).toEqual([
      { label: 'Owner team', value: 'Claims Platform' },
      { label: 'Support contact', value: 'claims-support@example.com' },
      { label: 'Data classification', value: 'Internal' },
    ]);
  });

  it('a full datasheet renders every field in the model declaration order', () => {
    const rows = datasheetRows(
      req({
        datasheet: datasheet({
          sla_tier: 'Gold',
          compliance: ['GDPR', 'SOC 2'],
          support_hours: '9x5 CET',
          version: '2.1.0',
          region: 'eu-central-1',
          guardrails: ['PII redaction'],
          pitch: 'Automates first notice of loss.',
        }),
      }),
    );
    expect(rows.map((r) => r.label)).toEqual([
      'Owner team',
      'Support contact',
      'Data classification',
      'SLA tier',
      'Compliance',
      'Support hours',
      'Version',
      'Region',
      'Guardrails',
      'Pitch',
    ]);
    expect(rows.find((r) => r.label === 'Version')?.value).toBe('2.1.0');
    expect(rows.find((r) => r.label === 'Pitch')?.value).toBe('Automates first notice of loss.');
  });

  it('omits empty optionals entirely — never emits a "—" placeholder', () => {
    const rows = datasheetRows(
      req({
        datasheet: datasheet({
          sla_tier: '',
          support_hours: '   ',
          version: null,
          region: undefined,
          pitch: null,
          compliance: [],
          guardrails: undefined,
        }),
      }),
    );
    expect(rows.map((r) => r.label)).toEqual([
      'Owner team',
      'Support contact',
      'Data classification',
    ]);
    expect(rows.some((r) => r.value === '—' || r.value === '')).toBe(false);
  });

  it('omits a blank MANDATORY field too (defensive — the row would render empty)', () => {
    const rows = datasheetRows(req({ datasheet: datasheet({ owner_team: '  ' }) }));
    expect(rows.map((r) => r.label)).toEqual(['Support contact', 'Data classification']);
  });

  it('joins list fields readably, trimming blanks and de-duplicating', () => {
    const rows = datasheetRows(
      req({
        datasheet: datasheet({
          compliance: [' GDPR ', '', 'GDPR', 'SOC 2'],
          guardrails: ['PII redaction', '   ', 'Prompt-injection filter'],
        }),
      }),
    );
    expect(rows.find((r) => r.label === 'Compliance')?.value).toBe('GDPR, SOC 2');
    expect(rows.find((r) => r.label === 'Guardrails')?.value).toBe(
      'PII redaction, Prompt-injection filter',
    );
  });

  it('a list whose entries are all blank is omitted like any empty optional', () => {
    const rows = datasheetRows(req({ datasheet: datasheet({ compliance: ['  ', ''] }) }));
    expect(rows.some((r) => r.label === 'Compliance')).toBe(false);
  });

  it('trims scalar values', () => {
    const rows = datasheetRows(req({ datasheet: datasheet({ version: '  1.0  ' }) }));
    expect(rows.find((r) => r.label === 'Version')?.value).toBe('1.0');
  });
});

describe('publishBadge', () => {
  it('pending → Pending (amber)', () => {
    const badge = publishBadge(req({ status: 'pending' }));
    expect(badge.label).toBe('Pending');
    expect(badge.cls).toContain('amber');
  });

  it('pending WITH a persisted error → Failed, because the approve write failed', () => {
    const badge = publishBadge(
      req({ status: 'pending', error: 'failed to write the marketplace block' }),
    );
    expect(badge.label).toBe('Failed');
    expect(badge.cls).toContain('red');
  });

  it('a blank error string does not flip the badge to Failed', () => {
    expect(publishBadge(req({ status: 'pending', error: '   ' })).label).toBe('Pending');
    expect(publishBadge(req({ status: 'pending', error: null })).label).toBe('Pending');
  });

  it('approved → Approved (emerald), rejected → Rejected (red)', () => {
    const approved = publishBadge(req({ status: 'approved' }));
    expect(approved.label).toBe('Approved');
    expect(approved.cls).toContain('emerald');
    const rejected = publishBadge(req({ status: 'rejected' }));
    expect(rejected.label).toBe('Rejected');
    expect(rejected.cls).toContain('red');
  });

  it('a stale error on a decided request does not override its status badge', () => {
    expect(publishBadge(req({ status: 'approved', error: 'old failure' })).label).toBe('Approved');
  });
});

describe('productTypeBadge', () => {
  it('agent → "Agent"; mcp → "MCP", not the CSS-capitalized "Mcp"', () => {
    expect(productTypeBadge(req({ product_type: 'agent' })).label).toBe('Agent');
    expect(productTypeBadge(req({ product_type: 'mcp' })).label).toBe('MCP');
  });

  it('is a neutral classification tint — never a status colour', () => {
    for (const badge of [
      productTypeBadge(req({ product_type: 'agent' })),
      productTypeBadge(req({ product_type: 'mcp' })),
    ]) {
      expect(badge.cls).toContain('slate');
      expect(badge.cls).not.toMatch(/amber|emerald|red/);
    }
  });
});

describe('productKey', () => {
  it('the two id spaces never collide on a shared id', () => {
    expect(productKey('agent', 'x-1')).not.toBe(productKey('mcp', 'x-1'));
  });

  it('is stable for the same pair', () => {
    expect(productKey('mcp', 'mcp-1')).toBe(productKey('mcp', 'mcp-1'));
  });
});

describe('unpublishStateFor', () => {
  // `publishedKeys` is the live set of (product_type, product_id) keys currently listed as
  // products — built from listAgentProducts + listMcpProducts through the same productKey.
  const published = (...pairs: ['agent' | 'mcp', string][]) =>
    new Set(pairs.map(([type, id]) => productKey(type, id)));

  it('an approved request whose product is live → the Unpublish action', () => {
    expect(unpublishStateFor(req({ status: 'approved' }), published(['agent', 'agent-1']))).toBe(
      'unpublish',
    );
  });

  it('a live listing whose record was OVERWRITTEN by a new pending re-publish still offers Unpublish', () => {
    // One publish record per product: a re-publish overwrites it back to PENDING while the
    // previous approval keeps the product live. Gating on status alone would strand it.
    expect(unpublishStateFor(req({ status: 'pending' }), published(['agent', 'agent-1']))).toBe(
      'unpublish',
    );
  });

  it('a live listing whose re-publish was REJECTED still offers Unpublish', () => {
    expect(unpublishStateFor(req({ status: 'rejected' }), published(['agent', 'agent-1']))).toBe(
      'unpublish',
    );
  });

  it('an approved request whose product is no longer listed → the muted Unpublished label', () => {
    expect(unpublishStateFor(req({ status: 'approved' }), published())).toBe('unpublished');
  });

  it('a pending / rejected request on a NOT-listed product → no unpublish affordance', () => {
    expect(unpublishStateFor(req({ status: 'pending' }), published())).toBe('none');
    expect(unpublishStateFor(req({ status: 'rejected' }), published())).toBe('none');
  });

  it('membership is keyed on the product, not the request id', () => {
    expect(unpublishStateFor(req({ status: 'approved' }), published(['agent', 'pub-abc1234567']))).toBe(
      'unpublished',
    );
  });

  it('an MCP request reads the MCP half of the set', () => {
    const mcpReq = req({ status: 'approved', product_type: 'mcp', product_id: 'mcp-1' });
    expect(unpublishStateFor(mcpReq, published(['mcp', 'mcp-1']))).toBe('unpublish');
    expect(unpublishStateFor(mcpReq, published())).toBe('unpublished');
  });

  it('the PAIR decides: a shared id in the OTHER registry never lights up Unpublish', () => {
    // An agent and an MCP may carry the same id (two registries, two id spaces). Keying on the
    // id alone would let a published MCP hand its Unpublish button to an unlisted agent.
    expect(unpublishStateFor(req({ status: 'approved' }), published(['mcp', 'agent-1']))).toBe(
      'unpublished',
    );
    expect(
      unpublishStateFor(
        req({ status: 'pending', product_type: 'mcp', product_id: 'shared-1' }),
        published(['agent', 'shared-1']),
      ),
    ).toBe('none');
  });
});
