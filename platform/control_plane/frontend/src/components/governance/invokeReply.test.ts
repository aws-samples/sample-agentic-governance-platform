import { describe, expect, it } from 'vitest';
import { parseMarkdown, parseInline, toolCallsView } from './invokeReply';
import { stripComments } from '../../rawSource';
import replyMarkdownRaw from './ReplyMarkdown.tsx?raw';

describe('parseInline', () => {
  it('splits bold and code spans out of text', () => {
    expect(parseInline('Total **391** via `getPolicy` ok')).toEqual([
      { kind: 'text', text: 'Total ' },
      { kind: 'bold', text: '391' },
      { kind: 'text', text: ' via ' },
      { kind: 'code', text: 'getPolicy' },
      { kind: 'text', text: ' ok' },
    ]);
  });

  it('leaves an unclosed marker as literal text', () => {
    expect(parseInline('a **b')).toEqual([{ kind: 'text', text: 'a **b' }]);
  });

  it('keeps HTML as literal text', () => {
    expect(parseInline('<img src=x onerror=alert(1)>')).toEqual([
      { kind: 'text', text: '<img src=x onerror=alert(1)>' },
    ]);
  });
});

describe('parseInline — hostile and nested input: plain data, never HTML, so an agent reply cannot inject markup', () => {
  const kinds = (s: string) => parseInline(s).map((n) => n.kind);

  it('has no link kind: a markdown link with a javascript: scheme stays literal text', () => {
    const s = '[click](javascript:alert(1)) and <a href="javascript:alert(1)">x</a>';
    expect(parseInline(s)).toEqual([{ kind: 'text', text: s }]);
  });

  it('keeps markup inside bold and code spans as their literal text', () => {
    expect(parseInline('**<script>alert(1)</script>**')).toEqual([
      { kind: 'bold', text: '<script>alert(1)</script>' },
    ]);
    expect(parseInline('`<img src=x onerror=alert(1)>`')).toEqual([
      { kind: 'code', text: '<img src=x onerror=alert(1)>' },
    ]);
  });

  it('does not nest: code inside bold is part of the bold text', () => {
    expect(parseInline('**a `b` c**')).toEqual([{ kind: 'bold', text: 'a `b` c' }]);
  });

  it('triple stars and stray markers fall back to literal text around one bold span', () => {
    expect(parseInline('***x***')).toEqual([
      { kind: 'text', text: '*' },
      { kind: 'bold', text: 'x' },
      { kind: 'text', text: '*' },
    ]);
    expect(kinds('** ** `')).toEqual(['bold', 'text']);
    expect(parseInline('****')).toEqual([{ kind: 'text', text: '****' }]);
  });

  it('stays linear on a long unclosed marker run', () => {
    const s = '**' + 'a'.repeat(50_000);
    const t0 = Date.now();
    expect(parseInline(s)).toEqual([{ kind: 'text', text: s }]);
    expect(Date.now() - t0).toBeLessThan(500);
  });
});

describe('parseMarkdown', () => {
  it('groups paragraphs, lists, headings and fenced code', () => {
    const md = [
      '### Policy',
      'Line one',
      'line two',
      '',
      '- **A**: one',
      '* B',
      '',
      '1. first',
      '2. second',
      '```',
      'raw **not bold**',
      '```',
    ].join('\n');
    expect(parseMarkdown(md)).toEqual([
      { kind: 'heading', inlines: [{ kind: 'text', text: 'Policy' }] },
      {
        kind: 'paragraph',
        lines: [[{ kind: 'text', text: 'Line one' }], [{ kind: 'text', text: 'line two' }]],
      },
      {
        kind: 'ul',
        items: [
          [{ kind: 'bold', text: 'A' }, { kind: 'text', text: ': one' }],
          [{ kind: 'text', text: 'B' }],
        ],
      },
      { kind: 'ol', items: [[{ kind: 'text', text: 'first' }], [{ kind: 'text', text: 'second' }]] },
      { kind: 'code', text: 'raw **not bold**' },
    ]);
  });

  it('keeps an unterminated fence as code to the end', () => {
    expect(parseMarkdown('```json\n{"a":1}')).toEqual([{ kind: 'code', text: '{"a":1}' }]);
  });

  it('keeps HTML in a fence, a heading and a list item as literal text', () => {
    expect(parseMarkdown('```\n<script>x</script>\n```')).toEqual([{ kind: 'code', text: '<script>x</script>' }]);
    expect(parseMarkdown('# <b>h</b>')).toEqual([{ kind: 'heading', inlines: [{ kind: 'text', text: '<b>h</b>' }] }]);
    expect(parseMarkdown('- <i>x</i>')).toEqual([{ kind: 'ul', items: [[{ kind: 'text', text: '<i>x</i>' }]] }]);
  });

  it('returns no blocks for blank input', () => {
    expect(parseMarkdown('  \n\n')).toEqual([]);
  });
});

describe('toolCallsView', () => {
  it('renders name plus compact JSON args', () => {
    expect(toolCallsView([{ name: 'api___getPolicy', args: { policyId: 'P-1' } }])).toEqual([
      { name: 'api___getPolicy', args: '{"policyId":"P-1"}', short: '{"policyId":"P-1"}', truncated: false },
    ]);
  });

  it('truncates long args but keeps the full text', () => {
    const long = { note: 'x'.repeat(200) };
    const [v] = toolCallsView([{ name: 't', args: long }]);
    expect(v.truncated).toBe(true);
    expect(v.short.length).toBeLessThanOrEqual(81);
    expect(v.short.endsWith('…')).toBe(true);
    expect(v.args).toBe(JSON.stringify(long));
  });

  it('shows {} for missing args and drops malformed entries', () => {
    expect(toolCallsView([{ name: 'a' }, { args: {} }, 'junk', null])).toEqual([
      { name: 'a', args: '{}', short: '{}', truncated: false },
    ]);
  });

  it('returns [] when the platform gave no tool-call source', () => {
    expect(toolCallsView(null)).toEqual([]);
    expect(toolCallsView(undefined)).toEqual([]);
  });
});

describe('parseMarkdown — GFM pipe tables', () => {
  const t = (s: string) => [{ kind: 'text', text: s }];

  it('parses header, separator and body rows into a table block', () => {
    const md = 'Status:\n\n| Field | Details |\n|---|:---:|\n| Claim | **CLM-1** |\n| State | Open |';
    expect(parseMarkdown(md)).toEqual([
      { kind: 'paragraph', lines: [t('Status:')] },
      {
        kind: 'table',
        header: [t('Field'), t('Details')],
        rows: [
          [t('Claim'), [{ kind: 'bold', text: 'CLM-1' }]],
          [t('State'), t('Open')],
        ],
      },
    ]);
  });

  it('accepts rows without outer pipes and ends at a blank line', () => {
    const md = 'A | B\n--- | ---\n1 | 2\n\nafter';
    expect(parseMarkdown(md)).toEqual([
      { kind: 'table', header: [t('A'), t('B')], rows: [[t('1'), t('2')]] },
      { kind: 'paragraph', lines: [t('after')] },
    ]);
  });

  it('starts a table right after paragraph text with no blank line', () => {
    const blocks = parseMarkdown('Here you go:\n| A | B |\n|---|---|\n| 1 | 2 |');
    expect(blocks.map((b) => b.kind)).toEqual(['paragraph', 'table']);
  });

  it('pads short ragged rows and drops extra cells', () => {
    const md = '| A | B | C |\n|---|---|---|\n| 1 |\n| 1 | 2 | 3 | 4 |';
    const [table] = parseMarkdown(md);
    expect(table).toEqual({
      kind: 'table',
      header: [t('A'), t('B'), t('C')],
      rows: [
        [t('1'), [], []],
        [t('1'), t('2'), t('3')],
      ],
    });
  });

  it('keeps pipes inside code spans and escaped pipes in their cell', () => {
    const md = '| Cmd | Note |\n|---|---|\n| `a | b` | x \\| y |';
    const [table] = parseMarkdown(md);
    expect(table).toEqual({
      kind: 'table',
      header: [t('Cmd'), t('Note')],
      rows: [[[{ kind: 'code', text: 'a | b' }], t('x | y')]],
    });
  });

  it('is not a table when the separator column count does not match the header', () => {
    const blocks = parseMarkdown('| A | B |\n|---|\n| 1 | 2 |');
    expect(blocks.map((b) => b.kind)).toEqual(['paragraph']);
  });

  it('is not a table without a separator row (plain text with pipes stays a paragraph)', () => {
    expect(parseMarkdown('a | b\nc | d').map((b) => b.kind)).toEqual(['paragraph']);
  });

  it('keeps HTML in a cell as literal text', () => {
    const [table] = parseMarkdown('| A |\n|---|\n| <b>x</b> |');
    expect(table).toEqual({ kind: 'table', header: [t('A')], rows: [[t('<b>x</b>')]] });
  });
});

describe('ReplyMarkdown', () => {
  it('renders text nodes only, so an agent reply can never inject markup', () => {
    expect(stripComments(replyMarkdownRaw)).not.toContain('dangerouslySetInnerHTML');
  });
});
