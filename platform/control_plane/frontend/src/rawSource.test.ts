import { describe, expect, it } from 'vitest';

import { stripComments } from './rawSource';

describe('stripComments', () => {
  it('drops a line comment', () => {
    const out = stripComments(['const a = 1; // trailing note', '// a whole line', 'const b = 2;'].join('\n'));
    expect(out).not.toContain('trailing note');
    expect(out).not.toContain('a whole line');
    expect(out).toContain('const a = 1;');
    expect(out).toContain('const b = 2;');
  });

  it('drops a block comment across lines', () => {
    const out = stripComments(
      ['const a = 1;', '/* first line', ' * second line', ' */', 'const b = 2; /* inline */ const c = 3;'].join('\n'),
    );
    expect(out).not.toContain('first line');
    expect(out).not.toContain('second line');
    expect(out).not.toContain('inline');
    expect(out).toContain('const a = 1;');
    expect(out).toContain('const b = 2;');
    expect(out).toContain('const c = 3;');
  });

  it('keeps a // inside a string literal', () => {
    const out = stripComments(
      ["const sep = 'a//b'; // after", 'const path = "M0 0 // M1 1";', 'const t = `x//y`;'].join('\n'),
    );
    expect(out).toContain("const sep = 'a//b';");
    expect(out).toContain('const path = "M0 0 // M1 1";');
    expect(out).toContain('const t = `x//y`;');
    expect(out).not.toContain('after');
  });

  it('keeps a /* inside a template literal', () => {
    const out = stripComments(
      [
        'const glob = `src/**/*.ts`;',
        'const label = `${name} /* ${kind} */`;',
        '/* a real block */',
        "const quoted = '../**/*.{ts,tsx}';",
        'const after = 1;',
      ].join('\n'),
    );
    expect(out).toContain('const glob = `src/**/*.ts`;');
    expect(out).toContain('const label = `${name} /* ${kind} */`;');
    expect(out).toContain("const quoted = '../**/*.{ts,tsx}';");
    expect(out).toContain('const after = 1;');
    expect(out).not.toContain('a real block');
  });

  it('parses every source as TSX, so a generic arrow <T,>(x: T) => x keeps its comment ranges right', () => {
    expect(stripComments('const f = <T,>(x: T) => x; // c')).toBe('const f = <T,>(x: T) => x; ');
  });

  it('keeps every newline, so line-anchored guards align', () => {
    const src = [
      'const a = 1;',
      '/* a block',
      '   spanning',
      '   three lines */',
      '<div>',
      '  {/* a JSX comment on its own line */}',
      '</div>;',
      'const s = "*/ inside a string";',
      '// at the end of the file, no newline after',
    ].join('\n');
    const out = stripComments(src);
    expect(out.split('\n')).toHaveLength(src.split('\n').length);
    expect(out).not.toContain('spanning');
    expect(out).not.toContain('JSX comment');
    expect(out).not.toContain('end of the file');
    expect(out).toContain('const s = "*/ inside a string";');
  });

  it('leaves code lines unchanged', () => {
    const src = [
      "import { a } from './a';",
      'export function f(x: number): number {',
      '  return x * 2 / a;',
      '}',
      '',
      'const ratio = f(4) / f(2);',
    ].join('\n');
    expect(stripComments(src)).toBe(src);
  });

  it('keeps a regex literal containing //, /* or a backtick, and JSX text with an apostrophe', () => {
    const out = stripComments(
      [
        "const host = config.workspace_url.replace(/^https:\\/\\//, '');",
        'const INLINE = /\\*\\*([^*]+)\\*\\*|`([^`]+)`/g;',
        '// a real comment that must go',
        "<p>Don't click {/* hidden */} here</p>",
      ].join('\n'),
    );
    expect(out).toContain("const host = config.workspace_url.replace(/^https:\\/\\//, '');");
    expect(out).toContain('const INLINE = /\\*\\*([^*]+)\\*\\*|`([^`]+)`/g;');
    expect(out).not.toContain('a real comment that must go');
    expect(out).not.toContain('hidden');
    expect(out).toContain("Don't click");
    expect(out).toContain('here');
  });
});
