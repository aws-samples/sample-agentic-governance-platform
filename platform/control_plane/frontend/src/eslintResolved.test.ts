import { ESLint, type Linter } from 'eslint';
import { readdirSync } from 'node:fs';
import { join, relative } from 'node:path';
import { fileURLToPath } from 'node:url';
import { isDeepStrictEqual } from 'node:util';
import { describe, expect, it } from 'vitest';

const FRONTEND = fileURLToPath(new URL('..', import.meta.url));
const SCRIPT = /\.(?:ts|tsx|mts|cts|js|jsx|mjs|cjs)$/;
const TYPESCRIPT = /\.(?:ts|tsx|mts|cts)$/;
const COMMENTS = '@eslint-community/eslint-comments';
const RESOLVED: Record<string, unknown> = {
  [`${COMMENTS}/require-description`]: [2, { ignore: [] }],
  'agp/suppression-tail': [2],
  [`${COMMENTS}/no-restricted-disable`]: [2, 'agp/*', `${COMMENTS}/*`],
  [`${COMMENTS}/no-use`]: [
    2,
    { allow: ['eslint-disable', 'eslint-disable-line', 'eslint-disable-next-line', 'eslint-enable'] },
  ],
};
const RESOLVED_TYPESCRIPT: Record<string, unknown> = { '@typescript-eslint/ban-ts-comment': [2] };
const META_FIELDS = new Set(['name', 'basePath']);
const [config] = Object.values(
  import.meta.glob<Linter.Config[]>('/eslint.config.js', { eager: true, import: 'default' }),
);

function sources(root: string, findings: string[], dir = root): string[] {
  return readdirSync(dir, { withFileTypes: true }).flatMap((entry) => {
    const path = join(dir, entry.name);
    const name = relative(root, path);
    if (entry.name === 'node_modules' || name === 'dist') return [];
    if (entry.isSymbolicLink()) {
      findings.push(`${name}: a symlink hides its target from this test`);
      return [];
    }
    if (entry.isDirectory()) return sources(root, findings, path);
    return entry.isFile() && SCRIPT.test(entry.name) ? [name] : [];
  });
}

describe('the resolved ESLint configuration', () => {
  it('every linted source resolves the suppression rules at error, and only dist is ignored', async () => {
    const eslint = new ESLint({ cwd: FRONTEND });
    const findings: string[] = [];
    const files = sources(FRONTEND, findings);
    expect(files).toEqual(
      expect.arrayContaining([
        'eslint.config.js',
        'postcss.config.js',
        'eslint-rules/suppressionTail.js',
        'src/App.tsx',
      ]),
    );
    for (const name of files) {
      const path = join(FRONTEND, name);
      if (await eslint.isPathIgnored(path)) {
        findings.push(`${name}: ignored, or matched by no files block`);
        continue;
      }
      const resolved: Linter.Config = await eslint.calculateConfigForFile(path);
      const expected = TYPESCRIPT.test(name) ? { ...RESOLVED, ...RESOLVED_TYPESCRIPT } : RESOLVED;
      for (const [rule, value] of Object.entries(expected)) {
        const actual = resolved.rules?.[rule];
        if (!isDeepStrictEqual(actual, value)) {
          findings.push(`${name}: ${rule} resolves to ${JSON.stringify(actual)}`);
        }
      }
      const unused = resolved.linterOptions?.reportUnusedDisableDirectives;
      if (unused !== 2) findings.push(`${name}: reportUnusedDisableDirectives resolves to ${JSON.stringify(unused)}`);
    }
    const ignored = config
      .filter((block) => block.ignores && Object.keys(block).filter((key) => !META_FIELDS.has(key)).length === 1)
      .flatMap((block) => block.ignores ?? []);
    expect(findings, findings.join('\n')).toEqual([]);
    expect(ignored).toEqual(['dist']);
  }, 30_000);

  it('src holds TypeScript sources only', () => {
    const findings: string[] = [];
    const strays = sources(FRONTEND, findings, join(FRONTEND, 'src')).filter((name) => !TYPESCRIPT.test(name));
    expect([...findings, ...strays]).toEqual([]);
  }, 30_000);
});
