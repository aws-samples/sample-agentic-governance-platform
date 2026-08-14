import { describe, it, expect } from 'vitest';
import { GOVERNANCE_NAV, allRoutes, visibleNavItems } from './agentsNav';
import { OPERATIONS_NAV } from '../operations/operationsNav';

/** Every leaf route in a nav config that carries `comingSoon: true`, in declaration order. */
function comingSoonRoutes(nav: typeof GOVERNANCE_NAV): string[] {
  return nav.flatMap((sec) => sec.items.filter((i) => i.comingSoon).map((i) => i.to));
}

describe('GOVERNANCE_NAV (Epic 15 IA)', () => {
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

  it('Inventory holds agents, tools, model registry and the graph', () => {
    const sec = GOVERNANCE_NAV.find((s) => s.id === 'inventory')!;
    expect(sec.items.map((i) => i.to)).toEqual([
      '/agents', '/tools-mcp', '/govern/models', '/governance-graph',
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

  it('renames the Inventory tools + models labels (Epic 15b)', () => {
    const inv = GOVERNANCE_NAV.find((s) => s.id === 'inventory')!;
    expect(inv.items.find((i) => i.to === '/tools-mcp')!.label).toBe('MCPs');
    expect(inv.items.find((i) => i.to === '/govern/models')!.label).toBe('LLM Gateway');
  });
});

describe('comingSoon flags (Epic 31F)', () => {
  it('flags exactly the three not-yet-live governance destinations', () => {
    expect(comingSoonRoutes(GOVERNANCE_NAV)).toEqual([
      '/govern/models', '/govern/audit', '/govern/finops',
    ]);
  });

  it('never flags Guardrails & Policy — it is a real surface', () => {
    const sec = GOVERNANCE_NAV.find((s) => s.id === 'security-policy')!;
    expect(sec.items.find((i) => i.to === '/secure/guardrails')!.comingSoon).toBeUndefined();
  });

  it('flags exactly the six operations mock destinations', () => {
    expect(comingSoonRoutes(OPERATIONS_NAV)).toEqual([
      '/ops/studio', '/ops/experiments', '/ops/playground',
      '/ops/deployments', '/ops/access', '/ops/models',
    ]);
  });

  it('flags every item under the operations Coming Soon group and nothing outside it', () => {
    const group = OPERATIONS_NAV.find((s) => s.id === 'coming-soon')!;
    expect(group.items.every((i) => i.comingSoon === true)).toBe(true);
    for (const sec of OPERATIONS_NAV.filter((s) => s.id !== 'coming-soon')) {
      for (const item of sec.items) expect(item.comingSoon).toBeUndefined();
    }
  });
});

describe('allRoutes (Epic 15)', () => {
  it('omits /prompts (hidden from nav this epic) and includes the new canonical paths', () => {
    const routes = allRoutes();
    expect(routes).not.toContain('/prompts');
    expect(routes).toContain('/marketplace');
    expect(routes).toContain('/admin');
  });
  it('keeps the relocated routes reachable', () => {
    const routes = allRoutes();
    for (const r of ['/govern/models', '/govern/audit', '/govern/finops', '/observability']) {
      expect(routes).toContain(r);
    }
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
