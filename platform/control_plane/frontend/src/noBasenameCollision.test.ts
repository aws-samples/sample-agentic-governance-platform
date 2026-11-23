/**
 * No two frontend sources differ only in casing or extension. With `moduleResolution: "bundler"` an
 * extensionless component import probes `.ts` before `.tsx`; on a case-insensitive filesystem the probe
 * for `GitHubLink.ts` is answered by `githubLink.ts`, so the import binds to a module with no default
 * export (`tsc` TS1261 on macOS, a blank page under vite and vitest, nothing at all on the Linux runners).
 * The check is over the file set, which no per-file lint sees: every `.ts`/`.tsx` under `src/` except
 * tests and declarations, keyed by its directory-qualified path minus the extension, lower-cased; a stem
 * with two paths is a collision. Fails at `5bae1d23` (the first pair) and passes at its parent `4286ed1a`.
 * Ladder rung 2, a test that fails on the real past mistake; rung 1, the `*Model.ts` name for a component's
 * logic file so the two stems cannot coexist, is what the six renames under `components/operations` now
 * follow, and the tree carries no pair.
 * What it does not see: the import sites; an extensionless import of a colliding component is caught only
 * by `tsc -b` on a case-insensitive filesystem.
 */
import { describe, expect, it } from 'vitest';

const files = Object.keys(import.meta.glob('/src/**/*.{ts,tsx}'));
export const TS_FILE_FLOOR = 200;

function isSource(path: string): boolean {
  return !/\.(test\.tsx?|d\.ts)$/.test(path);
}

function stem(path: string): string {
  return path.replace(/\.tsx?$/, '').toLowerCase();
}

function collisions(): Map<string, string[]> {
  const byStem = new Map<string, string[]>();
  for (const path of files.filter(isSource)) {
    const key = stem(path);
    byStem.set(key, [...(byStem.get(key) ?? []), path]);
  }
  return new Map([...byStem].filter(([, paths]) => paths.length > 1));
}

describe('source file stems', () => {
  it('no two source files differ only in casing or extension', () => {
    const problems = [...collisions()].map(([, paths]) => {
      const [a, ...rest] = paths;
      const b = rest.join(' and ');
      return `${a} and ${b} differ only in casing or extension; an extensionless import of either binds to the wrong module on macOS (tsc TS1261, a blank page under vite). Rename the logic file to <stem>Model.ts, as repoRowModel.ts does.`;
    });
    expect(problems, problems.join('\n')).toEqual([]);
  });

  it('the scan saw the source tree', () => {
    expect(files.length).toBeGreaterThanOrEqual(TS_FILE_FLOOR);
  });
});
