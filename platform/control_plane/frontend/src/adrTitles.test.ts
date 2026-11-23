import { describe, expect, it } from 'vitest';

const ADRS = import.meta.glob<string>('../../../../docs/adr/frontend-*.md', {
  query: '?raw',
  import: 'default',
  eager: true,
});

const TESTS = import.meta.glob<string>('./**/*.test.ts', { query: '?raw', import: 'default', eager: true });

const TITLE_MIN_WORDS = 4;

function codeSpans(markdown: string): string[] {
  const unwrapped = markdown.replace(/\n[ \t]*/g, ' ');
  return [...unwrapped.matchAll(/``(.+?)``|`([^`]+)`/g)].map((m) => (m[1] ?? m[2]).trim());
}

function looksLikeCode(span: string): boolean {
  return /[/(=<{]/.test(span) || /(?:^|\W)'/.test(span) || /^\S+:/.test(span) || /\d{4}-\d{2}-\d{2}/.test(span);
}

function quotedTitles(markdown: string): string[] {
  return codeSpans(markdown).filter(
    (span) => span.split(/\s+/).length >= TITLE_MIN_WORDS && !looksLikeCode(span),
  );
}

function testTitles(): Set<string> {
  const titles = new Set<string>();
  for (const source of Object.values(TESTS)) {
    for (const m of source.matchAll(/\b(?:it|describe)\(\s*(['"`])((?:\\.|(?!\1).)*)\1/g)) {
      titles.add(m[2].replace(/\\(['"`])/g, '$1'));
    }
  }
  return titles;
}

describe('ADR test titles', () => {
  it('every test title an ADR quotes exists in a test file, so a retitle cannot break the link silently', () => {
    const titles = testTitles();
    const missing: string[] = [];
    let checked = 0;
    for (const [path, markdown] of Object.entries(ADRS)) {
      for (const quoted of quotedTitles(markdown)) {
        checked += 1;
        if (!titles.has(quoted)) missing.push(`${path.replace(/^(\.\.\/)+/, '')}: ${quoted}`);
      }
    }
    expect(Object.keys(ADRS).length).toBeGreaterThan(0);
    expect(checked).toBeGreaterThan(0);
    expect(missing).toEqual([]);
  });
});
