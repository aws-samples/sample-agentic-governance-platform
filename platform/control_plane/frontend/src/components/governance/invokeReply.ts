export type Inline = { kind: 'text' | 'bold' | 'code'; text: string };
export type Block =
  | { kind: 'paragraph'; lines: Inline[][] }
  | { kind: 'heading'; inlines: Inline[] }
  | { kind: 'ul'; items: Inline[][] }
  | { kind: 'ol'; items: Inline[][] }
  | { kind: 'code'; text: string }
  | { kind: 'table'; header: Inline[][]; rows: Inline[][][] };

const INLINE = /\*\*([^*]+)\*\*|`([^`]+)`/g;

export function parseInline(s: string): Inline[] {
  const out: Inline[] = [];
  let last = 0;
  for (const m of s.matchAll(INLINE)) {
    const at = m.index ?? 0;
    if (at > last) out.push({ kind: 'text', text: s.slice(last, at) });
    out.push(m[1] !== undefined ? { kind: 'bold', text: m[1] } : { kind: 'code', text: m[2] });
    last = at + m[0].length;
  }
  if (last < s.length) out.push({ kind: 'text', text: s.slice(last) });
  return out;
}

const UL = /^\s*[-*]\s+(.*)$/;
const OL = /^\s*\d+[.)]\s+(.*)$/;
const HEADING = /^#{1,6}\s+(.*)$/;

const TABLE_SEP = /^\s*\|?\s*:?-+:?\s*(\|\s*:?-+:?\s*)*\|?\s*$/;

function splitRow(line: string): string[] {
  const row = line.trim();
  const cells: string[] = [];
  let cell = '';
  for (let j = 0; j < row.length; j++) {
    const c = row[j];
    if (c === '\\' && row[j + 1] === '|') {
      cell += '|';
      j++;
    } else if (c === '`' && row.indexOf('`', j + 1) !== -1) {
      const end = row.indexOf('`', j + 1);
      cell += row.slice(j, end + 1);
      j = end;
    } else if (c === '|') {
      cells.push(cell);
      cell = '';
    } else {
      cell += c;
    }
  }
  cells.push(cell);
  if (row.startsWith('|')) cells.shift();
  if (row.endsWith('|') && !row.endsWith('\\|') && cells.length > 0) cells.pop();
  return cells.map((x) => x.trim());
}

function isTableStart(lines: string[], i: number): boolean {
  const next = lines[i + 1];
  if (!lines[i].includes('|') || next === undefined || !next.includes('|') || !TABLE_SEP.test(next)) return false;
  return splitRow(lines[i]).length === splitRow(next).length;
}

export function parseMarkdown(md: string): Block[] {
  const blocks: Block[] = [];
  const lines = md.replace(/\r\n/g, '\n').split('\n');
  let i = 0;
  while (i < lines.length) {
    const line = lines[i];
    if (line.trim() === '') {
      i++;
    } else if (line.trim().startsWith('```')) {
      const body: string[] = [];
      i++;
      while (i < lines.length && !lines[i].trim().startsWith('```')) body.push(lines[i++]);
      i++;
      blocks.push({ kind: 'code', text: body.join('\n') });
    } else if (isTableStart(lines, i)) {
      const header = splitRow(line).map(parseInline);
      i += 2;
      const rows: Inline[][][] = [];
      while (i < lines.length && lines[i].trim() !== '' && lines[i].includes('|') && !lines[i].trim().startsWith('```')) {
        const cells = splitRow(lines[i++]);
        rows.push(header.map((_, c) => parseInline(cells[c] ?? '')));
      }
      blocks.push({ kind: 'table', header, rows });
    } else if (HEADING.test(line)) {
      blocks.push({ kind: 'heading', inlines: parseInline(line.match(HEADING)![1]) });
      i++;
    } else if (UL.test(line) || OL.test(line)) {
      const re = UL.test(line) ? UL : OL;
      const items: Inline[][] = [];
      while (i < lines.length && re.test(lines[i])) items.push(parseInline(lines[i++].match(re)![1]));
      blocks.push({ kind: re === UL ? 'ul' : 'ol', items });
    } else {
      const para: Inline[][] = [];
      while (
        i < lines.length &&
        lines[i].trim() !== '' &&
        !lines[i].trim().startsWith('```') &&
        !isTableStart(lines, i) &&
        !HEADING.test(lines[i]) &&
        !UL.test(lines[i]) &&
        !OL.test(lines[i])
      ) {
        para.push(parseInline(lines[i++]));
      }
      blocks.push({ kind: 'paragraph', lines: para });
    }
  }
  return blocks;
}

export type ToolCallView = { name: string; args: string; short: string; truncated: boolean };

const SHORT = 80;

export function toolCallsView(raw: unknown): ToolCallView[] {
  if (!Array.isArray(raw)) return [];
  return raw.flatMap((c): ToolCallView[] => {
    if (typeof c !== 'object' || c === null) return [];
    const { name, args } = c as { name?: unknown; args?: unknown };
    if (typeof name !== 'string' || name === '') return [];
    let json: string;
    try {
      json = JSON.stringify(args ?? {}) ?? '{}';
    } catch {
      json = String(args);
    }
    const truncated = json.length > SHORT;
    return [{ name, args: json, short: truncated ? `${json.slice(0, SHORT)}…` : json, truncated }];
  });
}
