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
import type {
  Agent,
  McpServer,
  MarketplacePublicationBlock,
  MarketplacePublishRequest,
} from '../../api/client';

// A valid draft; `over` narrows to the field under test.
const draft = (over: Partial<DatasheetDraft> = {}): DatasheetDraft => ({
  ...EMPTY_DATASHEET_DRAFT,
  owner_team: 'Claims Platform',
  support_contact: 'claims-support@example.com',
  data_classification: 'internal',
  ...over,
});

// A publish request. Post-Amendment-1 the record is keyed on the (product_type, product_id)
// PAIR — `agent_id`/`agent_name` are gone (C9).
const request = (over: Partial<MarketplacePublishRequest> = {}): MarketplacePublishRequest => ({
  id: 'pub-abc1234567',
  product_type: 'agent',
  product_id: 'agent-1',
  product_name: 'FNOL Agent',
  datasheet: buildDatasheet(draft()),
  status: 'pending',
  requested_by: 'oid-1',
  created_at: '2026-08-10T00:00:00Z',
  updated_at: '2026-08-10T00:00:00Z',
  ...over,
});

// The publication block is the SHARED shape (C11): the same value is what both
// `Agent.marketplace` and `McpServer.marketplace` carry.
const block = (over: Partial<MarketplacePublicationBlock> = {}): MarketplacePublicationBlock => ({
  published: true,
  datasheet: buildDatasheet(draft()),
  declared_by: 'admin@example.com',
  declared_at: '2026-08-10T09:00:00Z',
  ...over,
});

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

  it('round-trips a built datasheet back into the draft', () => {
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

  it('renders nulls and missing lists as empty strings', () => {
    expect(
      draftFromDatasheet({
        owner_team: 'Claims Platform',
        support_contact: 'ops@example.com',
        data_classification: 'internal',
        sla_tier: null,
        support_hours: null,
        version: null,
        region: null,
        pitch: null,
      }),
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
    expect(publishStateLabel(request({ status: 'pending' }), null)).toBe('pending');
  });

  it('pending wins over a live publication (an update is under review)', () => {
    expect(publishStateLabel(request({ status: 'pending' }), block({ published: true }))).toBe('pending');
  });

  it('pending wins over a delisted publication (a re-listing is under review)', () => {
    expect(publishStateLabel(request({ status: 'pending' }), block({ published: false }))).toBe('pending');
  });

  it('rejected: the last decision was a rejection', () => {
    expect(publishStateLabel(request({ status: 'rejected' }), null)).toBe('rejected');
  });

  it('rejected wins over the publication block (a rejected update must surface)', () => {
    expect(publishStateLabel(request({ status: 'rejected' }), block({ published: true }))).toBe('rejected');
  });

  it('published: approved request and the block says published', () => {
    expect(publishStateLabel(request({ status: 'approved' }), block({ published: true }))).toBe('published');
  });

  it('published from the block alone (the request read 404d as a foreign tenant)', () => {
    expect(publishStateLabel(null, block({ published: true }))).toBe('published');
  });

  it('unpublished: the block was delisted by an admin', () => {
    expect(publishStateLabel(request({ status: 'approved' }), block({ published: false }))).toBe('unpublished');
  });

  it('unpublished from the block alone', () => {
    expect(publishStateLabel(null, block({ published: false }))).toBe('unpublished');
  });

  it('never: an approved request with no block at all (degenerate — the block is the authority)', () => {
    expect(publishStateLabel(request({ status: 'approved' }), null)).toBe('never');
  });

  it('reads the block from either registry record (the widened C11 param)', () => {
    // Compile-time half of the assertion: both product types' `marketplace` fields must be
    // assignable to the shared param, so one panel implementation serves both.
    const fromAgent: Agent['marketplace'] = block({ published: true });
    const fromMcp: McpServer['marketplace'] = block({ published: false });
    expect(publishStateLabel(null, fromAgent)).toBe('published');
    expect(publishStateLabel(null, fromMcp)).toBe('unpublished');
  });

  it('is product-type agnostic: an mcp request drives the same states', () => {
    expect(publishStateLabel(request({ product_type: 'mcp', status: 'pending' }), null)).toBe('pending');
    expect(publishStateLabel(request({ product_type: 'mcp', status: 'rejected' }), block())).toBe('rejected');
  });
});

describe('listingStaysLive', () => {
  it('pending over a live listing: the card is still up while the update is reviewed', () => {
    expect(listingStaysLive('pending', block({ published: true }))).toBe(true);
  });

  it('rejected over a live listing: the declined update leaves the card up', () => {
    expect(listingStaysLive('rejected', block({ published: true }))).toBe(true);
  });

  it('pending over a delisted block: nothing is live', () => {
    expect(listingStaysLive('pending', block({ published: false }))).toBe(false);
  });

  it('rejected over a delisted block: nothing is live', () => {
    expect(listingStaysLive('rejected', block({ published: false }))).toBe(false);
  });

  it('pending with no block at all: a first-time request, nothing is live', () => {
    expect(listingStaysLive('pending', null)).toBe(false);
    expect(listingStaysLive('pending', undefined)).toBe(false);
  });

  it('rejected with no block at all: nothing is live', () => {
    expect(listingStaysLive('rejected', null)).toBe(false);
  });

  it('is false for the states that already state the listing truth themselves', () => {
    expect(listingStaysLive('published', block({ published: true }))).toBe(false);
    expect(listingStaysLive('unpublished', block({ published: false }))).toBe(false);
    expect(listingStaysLive('never', null)).toBe(false);
  });

  it('accepts the block from either registry record (the widened C11 param)', () => {
    const fromAgent: Agent['marketplace'] = block({ published: true });
    const fromMcp: McpServer['marketplace'] = block({ published: true });
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

  it('shares no copy with the E24 tenant published/shared controls on the same pages', () => {
    const labels = states.map((s) => MARKETPLACE_STATE_UI[s].label);
    expect(labels).not.toContain('Published');
    expect(labels).not.toContain('Private');
    expect(labels).not.toContain('Shared');
  });
});

describe('isMissingPublishRequest', () => {
  // THE contract case. This is the exact `_ERROR_DETAIL["not_found"]` literal the backend sends
  // (backend/src/api/routes/marketplace.py:71, raised for this route at :476) and the axios
  // interceptor prefers `response.data.detail` over `error.message`, so this string — not a
  // status code and not a generic phrase — is what actually reaches the predicate on a 404.
  // Asserted VERBATIM on purpose: a backend rewording must fail here, because both publish
  // panels would otherwise silently misclassify every 404 as a real load error.
  const BACKEND_NOT_FOUND_DETAIL = 'Marketplace product or subscription not found';

  it('treats the real backend not-found detail literal as "no request"', () => {
    expect(isMissingPublishRequest(BACKEND_NOT_FOUND_DETAIL)).toBe(true);
  });

  it('does NOT match the identity-precondition 409 detail (the mislabel-risk twin)', () => {
    // Both literals contain "not"; only one means "never published". Misreading the 409 as a
    // 404 would show "never offered in the marketplace" instead of telling the publisher why
    // their submit was refused. This is the case the predicate most plausibly gets wrong.
    expect(isMissingPublishRequest('identity is not provisioned')).toBe(false);
  });

  it('is case-insensitive about the "not found" phrase', () => {
    expect(isMissingPublishRequest('NOT FOUND')).toBe(true);
  });

  it('treats a bare 404 in the message as "no request" (the detail-less fallback path)', () => {
    // When there is no `detail` the interceptor falls back to the raw axios message.
    expect(isMissingPublishRequest('Request failed with status code 404')).toBe(true);
  });

  it('is false for the other publish 409 detail literals, which must surface', () => {
    expect(
      isMissingPublishRequest('Product must be approved before it can be published to the marketplace'),
    ).toBe(false);
    expect(
      isMissingPublishRequest('A marketplace publish request for this product is already pending'),
    ).toBe(false);
  });

  it('is false for a server-side failure that must surface', () => {
    expect(isMissingPublishRequest('Internal Server Error')).toBe(false);
    expect(isMissingPublishRequest('Request failed with status code 500')).toBe(false);
    expect(
      isMissingPublishRequest('Failed to write the marketplace publication; see backend logs and retry'),
    ).toBe(false);
  });

  it('is false for an empty message (no evidence of a 404)', () => {
    expect(isMissingPublishRequest('')).toBe(false);
  });
});
