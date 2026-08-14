import { describe, it, expect } from 'vitest';
import { mergeGrants, unconfirmedMutations } from './accessGrantsReconcile';
import type { PendingMutation } from './accessGrantsReconcile';
import type { Grant } from '../../api/client';

const grant = (over: Partial<Grant> = {}): Grant => ({
  assignment_id: 'asg-1',
  principal_id: 'oid-1',
  principal_display: 'Lars Svensson',
  principal_type: 'User',
  role: 'Invoker',
  ...over,
});

const pendingAdd = (g: Grant): PendingMutation => ({ kind: 'add', grant: g });
const pendingRemove = (assignmentId: string): PendingMutation => ({ kind: 'remove', assignmentId });

describe('mergeGrants', () => {
  it('keeps a pending add the server read does not yet show (THE bug)', () => {
    // Graph replication lag: POST returned grant asg-new, but the stale list does
    // not include it yet. The optimistic row MUST survive the merge.
    const fresh: Grant[] = [grant({ assignment_id: 'asg-1' })];
    const optimistic = grant({ assignment_id: 'asg-new', principal_display: 'Mira Patel' });
    const result = mergeGrants(fresh, [pendingAdd(optimistic)]);
    expect(result.map((g) => g.assignment_id)).toEqual(['asg-1', 'asg-new']);
    expect(result).toContainEqual(optimistic);
  });

  it('does not duplicate an add the server read already reflects (de-dupe by assignment_id)', () => {
    // Once Graph caught up, the same assignment_id appears in both the server read
    // and the overlay; the result must contain it exactly once (server row wins).
    const confirmed = grant({ assignment_id: 'asg-new', principal_display: 'Mira Patel' });
    const fresh: Grant[] = [grant({ assignment_id: 'asg-1' }), confirmed];
    const result = mergeGrants(fresh, [pendingAdd(confirmed)]);
    expect(result.filter((g) => g.assignment_id === 'asg-new')).toHaveLength(1);
    expect(result.map((g) => g.assignment_id)).toEqual(['asg-1', 'asg-new']);
  });

  it('filters out a pending remove still present in the server read (the resurrection bug)', () => {
    // DELETE succeeded but the stale list still shows the removed assignment. The
    // overlay must keep it hidden until Graph drops it.
    const fresh: Grant[] = [grant({ assignment_id: 'asg-1' }), grant({ assignment_id: 'asg-gone' })];
    const result = mergeGrants(fresh, [pendingRemove('asg-gone')]);
    expect(result.map((g) => g.assignment_id)).toEqual(['asg-1']);
  });

  it('returns the server read unchanged when there are no pending mutations', () => {
    const fresh: Grant[] = [grant({ assignment_id: 'asg-1' }), grant({ assignment_id: 'asg-2' })];
    const result = mergeGrants(fresh, []);
    expect(result).toEqual(fresh);
  });

  it('does not mutate its inputs', () => {
    const fresh: Grant[] = [grant({ assignment_id: 'asg-1' })];
    const optimistic = grant({ assignment_id: 'asg-new' });
    const pending: PendingMutation[] = [pendingAdd(optimistic), pendingRemove('asg-1')];
    const freshCopy = fresh.map((g) => ({ ...g }));
    const pendingLen = pending.length;
    mergeGrants(fresh, pending);
    expect(fresh).toEqual(freshCopy); // server array untouched
    expect(pending).toHaveLength(pendingLen); // pending array untouched
  });

  it('applies a mixed add + remove overlay deterministically (server rows first, then unconfirmed adds)', () => {
    const fresh: Grant[] = [grant({ assignment_id: 'asg-1' }), grant({ assignment_id: 'asg-gone' })];
    const optimistic = grant({ assignment_id: 'asg-new', principal_display: 'Mira Patel' });
    const result = mergeGrants(fresh, [pendingAdd(optimistic), pendingRemove('asg-gone')]);
    expect(result.map((g) => g.assignment_id)).toEqual(['asg-1', 'asg-new']);
  });
});

describe('unconfirmedMutations', () => {
  it('drops a pending add once the server read contains its id', () => {
    const fresh: Grant[] = [grant({ assignment_id: 'asg-new' })];
    const pending = [pendingAdd(grant({ assignment_id: 'asg-new' }))];
    expect(unconfirmedMutations(fresh, pending)).toEqual([]);
  });

  it('keeps a pending add the server read does not yet show', () => {
    const fresh: Grant[] = [grant({ assignment_id: 'asg-1' })];
    const add = pendingAdd(grant({ assignment_id: 'asg-new' }));
    expect(unconfirmedMutations(fresh, [add])).toEqual([add]);
  });

  it('drops a pending remove once the server read no longer contains its id', () => {
    const fresh: Grant[] = [grant({ assignment_id: 'asg-1' })];
    const pending = [pendingRemove('asg-gone')];
    expect(unconfirmedMutations(fresh, pending)).toEqual([]);
  });

  it('keeps a pending remove while the server read still contains its id', () => {
    const fresh: Grant[] = [grant({ assignment_id: 'asg-gone' })];
    const remove = pendingRemove('asg-gone');
    expect(unconfirmedMutations(fresh, [remove])).toEqual([remove]);
  });

  it('handles a mixed set, keeping only the still-unconfirmed mutations', () => {
    // asg-addA is confirmed (present); asg-addB still missing (kept);
    // asg-rmX confirmed (absent); asg-rmY still present (kept).
    const fresh: Grant[] = [grant({ assignment_id: 'asg-addA' }), grant({ assignment_id: 'asg-rmY' })];
    const addA = pendingAdd(grant({ assignment_id: 'asg-addA' }));
    const addB = pendingAdd(grant({ assignment_id: 'asg-addB' }));
    const rmX = pendingRemove('asg-rmX');
    const rmY = pendingRemove('asg-rmY');
    expect(unconfirmedMutations(fresh, [addA, addB, rmX, rmY])).toEqual([addB, rmY]);
  });

  it('returns empty for empty pending', () => {
    expect(unconfirmedMutations([grant()], [])).toEqual([]);
  });
});
