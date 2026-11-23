import { describe, it, expect } from 'vitest';
import type { DatasetItemOutput, DraftResponse } from '../../api/client';
import {
  agentNameProblem,
  agentRepoFiles,
  assignFile,
  assignTargets,
  attachment,
  authNoneSummary,
  blockers,
  blockSummary,
  compositionLabel,
  draftFilesProblem,
  fromDraft,
  mcpOptions,
  mcpRepoFiles,
  newBlock,
  parseSkill,
  pickTemplates,
  removeBlock,
  skillFile,
  specPasses,
  treeRows,
  validateSpec,
  withFrontmatter,
  SKILL_NO_FRONTMATTER,
  type AgentBlock,
  type Block,
  type McpBlock,
  type RegistryMcp,
} from './studioModel';
import { cannedSpecYaml, fnolAttachments, SKILL_FNOL_INTAKE } from './studioTestData';
import { datasetItem, templateView } from '../../test/wire';

const failed = (text: string, name?: string) =>
  validateSpec(text, text.length, name)
    .checks.filter((c) => !c.ok)
    .map((c) => c.key);

const REGISTRY: RegistryMcp[] = [{ id: 'mcp-claims', name: 'claims-api', tools: ['searchClaims'] }];

const FNOL_DRAFT: DraftResponse = {
  blocks: [
    {
      kind: 'mcp_server',
      name: 'fnol-claims',
      description: 'FNOL claims API: look up policies, open claims and read claim status.',
      openapi_file: 'fnol-claims-openapi.yaml',
      exposed_operations: ['getPolicy', 'openClaim', 'getClaimStatus'],
      auth: 'none',
    },
    {
      kind: 'agent',
      name: 'fnol_intake_agent',
      system_prompt: 'ignored, the prompt file wins',
      prompt_file: 'prompt.md',
      skill_files: ['SKILL.md'],
      mcp_block_names: ['fnol-claims', 'claims-api'],
      eval_items: [],
    },
  ],
  unused_files: [],
};

const fnol = () => fromDraft(FNOL_DRAFT, fnolAttachments(), REGISTRY);

describe('validateSpec (YAML)', () => {
  it('the canned FNOL spec passes and lists its four operations', () => {
    const y = cannedSpecYaml();
    const r = validateSpec(y, y.length);
    expect(specPasses(r)).toBe(true);
    expect(r.operations.map((o) => o.id)).toEqual(['getPolicy', 'openClaim', 'getClaimStatus', 'approvePayout']);
  });
  it('the broken example fails operationId, composition and server', () => {
    const y = cannedSpecYaml({ broken: true });
    expect(failed(y)).toEqual(['operation_ids', 'no_composition', 'real_server']);
  });
  it('reads block-style YAML only: a flow-style paths map yields no operations, and the check says so', () => {
    const r = validateSpec('openapi: 3.0.3\npaths: { /a: { get: { operationId: x } } }\nservers:\n  - url: https://claims.internal.northwind-insurance.com/v1\n', 100);
    expect(r.parseError).toBeUndefined();
    expect(r.operations).toEqual([]);
    expect(r.checks.find((c) => c.key === 'operation_ids')).toMatchObject({ ok: false, detail: 'No operations found under paths.' });
  });
  it('flags security schemes, non-JSON media and size', () => {
    const y = cannedSpecYaml().replace('application/json', 'application/xml') + 'components:\n  securitySchemes:\n    k:\n      type: apiKey\n';
    expect(failed(y)).toEqual(['no_security', 'json_only']);
    expect(validateSpec(y, 2 * 1024 * 1024).checks.find((c) => c.key === 'size')?.ok).toBe(false);
  });
});

describe('validateSpec (JSON)', () => {
  it('parses JSON for real', () => {
    const doc = {
      openapi: '3.0.3',
      servers: [{ url: 'http://localhost:8080' }],
      paths: { '/a': { get: { operationId: 'a', responses: { '200': { content: { 'application/json': { schema: { anyOf: [] } } } } } }, post: {} } },
    };
    expect(failed(JSON.stringify(doc), 'spec.json')).toEqual(['operation_ids', 'no_composition', 'real_server']);
  });
  it('reports a parse error', () => {
    expect(validateSpec('{nope', 5, 'x.json').parseError).toBeTruthy();
  });
});

describe('parseSkill + agent name', () => {
  it('reads valid frontmatter, flags a missing one, and the fix makes it valid', () => {
    expect(parseSkill(SKILL_FNOL_INTAKE)).toMatchObject({ name: 'fnol-intake', status: 'valid' });
    expect(parseSkill(SKILL_NO_FRONTMATTER).status).toBe('missing_frontmatter');
    expect(parseSkill(withFrontmatter(SKILL_NO_FRONTMATTER, 'claimant-tone', 'Tone rules.')).status).toBe('valid');
    expect(parseSkill('---\nname: Bad_Name\ndescription: x\n---\n').status).toBe('invalid');
  });
  it('asks for underscores over hyphens', () => {
    expect(agentNameProblem('fnol-intake')).toMatch(/underscores/);
    expect(agentNameProblem('fnol_intake_agent')).toBeNull();
    expect(agentNameProblem('1abc')).toBeTruthy();
  });
});

describe('fromDraft — the draft route\'s answer as plain blocks', () => {
  it('builds plain blocks and copies file contents verbatim by name', () => {
    const files = fnolAttachments();
    const { blocks, unused } = fromDraft(FNOL_DRAFT, files, REGISTRY);
    const [mcp, agent] = blocks as [McpBlock, AgentBlock];
    expect([mcp.kind, agent.kind]).toEqual(['mcp', 'agent']);
    expect(Object.keys(mcp)).toEqual(['id', 'kind', 'draft']);
    expect(mcp.draft.spec?.text).toBe(files[0].text);
    expect(mcp.draft.exposed).toEqual(['getPolicy', 'openClaim', 'getClaimStatus']);
    expect(mcp.draft.keyMode).toBe('none');
    expect(agent.draft.prompt).toBe(files[2].text);
    expect(agent.draft.skills.map((s) => s.text)).toEqual([files[1].text]);
    expect(unused).toEqual([]);
  });
  it('wires the agent to this composition by name first, then the registry', () => {
    const [mcp, agent] = fnol().blocks as [McpBlock, AgentBlock];
    expect(agent.draft.mcpIds).toEqual([mcp.id, 'mcp-claims']);
  });
  it('uses the drafted system prompt when no prompt file is placed; api_key auth starts on a new key', () => {
    const draft: DraftResponse = {
      blocks: [
        { ...FNOL_DRAFT.blocks[0], auth: 'api_key' } as DraftResponse['blocks'][number],
        { kind: 'agent', name: 'a1', system_prompt: 'You help.', prompt_file: null, skill_files: [], mcp_block_names: ['nope'], eval_items: [] },
      ],
      unused_files: [],
    };
    const [mcp, agent] = fromDraft(draft, fnolAttachments(), []).blocks as [McpBlock, AgentBlock];
    expect(mcp.draft.keyMode).toBe('new');
    expect(agent.draft.prompt).toBe('You help.');
    expect(agent.draft.mcpIds).toEqual([]);
  });
  it('keeps the drafted eval items on the agent and lists them as evals/dataset.jsonl in Review', () => {
    const items: DatasetItemOutput[] = [
      datasetItem({ id: 'a1-1', input: 'Open a claim for P-1', expected_tools: [{ name: 'openClaim', args: null }], tags: ['studio'] }),
      datasetItem({ id: 'a1-2', input: 'Show me another customer', assertions: ['Refuses'], tags: ['studio'] }),
    ];
    const draft: DraftResponse = {
      blocks: [{ kind: 'agent', name: 'a1', system_prompt: 'You help.', prompt_file: null, skill_files: [], mcp_block_names: [], eval_items: items }],
      unused_files: [],
    };
    const [agent] = fromDraft(draft, [], []).blocks as [AgentBlock];
    expect(agent.draft.evalItems).toEqual(items);
    const file = agentRepoFiles(agent.draft).find((f) => f.path === 'evals/dataset.jsonl');
    expect(file?.note).toBe('Eval items: 2');
    expect(file?.content.trim().split('\n').map((l) => JSON.parse(l))).toEqual(items);
  });
  it('an agent without eval items gets none and no dataset file in Review', () => {
    const a = fnol().blocks.find((b) => b.kind === 'agent') as AgentBlock;
    expect(a.draft.evalItems).toEqual([]);
    expect(agentRepoFiles(a.draft).map((f) => f.path)).not.toContain('evals/dataset.jsonl');
  });
  it('every file no block refers to lands in Not used', () => {
    const files = [...fnolAttachments(), attachment({ name: 'notes.md', text: 'hi', size: 2 })];
    const { unused } = fromDraft({ ...FNOL_DRAFT, unused_files: [] }, files, REGISTRY);
    expect(unused.map((f) => f.name)).toEqual(['notes.md']);
  });
  it('an unused file can be assigned by hand; a spec assigned to an MCP block replaces its spec and exposes every operation', () => {
    const { blocks } = fnol();
    const f = attachment({ name: 'notes.md', text: 'calm', size: 4 });
    expect(assignTargets(blocks).map((t) => t.label)).toEqual([
      'OpenAPI spec of fnol-claims',
      'Skill of fnol_intake_agent',
      'System prompt of fnol_intake_agent',
    ]);
    const next = assignFile(blocks, f, `${blocks[1].id}:prompt`) as [McpBlock, AgentBlock];
    expect(next[1].draft.prompt).toBe('calm');
    const specText = cannedSpecYaml({ only: ['getPolicy', 'openClaim'] });
    const spec = attachment({ name: 'other.yaml', text: specText, size: specText.length });
    const withSpec = assignFile(blocks, spec, `${blocks[0].id}:spec`) as [McpBlock, AgentBlock];
    expect(withSpec[0].draft.spec?.fileName).toBe('other.yaml');
    expect(withSpec[0].draft.exposed).toEqual(['getPolicy', 'openClaim']);
  });
});

describe('draftFilesProblem — the draft route\'s limits', () => {
  it('allows up to 10 files of 1 MB each', () => {
    expect(draftFilesProblem(fnolAttachments())).toBeNull();
    const many = Array.from({ length: 11 }, (_, i) => attachment({ name: `f${i}.md`, text: '', size: 1 }));
    expect(draftFilesProblem(many)).toMatch(/10 files/);
    expect(draftFilesProblem([attachment({ name: 'big.yaml', text: '', size: 2 * 1024 * 1024 })])).toMatch(/big\.yaml/);
  });
});

describe('composition', () => {
  const ctx = { registry: REGISTRY };
  it('needs a project and at least one block', () => {
    expect(blockers('', [], ctx).map((b) => b.text)).toEqual(['Pick a project.', 'Add at least one component.']);
  });
  it('block ids carry the block- prefix and never collide with a registry server id', () => {
    const a = newBlock('mcp');
    const b = newBlock('agent');
    expect(a.id).toMatch(/^block-\d+$/);
    expect(b.id).toMatch(/^block-\d+$/);
    expect(a.id).not.toBe(b.id);
    expect(REGISTRY.map((r) => r.id)).not.toContain(a.id);
  });
  it('the FNOL draft has no blockers, and templates still loading (absent) are not checked', () => {
    expect(blockers('p', fnol().blocks, ctx)).toEqual([]);
  });
  it('a new API key must be pasted; an existing secret must be an ARN', () => {
    const [mcp] = fnol().blocks as [McpBlock];
    const keyed = { ...mcp, draft: { ...mcp.draft, keyMode: 'new' as const } };
    expect(blockers('p', [keyed], ctx).map((b) => b.text)).toEqual(['Paste the API key or give an existing secret ARN.']);
    const arn = { ...mcp, draft: { ...mcp.draft, keyMode: 'existing' as const, secretArn: 'agp-mcp/x' } };
    expect(blockers('p', [arn], ctx).map((b) => b.text)).toEqual(['The existing secret must be a Secrets Manager ARN under agp-mcp/fnol-claims/.']);
  });
  it('no two blocks share a name, across kinds (each is a repository)', () => {
    const { blocks } = fnol();
    const twin: AgentBlock = { ...(blocks[1] as AgentBlock), id: 'block-x', draft: { ...(blocks[1] as AgentBlock).draft, name: 'fnol-claims' } };
    expect(blockers('p', [blocks[0], twin], ctx).filter((b) => b.text.includes('also named'))).toHaveLength(2);
  });
  it('flags a registry collision and the backend MCP name rule (31 characters)', () => {
    const [mcp] = fnol().blocks as [McpBlock];
    const clash = { ...mcp, draft: { ...mcp.draft, name: 'claims-api' } };
    expect(blockers('p', [clash], ctx).map((b) => b.text)).toContain('claims-api already exists in the registry.');
    const long = { ...mcp, draft: { ...mcp.draft, name: `a${'b'.repeat(31)}` } };
    expect(blockers('p', [long], ctx).map((b) => b.text)).toContain('Name is missing or invalid.');
  });
  it('flags a name already used by a repository in the project', () => {
    const [, agent] = fnol().blocks;
    expect(blockers('p', [agent], { ...ctx, existingRepos: ['fnol_intake_agent'] }).map((b) => b.text)).toContain(
      'fnol_intake_agent already exists in this project.',
    );
  });
  it('flags a missing template for a kind in use', () => {
    const t = { agent: null, mcp: null };
    expect(blockers('p', fnol().blocks, { ...ctx, templates: t }).filter((b) => b.blockId === null).map((b) => b.text)).toEqual([
      'No MCP server template is available for this project.',
      'No Strands agent template is available for this project.',
    ]);
  });
  it('an agent needs a system prompt, and skills with distinct names', () => {
    const a = newBlock('agent') as AgentBlock;
    const named = { ...a, draft: { ...a.draft, name: 'a1' } };
    expect(blockers('p', [named], ctx).map((b) => b.text)).toEqual(['Write a system prompt.']);
    const dup = { ...named, draft: { ...named.draft, prompt: 'x', skills: [skillFile('SKILL.md', SKILL_FNOL_INTAKE), skillFile('SKILL.md', SKILL_FNOL_INTAKE)] } };
    expect(blockers('p', [dup], ctx).map((b) => b.text)).toEqual(['Two skills share a name.']);
  });
  it('the agent MCP picker lists this composition first, then the registry', () => {
    const { blocks } = fnol();
    const opts = mcpOptions(blocks, REGISTRY);
    expect(opts.map((o) => [o.name, o.status])).toEqual([
      ['fnol-claims', 'new'],
      ['claims-api', 'provisioned'],
    ]);
  });
  it('removing an MCP block unwires every agent that used it', () => {
    const { blocks } = fnol();
    const left = removeBlock(blocks, blocks[0].id) as AgentBlock[];
    expect(left[0].draft.mcpIds).toEqual(['mcp-claims']);
  });
  it('labels the composition and summarises unauthenticated servers', () => {
    const { blocks } = fnol();
    expect(compositionLabel([...blocks, newBlock('agent')])).toBe('1 MCP server and 2 agents');
    expect(authNoneSummary(blocks)).toBe('1 MCP server has no authentication');
    expect(authNoneSummary([blocks[0], { ...blocks[0], id: 'b2' } as Block])).toBe('2 MCP servers have no authentication');
    expect(authNoneSummary([blocks[1]])).toBeNull();
  });
});

describe('repo file preview', () => {
  it('the spec preview names the unexposed operations Studio drops; gateway.yaml says auth: none', () => {
    const [mcp] = fnol().blocks as [McpBlock];
    const [spec, gw] = mcpRepoFiles(mcp.draft);
    expect(spec.note).toBe('3 of 4 operations');
    expect(spec.content.split('\n')[0]).toBe('# Studio removes approvePayout before commit.');
    expect(gw.content).toContain('auth: none');
    expect(gw.content).not.toContain('secret_arn');
  });
  it('a new key names the agp-mcp/<name>/api-key secret; an existing one shows its ARN', () => {
    const [mcp] = fnol().blocks as [McpBlock];
    const gw = mcpRepoFiles({ ...mcp.draft, keyMode: 'new', apiKey: 'sk-live-123' })[1].content;
    expect(gw).toContain('auth: api_key');
    expect(gw).toContain('agp-mcp/fnol-claims/api-key');
    expect(gw).not.toContain('sk-live-123');
    const arn = 'arn:aws:secretsmanager:<region>:<account>:secret:agp-mcp/fnol-claims/key-abcdef';
    expect(mcpRepoFiles({ ...mcp.draft, keyMode: 'existing', secretArn: arn })[1].content).toContain(arn);
  });
  it('agent files follow prompt/system.md and skills/<name>/SKILL.md', () => {
    const [, agent] = fnol().blocks as [McpBlock, AgentBlock];
    expect(agentRepoFiles(agent.draft).map((f) => f.path)).toEqual(['prompt/system.md', 'skills/fnol-intake/SKILL.md']);
  });
});

describe('blockSummary and treeRows — the compose card line and the review tree', () => {
  it('an MCP block names its spec and counts the exposed operations, or says it has no spec yet', () => {
    const { blocks } = fnol();
    const [mcp] = blocks as [McpBlock];
    expect(blockSummary(mcp, blocks, REGISTRY)).toBe('fnol-claims-openapi.yaml · 3 operations exposed');
    const one = { ...mcp, draft: { ...mcp.draft, exposed: ['getPolicy'] } };
    expect(blockSummary(one, blocks, REGISTRY)).toBe('fnol-claims-openapi.yaml · 1 operation exposed');
    expect(blockSummary(newBlock('mcp'), blocks, REGISTRY)).toBe('No OpenAPI spec yet');
  });
  it('an agent block counts its skills and names the MCP servers it uses, this composition first', () => {
    const { blocks } = fnol();
    const [, agent] = blocks as [McpBlock, AgentBlock];
    expect(blockSummary(agent, blocks, REGISTRY)).toBe('1 skill · uses fnol-claims, claims-api');
    expect(blockSummary(newBlock('agent'), blocks, REGISTRY)).toBe('0 skills · no MCP servers');
  });
  it('treeRows lists each directory once by its full path, before its files, one level deeper per segment', () => {
    const file = (path: string) => ({ path, content: '' });
    const files = [file('README.md'), file('skills/a/SKILL.md'), file('skills/b/SKILL.md'), file('docs/a/notes.md')];
    expect(treeRows(files).map((r) => [r.depth, r.label, r.file?.path])).toEqual([
      [0, 'README.md', 'README.md'],
      [0, 'skills/', undefined],
      [1, 'a/', undefined],
      [2, 'SKILL.md', 'skills/a/SKILL.md'],
      [1, 'b/', undefined],
      [2, 'SKILL.md', 'skills/b/SKILL.md'],
      [0, 'docs/', undefined],
      [1, 'a/', undefined],
      [2, 'notes.md', 'docs/a/notes.md'],
    ]);
    expect(treeRows([])).toEqual([]);
  });
});

describe('pickTemplates', () => {
  it('picks the first Strands agent template and the first MCP template on the tenant platform', () => {
    const list = [
      templateView({ name: 'lg', framework: 'langgraph' }),
      templateView({ name: 'dbx-strands', framework: 'strands', platform: 'databricks' }),
      templateView({ name: 'strands-agentcore', framework: 'strands' }),
      templateView({ name: 'mcp-gw', artifact_kind: 'mcp_server', framework: '' }),
    ];
    const p = pickTemplates(list, 'aws');
    expect(p.agent?.name).toBe('strands-agentcore');
    expect(p.mcp?.name).toBe('mcp-gw');
    expect(pickTemplates(list, 'gcp')).toEqual({ agent: null, mcp: null });
  });
});
