// projectDetailTabs.test.ts — the registry + the roving-tabindex keyboard model.
import { describe, expect, it } from 'vitest';
import {
  PROJECT_DETAIL_TABS,
  nextTabKey,
  showPageSkeleton,
  tabId,
  tabPanelId,
} from './projectDetailTabs';

const KEYS = PROJECT_DETAIL_TABS.map((t) => t.key);

describe('PROJECT_DETAIL_TABS', () => {
  it('is Repositories then Access, in that order', () => {
    expect(PROJECT_DETAIL_TABS.map((t) => t.label)).toEqual(['Repositories', 'Access']);
    expect(KEYS).toEqual(['repositories', 'access']);
  });
  it('keys are unique and every tab has a label', () => {
    expect(new Set(KEYS).size).toBe(KEYS.length);
    for (const t of PROJECT_DETAIL_TABS) expect(t.label.length).toBeGreaterThan(0);
  });
});

// The page frame must not blank on a REVALIDATION: doing so unmounts both tab bodies, and
// a role mutation triggers one on every grant / change / revoke — which reset the Access
// tab's `rosterLoaded`, replayed its Grant pop-in and doubled its roster read.
describe('showPageSkeleton', () => {
  it('blanks the page on the FIRST load, when there is nothing to keep showing', () => {
    expect(showPageSkeleton(true, false)).toBe(true);
  });
  it('does NOT blank while revalidating an already-loaded page', () => {
    // The regression this pins: `loading` alone remounted both tabs per mutation.
    expect(showPageSkeleton(true, true)).toBe(false);
  });
  it('never blanks once the load settles', () => {
    expect(showPageSkeleton(false, true)).toBe(false);
    expect(showPageSkeleton(false, false)).toBe(false);
  });
});

describe('tabId / tabPanelId', () => {
  it('gives a tab and its panel DISTINCT ids', () => {
    // aria-controls points at the panel and aria-labelledby back at the tab; if the two
    // derivations collided the relationship would be self-referential.
    for (const key of KEYS) expect(tabId(key)).not.toBe(tabPanelId(key));
  });
  it('is unique per tab', () => {
    const ids = [...KEYS.map(tabId), ...KEYS.map(tabPanelId)];
    expect(new Set(ids).size).toBe(ids.length);
  });
});

describe('nextTabKey', () => {
  it('steps right and left', () => {
    expect(nextTabKey(['a', 'b', 'c'], 'a', 'ArrowRight')).toBe('b');
    expect(nextTabKey(['a', 'b', 'c'], 'c', 'ArrowLeft')).toBe('b');
  });
  it('WRAPS at both ends (the APG horizontal-tablist model)', () => {
    expect(nextTabKey(['a', 'b', 'c'], 'c', 'ArrowRight')).toBe('a');
    expect(nextTabKey(['a', 'b', 'c'], 'a', 'ArrowLeft')).toBe('c');
  });
  it('jumps to the ends on Home/End', () => {
    expect(nextTabKey(['a', 'b', 'c'], 'b', 'Home')).toBe('a');
    expect(nextTabKey(['a', 'b', 'c'], 'b', 'End')).toBe('c');
  });
  it('returns null for keys outside the model so the press is left alone', () => {
    // Enter/Space must keep activating the focused tab as a plain button, and Tab must
    // still leave the tablist — swallowing them would break the single-tab-stop pattern.
    for (const k of ['Enter', ' ', 'Tab', 'Escape', 'ArrowDown', 'a']) {
      expect(nextTabKey(['a', 'b'], 'a', k)).toBeNull();
    }
  });
  it('resolves an unknown current key from the start rather than stranding focus', () => {
    expect(nextTabKey(['a', 'b'], 'gone', 'ArrowRight')).toBe('b');
    expect(nextTabKey(['a', 'b'], 'gone', 'ArrowLeft')).toBe('b');
  });
  it('is a no-op on an empty tab list', () => {
    expect(nextTabKey([], 'a', 'ArrowRight')).toBeNull();
  });
  it('works on the REAL two-tab registry, wrapping in both directions', () => {
    expect(nextTabKey(KEYS, 'repositories', 'ArrowRight')).toBe('access');
    expect(nextTabKey(KEYS, 'access', 'ArrowRight')).toBe('repositories');
    expect(nextTabKey(KEYS, 'repositories', 'ArrowLeft')).toBe('access');
  });
});
