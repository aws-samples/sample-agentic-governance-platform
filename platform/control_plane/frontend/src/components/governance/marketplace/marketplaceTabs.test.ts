import { describe, it, expect } from 'vitest';
import { marketplaceTabFromPath, MARKETPLACE_TABS } from './marketplaceTabs';

describe('marketplaceTabFromPath', () => {
  it('maps the agents path to the agents tab', () => {
    expect(marketplaceTabFromPath('/marketplace/agents')).toBe('agents');
  });
  it('maps the mcps path to the mcps tab', () => {
    expect(marketplaceTabFromPath('/marketplace/mcps')).toBe('mcps');
  });
  it('defaults the bare /marketplace path to agents', () => {
    expect(marketplaceTabFromPath('/marketplace')).toBe('agents');
  });
  it('defaults any unknown subpath to agents', () => {
    expect(marketplaceTabFromPath('/marketplace/anything-else')).toBe('agents');
  });
});

describe('MARKETPLACE_TABS', () => {
  it('declares agents then mcps with their canonical child paths', () => {
    expect(MARKETPLACE_TABS.map((t) => t.key)).toEqual(['agents', 'mcps']);
    expect(MARKETPLACE_TABS.find((t) => t.key === 'agents')!.to).toBe('/marketplace/agents');
    expect(MARKETPLACE_TABS.find((t) => t.key === 'mcps')!.to).toBe('/marketplace/mcps');
  });
  it('every tab has a non-empty label', () => {
    for (const t of MARKETPLACE_TABS) expect(t.label.length).toBeGreaterThan(0);
  });
});
