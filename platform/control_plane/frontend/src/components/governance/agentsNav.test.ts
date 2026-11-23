import { describe, it, expect } from 'vitest';
import { GOVERNANCE_NAV, allRoutes, visibleNavItems } from './agentsNav';
import { OPERATIONS_NAV } from '../operations/operationsNav';

function comingSoonRoutes(nav: typeof GOVERNANCE_NAV): string[] {
  return nav.flatMap((sec) => sec.items.filter((i) => i.comingSoon).map((i) => i.to));
}

describe('GOVERNANCE_NAV', () => {
  it('declares the collapsible groups in order', () => {
    const groups = GOVERNANCE_NAV.filter((s) => s.label !== '').map((s) => s.label);
    expect(groups).toEqual(['Inventory', 'Security & Policy', 'Operations', 'Resources']);
  });

  it('has the expected section ids in order', () => {
    expect(GOVERNANCE_NAV.map((s) => s.id)).toEqual([
      'home', 'inventory', 'security-policy', 'operations', 'marketplace', 'admin', 'resources',
    ]);
  });

  it('home, marketplace and admin are standalone (headerless, non-collapsible, single item)', () => {
    for (const id of ['home', 'marketplace', 'admin']) {
      const sec = GOVERNANCE_NAV.find((s) => s.id === id)!;
      expect(sec.label).toBe('');
      expect(sec.collapsible).toBe(false);
      expect(sec.items).toHaveLength(1);
    }
  });

  it('collapsible groups have a non-empty label', () => {
    for (const sec of GOVERNANCE_NAV.filter((s) => s.collapsible)) {
      expect(sec.label.length).toBeGreaterThan(0);
    }
  });

  it('Inventory holds agents, tools and the graph; the LLM Gateway mock has no entry', () => {
    const sec = GOVERNANCE_NAV.find((s) => s.id === 'inventory')!;
    expect(sec.items.map((i) => i.to)).toEqual([
      '/agents', '/tools-mcp', '/governance-graph',
    ]);
  });

  it('Marketplace and Admin point at the new canonical paths', () => {
    expect(GOVERNANCE_NAV.find((s) => s.id === 'marketplace')!.items[0].to).toBe('/marketplace');
    expect(GOVERNANCE_NAV.find((s) => s.id === 'admin')!.items[0].to).toBe('/admin');
  });

  it('the Admin item is adminOnly', () => {
    expect(GOVERNANCE_NAV.find((s) => s.id === 'admin')!.items[0].adminOnly).toBe(true);
  });

  it('every nav item has an icon', () => {
    for (const sec of GOVERNANCE_NAV) {
      for (const item of sec.items) expect(item.icon.length).toBeGreaterThan(0);
    }
  });

  it('labels the Inventory tools entry MCPs', () => {
    const inv = GOVERNANCE_NAV.find((s) => s.id === 'inventory')!;
    expect(inv.items.find((i) => i.to === '/tools-mcp')!.label).toBe('MCPs');
  });
});

describe('no SOON entries in either nav', () => {
  it('no governance item is SOON-tagged, and the three mock pages have no entry', () => {
    expect(comingSoonRoutes(GOVERNANCE_NAV)).toEqual([]);
    for (const r of ['/govern/models', '/govern/audit', '/govern/finops']) {
      expect(allRoutes()).not.toContain(r);
    }
  });

  it('never flags Guardrails & Policy — it is a real surface', () => {
    const sec = GOVERNANCE_NAV.find((s) => s.id === 'security-policy')!;
    expect(sec.items.find((i) => i.to === '/secure/guardrails')!.comingSoon).toBeUndefined();
  });

  it('no operations item is SOON-tagged', () => {
    expect(comingSoonRoutes(OPERATIONS_NAV)).toEqual([]);
  });
});

describe('allRoutes', () => {
  it('omits /prompts, hidden from the nav while its route remains, and includes the canonical Marketplace and Admin paths', () => {
    const routes = allRoutes();
    expect(routes).not.toContain('/prompts');
    expect(routes).toContain('/marketplace');
    expect(routes).toContain('/admin');
  });
  it('keeps Observability reachable', () => {
    expect(allRoutes()).toContain('/observability');
  });
  it('returns a deduped set (no repeated leaf route)', () => {
    const routes = allRoutes();
    expect(new Set(routes).size).toBe(routes.length);
  });
});

describe('visibleNavItems (admin gating)', () => {
  it('drops the Admin item for non-admins and keeps it for admins', () => {
    const admin = GOVERNANCE_NAV.find((s) => s.id === 'admin')!.items;
    expect(visibleNavItems(admin, false).map((i) => i.to)).toEqual([]);
    expect(visibleNavItems(admin, true).map((i) => i.to)).toEqual(['/admin']);
  });
  it('does not filter non-admin groups', () => {
    const inv = GOVERNANCE_NAV.find((s) => s.id === 'inventory')!.items;
    expect(visibleNavItems(inv, false)).toEqual(inv);
  });
});
