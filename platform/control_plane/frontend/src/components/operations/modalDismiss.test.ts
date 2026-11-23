import { describe, it, expect } from 'vitest';
import { canDismissModal } from './modalDismiss';

describe('canDismissModal — ×, Escape and a backdrop click ask one predicate, so all three close or none does', () => {
  it('allows every dismiss path when nothing is pending', () => {
    expect(canDismissModal(false)).toBe(true);
  });
  it('blocks every dismiss path while an action is pending', () => {
    expect(canDismissModal(true)).toBe(false);
  });
});
