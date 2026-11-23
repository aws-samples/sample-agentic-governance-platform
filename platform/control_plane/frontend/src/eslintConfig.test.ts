import { ESLint, type Linter } from 'eslint';
import { fileURLToPath } from 'node:url';
import { describe, expect, it } from 'vitest';
import configText from '../eslint.config.js?raw';
import nodeTsconfig from '../tsconfig.node.json?raw';

const FRONTEND = fileURLToPath(new URL('..', import.meta.url));
const [config] = Object.values(
  import.meta.glob<Linter.Config[]>('/eslint.config.js', { eager: true, import: 'default' }),
);

describe('eslint.config.js', () => {
  it('loads the eslint-comments plugin', () => {
    expect(configText).toContain('eslint-plugin-eslint-comments');
  });

  it('requires a description on every disable directive', () => {
    expect(configText).toMatch(/@eslint-community\/eslint-comments\/require-description':\s*\[\s*'error'/);
  });

  it('callers of useLoad list their dependencies', () => {
    expect(configText).toMatch(
      /'react-hooks\/exhaustive-deps':\s*\[\s*'error',\s*\{\s*additionalHooks:\s*'\(useLoad\)'\s*\}\s*\]/,
    );
  });

  it('an unused eslint-disable is an error', () => {
    expect(configText).toMatch(/reportUnusedDisableDirectives:\s*'error'/);
  });

  it('registers the agp plugin and holds suppression-tail at error', () => {
    expect(configText).toMatch(/'agp\/suppression-tail':\s*'error'/);
    expect(configText).toMatch(/plugins:\s*\{\s*agp\s*\}/);
  });

  it('restricts disabling the agp and eslint-comments rules by directive', () => {
    const blocks = config.filter((block) => block.rules?.['agp/suppression-tail'] !== undefined);
    expect(blocks.length).toBeGreaterThan(0);
    for (const block of blocks) {
      expect(block.rules?.['@eslint-community/eslint-comments/no-restricted-disable']).toEqual([
        'error',
        'agp/*',
        '@eslint-community/eslint-comments/*',
      ]);
    }
  });

  it('allows only the four disable and enable directive kinds as inline configuration', () => {
    const blocks = config.filter((block) => block.rules?.['agp/suppression-tail'] !== undefined);
    expect(blocks.length).toBeGreaterThan(0);
    for (const block of blocks) {
      expect(block.rules?.['@eslint-community/eslint-comments/no-use']).toEqual([
        'error',
        { allow: ['eslint-disable', 'eslint-disable-line', 'eslint-disable-next-line', 'eslint-enable'] },
      ]);
    }
  });

  it('lints the rule files with the js recommended block', () => {
    expect(configText).toMatch(/files:\s*\['eslint-rules\/\*\*\/\*\.js'\]/);
  });

  it('holds the rule files to the same directive rules as the TypeScript sources', () => {
    const merged = (glob: string) => {
      const blocks = config.filter((block) => block.files?.length === 1 && block.files[0] === glob);
      expect(blocks.length).toBeGreaterThan(0);
      return {
        plugins: Object.assign({}, ...blocks.map((block) => block.plugins)),
        rules: Object.assign({}, ...blocks.map((block) => block.rules)),
        linterOptions: Object.assign({}, ...blocks.map((block) => block.linterOptions)),
      };
    };
    const typescript = merged('**/*.{ts,tsx,mts,cts}');
    const ruleFiles = merged('eslint-rules/**/*.js');
    for (const rule of [
      'agp/suppression-tail',
      '@eslint-community/eslint-comments/require-description',
      '@eslint-community/eslint-comments/no-restricted-disable',
      '@eslint-community/eslint-comments/no-use',
    ]) {
      expect(typescript.rules[rule]).toBeDefined();
      expect(ruleFiles.rules[rule]).toEqual(typescript.rules[rule]);
    }
    expect(typescript.linterOptions.reportUnusedDisableDirectives).toBe('error');
    expect(ruleFiles.linterOptions.reportUnusedDisableDirectives).toBe('error');
    expect(ruleFiles.plugins.agp).toBe(typescript.plugins.agp);
    expect(Object.keys(ruleFiles.plugins)).toContain('@eslint-community/eslint-comments');
  });

  it('holds switch-exhaustiveness-check at error with the strict options', () => {
    const blocks = config.filter(
      (block) => block.rules?.['@typescript-eslint/switch-exhaustiveness-check'] !== undefined,
    );
    expect(blocks.length).toBeGreaterThan(0);
    for (const block of blocks) {
      expect(block.rules?.['@typescript-eslint/switch-exhaustiveness-check']).toEqual([
        'error',
        {
          allowDefaultCaseForExhaustiveSwitch: false,
          considerDefaultExhaustiveForUnions: false,
          requireDefaultForNonUnion: true,
        },
      ]);
    }
  });

  it('lints with type information through the project service', () => {
    expect(configText).toMatch(/projectService:\s*true/);
  });

  it('the node project includes vite.config.ts, vitest.config.ts and the rule declarations', () => {
    expect(nodeTsconfig).toMatch(
      /"include":\s*\[\s*"vite\.config\.ts",\s*"vitest\.config\.ts",\s*"eslint-rules\/\*\.d\.ts"\s*\]/,
    );
  });

  it('the resolved warn set is exactly the two react-hooks rules', async () => {
    const eslint = new ESLint({ cwd: FRONTEND });
    const warned = async (file: string) => {
      const resolved: Linter.Config = await eslint.calculateConfigForFile(file);
      return Object.entries(resolved.rules ?? {})
        .filter(([, entry]) => {
          const severity = Array.isArray(entry) ? entry[0] : entry;
          return severity === 1 || severity === 'warn';
        })
        .map(([rule]) => rule)
        .sort();
    };
    expect(await warned('src/App.tsx')).toEqual(['react-hooks/incompatible-library', 'react-hooks/unsupported-syntax']);
    expect(await warned('eslint-rules/suppressionTail.js')).toEqual([]);
  });
});
