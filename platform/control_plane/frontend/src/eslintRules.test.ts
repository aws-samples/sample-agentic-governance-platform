import comments from '@eslint-community/eslint-plugin-eslint-comments/configs';
import { Linter, type Rule, type SourceCode } from 'eslint';
import { mkdirSync, mkdtempSync, readdirSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join, relative } from 'node:path';
import { fileURLToPath } from 'node:url';
import tseslint from 'typescript-eslint';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import suppressionTail, { checkTail } from '../eslint-rules/suppressionTail.js';

const TODAY = new Date(2026, 10, 4);
const TAIL = 'reason; expires 2026-12-04; added E54/T2';
const RESTRICT = '@eslint-community/eslint-comments/no-restricted-disable';
const NO_USE = '@eslint-community/eslint-comments/no-use';
const DIRECTIVE_KINDS = ['eslint-disable', 'eslint-disable-line', 'eslint-disable-next-line', 'eslint-enable'];
const INLINE_CONFIG_KINDS = ['eslint', 'eslint-env', 'exported', 'global', 'globals'];
const DIRECTIVE_WORD = /\b(?:eslint|exported|globals?)\b/;
const FRONTEND = fileURLToPath(new URL('..', import.meta.url));
export const LINTED_FILE_FLOOR = 300;
const [realConfig] = Object.values(
  import.meta.glob<Linter.Config[]>('/eslint.config.js', { eager: true, import: 'default' }),
);
const realBlock = realConfig.find((block) => block.rules?.['agp/suppression-tail'] !== undefined);

type CommentNode = { value: string; loc: { start: { line: number } }; range: [number, number] };
type DirectiveSource = SourceCode & {
  getDisableDirectives(): { directives: { value: string; node: CommentNode }[] };
  getInlineConfigNodes(): CommentNode[];
};

function linted(root: string, links: string[], dir = root): string[] {
  return readdirSync(dir, { withFileTypes: true }).flatMap((entry) => {
    const path = join(dir, entry.name);
    const name = relative(root, path);
    if (name === 'node_modules') return [];
    if (entry.isSymbolicLink()) {
      links.push(`${name}: a symlink hides its target from this test`);
      return [];
    }
    if (entry.name === 'node_modules' || name === 'dist') return [];
    if (entry.isDirectory()) return linted(root, links, path);
    return entry.isFile() && /\.(ts|tsx|mts|cts|js|mjs|cjs)$/.test(entry.name) ? [path] : [];
  });
}

describe('checkTail', () => {
  it('passes a reason, an expiry and an origin', () => {
    expect(checkTail('fixed argv; expires 2027-01-01; added E54/T2', TODAY)).toBeNull();
    expect(checkTail(`  ${TAIL}  `, TODAY)).toBeNull();
  });

  it('asks for the tail when the reason stands alone', () => {
    expect(checkTail('fixed argv', TODAY)).toBe('sayWhy');
    expect(checkTail('fixed argv; expires 2027-01-01', TODAY)).toBe('sayWhy');
    expect(checkTail('fixed argv; git from PATH; expires 2027-01-01; added E54/T2', TODAY)).toBe('sayWhy');
    expect(checkTail('fixed argv; Expires 2027-01-01; added E54/T2', TODAY)).toBe('sayWhy');
    expect(checkTail('fixed argv; expires 2027-01-01; Added E54/T2', TODAY)).toBe('sayWhy');
  });

  it('refuses a band-aid reason or origin', () => {
    for (const reason of ['temporary until the wrapper lands', 'a workaround', 'TODO revisit', 'fallback for now']) {
      expect(checkTail(`${reason}; expires 2027-01-01; added E54/T2`, TODAY), reason).toBe('bandAid');
    }
    for (const origin of ['hotfix/quick-hack', 'hotfix/a-workaround', 'hotfix/todo']) {
      expect(checkTail(`fixed argv; expires 2027-01-01; added ${origin}`, TODAY), origin).toBe('bandAid');
    }
  });

  it('passes every origin shape and asks for the tail on an approval or a malformed origin', () => {
    const origins = [
      'E54/T5',
      'E52A/T14a',
      'E54/T3B',
      'E49A-50A/T17',
      'E54/review',
      'E54/fold',
      'E51/merge',
      'hotfix/frontend-scan',
    ];
    for (const origin of origins) {
      expect(checkTail(`fixed argv; expires 2027-01-01; added ${origin}`, TODAY), origin).toBeNull();
    }
    const refused = [
      'approved jcastans',
      'added jcastans',
      'added E54',
      'added E54/',
      'added hotfix/Upper',
      'added E54/T5 extra',
      'added E54/livefix',
      'Added E54/T5',
      'added by E54/T5',
      'added',
    ];
    for (const tail of refused) {
      expect(checkTail(`fixed argv; expires 2027-01-01; ${tail}`, TODAY), tail).toBe('sayWhy');
    }
  });

  it('refuses a date that is not a calendar day', () => {
    expect(checkTail('fixed argv; expires 2027-02-30; added E54/T2', TODAY)).toBe('sayWhy');
    expect(checkTail('fixed argv; expires 2027-13-01; added E54/T2', TODAY)).toBe('sayWhy');
  });

  it('passes an expiry of today and refuses yesterday', () => {
    expect(checkTail('fixed argv; expires 2026-11-04; added E54/T2', TODAY)).toBeNull();
    expect(checkTail('fixed argv; expires 2026-11-03; added E54/T2', TODAY)).toBe('expired');
  });

  it('passes an expiry 365 days out and refuses 366', () => {
    expect(checkTail('fixed argv; expires 2027-11-04; added E54/T2', TODAY)).toBeNull();
    expect(checkTail('fixed argv; expires 2027-11-05; added E54/T2', TODAY)).toBe('tooFar');
  });
});

describe('agp/suppression-tail under the Linter', () => {
  const linter = new Linter();
  const config: Linter.Config[] = [
    {
      files: ['**/*.ts'],
      languageOptions: { parser: tseslint.parser },
      plugins: { agp: { rules: { 'suppression-tail': suppressionTail } }, ...comments.recommended.plugins },
      linterOptions: { reportUnusedDisableDirectives: 'error' },
      rules: {
        'agp/suppression-tail': 'error',
        'no-console': 'error',
        'no-debugger': 'error',
        [RESTRICT]: realBlock?.rules?.[RESTRICT] ?? 'off',
        [NO_USE]: realBlock?.rules?.[NO_USE] ?? 'off',
      },
    },
  ];
  const lint = (lines: string[]) => linter.verify(lines.join('\n'), config, { filename: 'fixture.ts' });
  const tailFaults = (lines: string[]) =>
    lint(lines)
      .filter((message) => message.ruleId === 'agp/suppression-tail')
      .map((message) => message.messageId);
  const restricted = (lines: string[]) => lint(lines).filter((message) => message.ruleId === RESTRICT);
  const collected = (name: string, text: string) => {
    const found: string[] = [];
    const collect: Rule.RuleModule = {
      create(context) {
        const sourceCode = context.sourceCode as DirectiveSource;
        for (const { value, node } of sourceCode.getDisableDirectives().directives) {
          const rules = value
            .split(',')
            .map((rule) => rule.trim())
            .filter(Boolean);
          if (
            rules.length === 0 ||
            rules.some((rule) => rule.startsWith('agp/') || rule.startsWith('@eslint-community/eslint-comments/'))
          ) {
            found.push(`${name}:${node.loc.start.line}: ${sourceCode.text.slice(...node.range)}`);
          }
        }
        for (const node of sourceCode.getInlineConfigNodes()) {
          const [kind] = node.value.trim().split(/\s+/);
          if (!DIRECTIVE_KINDS.includes(kind)) {
            found.push(`${name}:${node.loc.start.line}: ${sourceCode.text.slice(...node.range)}`);
          }
        }
        return {};
      },
    };
    const messages = linter.verify(
      text,
      [
        {
          files: ['**/*.{ts,tsx,mts,cts,js,mjs,cjs}'],
          languageOptions: { parser: tseslint.parser },
          plugins: { tree: { rules: { directives: collect } } },
          linterOptions: { noInlineConfig: true, reportUnusedDisableDirectives: 'off' },
          rules: { 'tree/directives': 'error' },
        },
      ],
      { filename: name },
    );
    const unread = messages.filter((message) => message.fatal);
    return [...found, ...unread.map((message) => `${name}:${message.line}: ${message.message}`)];
  };
  const treeFindings = (root: string) => {
    const links: string[] = [];
    const files = linted(root, links);
    const findings = files.flatMap((path) => {
      const text = readFileSync(path, 'utf8');
      return DIRECTIVE_WORD.test(text) ? collected(relative(root, path), text) : [];
    });
    return { files, findings: [...links, ...findings] };
  };

  beforeEach(() => {
    vi.useFakeTimers({ toFake: ['Date'] });
    vi.setSystemTime(TODAY);
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it('passes a disable directive that carries the full tail', () => {
    expect(lint([`// eslint-disable-next-line no-console -- ${TAIL}`, "console.log('x');"])).toEqual([]);
  });

  it('refuses a disable directive whose reason stands alone', () => {
    const messages = lint(['// eslint-disable-next-line no-console -- reason', "console.log('x');"]);
    expect(messages).toHaveLength(1);
    expect(messages[0]).toMatchObject({ ruleId: 'agp/suppression-tail', messageId: 'sayWhy', line: 1 });
  });

  it('judges every directive form', () => {
    expect(tailFaults(["console.log('x'); // eslint-disable-line no-console -- reason"])).toEqual(['sayWhy']);
    const block = (reason: string) => [
      `/* eslint-disable no-console -- ${reason} */`,
      "console.log('x');",
      `/* eslint-enable no-console -- ${reason} */`,
    ];
    expect(tailFaults(block('reason'))).toEqual(['sayWhy', 'sayWhy']);
    expect(tailFaults(block(TAIL))).toEqual([]);
  });

  it('names the date of an expired directive', () => {
    const messages = lint([
      '// eslint-disable-next-line no-console -- reason; expires 2026-11-03; added E54/T2',
      "console.log('x');",
    ]);
    expect(messages).toHaveLength(1);
    expect(messages[0].message).toBe('Expired 2026-11-03: fix the finding or renew with a new date in a reviewed diff.');
  });

  it('leaves a directive with no reason to require-description', () => {
    expect(tailFaults(['// eslint-disable-next-line no-console', "console.log('x');"])).toEqual([]);
  });

  it('reads an em dash as no separator, so the directive suppresses nothing', () => {
    const messages = lint([`// eslint-disable-next-line no-console — ${TAIL}`, "console.log('x');"]);
    expect(messages.filter((message) => message.ruleId === 'agp/suppression-tail')).toEqual([]);
    expect(messages.some((message) => message.ruleId === 'no-console')).toBe(true);
  });

  it('passes @ts-expect-error with the full tail', () => {
    expect(lint([`// @ts-expect-error -- ${TAIL}`, 'const n: number = "x";'])).toEqual([]);
  });

  it('refuses @ts-expect-error whose reason stands alone', () => {
    const messages = lint(['// @ts-expect-error -- reason', 'const n: number = "x";']);
    expect(messages).toHaveLength(1);
    expect(messages[0]).toMatchObject({ ruleId: 'agp/suppression-tail', messageId: 'sayWhy', line: 1 });
  });

  it('refuses @ts-expect-error with an em dash or no separator', () => {
    expect(tailFaults([`// @ts-expect-error — ${TAIL}`, 'const n: number = "x";'])).toEqual(['sayWhy']);
    expect(tailFaults([`// @ts-expect-error ${TAIL}`, 'const n: number = "x";'])).toEqual(['sayWhy']);
  });

  it('judges @ts-expect-error in every comment form TypeScript honours', () => {
    expect(tailFaults(['/// @ts-expect-error -- reason', 'const n: number = "x";'])).toEqual(['sayWhy']);
    expect(tailFaults(['/* @ts-expect-error -- reason */', 'const n: number = "x";'])).toEqual(['sayWhy']);
    expect(tailFaults(['/*', ' * @ts-expect-error -- reason */', 'const n: number = "x";'])).toEqual(['sayWhy']);
    expect(tailFaults([`/** @ts-expect-error -- ${TAIL} */`, 'const n: number = "x";'])).toEqual([]);
  });

  it('leaves @ts-expect-error with no description to ban-ts-comment', () => {
    expect(tailFaults(['// @ts-expect-error', 'const n: number = "x";'])).toEqual([]);
  });

  it('reports a full-tail directive over a clean line as unused, and nothing else', () => {
    const messages = lint([`// eslint-disable-next-line no-console -- ${TAIL}`, 'export const a = 1;']);
    expect(messages).toHaveLength(1);
    expect(messages[0]).toMatchObject({ ruleId: null, severity: 2 });
    expect(messages[0].message).toMatch(/^Unused eslint-disable directive/);
  });

  it('reports a directive that names the suppression-tail rule, tail or no tail', () => {
    expect(
      restricted(['console.log(1) // eslint-disable-line no-console, agp/suppression-tail -- any reason']),
    ).toHaveLength(1);
    expect(
      restricted([
        '/* eslint-disable agp/suppression-tail -- reason; expires 2027-11-04; added E54/T2 */',
        'console.log(1)',
      ]),
    ).toHaveLength(1);
  });

  it('reports a bare eslint-disable', () => {
    expect(
      restricted(['/* eslint-disable -- reason; expires 2027-11-04; added E54/T2 */', 'console.log(1)']),
    ).toHaveLength(1);
  });

  it('a directive naming no-restricted-disable beside the rule silences ESLint', () => {
    expect(
      lint([
        'console.log(1) // eslint-disable-line no-console, agp/suppression-tail, @eslint-community/eslint-comments/no-restricted-disable -- any reason',
      ]),
    ).toEqual([]);
  });

  it('reports an inline configuration comment that switches a rule off', () => {
    const messages = lint([
      '/* eslint agp/suppression-tail: "off" -- any reason */',
      '// eslint-disable-next-line no-debugger -- any reason',
      'debugger;',
    ]);
    expect(messages.some((message) => message.ruleId === NO_USE)).toBe(true);
  });

  it('an inline configuration comment can switch no-use off too', () => {
    expect(
      lint([
        '/* eslint agp/suppression-tail: "off", @eslint-community/eslint-comments/no-use: "off" -- any reason */',
        '// eslint-disable-next-line no-debugger -- any reason',
        'debugger;',
      ]),
    ).toEqual([]);
  });

  it('an inline comment cannot switch the tree collector off', () => {
    const inline = '/* eslint tree/directives: "off" -- any reason */';
    const directive = `// eslint-disable-line no-console, agp/suppression-tail, ${RESTRICT} -- any reason`;
    expect(collected('fixture.ts', [inline, `console.log(1) ${directive}`].join('\n'))).toEqual([
      `fixture.ts:2: ${directive}`,
      `fixture.ts:1: ${inline}`,
    ]);
  });

  it('every comment ESLint reads as a directive or an inline configuration opens with a directive word', () => {
    for (const kind of [...DIRECTIVE_KINDS, ...INLINE_CONFIG_KINDS]) {
      const text = `/* ${kind} */\nexport const a = 1;\n`;
      expect(collected('fixture.ts', text), kind).toHaveLength(1);
      expect(DIRECTIVE_WORD.test(text), kind).toBe(true);
    }
    for (const near of ['/* eslinted */', '/* globalThis */', '// a note on the export\nexport const a = 1;\n']) {
      expect(collected('fixture.ts', near), near).toEqual([]);
      expect(DIRECTIVE_WORD.test(near), near).toBe(false);
    }
  });

  it('the tree scan reads each planted shape through the word filter and parses no file without a directive word', () => {
    const root = mkdtempSync(join(tmpdir(), 'agp-directives-'));
    try {
      mkdirSync(join(root, 'src'));
      const named = `// eslint-disable-line no-console, agp/suppression-tail -- ${TAIL}`;
      const all = `/* eslint-disable -- ${TAIL} */`;
      const inline = '/* eslint no-console: "off" -- any reason */';
      writeFileSync(join(root, 'src', 'named.ts'), `console.log(1) ${named}\n`);
      writeFileSync(join(root, 'src', 'all.tsx'), `${all}\nexport const a = 1;\n`);
      writeFileSync(join(root, 'src', 'inline.js'), `${inline}\nexport const b = 2;\n`);
      writeFileSync(join(root, 'src', 'clean.ts'), 'export const c: number = ;\n');
      writeFileSync(join(root, 'README.md'), `${all}\n`);
      const { files, findings } = treeFindings(root);
      expect(files.map((path) => relative(root, path)).sort()).toEqual([
        'src/all.tsx',
        'src/clean.ts',
        'src/inline.js',
        'src/named.ts',
      ]);
      expect(findings.sort()).toEqual([
        `src/all.tsx:1: ${all}`,
        `src/inline.js:1: ${inline}`,
        `src/named.ts:1: ${named}`,
      ]);
    } finally {
      rmSync(root, { recursive: true, force: true });
    }
  });

  it('no linted frontend file names agp/ or the eslint-comments rules in a directive, disables all rules, or configures a rule inline', () => {
    const { files, findings } = treeFindings(FRONTEND);
    expect(files.length).toBeGreaterThanOrEqual(LINTED_FILE_FLOOR);
    expect(findings, findings.join('\n')).toEqual([]);
  }, 30_000);
});
