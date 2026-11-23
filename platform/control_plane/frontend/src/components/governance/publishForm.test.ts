import { describe, it, expect } from 'vitest';
import {
  EMPTY_DATASHEET_DRAFT,
  MARKETPLACE_STATE_UI,
  validateDatasheet,
  buildDatasheet,
  draftFromDatasheet,
  publishStateLabel,
  listingStaysLive,
  isMissingPublishRequest,
} from './publishForm';
import type { DatasheetDraft, PublishState } from './publishForm';
import { ApiFailure, type ErrorCode } from '../../api/failure';
import type { Agent, McpServer } from '../../api/client';
import type { MarketplacePublication } from '../../api/openapi';
import { datasheet, publishRequest } from '../../test/wire';

const draft = (over: Partial<DatasheetDraft> = {}): DatasheetDraft => ({
  ...EMPTY_DATASHEET_DRAFT,
  owner_team: 'Claims Platform',
  support_contact: 'claims-support@example.com',
  data_classification: 'internal',
  ...over,
});

function marketplacePublication(overrides: Partial<MarketplacePublication> = {}): MarketplacePublication {
  return {
    published: false,
    datasheet: datasheet(),
    declared_by: 'admin@example.com',
    declared_at: '2026-08-10T09:00:00Z',
    ...overrides,
  };
}

describe('validateDatasheet', () => {
  it('returns [] for a complete draft', () => {
    expect(validateDatasheet(draft())).toEqual([]);
  });

  it('accepts a draft whose mandatory fields need trimming', () => {
    expect(
      validateDatasheet(
        draft({ owner_team: '  Claims Platform  ', data_classification: ' internal ' }),
      ),
    ).toEqual([]);
  });

  it('names a missing owner team', () => {
    expect(validateDatasheet(draft({ owner_team: '' }))).toEqual(['Owner team is required.']);
  });

  it('treats a whitespace-only owner team as missing', () => {
    expect(validateDatasheet(draft({ owner_team: '   ' }))).toEqual(['Owner team is required.']);
  });

  it('names a missing support contact (and does not also complain about the email shape)', () => {
    expect(validateDatasheet(draft({ support_contact: '' }))).toEqual([
      'Support contact is required.',
    ]);
  });

  it('rejects a support contact without an @', () => {
    expect(validateDatasheet(draft({ support_contact: 'claims-support' }))).toEqual([
      'Support contact must be an email address.',
    ]);
  });

  it('names a missing data classification', () => {
    expect(validateDatasheet(draft({ data_classification: '' }))).toEqual([
      'Data classification is required.',
    ]);
  });

  it('reports every problem at once, in field order', () => {
    expect(
      validateDatasheet(
        draft({ owner_team: '', support_contact: 'not-an-email', data_classification: '' }),
      ),
    ).toEqual([
      'Owner team is required.',
      'Support contact must be an email address.',
      'Data classification is required.',
    ]);
  });

  it('does not require any optional field', () => {
    expect(validateDatasheet(draft({ sla_tier: '', compliance: '', pitch: '' }))).toEqual([]);
  });
});

describe('buildDatasheet', () => {
  it('trims the mandatory fields', () => {
    const d = buildDatasheet(
      draft({ owner_team: '  Claims Platform ', support_contact: ' ops@example.com ', data_classification: ' internal ' }),
    );
    expect(d.owner_team).toBe('Claims Platform');
    expect(d.support_contact).toBe('ops@example.com');
    expect(d.data_classification).toBe('internal');
  });

  it('nulls blank optional scalars', () => {
    const d = buildDatasheet(draft({ sla_tier: '', support_hours: '   ', version: '', region: '', pitch: '' }));
    expect(d.sla_tier).toBeNull();
    expect(d.support_hours).toBeNull();
    expect(d.version).toBeNull();
    expect(d.region).toBeNull();
    expect(d.pitch).toBeNull();
  });

  it('trims non-blank optional scalars', () => {
    const d = buildDatasheet(draft({ sla_tier: ' gold ', support_hours: ' 9-5 CET ', version: ' 1.2.0 ', region: ' eu-central-1 ', pitch: ' Files claims. ' }));
    expect(d.sla_tier).toBe('gold');
    expect(d.support_hours).toBe('9-5 CET');
    expect(d.version).toBe('1.2.0');
    expect(d.region).toBe('eu-central-1');
    expect(d.pitch).toBe('Files claims.');
  });

  it('splits compliance and guardrails on commas, trimming each entry', () => {
    const d = buildDatasheet(draft({ compliance: 'GDPR, SOC2 ,ISO27001', guardrails: 'pii-redaction , no-pricing' }));
    expect(d.compliance).toEqual(['GDPR', 'SOC2', 'ISO27001']);
    expect(d.guardrails).toEqual(['pii-redaction', 'no-pricing']);
  });

  it('drops empty entries from the comma lists', () => {
    const d = buildDatasheet(draft({ compliance: 'GDPR,,  , SOC2,', guardrails: ' , ' }));
    expect(d.compliance).toEqual(['GDPR', 'SOC2']);
    expect(d.guardrails).toEqual([]);
  });

  it('yields empty lists for blank list fields', () => {
    const d = buildDatasheet(draft({ compliance: '', guardrails: '' }));
    expect(d.compliance).toEqual([]);
    expect(d.guardrails).toEqual([]);
  });
});

describe('draftFromDatasheet', () => {
  it('returns the empty draft for a missing datasheet', () => {
    expect(draftFromDatasheet(null)).toEqual(EMPTY_DATASHEET_DRAFT);
    expect(draftFromDatasheet(undefined)).toEqual(EMPTY_DATASHEET_DRAFT);
  });

  it('round-trips a built datasheet back into the draft, so an Update datasheet re-publish edits the record instead of overwriting it with a half-filled one', () => {
    const original = draft({
      sla_tier: 'gold',
      compliance: 'GDPR, SOC2',
      support_hours: '9-5 CET',
      version: '1.2.0',
      region: 'eu-central-1',
      guardrails: 'pii-redaction, no-pricing',
      pitch: 'Files claims.',
    });
    expect(draftFromDatasheet(buildDatasheet(original))).toEqual(original);
  });

  it('renders nulls and empty lists as empty strings', () => {
    expect(
      draftFromDatasheet(
        datasheet({
          owner_team: 'Claims Platform',
          support_contact: 'ops@example.com',
          data_classification: 'internal',
          sla_tier: null,
          support_hours: null,
          version: null,
          region: null,
          pitch: null,
        }),
      ),
    ).toEqual({
      ...EMPTY_DATASHEET_DRAFT,
      owner_team: 'Claims Platform',
      support_contact: 'ops@example.com',
      data_classification: 'internal',
    });
  });
});

describe('publishStateLabel', () => {
  it('never: no request and no publication block', () => {
    expect(publishStateLabel(null, null)).toBe('never');
    expect(publishStateLabel(null, undefined)).toBe('never');
  });

  it('pending: a pending request and nothing published yet', () => {
    expect(publishStateLabel(publishRequest({ status: 'pending' }), null)).toBe('pending');
  });

  it('pending wins over a live publication: an update is under review, and a second create would answer 409 publish_conflict', () => {
    expect(publishStateLabel(publishRequest({ status: 'pending' }), marketplacePublication({ published: true }))).toBe('pending');
  });

  it('pending wins over a delisted publication (a re-listing is under review)', () => {
    expect(publishStateLabel(publishRequest({ status: 'pending' }), marketplacePublication({ published: false }))).toBe('pending');
  });

  it('rejected: the last decision was a rejection', () => {
    expect(publishStateLabel(publishRequest({ status: 'rejected' }), null)).toBe('rejected');
  });

  it('rejected wins over the publication block (a rejected update must surface)', () => {
    expect(publishStateLabel(publishRequest({ status: 'rejected' }), marketplacePublication({ published: true }))).toBe('rejected');
  });

  it('published: approved request and the block says published', () => {
    expect(publishStateLabel(publishRequest({ status: 'approved' }), marketplacePublication({ published: true }))).toBe('published');
  });

  it('published from the block alone: the request read answers 404 for a foreign tenant and for never-requested alike, so the block decides', () => {
    expect(publishStateLabel(null, marketplacePublication({ published: true }))).toBe('published');
  });

  it('unpublished: the block was delisted by an admin', () => {
    expect(publishStateLabel(publishRequest({ status: 'approved' }), marketplacePublication({ published: false }))).toBe('unpublished');
  });

  it('unpublished from the block alone', () => {
    expect(publishStateLabel(null, marketplacePublication({ published: false }))).toBe('unpublished');
  });

  it('never: an approved request with no block at all (degenerate — the block is the authority)', () => {
    expect(publishStateLabel(publishRequest({ status: 'approved' }), null)).toBe('never');
  });

  it('reads the block from either registry record: both product types\' marketplace fields are assignable to the shared param, so one panel serves both', () => {
    const fromAgent: Agent['marketplace'] = marketplacePublication({ published: true });
    const fromMcp: McpServer['marketplace'] = marketplacePublication({ published: false });
    expect(publishStateLabel(null, fromAgent)).toBe('published');
    expect(publishStateLabel(null, fromMcp)).toBe('unpublished');
  });

  it('is product-type agnostic: an mcp request drives the same states', () => {
    expect(publishStateLabel(publishRequest({ product_type: 'mcp', status: 'pending' }), null)).toBe('pending');
    expect(publishStateLabel(publishRequest({ product_type: 'mcp', status: 'rejected' }), marketplacePublication({ published: true }))).toBe('rejected');
  });
});

describe('listingStaysLive', () => {
  it('pending over a live listing: the card is still up while the update is reviewed', () => {
    expect(listingStaysLive('pending', marketplacePublication({ published: true }))).toBe(true);
  });

  it('rejected over a live listing: the declined update leaves the card up', () => {
    expect(listingStaysLive('rejected', marketplacePublication({ published: true }))).toBe(true);
  });

  it('pending over a delisted block: nothing is live', () => {
    expect(listingStaysLive('pending', marketplacePublication({ published: false }))).toBe(false);
  });

  it('rejected over a delisted block: nothing is live', () => {
    expect(listingStaysLive('rejected', marketplacePublication({ published: false }))).toBe(false);
  });

  it('pending with no block at all: a first-time request, nothing is live', () => {
    expect(listingStaysLive('pending', null)).toBe(false);
    expect(listingStaysLive('pending', undefined)).toBe(false);
  });

  it('rejected with no block at all: nothing is live', () => {
    expect(listingStaysLive('rejected', null)).toBe(false);
  });

  it('is false for the states that already state the listing truth themselves', () => {
    expect(listingStaysLive('published', marketplacePublication({ published: true }))).toBe(false);
    expect(listingStaysLive('unpublished', marketplacePublication({ published: false }))).toBe(false);
    expect(listingStaysLive('never', null)).toBe(false);
  });

  it('accepts the block from either registry record', () => {
    const fromAgent: Agent['marketplace'] = marketplacePublication({ published: true });
    const fromMcp: McpServer['marketplace'] = marketplacePublication({ published: true });
    expect(listingStaysLive('pending', fromAgent)).toBe(true);
    expect(listingStaysLive('pending', fromMcp)).toBe(true);
  });
});

describe('MARKETPLACE_STATE_UI', () => {
  const states: PublishState[] = ['never', 'pending', 'rejected', 'published', 'unpublished'];

  it('covers every publish state with a label', () => {
    for (const state of states) {
      expect(MARKETPLACE_STATE_UI[state].label).toBeTruthy();
      expect(MARKETPLACE_STATE_UI[state].cls).toBeTruthy();
    }
  });

  it('offers no CTA while a request is pending (a second create is a 409 publish_conflict)', () => {
    expect(MARKETPLACE_STATE_UI.pending.cta).toBeNull();
  });

  it('offers a CTA in every other state', () => {
    for (const state of states.filter((s) => s !== 'pending')) {
      expect(MARKETPLACE_STATE_UI[state].cta).toBeTruthy();
    }
  });

  it('shares no copy with the tenant published/shared controls on the same pages, so the two features never read as one', () => {
    const labels = states.map((s) => MARKETPLACE_STATE_UI[s].label);
    expect(labels).not.toContain('Published');
    expect(labels).not.toContain('Private');
    expect(labels).not.toContain('Shared');
  });
});

describe('isMissingPublishRequest: the marketplace not-found code is the never state, not an error; the predicate reads the failure\'s code, never its status or sentence', () => {
  function failure(status: number, code: ErrorCode | null = null, detail?: string): ApiFailure {
    return new ApiFailure(status, code, detail, detail ?? `Request failed with status code ${status}`);
  }

  it('a 404 carrying the marketplace not-found code is "no request"', () => {
    expect(isMissingPublishRequest(failure(404, 'marketplace.not_found', 'Marketplace product or subscription not found'))).toBe(true);
  });

  it('a 404 with no code, the axios sentence as its message, is not "no request": the code is the key', () => {
    expect(isMissingPublishRequest(failure(404))).toBe(false);
    expect(isMissingPublishRequest(failure(404, null, 'Marketplace product or subscription not found'))).toBe(false);
  });

  it('the identity-precondition 409 is not "no request"', () => {
    expect(isMissingPublishRequest(failure(409, 'marketplace.identity_not_provisioned', 'identity is not provisioned'))).toBe(false);
  });

  it('the other publish 409s surface', () => {
    expect(
      isMissingPublishRequest(failure(409, 'marketplace.agent_not_approved', 'Product must be approved before it can be published to the marketplace')),
    ).toBe(false);
    expect(
      isMissingPublishRequest(failure(409, 'marketplace.publish_conflict', 'A marketplace publish request for this product is already pending')),
    ).toBe(false);
  });

  it('a server-side failure surfaces whatever its sentence says, so a 500 reading "not found" is still an error', () => {
    expect(isMissingPublishRequest(failure(500))).toBe(false);
    expect(isMissingPublishRequest(failure(500, null, 'not found'))).toBe(false);
    expect(
      isMissingPublishRequest(failure(502, 'marketplace.publish_write_failed', 'Failed to write the marketplace publication; see backend logs and retry')),
    ).toBe(false);
  });

  it('a network failure, status 0, surfaces', () => {
    expect(isMissingPublishRequest(failure(0))).toBe(false);
  });
});
