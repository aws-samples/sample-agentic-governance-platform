import { describe, expect, it } from 'vitest';

import { stripComments } from './rawSource';

interface Density {
  nonBlank: number;
  commentOnly: number;
}

const CEILING_RATIO = 0.1;
const JUDGED_ABOVE = 10;

const JSX_COMMENT_EDGE = /^\{\/\*|\*\/\}$/;

function measure(text: string): Density {
  const stripped = stripComments(text).split('\n');
  let nonBlank = 0;
  let commentOnly = 0;
  text.split('\n').forEach((line, i) => {
    const raw = line.trim();
    if (raw === '') return;
    nonBlank += 1;
    const code = stripped[i].trim();
    if (code === raw) return;
    if (code === '' || code === '{}' || ((code === '{' || code === '}') && JSX_COMMENT_EDGE.test(raw))) commentOnly += 1;
  });
  return { nonBlank, commentOnly };
}

function aboveCeiling({ nonBlank, commentOnly }: Density): boolean {
  return commentOnly > JUDGED_ABOVE && commentOnly / nonBlank > CEILING_RATIO;
}

function lines(...rows: string[]): string {
  return rows.join('\n');
}

describe('measure', () => {
  it('counts a block comment across lines', () => {
    const text = lines('/**', ' * The opening line, the continuation and the closer are comment-only.', ' */', 'export const a = 1;');
    expect(measure(text)).toEqual({ nonBlank: 4, commentOnly: 3 });
  });

  it('does not count a trailing comment', () => {
    const text = lines('const a = 1; // trailing', 'const b = 2; /* also trailing */', 'const c = a + b;');
    expect(measure(text)).toEqual({ nonBlank: 3, commentOnly: 0 });
  });

  it('counts a JSX comment line', () => {
    const text = lines('<div>', '  {/* hidden */}', '  {/*', '    spanning two lines', '  */}', '  <span>text</span>', '</div>');
    expect(measure(text)).toEqual({ nonBlank: 7, commentOnly: 4 });
  });

  it('treats a line inside a template literal as code', () => {
    const text = lines(
      'const s = `',
      '// not a comment',
      '/* nor this */',
      '`;',
      "const t = 'quoted // slashes' + `inline ${s} // tail`;",
      '// a real comment after the literal closed',
    );
    expect(measure(text)).toEqual({ nonBlank: 6, commentOnly: 1 });
  });

  it('counts a comment line after a regex literal carrying an odd backtick: a regex is not a template', () => {
    const text = lines('const TICK = /^`/;', '// a comment line', 'export const a = 1;');
    expect(measure(text)).toEqual({ nonBlank: 3, commentOnly: 1 });
  });

  it('does not open a block on /* inside a regex character class', () => {
    const text = lines('const STAR = /[/*]+/g;', 'export const a = 1;', 'export const b = 2;');
    expect(measure(text)).toEqual({ nonBlank: 3, commentOnly: 0 });
  });

  it('passes a file under the eleven-line floor', () => {
    const comments = Array.from({ length: 10 }, (_, i) => `// comment ${i}`);
    const code = Array.from({ length: 10 }, (_, i) => `export const v${i} = ${i};`);
    const tenAtHalf = measure(lines(...comments, ...code));
    expect(tenAtHalf).toEqual({ nonBlank: 20, commentOnly: 10 });
    expect(aboveCeiling(tenAtHalf)).toBe(false);
    const elevenAtHalf = measure(lines(...comments, '// comment 10', ...code, 'export const v10 = 10;'));
    expect(elevenAtHalf).toEqual({ nonBlank: 22, commentOnly: 11 });
    expect(aboveCeiling(elevenAtHalf)).toBe(true);
  });
});

describe('src', () => {
  it('no non-test file is above 10% with more than ten comment lines', () => {
    const files = Object.entries(
      import.meta.glob<string>('/src/**/*.{ts,tsx}', { query: '?raw', import: 'default', eager: true }),
    ).filter(([file]) => !file.endsWith('.test.ts') && !file.endsWith('/vite-env.d.ts'));
    expect(files.length, 'the glob reached no source file').toBeGreaterThan(0);
    const problems = files.flatMap(([file, text]) => {
      const density = measure(text);
      if (!aboveCeiling(density)) return [];
      const pct = Math.round((100 * density.commentOnly) / density.nonBlank);
      return [
        `${file}: ${density.commentOnly} comment lines in ${density.nonBlank} (${pct}%). Move the knowledge: an invariant to a test name or a type, a UI trap to the feature map's Gotchas, a decision to docs/adr/, history to the commit; delete narration.`,
      ];
    });
    expect(problems, problems.join('\n')).toEqual([]);
  }, 30_000);
});
