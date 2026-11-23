import { readdirSync, readFileSync } from 'node:fs';
import { join, relative } from 'node:path';
import { fileURLToPath } from 'node:url';
import ts from 'typescript';
import { describe, expect, it } from 'vitest';
import openapiTS, { astToString, COMMENT_HEADER } from 'openapi-typescript';
import snapshot from '../../../backend/tests/fixtures/openapi.json';
import committed from './openapi.ts?raw';
import clientSource from './client.ts?raw';
import failureSource from './failure.ts?raw';
import { stripComments } from '../rawSource';

const GENERATOR_OPTIONS = {
  emptyObjectsUnknown: true,
  rootTypes: true,
  rootTypesNoSchemaPrefix: true,
  defaultNonNullable: false,
  silent: true,
} as const;

const SYNC_MESSAGE =
  'frontend/src/api/openapi.ts is stale. If the backend changed: cd backend && AGP_UPDATE_OPENAPI_SNAPSHOT=1 ./venv/bin/python -m pytest tests/test_openapi_contract.py, review the snapshot diff, then cd frontend && npm run generate:api';

const ZERO_TARGET_MESSAGE =
  'alias the generated type from `./openapi`; a hand-written wire type is where the drift starts';

const EXPORTED_INTERFACE = /^export interface \w+/gm;
const LITERAL = /(?:'[^']*'|"[^"]*")/.source;
const LITERAL_UNION_ALIAS = new RegExp(
  String.raw`^(?:export )?type \w+\s*=\s*\|?\s*${LITERAL}(?:\s*\|\s*${LITERAL})*\s*(?:;|$)`,
  'gm',
);

const SRC = fileURLToPath(new URL('..', import.meta.url));
const HAND_COPY_MESSAGE =
  'alias it from `../api/client`; a form draft derives its keys (`Record<keyof DatasheetInput, string>`)';
const SHAPE_MIN_NAMES = 3;
const ENUM_MIN_MEMBERS = 2;
const ROOT_ALIAS = /^export type (\w+) = components\['schemas'\]\['([^']+)'\];$/gm;
const WORD = /[\p{ID_Continue}$]+/gu;
const WHOLE_WORD = /^[\p{ID_Continue}$]+$/u;
const UNTYING = new Set(['Array', 'ReadonlyArray', 'Readonly', 'Iterable', 'ArrayLike']);

interface WireKey {
  label: string;
  tokens: string[];
}

interface WireIndex {
  shapes: Map<string, WireKey>;
  enums: Map<string, WireKey>;
}

interface HandCopy {
  at: string;
  declared: string;
  generated: string | null;
}

const keyOf = (items: Iterable<string>): string => JSON.stringify([...new Set(items)].sort());
const depth = (label: string): number => label.split('[').length;

function visit(node: ts.Node, each: (node: ts.Node) => void): void {
  each(node);
  ts.forEachChild(node, (child) => visit(child, each));
}

function nameText(name: ts.PropertyName | undefined): string | null {
  if (name === undefined) return null;
  if (!ts.isComputedPropertyName(name)) return name.text;
  return ts.isStringLiteralLike(name.expression) ? name.expression.text : null;
}

function memberNames(members: ts.NodeArray<ts.TypeElement>): string[] {
  return [...new Set(members.flatMap((member) => nameText(member.name) ?? []))];
}

function literalMembers(node: ts.TypeNode): string[] | null {
  if (ts.isParenthesizedTypeNode(node)) return literalMembers(node.type);
  if (ts.isUnionTypeNode(node)) {
    const members: string[] = [];
    for (const type of node.types) {
      const part = literalMembers(type);
      if (part === null) return null;
      members.push(...part);
    }
    return members;
  }
  if (node.kind === ts.SyntaxKind.UndefinedKeyword) return [];
  if (!ts.isLiteralTypeNode(node)) return null;
  if (node.literal.kind === ts.SyntaxKind.NullKeyword) return [];
  return ts.isStringLiteralLike(node.literal) ? [node.literal.text] : null;
}

function unionMembers(node: ts.Node): string[] | null {
  if (!ts.isUnionTypeNode(node)) return null;
  let parent = node.parent;
  while (ts.isParenthesizedTypeNode(parent)) parent = parent.parent;
  if (ts.isUnionTypeNode(parent)) return null;
  const members = new Set(literalMembers(node));
  return members.size >= ENUM_MIN_MEMBERS ? [...members] : null;
}

function wireLabel(node: ts.Node, alias: Map<string, string>): string {
  const path: string[] = [];
  let root = '';
  for (let at = node; !ts.isSourceFile(at); at = at.parent) {
    if (ts.isPropertySignature(at)) path.unshift(nameText(at.name) ?? '?');
    if (ts.isInterfaceDeclaration(at) || ts.isTypeAliasDeclaration(at)) root = at.name.text;
  }
  const indexed = (names: string[]) => names.map((name) => `['${name}']`).join('');
  if (root !== 'components' || path[0] !== 'schemas' || path.length < 2) return root + indexed(path);
  const [, schema, ...rest] = path;
  return (alias.get(schema) ?? schema) + indexed(rest);
}

function wireIndex(generatedText: string): WireIndex {
  const file = ts.createSourceFile('openapi.ts', generatedText, ts.ScriptTarget.Latest, true, ts.ScriptKind.TS);
  const alias = new Map([...generatedText.matchAll(ROOT_ALIAS)].map(([, root, schema]) => [schema, root]));
  const index: WireIndex = { shapes: new Map(), enums: new Map() };
  const keep = (into: Map<string, WireKey>, tokens: string[], label: string) => {
    const key = keyOf(tokens);
    const held = into.get(key);
    if (held === undefined || depth(label) < depth(held.label)) into.set(key, { label, tokens });
  };
  visit(file, (node) => {
    const members = unionMembers(node);
    if (members !== null) keep(index.enums, members, wireLabel(node, alias));
    const schemas = node.parent?.parent;
    if (
      ts.isPropertySignature(node) &&
      node.type !== undefined &&
      ts.isTypeLiteralNode(node.type) &&
      schemas !== undefined &&
      ts.isPropertySignature(schemas) &&
      nameText(schemas.name) === 'schemas' &&
      ts.isInterfaceDeclaration(schemas.parent) &&
      schemas.parent.name.text === 'components'
    ) {
      const names = memberNames(node.type.members);
      if (names.length >= SHAPE_MIN_NAMES) keep(index.shapes, names, wireLabel(node.type, alias));
    }
  });
  return index;
}

function namesAType(type: ts.TypeNode | undefined): boolean {
  let named = false;
  if (type !== undefined) {
    visit(type, (node) => {
      if (ts.isTypeReferenceNode(node) && !UNTYING.has(node.typeName.getText())) named = true;
    });
  }
  return named;
}

function untiedConstArray(node: ts.Node): string[] | null {
  const bound =
    ts.isVariableDeclaration(node) || ts.isPropertyDeclaration(node) || ts.isPropertyAssignment(node)
      ? node.initializer
      : ts.isExportAssignment(node)
        ? node.expression
        : undefined;
  if (bound === undefined || ((ts.isVariableDeclaration(node) || ts.isPropertyDeclaration(node)) && namesAType(node.type))) {
    return null;
  }
  let expression = bound;
  for (;;) {
    if (ts.isParenthesizedExpression(expression)) expression = expression.expression;
    else if (ts.isSatisfiesExpression(expression) && !namesAType(expression.type)) expression = expression.expression;
    else break;
  }
  if (!(ts.isAsExpression(expression) || ts.isTypeAssertionExpression(expression))) return null;
  if (!ts.isTypeReferenceNode(expression.type) || expression.type.typeName.getText() !== 'const') return null;
  let array = expression.expression;
  while (ts.isParenthesizedExpression(array)) array = array.expression;
  if (!ts.isArrayLiteralExpression(array)) return null;
  const values = array.elements.flatMap((element) => (ts.isStringLiteralLike(element) ? [element.text] : []));
  return values.length === array.elements.length && new Set(values).size >= ENUM_MIN_MEMBERS ? values : null;
}

function bindingName(node: ts.Node, source: ts.SourceFile): string {
  if (ts.isVariableDeclaration(node) || ts.isPropertyDeclaration(node) || ts.isPropertyAssignment(node)) {
    return node.name.getText(source);
  }
  return 'of the default export';
}

function mayHoldACopy(text: string, wire: WireIndex): boolean {
  if (/\benum\b|\\/.test(text)) return true;
  const words = new Set(text.match(WORD));
  const present = (token: string) => (WHOLE_WORD.test(token) ? words.has(token) : text.includes(token));
  return [...wire.shapes.values(), ...wire.enums.values()].some(({ tokens }) => tokens.every(present));
}

/**
 * The hand copies of generated wire types in one source, a pure function of its text. A shape is an object
 * schema under `components.schemas` with at least three property names; an enum is a literal union of at least two
 * strings anywhere in `openapi.ts`, `null` and `undefined` dropped. Flagged: an interface or an object type in any
 * position whose names equal a shape; a literal union in any type position, any member order, equal to an enum;
 * an `as const` string array bound to a name with no annotation and no `satisfies` that names a type; any `enum`.
 * Not flagged: an array a named type annotates or ties by `satisfies`, and an inline `([...] as const).map`, since
 * tsc binds their members. A file is parsed only if it holds every token of some shape or enum, the word `enum`, or
 * a backslash. Not seen: two-name objects, a hand subset or superset of a schema or enum, a class with a schema's
 * names, a key union equal to a shape's names, a field typed `string` where the wire has a union, a schema split
 * across an intersection, an array inside `Object.freeze`, and an array tied by `String`, by a `string` utility
 * such as `Lowercase<string>` or `NonNullable<string>`, or by a local alias of `string`.
 */
function handCopies(file: string, text: string, wire: WireIndex): HandCopy[] {
  if (!mayHoldACopy(text, wire)) return [];
  const kind = file.endsWith('.tsx') ? ts.ScriptKind.TSX : ts.ScriptKind.TS;
  const source = ts.createSourceFile(file, text, ts.ScriptTarget.Latest, true, kind);
  const found: HandCopy[] = [];
  const report = (node: ts.Node, declared: string, generated: string | null) => {
    const { line } = source.getLineAndCharacterOfPosition(node.getStart(source));
    found.push({ at: `${file}:${line + 1}`, declared, generated });
  };
  const named = (node: ts.Node, fallback: string) =>
    ts.isTypeAliasDeclaration(node.parent) ? `type ${node.parent.name.text}` : fallback;
  visit(source, (node) => {
    if (ts.isInterfaceDeclaration(node) || ts.isTypeLiteralNode(node)) {
      const names = memberNames(node.members);
      const shape = names.length >= SHAPE_MIN_NAMES ? wire.shapes.get(keyOf(names)) : undefined;
      const declared = ts.isInterfaceDeclaration(node) ? `interface ${node.name.text}` : named(node, 'an object type');
      if (shape !== undefined) report(node, declared, shape.label);
    }
    const members = unionMembers(node);
    const union = members === null ? undefined : wire.enums.get(keyOf(members));
    if (union !== undefined) report(node, named(node, `the union ${node.getText(source)}`), union.label);
    if (ts.isEnumDeclaration(node)) {
      const values = node.members.flatMap((member) =>
        member.initializer !== undefined && ts.isStringLiteralLike(member.initializer) ? [member.initializer.text] : [],
      );
      report(node, `enum ${node.name.text}`, wire.enums.get(keyOf(values))?.label ?? null);
    }
    const array = untiedConstArray(node);
    const tied = array === null ? undefined : wire.enums.get(keyOf(array));
    if (tied !== undefined) report(node, `the as-const array ${bindingName(node, source)}`, tied.label);
  });
  return found;
}

function sources(dir: string): string[] {
  return readdirSync(dir, { withFileTypes: true }).flatMap((entry) => {
    const path = join(dir, entry.name);
    if (entry.isDirectory()) return sources(path);
    return /\.tsx?$/.test(entry.name) && !/\.(test\.tsx?|d\.ts)$/.test(entry.name) ? [relative(SRC, path)] : [];
  });
}

const described = ({ at, declared, generated }: HandCopy): string =>
  generated === null
    ? `${at}: ${declared} is an enum, and the wire's enums are string-literal unions`
    : `${at}: ${declared} re-declares the generated ${generated}`;

const ARTIFACT_KIND = "Repository['artifact_kind']";
const USER_INFO_NAMES =
  'email: string; role: string; role_level: number; can_deploy: boolean; oid: string | null; name: string | null; tenants: unknown[]';

const PLANTS: [string, string, string][] = [
  ['a non-exported interface', `interface HandUserInfo { ${USER_INFO_NAMES} }`, 'UserInfo'],
  ['an object alias', 'export type HandTotals = { traces: number; cost_usd: number; tokens: number };', 'MetricTotals'],
  [
    'an as const array',
    "const HAND_KINDS = ['agent', 'mcp_server'] as const;\nexport type HandKind = (typeof HAND_KINDS)[number];",
    ARTIFACT_KIND,
  ],
  ['an inline parameter union', "export function pick(kind: 'agent' | 'mcp_server'): string {\n  return kind;\n}", ARTIFACT_KIND],
  ['reversed member order', "export type HandKind = 'mcp_server' | 'agent';", ARTIFACT_KIND],
  ['a Record key union', "export const counts: Record<'agent' | 'mcp_server', number> = { agent: 0, mcp_server: 0 };", ARTIFACT_KIND],
  ['a nullable union', "export const picked: 'agent' | 'mcp_server' | null = null;", ARTIFACT_KIND],
  ['an enum', "enum KindEnum {\n  A = 'agent',\n  B = 'mcp_server',\n}", ARTIFACT_KIND],
  ['a keyword-only satisfies', "export const KINDS = ['agent', 'mcp_server'] as const satisfies readonly string[];", ARTIFACT_KIND],
  ['a parameter typed with a schema literal', `export function greet(user: { ${USER_INFO_NAMES} }): string {\n  return user.email;\n}`, 'UserInfo'],
];

const NEGATIVES: [string, string][] = [
  ['an array a satisfies ties', "export const KINDS = ['agent', 'mcp_server'] as const satisfies readonly ArtifactKind[];"],
  ['an array a named type annotates', "export const KINDS: readonly ArtifactKind[] = ['agent', 'mcp_server'] as const;"],
  ['an inline as const read in place', "export const LABELS = (['agent', 'mcp_server'] as const).map((kind) => kind.toUpperCase());"],
  ['a two-name object', 'export type HandAgent = { id: string; name: string };'],
];

async function generated(document: unknown): Promise<string> {
  return COMMENT_HEADER + astToString(await openapiTS(JSON.stringify(document), GENERATOR_OPTIONS));
}

function withoutOnePath(document: typeof snapshot): unknown {
  const { paths, ...rest } = document;
  return { ...rest, paths: Object.fromEntries(Object.entries(paths).slice(1)) };
}

describe('src/api/openapi.ts', () => {
  it("the committed file is the generator's output over the snapshot", async () => {
    expect(await generated(snapshot), SYNC_MESSAGE).toBe(committed);
  });

  it('the sync message names both commands', async () => {
    const stale = await generated(withoutOnePath(snapshot));
    expect(stale).not.toBe(committed);
    const assertion = () => expect(stale, SYNC_MESSAGE).toBe(committed);
    expect(assertion).toThrow('AGP_UPDATE_OPENAPI_SNAPSHOT=1');
    expect(assertion).toThrow('npm run generate:api');
  });

  it.each([
    ['client.ts', clientSource],
    ['failure.ts', failureSource],
  ])('%s declares no interface and no literal union', (_name, raw) => {
    const source = stripComments(raw);
    const declared = [...(source.match(EXPORTED_INTERFACE) ?? []), ...(source.match(LITERAL_UNION_ALIAS) ?? [])];
    expect(declared, ZERO_TARGET_MESSAGE).toEqual([]);
  });

  it('no source under src re-declares a generated schema, enum or literal union', () => {
    const wire = wireIndex(committed);
    const scanned = sources(SRC).filter((file) => file !== join('api', 'openapi.ts'));
    const copies = scanned.flatMap((file) => handCopies(file, readFileSync(join(SRC, file), 'utf8'), wire).map(described));
    expect(wire.shapes.size).toBeGreaterThan(0);
    expect(wire.enums.size).toBeGreaterThan(0);
    expect(scanned).toContain(join('contexts', 'useUser.ts'));
    expect(scanned).toContain(join('components', 'operations', 'studioModel.ts'));
    expect(scanned).toContain(join('components', 'operations', 'repo-tabs', 'evaluationsLogic.ts'));
    expect(copies, `${copies.join('\n')}\n${HAND_COPY_MESSAGE}`).toEqual([]);
  }, 30_000);

  it('the matcher refuses every hand-copy shape', () => {
    const wire = wireIndex(committed);
    const caught = (text: string) => handCopies('zzPlant.ts', text, wire).map((copy) => copy.generated);
    expect(PLANTS.map(([shape, text]) => [shape, caught(text)])).toEqual(
      PLANTS.map(([shape, , label]) => [shape, [label]]),
    );
    expect(NEGATIVES.map(([shape, text]) => [shape, caught(text)])).toEqual(NEGATIVES.map(([shape]) => [shape, []]));
  });
});
