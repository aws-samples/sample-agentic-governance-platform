import { describe, expect, it } from 'vitest';

const sources = Object.entries(
  import.meta.glob<string>('/src/**/*.{ts,tsx}', { query: '?raw', import: 'default', eager: true }),
);

const SPECIFIER = /\b(?:from|import)\s*\(?\s*(['"`])([^'"`]+)\1/g;
const GLOB = /import\.meta\.glob\s*(?:<[^>]*>)?\s*\(\s*(['"`])([^'"`]+)\1/g;
export const SHIPPED_FILE_FLOOR = 200;

function importsIntoSrcTest(file: string, text: string): string[] {
  const specifiers = [...text.matchAll(SPECIFIER)]
    .map((m) => m[2])
    .filter((spec) => spec.startsWith('.') || spec.startsWith('/'))
    .filter((spec) => new URL(spec, `file://${file}`).pathname.startsWith('/src/test/'));
  const globs = [...text.matchAll(GLOB)]
    .map((m) => m[2])
    .filter((pattern) => {
      const dir = new URL(pattern.split(/[*?{]/)[0].replace(/[^/]*$/, ''), `file://${file}`).pathname;
      return '/src/test/'.startsWith(dir) || dir.startsWith('/src/test/');
    });
  return [...specifiers, ...globs];
}

function isShipped(file: string): boolean {
  return !file.endsWith('.test.ts') && !file.startsWith('/src/test/');
}

describe('src/test is reached only from tests', () => {
  it('no file outside src/test and the *.test.ts files imports from src/test', () => {
    const problems = sources
      .filter(([file]) => isShipped(file))
      .flatMap(([file, text]) => importsIntoSrcTest(file, text).map((spec) => `${file} imports ${spec}`));
    expect(
      problems,
      `${problems.join('\n')}\nsrc/test holds test fixtures; a page or module that imports them ships the fixtures in the bundle. Build the value in the module or move the importer into a *.test.ts file.`,
    ).toEqual([]);
  });

  it('a relative, a raw, a dynamic and a glob import into src/test are seen; a sibling named test and a glob elsewhere are not', () => {
    expect(importsIntoSrcTest('/src/components/operations/x.ts', "import { deployment } from '../../test/wire';")).toEqual(['../../test/wire']);
    expect(importsIntoSrcTest('/src/App.tsx', "import raw from './test/wire.ts?raw';")).toEqual(['./test/wire.ts?raw']);
    expect(importsIntoSrcTest('/src/App.tsx', "const m = await import('/src/test/wire');")).toEqual(['/src/test/wire']);
    expect(importsIntoSrcTest('/src/App.tsx', 'const m = await import(`./test/${name}`);')).toEqual(['./test/${name}']);
    expect(importsIntoSrcTest('/src/components/x.ts', "import { a } from './test/wire';")).toEqual([]);
    expect(importsIntoSrcTest('/src/components/x.ts', "import { a } from '../testing/wire';")).toEqual([]);
    expect(importsIntoSrcTest('/src/App.tsx', "const f = import.meta.glob('/src/test/*.ts', { eager: true });")).toEqual(['/src/test/*.ts']);
    expect(importsIntoSrcTest('/src/App.tsx', "const f = import.meta.glob('/src/**/*.{ts,tsx}');")).toEqual(['/src/**/*.{ts,tsx}']);
    expect(importsIntoSrcTest('/src/App.tsx', "const f = import.meta.glob('/src/pages/*.tsx');")).toEqual([]);
    expect(importsIntoSrcTest('/src/App.tsx', "const f = import.meta.glob('./test/*.ts');")).toEqual(['./test/*.ts']);
    expect(importsIntoSrcTest('/src/components/x.ts', "const f = import.meta.glob('./test/*.ts');")).toEqual([]);
  });

  it('the scan saw the source tree', () => {
    expect(sources.filter(([file]) => isShipped(file)).length).toBeGreaterThanOrEqual(SHIPPED_FILE_FLOOR);
  });
});
