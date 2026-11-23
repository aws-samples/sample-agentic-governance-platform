import ts from 'typescript';

export function stripComments(source: string): string {
  const file = ts.createSourceFile('source.tsx', source, ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
  const comments = new Map<number, ts.CommentRange>();
  const jsxText: Array<{ pos: number; end: number }> = [];
  const collect = (found: ts.CommentRange[] | undefined) => {
    for (const range of found ?? []) comments.set(range.pos, range);
  };
  const visit = (node: ts.Node) => {
    if (node.kind === ts.SyntaxKind.JsxText) jsxText.push({ pos: node.pos, end: node.end });
    collect(ts.getLeadingCommentRanges(source, node.getFullStart()));
    collect(ts.getTrailingCommentRanges(source, node.getEnd()));
    for (const child of node.getChildren(file)) visit(child);
  };
  visit(file);
  collect(ts.getLeadingCommentRanges(source, file.endOfFileToken.getFullStart()));
  const insideJsxText = (pos: number) => jsxText.some((span) => span.pos <= pos && pos < span.end);
  const ranges = [...comments.values()].filter((range) => !insideJsxText(range.pos)).sort((a, b) => a.pos - b.pos);
  let out = '';
  let cursor = 0;
  for (const range of ranges) {
    if (range.pos < cursor) continue;
    out += source.slice(cursor, range.pos) + source.slice(range.pos, range.end).replace(/[^\n]/g, '');
    cursor = range.end;
  }
  return out + source.slice(cursor);
}
