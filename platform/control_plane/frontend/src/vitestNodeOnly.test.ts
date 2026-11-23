import { readdirSync, readFileSync } from 'node:fs';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { describe, expect, it } from 'vitest';
import { resolveConfig } from 'vitest/node';

const FRONTEND = fileURLToPath(new URL('..', import.meta.url));
const SCRIPT = /\.(?:ts|tsx|mts|cts|js|jsx|mjs|cjs)$/;
const PRAGMA = /@(?:vitest|jest)-environment\b/;
const WORKSPACE_FILE = /^vitest\.(?:workspace|projects)\./;
const DOM_PACKAGE = /^(?:jsdom|happy-dom|@testing-library\/.+|@vitest\/browser(?:-.+)?)$/;

describe('the resolved Vitest configuration', () => {
  it('the resolved vitest config runs every test file in node', async () => {
    const { vitestConfig, viteConfig } = await resolveConfig({ root: FRONTEND });
    const source = `resolved from ${viteConfig.configFile}`;
    expect(vitestConfig.environment, source).toBe('node');
    expect(vitestConfig.projects ?? [], source).toEqual([]);
    expect(vitestConfig.browser.enabled, source).toBe(false);

    const root = readdirSync(FRONTEND);
    expect(root).toContain('package.json');
    expect(root.filter((name) => WORKSPACE_FILE.test(name))).toEqual([]);

    const src = join(FRONTEND, 'src');
    const scripts = readdirSync(src, { recursive: true, encoding: 'utf8' }).filter((name) => SCRIPT.test(name));
    expect(scripts).toContain('App.tsx');
    const pragmas = scripts
      .filter((name) => {
        const text = readFileSync(join(src, name), 'utf8');
        return text.includes('-environment') && PRAGMA.test(text);
      })
      .map((name) => `src/${name}`);
    expect(pragmas).toEqual([]);

    const manifest = JSON.parse(readFileSync(join(FRONTEND, 'package.json'), 'utf8')) as Record<string, unknown>;
    const named = Object.entries(manifest)
      .filter(([group]) => /dependencies$/i.test(group))
      .flatMap(([, names]) => (Array.isArray(names) ? names : Object.keys(names as object)));
    expect(named).toContain('vitest');
    expect(named.filter((name) => DOM_PACKAGE.test(name))).toEqual([]);

    const lock = readFileSync(join(FRONTEND, 'package-lock.json'), 'utf8');
    expect(lock.match(/"(?:[^"]*\/)?node_modules\/(?:vitest|jsdom|happy-dom)"/g)).toEqual(['"node_modules/vitest"']);
  }, 30_000);
});
