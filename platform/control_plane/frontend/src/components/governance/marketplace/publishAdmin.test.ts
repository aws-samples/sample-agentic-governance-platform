import { afterEach, describe, it, expect, vi } from 'vitest';
import {
  datasheetRows,
  productKey,
  productTypeBadge,
  publishBadge,
  publishQueueCounts,
  subscriptionStatusBadge,
  unpublishStateFor,
} from './publishAdmin';
import { datasheet, publishRequest } from '../../../test/wire';

describe('datasheetRows', () => {
  it('mandatory-only datasheet → exactly the three mandatory rows, in declaration order', () => {
    expect(datasheetRows(publishRequest())).toEqual([
      { label: 'Owner team', value: 'Claims Platform' },
      { label: 'Support contact', value: 'claims-support@example.com' },
      { label: 'Data classification', value: 'Internal' },
    ]);
  });

  it('a full datasheet renders every field in the model declaration order', () => {
    const rows = datasheetRows(
      publishRequest({
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

  it('omits empty optionals entirely, never a "—" placeholder: a declaration is publisher-asserted, so a blank reads as not declared, not as a measured absence', () => {
    const rows = datasheetRows(
      publishRequest({
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
    const rows = datasheetRows(publishRequest({ datasheet: datasheet({ owner_team: '  ' }) }));
    expect(rows.map((r) => r.label)).toEqual(['Support contact', 'Data classification']);
  });

  it('joins list fields readably, trimming blanks and de-duplicating', () => {
    const rows = datasheetRows(
      publishRequest({
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
    const rows = datasheetRows(publishRequest({ datasheet: datasheet({ compliance: ['  ', ''] }) }));
    expect(rows.some((r) => r.label === 'Compliance')).toBe(false);
  });

  it('trims scalar values', () => {
    const rows = datasheetRows(publishRequest({ datasheet: datasheet({ version: '  1.0  ' }) }));
    expect(rows.find((r) => r.label === 'Version')?.value).toBe('1.0');
  });
});

describe('publishBadge', () => {
  it('pending → Pending (amber)', () => {
    const badge = publishBadge(publishRequest({ status: 'pending' }));
    expect(badge.label).toBe('Pending');
    expect(badge.cls).toContain('amber');
  });

  it('pending with a persisted error → Failed: the approve write failed and the backend kept the request pending, so Approve again retries', () => {
    const badge = publishBadge(
      publishRequest({ status: 'pending', error: 'failed to write the marketplace block' }),
    );
    expect(badge.label).toBe('Failed');
    expect(badge.cls).toContain('red');
  });

  it('a blank error string does not flip the badge to Failed', () => {
    expect(publishBadge(publishRequest({ status: 'pending', error: '   ' })).label).toBe('Pending');
    expect(publishBadge(publishRequest({ status: 'pending', error: null })).label).toBe('Pending');
  });

  it('approved → Approved (emerald), rejected → Rejected (red)', () => {
    const approved = publishBadge(publishRequest({ status: 'approved' }));
    expect(approved.label).toBe('Approved');
    expect(approved.cls).toContain('emerald');
    const rejected = publishBadge(publishRequest({ status: 'rejected' }));
    expect(rejected.label).toBe('Rejected');
    expect(rejected.cls).toContain('red');
  });

  it('a stale error on a decided request does not override its status badge', () => {
    expect(publishBadge(publishRequest({ status: 'approved', error: 'old failure' })).label).toBe('Approved');
  });
});

describe('productTypeBadge', () => {
  it('agent → "Agent"; mcp → "MCP", not the CSS-capitalized "Mcp"', () => {
    expect(productTypeBadge(publishRequest({ product_type: 'agent' })).label).toBe('Agent');
    expect(productTypeBadge(publishRequest({ product_type: 'mcp' })).label).toBe('MCP');
  });

  it('is a neutral classification tint — never a status colour', () => {
    for (const badge of [
      productTypeBadge(publishRequest({ product_type: 'agent' })),
      productTypeBadge(publishRequest({ product_type: 'mcp' })),
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

describe('unpublishStateFor: publishedKeys is the live set of listed (product_type, product_id) keys, built through the same productKey the probe uses', () => {
  const published = (...pairs: ['agent' | 'mcp', string][]) =>
    new Set(pairs.map(([type, id]) => productKey(type, id)));

  it('an approved request whose product is live → the Unpublish action', () => {
    expect(unpublishStateFor(publishRequest({ status: 'approved' }), published(['agent', 'agent-1']))).toBe(
      'unpublish',
    );
  });

  it('a live listing whose record was overwritten by a new pending re-publish still offers Unpublish: one publish record per product, so gating on status alone would strand a live listing', () => {
    expect(unpublishStateFor(publishRequest({ status: 'pending' }), published(['agent', 'agent-1']))).toBe(
      'unpublish',
    );
  });

  it('a live listing whose re-publish was REJECTED still offers Unpublish', () => {
    expect(unpublishStateFor(publishRequest({ status: 'rejected' }), published(['agent', 'agent-1']))).toBe(
      'unpublish',
    );
  });

  it('an approved request whose product is no longer listed → the muted Unpublished label', () => {
    expect(unpublishStateFor(publishRequest({ status: 'approved' }), published())).toBe('unpublished');
  });

  it('a pending / rejected request on a NOT-listed product → no unpublish affordance', () => {
    expect(unpublishStateFor(publishRequest({ status: 'pending' }), published())).toBe('none');
    expect(unpublishStateFor(publishRequest({ status: 'rejected' }), published())).toBe('none');
  });

  it('membership is keyed on the product, not the request id', () => {
    expect(unpublishStateFor(publishRequest({ status: 'approved' }), published(['agent', 'pub-abc1234567']))).toBe(
      'unpublished',
    );
  });

  it('an MCP request reads the MCP half of the set', () => {
    const mcpReq = publishRequest({ status: 'approved', product_type: 'mcp', product_id: 'mcp-1' });
    expect(unpublishStateFor(mcpReq, published(['mcp', 'mcp-1']))).toBe('unpublish');
    expect(unpublishStateFor(mcpReq, published())).toBe('unpublished');
  });

  it('the pair decides: a shared id in the other registry never lights up Unpublish, since agents and MCP servers have independent id spaces', () => {
    expect(unpublishStateFor(publishRequest({ status: 'approved' }), published(['mcp', 'agent-1']))).toBe(
      'unpublished',
    );
    expect(
      unpublishStateFor(
        publishRequest({ status: 'pending', product_type: 'mcp', product_id: 'shared-1' }),
        published(['agent', 'shared-1']),
      ),
    ).toBe('none');
  });
});

describe('publishQueueCounts', () => {
  it('counts rows by the badge the table shows, so a failed approve counts as Failed', () => {
    const rows = [
      publishRequest({ id: 'a', status: 'pending' }),
      publishRequest({ id: 'b', status: 'pending', error: 'registry write failed' }),
      publishRequest({ id: 'c', status: 'approved' }),
      publishRequest({ id: 'd', status: 'rejected' }),
      publishRequest({ id: 'e', status: 'pending' }),
    ];
    expect(publishQueueCounts(rows)).toEqual({ total: 5, pending: 2, failed: 1, approved: 1, rejected: 1 });
  });
  it('is all zero for an empty queue', () => {
    expect(publishQueueCounts([])).toEqual({ total: 0, pending: 0, failed: 0, approved: 0, rejected: 0 });
  });
});

describe('subscriptionStatusBadge', () => {
  afterEach(() => vi.restoreAllMocks());

  it('labels every declared status', () => {
    expect(subscriptionStatusBadge('pending').label).toBe('Pending');
    expect(subscriptionStatusBadge('approved').label).toBe('Approved');
    expect(subscriptionStatusBadge('rejected').label).toBe('Rejected');
    expect(subscriptionStatusBadge('failed').label).toBe('Failed');
    expect(subscriptionStatusBadge('revoked').label).toBe('Revoked');
  });

  it('renders the raw word for a subscription status outside the union, without throwing', () => {
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => undefined);
    const badge = subscriptionStatusBadge('expired' as never);
    expect(badge.label).toBe('expired');
    expect(badge.cls).toMatch(/slate/);
    subscriptionStatusBadge('expired' as never);
    expect(warn).toHaveBeenCalledTimes(1);
  });
});
