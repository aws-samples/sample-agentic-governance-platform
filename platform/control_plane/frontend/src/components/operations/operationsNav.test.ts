import { describe, it, expect } from 'vitest';
import { OPERATIONS_NAV, operationsRoutes } from './operationsNav';
// Vite's `?raw` rather than `node:fs`: `tsconfig.app.json` sets `types: ["vite/client"]`
// (no `node`), so `node:fs` has no type declarations here — while `vite/client`
// declares `*?raw`. Same idiom as githubLinkApi.test.ts.
import appSrc from '../../App.tsx?raw';

// The six pages that are still pure fixtures (demoData.ts / demoStore.tsx /
// setTimeout). E28/D12 demotes them in the nav; none is deleted.
const MOCK_ROUTES = [
  '/ops/studio',
  '/ops/experiments',
  '/ops/playground',
  '/ops/deployments',
  '/ops/access',
  '/ops/models',
];

/**
 * Every `path="..."` declared in App.tsx. Read from the real source instead of a
 * hand-copied literal so "the nav points at a route that exists" is an actual
 * cross-file check: deleting a <Route> or fat-fingering a nav `to` fails here.
 */
function declaredRoutes(): string[] {
  return [...appSrc.matchAll(/<Route\s+path="([^"]+)"/g)].map((m) => m[1]);
}

describe('OPERATIONS_NAV', () => {
  it('has the expected section ids in order (E28: 4 real tabs, then Coming Soon)', () => {
    expect(OPERATIONS_NAV.map((s) => s.id)).toEqual([
      'overview', 'primary', 'coming-soon',
    ]);
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

  it('has exactly 4 primary entries: Repos, Projects, Templates, Settings', () => {
    const sec = OPERATIONS_NAV.find((s) => s.id === 'primary')!;
    expect(sec.items).toHaveLength(4);
    expect(sec.items.map((i) => i.label)).toEqual([
      'Repos', 'Projects', 'Templates', 'Settings',
    ]);
    expect(sec.items.map((i) => i.to)).toEqual([
      '/ops/repositories', '/ops/projects', '/ops/templates', '/ops/settings',
    ]);
  });
  it('primary is headerless + non-collapsible (the real tabs are never hidden)', () => {
    const sec = OPERATIONS_NAV.find((s) => s.id === 'primary')!;
    expect(sec.label).toBe('');
    expect(sec.collapsible).toBe(false);
  });

  it('all 6 mock destinations live in coming-soon, and nothing else does', () => {
    const sec = OPERATIONS_NAV.find((s) => s.id === 'coming-soon')!;
    expect(sec.label).toBe('Coming Soon');
    expect(sec.collapsible).toBe(true);
    expect([...sec.items.map((i) => i.to)].sort()).toEqual([...MOCK_ROUTES].sort());
  });
  it('no mock destination leaks back into a non-coming-soon group', () => {
    for (const sec of OPERATIONS_NAV.filter((s) => s.id !== 'coming-soon')) {
      for (const item of sec.items) expect(MOCK_ROUTES).not.toContain(item.to);
    }
  });

  it('every nav item has an icon and a label', () => {
    for (const sec of OPERATIONS_NAV) for (const item of sec.items) {
      expect(item.icon.length).toBeGreaterThan(0);
      expect(item.label.length).toBeGreaterThan(0);
    }
  });
  it('no operations item is adminOnly (no gating this epic)', () => {
    for (const sec of OPERATIONS_NAV) for (const item of sec.items) {
      expect(item.adminOnly).toBeUndefined();
    }
  });
});

describe('nav ↔ router agreement', () => {
  it('parses App.tsx (guards the assertions below against a silent empty match)', () => {
    const routes = declaredRoutes();
    expect(routes.length).toBeGreaterThan(20);
    expect(routes).toContain('/ops');
  });

  it('every nav `to` resolves to a route declared in App.tsx', () => {
    const declared = new Set(declaredRoutes());
    const missing = operationsRoutes().filter((r) => !declared.has(r));
    expect(missing).toEqual([]);
  });

  it('/ops/admin still resolves — kept as a redirect to /ops/settings', () => {
    expect(declaredRoutes()).toContain('/ops/admin');
    expect(appSrc).toMatch(
      /<Route\s+path="\/ops\/admin"\s+element=\{<Navigate\s+to="\/ops\/settings"\s+replace\s*\/>\}\s*\/>/,
    );
  });
});

describe('operationsRoutes', () => {
  it('returns every leaf route, all under /ops, deduped, in nav order', () => {
    expect(operationsRoutes()).toEqual([
      '/ops',
      '/ops/repositories', '/ops/projects', '/ops/templates', '/ops/settings',
      '/ops/studio', '/ops/experiments', '/ops/playground',
      '/ops/deployments', '/ops/access', '/ops/models',
    ]);
    const routes = operationsRoutes();
    expect(new Set(routes).size).toBe(routes.length);
    expect(routes.every((r) => r === '/ops' || r.startsWith('/ops/'))).toBe(true);
  });
});
