/**
 * What the census does not see: a phrase other than `soon` split across literals or a `<br />`, a lone `Soon.`,
 * an `&nbsp;` entity in JSX text, a zero-width space between the words, a regex literal, and an escape in a file
 * that the raw-text prefilter skips.
 */
import ts from 'typescript';
import { describe, expect, it } from 'vitest';
import {
  COMING_SOON_BODY,
  COMING_SOON_TITLE,
  SAMPLE_BADGE_LABEL,
  SOON_TAG_LABEL,
} from './comingSoonCopy';

interface Site {
  path: string;
  line: number;
  text: string;
}

const COPY_MODULE = '/src/components/shared/comingSoonCopy.ts';
const COPY_CONSUMERS = [
  '/src/components/operations/repo-tabs/EvaluationsTab.tsx',
  '/src/components/Sidebar.tsx',
  '/src/components/governance/marketplace/marketplaceCardStyles.ts',
];
const LONE_WORD = 'soon';
const HONESTY_PHRASES = ['coming soon', 'not functional yet', 'sample data', 'example design'];

function censused(path: string): boolean {
  return path !== COPY_MODULE && !/\.test\.tsx?$/.test(path);
}

function mayHoldHonestyCopy(source: string): boolean {
  const lowered = source.toLowerCase();
  return [LONE_WORD, ...HONESTY_PHRASES].some((phrase) => phrase.split(' ').some((word) => lowered.includes(word)));
}

function spellsHonestyCopy(text: string): boolean {
  const lowered = text.toLowerCase();
  return lowered === LONE_WORD || HONESTY_PHRASES.some((phrase) => lowered.includes(phrase));
}

function census(sources: Record<string, string>): Site[] {
  return Object.entries(sources)
    .filter(([, source]) => mayHoldHonestyCopy(source))
    .flatMap(([path, source]) => {
      const file = ts.createSourceFile(path, source, ts.ScriptTarget.Latest, true);
      const sites: Site[] = [];
      const visit = (node: ts.Node) => {
        if (ts.isStringLiteral(node) || ts.isTemplateLiteralToken(node) || ts.isJsxText(node)) {
          const text = node.text.replace(/\s+/g, ' ').trim();
          if (spellsHonestyCopy(text)) {
            sites.push({ path, line: file.getLineAndCharacterOfPosition(node.getStart(file)).line + 1, text });
          }
        }
        ts.forEachChild(node, visit);
      };
      visit(file);
      return sites;
    });
}

describe('honesty copy constants', () => {
  it('COMING_SOON_TITLE is the pinned banner title', () => {
    expect(COMING_SOON_TITLE).toBe('Coming soon');
  });

  it('COMING_SOON_BODY is the pinned banner body', () => {
    expect(COMING_SOON_BODY).toBe('Example design — not functional yet.');
  });

  it('SAMPLE_BADGE_LABEL is the pinned badge label', () => {
    expect(SAMPLE_BADGE_LABEL).toBe('Sample data');
  });

  it('SOON_TAG_LABEL is the pinned nav-row tag label', () => {
    expect(SOON_TAG_LABEL).toBe('soon');
  });
});

describe('honesty copy hygiene', () => {
  it('no string is empty or padded with whitespace', () => {
    for (const s of [COMING_SOON_TITLE, COMING_SOON_BODY, SAMPLE_BADGE_LABEL, SOON_TAG_LABEL]) {
      expect(s.length).toBeGreaterThan(0);
      expect(s).toBe(s.trim());
    }
  });

  it('the body uses a real em dash, not a hyphen', () => {
    expect(COMING_SOON_BODY).toContain('—');
    expect(COMING_SOON_BODY).not.toContain(' - ');
  });
});

describe('honesty copy spelling', () => {
  it('the census reads string literals, template parts and JSX text in any file holding one phrase, skips comments and identifiers, and spares only the copy module and test files', () => {
    const plant = [
      "const title = 'Coming soon';",
      'const plain = `sample data`;',
      'const head = `Example design — ${name}`;',
      'const middle = `${a} coming soon ${b}`;',
      'const tail = `${name}: not functional yet`;',
      'const badge = <span>Sample data</span>;',
      'const tag = <i> soon </i>;',
      'const label = <s aria-label="COMING SOON" />;',
      'const split = <h3>Coming',
      '  soon</h3>;',
      '// coming soon',
      '/* sample data */',
      'const soonest = sooner;',
      "const near = 'soonest';",
    ].join('\n');
    expect(census({ '/src/plant.tsx': plant }).map(({ line, text }) => [line, text])).toEqual([
      [1, 'Coming soon'],
      [2, 'sample data'],
      [3, 'Example design —'],
      [4, 'coming soon'],
      [5, ': not functional yet'],
      [6, 'Sample data'],
      [7, 'soon'],
      [8, 'COMING SOON'],
      [9, 'Coming soon'],
    ]);
    const alone = {
      '/src/coming.ts': "'Coming soon';",
      '/src/functional.tsx': '<p>Not functional yet</p>;',
      '/src/sample.ts': "'Sample data';",
      '/src/example.ts': '`Example design`;',
      '/src/soon.tsx': '<i>soon</i>;',
      '/src/escaped.ts': "'Sample d\\u0061ta';",
    };
    expect(census(alone).map(({ path, text }) => [path, text])).toEqual([
      ['/src/coming.ts', 'Coming soon'],
      ['/src/functional.tsx', 'Not functional yet'],
      ['/src/sample.ts', 'Sample data'],
      ['/src/example.ts', 'Example design'],
      ['/src/soon.tsx', 'soon'],
      ['/src/escaped.ts', 'Sample data'],
    ]);
    const paths = ['/src/a.ts', '/src/a.tsx', '/src/test/wire.ts', '/src/a.test.ts', '/src/a.test.tsx', COPY_MODULE];
    expect(paths.filter(censused)).toEqual(['/src/a.ts', '/src/a.tsx', '/src/test/wire.ts']);
  });

  it('no source outside comingSoonCopy.ts spells the honesty copy', () => {
    const sources = import.meta.glob<string>('/src/**/*.{ts,tsx}', { query: '?raw', import: 'default', eager: true });
    expect(
      census({ [COPY_MODULE]: sources[COPY_MODULE] ?? '' }).map(({ text }) => text),
      `the census reads every frozen string in ${COPY_MODULE}`,
    ).toEqual(expect.arrayContaining([COMING_SOON_TITLE, COMING_SOON_BODY, SAMPLE_BADGE_LABEL, SOON_TAG_LABEL]));
    const outside = Object.fromEntries(Object.entries(sources).filter(([path]) => censused(path)));
    const read = Object.keys(outside);
    expect(read, `the census reads ${COPY_CONSUMERS.join(', ')}`).toEqual(expect.arrayContaining(COPY_CONSUMERS));
    expect(
      read.filter((path) => path === COPY_MODULE || /\.test\.tsx?$/.test(path)),
      'the census spares the copy module and every test file',
    ).toEqual([]);
    const sites = census(outside).map(({ path, line, text }) => `${path}:${line} ${JSON.stringify(text)}`);
    expect(sites, `spell the honesty copy through ${COPY_MODULE}:\n${sites.join('\n')}`).toEqual([]);
  }, 30_000);
});
