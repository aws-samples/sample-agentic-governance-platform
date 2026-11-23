import { describe, expect, it } from 'vitest';
import { listingSteps } from './listingSteps';

const base = {
  lifecycle: 'approved',
  publishState: 'never' as const,
  staysLive: false,
  blocker: null,
};

describe('listingSteps', () => {
  it('draft: step 1 is current, the rest upcoming, next = submit for approval', () => {
    const s = listingSteps({ ...base, lifecycle: 'draft', publishState: null });
    expect(s.steps.map((x) => x.status)).toEqual(['current', 'upcoming', 'upcoming']);
    expect(s.next).toEqual({ kind: 'note', text: 'Submit it for approval (top of the page).' });
  });

  it('pending approval: waits on an admin', () => {
    const s = listingSteps({ ...base, lifecycle: 'pending_approval', publishState: null });
    expect(s.steps[0].status).toBe('current');
    expect(s.next.kind).toBe('note');
    expect(s.next.text).toMatch(/admin/i);
  });

  it('rejected / deprecated block step 1', () => {
    expect(listingSteps({ ...base, lifecycle: 'rejected', publishState: null }).steps[0].status).toBe('blocked');
    const dep = listingSteps({ ...base, lifecycle: 'deprecated', publishState: null });
    expect(dep.steps[0].status).toBe('blocked');
    expect(dep.next.text).toMatch(/deprecated/i);
  });

  it('approved, never offered: step 2 current with the datasheet CTA', () => {
    const s = listingSteps(base);
    expect(s.steps.map((x) => x.status)).toEqual(['done', 'current', 'upcoming']);
    expect(s.next).toEqual({ kind: 'cta', text: 'Publish to marketplace' });
  });

  it('approved but publication still loading: step 2 current, no action yet', () => {
    const s = listingSteps({ ...base, publishState: null });
    expect(s.steps.map((x) => x.status)).toEqual(['done', 'current', 'upcoming']);
    expect(s.next.kind).toBe('none');
  });

  it('a blocker (identity / platform) blocks step 2 and is the next note', () => {
    const s = listingSteps({ ...base, blocker: 'Needs a provisioned Entra identity.' });
    expect(s.steps[1].status).toBe('blocked');
    expect(s.next).toEqual({ kind: 'note', text: 'Needs a provisioned Entra identity.' });
  });

  it('pending first submission: step 2 done, step 3 current waiting on review', () => {
    const s = listingSteps({ ...base, publishState: 'pending' });
    expect(s.steps.map((x) => x.status)).toEqual(['done', 'done', 'current']);
    expect(s.next.kind).toBe('review');
  });

  it('pending update on a live card: all done, still waiting on review', () => {
    const s = listingSteps({ ...base, publishState: 'pending', staysLive: true });
    expect(s.steps.map((x) => x.status)).toEqual(['done', 'done', 'done']);
    expect(s.next.kind).toBe('review');
  });

  it('declined first submission: step 2 blocked with the resubmit CTA', () => {
    const s = listingSteps({ ...base, publishState: 'rejected' });
    expect(s.steps.map((x) => x.status)).toEqual(['done', 'blocked', 'upcoming']);
    expect(s.next).toEqual({ kind: 'cta', text: 'Revise and resubmit' });
  });

  it('declined update on a live card keeps step 3 done', () => {
    const s = listingSteps({ ...base, publishState: 'rejected', staysLive: true });
    expect(s.steps[2].status).toBe('done');
  });

  it('listed: everything done, nothing required', () => {
    const s = listingSteps({ ...base, publishState: 'published' });
    expect(s.steps.map((x) => x.status)).toEqual(['done', 'done', 'done']);
    expect(s.next.kind).toBe('none');
  });

  it('delisted: step 2 current again with the publish CTA', () => {
    const s = listingSteps({ ...base, publishState: 'unpublished' });
    expect(s.steps.map((x) => x.status)).toEqual(['done', 'current', 'upcoming']);
    expect(s.next).toEqual({ kind: 'cta', text: 'Publish to marketplace' });
  });

  it('never names a sharing step — listing does not require cross-tenant sharing', () => {
    const labels = listingSteps(base).steps.map((x) => x.label.toLowerCase());
    expect(labels.some((l) => l.includes('share'))).toBe(false);
  });
});
