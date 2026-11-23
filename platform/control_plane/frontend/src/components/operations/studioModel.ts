import type { DatasetItemInput, McpServer, ProductType, TemplateView } from '../../api/client';
import type { DraftResponse } from '../../api/client';
import { MCP_DESCRIPTION_MAX, MCP_NAME_RE, templatesForKind, templatesForPlatform } from './templateChain.ts';

export { MCP_DESCRIPTION_MAX };
export const SPEC_MAX_BYTES = 1024 * 1024;
/** The draft route's request limits, verbatim. */
export const DRAFT_MAX_FILES = 10;
export const DRAFT_FILE_MAX_BYTES = 1024 * 1024;

export interface SpecOperation {
  /** operationId, or null when the operation has none. */
  id: string | null;
  method: string;
  path: string;
  summary?: string;
}

export interface SpecCheck {
  key: 'operation_ids' | 'no_composition' | 'no_security' | 'json_only' | 'real_server' | 'size';
  label: string;
  ok: boolean;
  detail?: string;
}

export interface SpecReport {
  operations: SpecOperation[];
  checks: SpecCheck[];
  parseError?: string;
}

const METHODS = ['get', 'put', 'post', 'delete', 'patch', 'head', 'options', 'trace'];

interface Facts {
  operations: SpecOperation[];
  composition: boolean;
  security: boolean;
  mediaTypes: string[];
  serverUrl: string | null;
}

function jsonFacts(doc: Record<string, unknown>): Facts {
  const operations: SpecOperation[] = [];
  const mediaTypes = new Set<string>();
  let composition = false;
  const walk = (v: unknown, parentKey: string) => {
    if (Array.isArray(v)) return v.forEach((x) => walk(x, parentKey));
    if (v === null || typeof v !== 'object') return;
    for (const [k, child] of Object.entries(v)) {
      if (k === 'oneOf' || k === 'anyOf' || k === 'allOf') composition = true;
      if (parentKey === 'content' && k.includes('/')) mediaTypes.add(k);
      walk(child, k);
    }
  };
  walk(doc, '');
  const paths = (doc.paths ?? {}) as Record<string, Record<string, unknown>>;
  for (const [path, item] of Object.entries(paths)) {
    for (const [method, op] of Object.entries(item ?? {})) {
      if (!METHODS.includes(method)) continue;
      const o = (op ?? {}) as { operationId?: unknown; summary?: unknown };
      operations.push({
        id: typeof o.operationId === 'string' ? o.operationId : null,
        method,
        path,
        summary: typeof o.summary === 'string' ? o.summary : undefined,
      });
    }
  }
  const components = (doc.components ?? {}) as Record<string, unknown>;
  const schemes = components.securitySchemes as Record<string, unknown> | undefined;
  const security = Boolean(doc.security) || Boolean(schemes && Object.keys(schemes).length > 0);
  const servers = doc.servers as { url?: unknown }[] | undefined;
  const serverUrl = Array.isArray(servers) && typeof servers[0]?.url === 'string' ? servers[0].url : null;
  return { operations, composition, security, mediaTypes: [...mediaTypes], serverUrl };
}

const indentOf = (line: string) => line.length - line.trimStart().length;
const keyOf = (line: string) => line.trim().replace(/^['"]|['"]?:.*$/g, '');

function yamlFacts(text: string): Facts {
  const lines = text.split(/\r?\n/).filter((l) => l.trim() !== '' && !l.trim().startsWith('#'));
  const operations: SpecOperation[] = [];
  let inPaths = false;
  let pathIndent = -1;
  let methodIndent = -1;
  let currentPath = '';
  let current: SpecOperation | null = null;
  for (const line of lines) {
    const ind = indentOf(line);
    if (ind === 0) {
      inPaths = /^paths:\s*$/.test(line);
      current = null;
      continue;
    }
    if (!inPaths) continue;
    if (pathIndent < 0) pathIndent = ind;
    const key = keyOf(line);
    if (ind === pathIndent) {
      currentPath = key;
      current = null;
      methodIndent = -1;
      continue;
    }
    if (methodIndent < 0 && ind > pathIndent) methodIndent = ind;
    if (ind === methodIndent) {
      current = METHODS.includes(key) ? { id: null, method: key, path: currentPath } : null;
      if (current) operations.push(current);
      continue;
    }
    if (current && ind > methodIndent) {
      const m = line.match(/^\s*(operationId|summary):\s*['"]?(.+?)['"]?\s*$/);
      if (m && ind === methodIndent + (methodIndent - pathIndent)) {
        if (m[1] === 'operationId') current.id = m[2];
        else current.summary = m[2];
      }
    }
  }
  const mediaTypes = [...text.matchAll(/^\s+['"]?([a-z]+\/[a-z0-9.+*-]+)['"]?:\s*$/gim)].map((m) => m[1]);
  const server = text.match(/^servers:\s*\n\s*-\s*url:\s*['"]?([^\s'"]+)/m);
  return {
    operations,
    composition: /^\s*-?\s*(oneOf|anyOf|allOf):/m.test(text),
    security: /^\s+securitySchemes:/m.test(text) || /^security:/m.test(text),
    mediaTypes,
    serverUrl: server ? server[1] : null,
  };
}

export function isPlaceholderUrl(url: string | null): boolean {
  if (!url || /[{}<>]/.test(url)) return true;
  try {
    const host = new URL(url).hostname;
    return (
      host === 'localhost' ||
      host === '127.0.0.1' ||
      /(^|\.)example\.(com|org|net)$/.test(host) ||
      host.startsWith('your-')
    );
  } catch {
    return true;
  }
}

export function validateSpec(text: string, sizeBytes: number, fileName = 'openapi.yaml'): SpecReport {
  let facts: Facts;
  const looksJson = fileName.toLowerCase().endsWith('.json') || text.trimStart().startsWith('{');
  try {
    facts = looksJson ? jsonFacts(JSON.parse(text) as Record<string, unknown>) : yamlFacts(text);
  } catch (e) {
    return { operations: [], checks: [], parseError: e instanceof Error ? e.message : 'Could not parse the file.' };
  }
  const missing = facts.operations.filter((o) => !o.id);
  const nonJson = facts.mediaTypes.filter((m) => m !== 'application/json');
  const checks: SpecCheck[] = [
    {
      key: 'operation_ids',
      label: 'Every operation has an operationId',
      ok: facts.operations.length > 0 && missing.length === 0,
      detail:
        facts.operations.length === 0
          ? 'No operations found under paths.'
          : missing.length
            ? `Missing on ${missing.map((o) => `${o.method.toUpperCase()} ${o.path}`).join(', ')}`
            : undefined,
    },
    {
      key: 'no_composition',
      label: 'No oneOf / anyOf / allOf schemas',
      ok: !facts.composition,
      detail: facts.composition ? 'The gateway cannot map composed schemas to tool inputs. Flatten them.' : undefined,
    },
    {
      key: 'no_security',
      label: 'No spec-level security schemes',
      ok: !facts.security,
      detail: facts.security ? 'Remove them. The gateway injects the API key from Secrets Manager.' : undefined,
    },
    {
      key: 'json_only',
      label: 'JSON media types only',
      ok: nonJson.length === 0,
      detail: nonJson.length ? `Found ${[...new Set(nonJson)].join(', ')}` : undefined,
    },
    {
      key: 'real_server',
      label: 'servers[0].url is a real endpoint',
      ok: !isPlaceholderUrl(facts.serverUrl),
      detail: isPlaceholderUrl(facts.serverUrl)
        ? facts.serverUrl
          ? `${facts.serverUrl} looks like a placeholder.`
          : 'No servers entry.'
        : facts.serverUrl ?? undefined,
    },
    {
      key: 'size',
      label: 'File is 1 MB or smaller',
      ok: sizeBytes <= SPEC_MAX_BYTES,
      detail: `${(sizeBytes / 1024).toFixed(1)} KB`,
    },
  ];
  return { operations: facts.operations, checks };
}

export const specPasses = (r: SpecReport | null): boolean =>
  Boolean(r && !r.parseError && r.checks.every((c) => c.ok));

export type SkillStatus = 'valid' | 'missing_frontmatter' | 'invalid';

export interface SkillReport {
  name: string;
  description: string;
  status: SkillStatus;
  problem?: string;
}

const SKILL_NAME_RE = /^[a-z0-9]+(-[a-z0-9]+)*$/;

/** agentskills.io: frontmatter `name` (lowercase, hyphens, <= 64, = the folder) + `description` (<= 1024). */
export function parseSkill(text: string): SkillReport {
  const fm = text.match(/^---\r?\n([\s\S]*?)\r?\n---/);
  if (!fm) return { name: '', description: '', status: 'missing_frontmatter', problem: 'No frontmatter block.' };
  const field = (k: string) => fm[1].match(new RegExp(`^${k}:\\s*['"]?(.*?)['"]?\\s*$`, 'm'))?.[1] ?? '';
  const name = field('name');
  const description = field('description');
  if (!name || !description) {
    return { name, description, status: 'missing_frontmatter', problem: `Frontmatter has no ${!name ? 'name' : 'description'}.` };
  }
  if (!SKILL_NAME_RE.test(name) || name.length > 64) {
    return { name, description, status: 'invalid', problem: 'name: lowercase letters, digits and hyphens, 64 characters max.' };
  }
  if (description.length > 1024) return { name, description, status: 'invalid', problem: 'description is over 1024 characters.' };
  return { name, description, status: 'valid' };
}

export function withFrontmatter(text: string, name: string, description: string): string {
  const body = text.replace(/^---\r?\n[\s\S]*?\r?\n---\r?\n?/, '');
  return `---\nname: ${name}\ndescription: ${description}\n---\n${body}`;
}

// The backend's agent name rule (models/project.py), verbatim.
export const AGENT_NAME_RE = /^[a-zA-Z][a-zA-Z0-9_]{0,31}$/;

export function agentNameProblem(name: string): string | null {
  if (!name) return 'Required.';
  if (name.includes('-')) return 'Use underscores, not hyphens.';
  if (!AGENT_NAME_RE.test(name)) return 'Start with a letter; letters, digits and underscores only; 32 characters max.';
  return null;
}

export const SKILL_NO_FRONTMATTER = `# Claimant tone

Keep replies short and calm. Acknowledge the loss before asking for details.
`;

export interface SpecFile {
  fileName: string;
  text: string;
  size: number;
}

export interface McpDraft {
  name: string;
  description: string;
  spec: SpecFile | null;
  exposed: string[];
  /** 'none' = the gateway calls the API without credentials (public endpoints only). */
  keyMode: 'none' | 'new' | 'existing';
  /** Held in memory only: never persisted, never sent to the draft route. */
  apiKey: string;
  /** An existing `agp-mcp/<name>/...` Secrets Manager ARN. */
  secretArn: string;
}

export interface SkillFile {
  id: string;
  fileName: string;
  text: string;
}

export interface AgentDraft {
  name: string;
  prompt: string;
  skills: SkillFile[];
  /** MCP block ids from this composition, or registry MCP server ids. */
  mcpIds: string[];
  /** Eval items from Compose with AI, passed to the agent PR unchanged; absent for hand-built agents. */
  evalItems?: DatasetItemInput[];
}

export type BlockKind = ProductType;
export type McpBlock = { id: string; kind: 'mcp'; draft: McpDraft };
export type AgentBlock = { id: string; kind: 'agent'; draft: AgentDraft };
export type Block = McpBlock | AgentBlock;

export const KIND_LABEL: Record<BlockKind, string> = { mcp: 'MCP server', agent: 'Agent' };

export const emptyMcp = (): McpDraft => ({
  name: '',
  description: '',
  spec: null,
  exposed: [],
  keyMode: 'new',
  apiKey: '',
  secretArn: '',
});

export const emptyAgent = (): AgentDraft => ({ name: '', prompt: '', skills: [], mcpIds: [] });

let blockSeq = 0;
export const newBlock = (kind: BlockKind): Block =>
  kind === 'mcp' ? { id: `block-${++blockSeq}`, kind, draft: emptyMcp() } : { id: `block-${++blockSeq}`, kind, draft: emptyAgent() };

export function removeBlock(blocks: Block[], id: string): Block[] {
  return blocks
    .filter((b) => b.id !== id)
    .map((b) => (b.kind === 'agent' && b.draft.mcpIds.includes(id) ? { ...b, draft: { ...b.draft, mcpIds: b.draft.mcpIds.filter((x) => x !== id) } } : b));
}

export const mcpBlocks = (blocks: Block[]): McpBlock[] => blocks.filter((b): b is McpBlock => b.kind === 'mcp');
export const agentBlocks = (blocks: Block[]): AgentBlock[] => blocks.filter((b): b is AgentBlock => b.kind === 'agent');

export interface RegistryMcp {
  id: string;
  name: string;
  tools: string[];
}

export const registryFrom = (servers: McpServer[]): RegistryMcp[] =>
  servers.map((s) => ({ id: s.id, name: s.name, tools: (s.available_tools ?? []).map((t) => t.name) }));

export interface McpOption {
  id: string;
  name: string;
  tools: string[];
  status: 'new' | 'provisioned';
}

export function mcpOptions(blocks: Block[], registry: RegistryMcp[]): McpOption[] {
  return [
    ...mcpBlocks(blocks).map((b): McpOption => ({ id: b.id, name: b.draft.name, tools: b.draft.exposed, status: 'new' })),
    ...registry.map((r): McpOption => ({ id: r.id, name: r.name, tools: r.tools, status: 'provisioned' })),
  ];
}

export function compositionLabel(blocks: Block[]): string {
  const part = (n: number, one: string, many: string) => (n ? `${n} ${n === 1 ? one : many}` : '');
  const parts = [part(mcpBlocks(blocks).length, 'MCP server', 'MCP servers'), part(agentBlocks(blocks).length, 'agent', 'agents')].filter(Boolean);
  return parts.join(' and ') || 'nothing yet';
}

export function authNoneSummary(blocks: Block[]): string | null {
  const n = mcpBlocks(blocks).filter((b) => b.draft.keyMode === 'none').length;
  if (n === 0) return null;
  return n === 1 ? '1 MCP server has no authentication' : `${n} MCP servers have no authentication`;
}

export const blockName = (b: Block): string => b.draft.name || `unnamed ${KIND_LABEL[b.kind].toLowerCase()}`;

export function defaultExposed(spec: SpecFile): string[] {
  return validateSpec(spec.text, spec.size, spec.fileName)
    .operations.map((o) => o.id)
    .filter((id): id is string => Boolean(id));
}

let skillSeq = 0;
export const skillFile = (fileName: string, text: string): SkillFile => ({ id: `skill-${++skillSeq}`, fileName, text });

export interface Attachment {
  id: string;
  name: string;
  text: string;
  size: number;
}

let fileSeq = 0;
export const attachment = (f: { name: string; text: string; size: number }): Attachment => ({ id: `file-${++fileSeq}`, ...f });

export function draftFilesProblem(files: Attachment[]): string | null {
  if (files.length > DRAFT_MAX_FILES) return `Attach at most ${DRAFT_MAX_FILES} files.`;
  const big = files.find((f) => f.size > DRAFT_FILE_MAX_BYTES);
  return big ? `${big.name} is over 1 MB.` : null;
}

const toSpec = (f: Attachment): SpecFile => ({ fileName: f.name, text: f.text, size: f.size });

export function fromDraft(resp: DraftResponse, files: Attachment[], registry: RegistryMcp[]): { blocks: Block[]; unused: Attachment[] } {
  const byName = new Map(files.map((f) => [f.name, f]));
  const used = new Set<string>();
  const take = (name: string | null) => {
    const f = name ? byName.get(name) : undefined;
    if (f) used.add(f.name);
    return f;
  };
  const mcps: McpBlock[] = [];
  const agents: [AgentBlock, string[]][] = [];
  for (const d of resp.blocks) {
    if (d.kind === 'mcp_server') {
      const b = newBlock('mcp') as McpBlock;
      const f = take(d.openapi_file);
      const spec = f ? toSpec(f) : null;
      const known = spec ? defaultExposed(spec) : [];
      const exposed = spec ? d.exposed_operations.filter((o) => known.includes(o)) : [];
      b.draft = {
        ...emptyMcp(),
        name: d.name,
        description: d.description,
        spec,
        exposed: spec && exposed.length === 0 ? known : exposed,
        keyMode: d.auth === 'none' ? 'none' : 'new',
      };
      mcps.push(b);
    } else {
      const b = newBlock('agent') as AgentBlock;
      const promptFile = take(d.prompt_file);
      const skills = d.skill_files.map(take).filter((f): f is Attachment => Boolean(f));
      b.draft = {
        name: d.name,
        prompt: promptFile?.text ?? d.system_prompt,
        skills: skills.map((f) => skillFile(f.name, f.text)),
        mcpIds: [],
        evalItems: d.eval_items,
      };
      agents.push([b, d.mcp_block_names]);
    }
  }
  for (const [b, names] of agents) {
    b.draft.mcpIds = names
      .map((n) => mcps.find((m) => m.draft.name === n)?.id ?? registry.find((r) => r.name === n)?.id)
      .filter((id): id is string => Boolean(id));
  }
  return { blocks: [...mcps, ...agents.map(([b]) => b)], unused: files.filter((f) => !used.has(f.name)) };
}

export function assignTargets(blocks: Block[]): { key: string; label: string }[] {
  return blocks.flatMap((b) =>
    b.kind === 'mcp'
      ? [{ key: `${b.id}:spec`, label: `OpenAPI spec of ${blockName(b)}` }]
      : [
          { key: `${b.id}:skill`, label: `Skill of ${blockName(b)}` },
          { key: `${b.id}:prompt`, label: `System prompt of ${blockName(b)}` },
        ],
  );
}

export function assignFile(blocks: Block[], f: Attachment, target: string): Block[] {
  const [id, slot] = target.split(':');
  return blocks.map((b): Block => {
    if (b.id !== id) return b;
    if (b.kind === 'mcp') {
      const spec = toSpec(f);
      return { ...b, draft: { ...b.draft, spec, exposed: defaultExposed(spec) } };
    }
    return slot === 'prompt'
      ? { ...b, draft: { ...b.draft, prompt: f.text } }
      : { ...b, draft: { ...b.draft, skills: [...b.draft.skills, skillFile(f.name, f.text)] } };
  });
}

export interface StudioTemplates {
  agent: TemplateView | null;
  mcp: TemplateView | null;
}

export function pickTemplates(templates: TemplateView[], platform: string): StudioTemplates {
  const offered = templatesForPlatform(templates, platform);
  return {
    agent: templatesForKind(offered, 'agent').find((t) => t.framework === 'strands') ?? null,
    mcp: templatesForKind(offered, 'mcp_server')[0] ?? null,
  };
}

export interface Blocker {
  /** The block to go to; null for a composition-level problem. */
  blockId: string | null;
  text: string;
}

export function mcpNameProblem(name: string): string | null {
  if (!name) return 'Required.';
  if (!MCP_NAME_RE.test(name)) return 'Lowercase letters, digits and hyphens; start with a letter; 31 characters max.';
  return null;
}

// Mirrors the backend's check in studio_proposal.py; account and region are the caller's own, so they are not matched.
const secretArnOk = (arn: string, name: string) =>
  /^arn:[^:]+:secretsmanager:[^:]+:[^:]+:secret:/.test(arn) && (!name || arn.includes(`:secret:agp-mcp/${name}/`));

function mcpBlockers(m: McpDraft, registry: RegistryMcp[]): string[] {
  const out: string[] = [];
  if (mcpNameProblem(m.name)) out.push('Name is missing or invalid.');
  else if (registry.some((r) => r.name === m.name)) out.push(`${m.name} already exists in the registry.`);
  if (!m.description.trim()) out.push('Add a description.');
  if (m.description.length > MCP_DESCRIPTION_MAX) out.push(`Description is over ${MCP_DESCRIPTION_MAX} characters.`);
  if (!m.spec) out.push('Add an OpenAPI spec.');
  else if (!specPasses(validateSpec(m.spec.text, m.spec.size, m.spec.fileName))) out.push('The OpenAPI spec fails a check.');
  if (m.spec && m.exposed.length === 0) out.push('Expose at least one operation.');
  if (m.keyMode === 'new' && !m.apiKey) out.push('Paste the API key or give an existing secret ARN.');
  if (m.keyMode === 'existing') {
    if (!m.secretArn) out.push('Paste the API key or give an existing secret ARN.');
    else if (!secretArnOk(m.secretArn, m.name)) out.push(`The existing secret must be a Secrets Manager ARN under ${secretName(m.name).replace(/api-key$/, '')}.`);
  }
  return out;
}

function agentBlockers(a: AgentDraft): string[] {
  const out: string[] = [];
  if (agentNameProblem(a.name)) out.push('Name is missing or invalid.');
  if (!a.prompt.trim()) out.push('Write a system prompt.');
  if (a.skills.some((s) => parseSkill(s.text).status !== 'valid')) out.push('A skill needs its frontmatter fixed.');
  const names = a.skills.map((s) => parseSkill(s.text).name).filter(Boolean);
  if (new Set(names).size !== names.length) out.push('Two skills share a name.');
  return out;
}

export function blockers(
  projectId: string,
  blocks: Block[],
  ctx: { registry: RegistryMcp[]; templates?: StudioTemplates; existingRepos?: string[] },
): Blocker[] {
  const out: Blocker[] = [];
  if (!projectId) out.push({ blockId: null, text: 'Pick a project.' });
  if (blocks.length === 0) out.push({ blockId: null, text: 'Add at least one component.' });
  if (ctx.templates) {
    if (!ctx.templates.mcp && mcpBlocks(blocks).length) out.push({ blockId: null, text: 'No MCP server template is available for this project.' });
    if (!ctx.templates.agent && agentBlocks(blocks).length) out.push({ blockId: null, text: 'No Strands agent template is available for this project.' });
  }
  const seen = new Map<string, number>();
  for (const b of blocks) if (b.draft.name) seen.set(b.draft.name, (seen.get(b.draft.name) ?? 0) + 1);
  for (const b of blocks) {
    const own = b.kind === 'mcp' ? mcpBlockers(b.draft, ctx.registry) : agentBlockers(b.draft);
    for (const text of own) out.push({ blockId: b.id, text });
    if (b.draft.name && ctx.existingRepos?.includes(b.draft.name)) out.push({ blockId: b.id, text: `${b.draft.name} already exists in this project.` });
    if ((seen.get(b.draft.name) ?? 0) > 1) out.push({ blockId: b.id, text: `Another component is also named ${b.draft.name}; each becomes its own repository.` });
  }
  return out;
}

export interface RepoFile {
  path: string;
  note?: string;
  content: string;
}

export const secretName = (name: string): string => `agp-mcp/${name || '<name>'}/api-key`;

export function gatewayYaml(m: Pick<McpDraft, 'name' | 'description' | 'keyMode' | 'secretArn'>): string {
  const arn = m.keyMode === 'existing' ? m.secretArn || '<secret ARN>' : `<created at Create>  # secret ${secretName(m.name)}`;
  return [
    `description: ${m.description || '<description>'}`,
    'openapi_spec: openapi.yaml',
    `auth: ${m.keyMode === 'none' ? 'none' : 'api_key'}`,
    ...(m.keyMode === 'none'
      ? []
      : ['api_key:', `  secret_arn: ${arn}`, '  json_key: api_key', '  location: HEADER', '  parameter_name: x-api-key', "  prefix: ''"]),
    "instructions: ''",
    '',
  ].join('\n');
}

export function mcpRepoFiles(mcp: McpDraft): RepoFile[] {
  const ops = mcp.spec ? validateSpec(mcp.spec.text, mcp.spec.size, mcp.spec.fileName).operations : [];
  const total = ops.filter((o) => o.id).length;
  const dropped = ops.map((o) => o.id).filter((id): id is string => Boolean(id) && !mcp.exposed.includes(id as string));
  const specContent = !mcp.spec
    ? '# No spec yet'
    : `${dropped.length ? `# Studio removes ${dropped.join(', ')} before commit.\n` : ''}${mcp.spec.text}`;
  return [
    { path: 'openapi.yaml', note: `${mcp.exposed.length} of ${total} operations`, content: specContent },
    { path: 'gateway.yaml', content: gatewayYaml(mcp) },
  ];
}

export function agentRepoFiles(agent: AgentDraft): RepoFile[] {
  const files: RepoFile[] = [{ path: 'prompt/system.md', content: agent.prompt || '# No prompt yet' }];
  for (const s of agent.skills) {
    const r = parseSkill(s.text);
    files.push({ path: `skills/${r.name || '<name>'}/SKILL.md`, note: r.status === 'valid' ? undefined : 'needs fix', content: s.text });
  }
  if (agent.evalItems?.length) {
    files.push({
      path: 'evals/dataset.jsonl',
      note: `Eval items: ${agent.evalItems.length}`,
      content: agent.evalItems.map((i) => JSON.stringify(i)).join('\n') + '\n',
    });
  }
  return files;
}

export const blockFiles = (b: Block): RepoFile[] => (b.kind === 'mcp' ? mcpRepoFiles(b.draft) : agentRepoFiles(b.draft));

export function wiredNames(agent: AgentDraft, blocks: Block[], registry: RegistryMcp[]): string[] {
  return mcpOptions(blocks, registry)
    .filter((o) => agent.mcpIds.includes(o.id))
    .map((o) => o.name || 'unnamed MCP server');
}

export function blockSummary(b: Block, blocks: Block[], registry: RegistryMcp[]): string {
  if (b.kind === 'mcp') {
    const d = b.draft;
    return d.spec ? `${d.spec.fileName} · ${d.exposed.length} ${d.exposed.length === 1 ? 'operation' : 'operations'} exposed` : 'No OpenAPI spec yet';
  }
  const wired = wiredNames(b.draft, blocks, registry);
  const skills = `${b.draft.skills.length} ${b.draft.skills.length === 1 ? 'skill' : 'skills'}`;
  return `${skills} · ${wired.length ? `uses ${wired.join(', ')}` : 'no MCP servers'}`;
}

export interface TreeRow {
  depth: number;
  label: string;
  file?: RepoFile;
}

export function treeRows(files: RepoFile[]): TreeRow[] {
  const rows: TreeRow[] = [];
  const seen = new Set<string>();
  for (const f of files) {
    const parts = f.path.split('/');
    parts.slice(0, -1).forEach((dir, i) => {
      const key = parts.slice(0, i + 1).join('/');
      if (!seen.has(key)) {
        seen.add(key);
        rows.push({ depth: i, label: `${dir}/` });
      }
    });
    rows.push({ depth: parts.length - 1, label: parts[parts.length - 1], file: f });
  }
  return rows;
}
