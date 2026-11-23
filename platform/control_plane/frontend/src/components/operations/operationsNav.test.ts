import { describe, it, expect } from 'vitest';
import { OPERATIONS_NAV, operationsRoutes } from './operationsNav';
import { MOCK_ROUTES, PAGE_PATHS, REDIRECTS } from '../../routes';

export const DECLARED_ROUTE_FLOOR = 20;

function declaredRoutes(): string[] {
  return [...PAGE_PATHS, ...Object.keys(REDIRECTS)];
}

describe('OPERATIONS_NAV', () => {
  it('has the expected section ids in order, and no Coming Soon group', () => {
    expect(OPERATIONS_NAV.map((s) => s.id)).toEqual(['overview', 'primary']);
  });
  it('overview is standalone (headerless, non-collapsible, single item at /ops)', () => {
    const sec = OPERATIONS_NAV.find((s) => s.id === 'overview')!;
    expect(sec.label).toBe('');
    expect(sec.collapsible).toBe(false);
    expect(sec.items).toHaveLength(1);
    expect(sec.items[0].to).toBe('/ops');
  });
  it('collapsible groups have a non-empty label', () => {
    for (const sec of OPERATIONS_NAV.filter((s) => s.collapsible)) {
      expect(sec.label.length).toBeGreaterThan(0);
    }
  });

  it('leads with the two objects an operator builds, Agents and MCP servers, then Studio, Projects, Templates, Settings', () => {
    const sec = OPERATIONS_NAV.find((s) => s.id === 'primary')!;
    expect(sec.items.map((i) => i.label)).toEqual([
      'Agents', 'MCP servers', 'Studio', 'Projects', 'Templates', 'Settings',
    ]);
    expect(sec.items.map((i) => i.to)).toEqual([
      '/ops/agents', '/ops/mcp-servers', '/ops/studio', '/ops/projects', '/ops/templates', '/ops/settings',
    ]);
  });
  it('has no Repos entry — the repo is the object\'s Source field, and the fleet-wide list keeps only its route for old links', () => {
    expect(operationsRoutes()).not.toContain('/ops/repositories');
    expect(declaredRoutes()).toContain('/ops/repositories');
    expect(declaredRoutes()).toContain('/ops/repositories/:id');
  });
  it('primary is headerless + non-collapsible (the real tabs are never hidden)', () => {
    const sec = OPERATIONS_NAV.find((s) => s.id === 'primary')!;
    expect(sec.label).toBe('');
    expect(sec.collapsible).toBe(false);
  });

  it('no mock destination (the pure-fixture pages) has a nav entry, and no item is SOON-tagged: a fake surface in the menu undercuts the real-infra rule', () => {
    for (const r of MOCK_ROUTES) expect(operationsRoutes()).not.toContain(r);
    for (const sec of OPERATIONS_NAV) for (const item of sec.items) expect(item.comingSoon).toBeUndefined();
  });

  it('every nav item has an icon and a label', () => {
    for (const sec of OPERATIONS_NAV) for (const item of sec.items) {
      expect(item.icon.length).toBeGreaterThan(0);
      expect(item.label.length).toBeGreaterThan(0);
    }
  });
  it('no operations item is adminOnly', () => {
    for (const sec of OPERATIONS_NAV) for (const item of sec.items) {
      expect(item.adminOnly).toBeUndefined();
    }
  });
});

describe('nav ↔ route table agreement', () => {
  it('the route table holds the pages (guards the assertions below against an empty table)', () => {
    expect(PAGE_PATHS.length).toBeGreaterThan(DECLARED_ROUTE_FLOOR);
    expect(PAGE_PATHS).toContain('/ops');
  });

  it('every nav `to` resolves to a route in the route table', () => {
    const declared = new Set(declaredRoutes());
    const missing = operationsRoutes().filter((r) => !declared.has(r));
    expect(missing).toEqual([]);
  });

  it('/ops/admin still resolves — kept as a redirect to /ops/settings', () => {
    expect(REDIRECTS['/ops/admin']).toBe('/ops/settings');
  });
});

describe('operationsRoutes', () => {
  it('returns every leaf route, all under /ops, deduped, in nav order', () => {
    expect(operationsRoutes()).toEqual([
      '/ops',
      '/ops/agents', '/ops/mcp-servers', '/ops/studio', '/ops/projects', '/ops/templates', '/ops/settings',
    ]);
    const routes = operationsRoutes();
    expect(new Set(routes).size).toBe(routes.length);
    expect(routes.every((r) => r === '/ops' || r.startsWith('/ops/'))).toBe(true);
  });
});
