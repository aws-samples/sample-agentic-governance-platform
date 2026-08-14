// Freezes the honesty copy (Epic 31F, Task 1). These four strings are a
// cross-task contract: T2–T6 and T8 render them on real pages, and the plan
// pins them verbatim. A drifted string is a spec violation, not a wording
// preference — so the test asserts exact equality, not a `toContain` shape.

import { describe, expect, it } from 'vitest';
import {
  COMING_SOON_BODY,
  COMING_SOON_TITLE,
  SAMPLE_BADGE_LABEL,
  SOON_TAG_LABEL,
} from './comingSoonCopy';

describe('honesty copy constants', () => {
  it('COMING_SOON_TITLE is the pinned banner title', () => {
    expect(COMING_SOON_TITLE).toBe('Coming soon');
  });

  it('COMING_SOON_BODY is the pinned banner body', () => {
    expect(COMING_SOON_BODY).toBe('Example design — not functional yet.');
  });

  it('SAMPLE_BADGE_LABEL is the pinned badge label', () => {
    expect(SAMPLE_BADGE_LABEL).toBe('Sample data');
  });

  it('SOON_TAG_LABEL is the pinned nav-row tag label', () => {
    expect(SOON_TAG_LABEL).toBe('soon');
  });
});

describe('honesty copy hygiene', () => {
  it('no string is empty or padded with whitespace', () => {
    for (const s of [COMING_SOON_TITLE, COMING_SOON_BODY, SAMPLE_BADGE_LABEL, SOON_TAG_LABEL]) {
      expect(s.length).toBeGreaterThan(0);
      expect(s).toBe(s.trim());
    }
  });

  it('the body uses a real em dash, not a hyphen', () => {
    expect(COMING_SOON_BODY).toContain('—');
    expect(COMING_SOON_BODY).not.toContain(' - ');
  });
});
