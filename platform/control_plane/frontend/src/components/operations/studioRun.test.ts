import { describe, it, expect } from 'vitest';
import type {
  AgentBlockDraft,
  DraftResponse,
  McpBlockDraft,
  ProposalResponse,
  Repository,
  RuntimeStatus,
  StepState,
} from '../../api/openapi';
import { serverErrorMessage } from '../../api/studio';
import type { ProposalRequest } from '../../api/client';
import { ApiFailure } from '../../api/failure';
import { fromDraft, newBlock, type AgentBlock, type Block } from './studioModel';
import { fnolAttachments } from './studioTestData';
import { repository, stepState, templateView } from '../../test/wire';
import {
  apply,
  awaitMergeCopy,
  compositionLabelOf,
  declaredNames,
  findSavedRun,
  isPoll,
  loadRun,
  NO_BUILD_HINT,
  NO_BUILD_HINT_MS,
  nextAction,
  planRun,
  runAction,
  runBadge,
  runKey,
  saveRun,
  timeline,
  type RunState,
  type StudioRunApi,
} from './studioRun';
import runViewRaw from './StudioRunView.tsx?raw';
import { stripComments } from '../../rawSource';

function mcpBlockDraft(overrides: Partial<McpBlockDraft> = {}): McpBlockDraft {
  return {
    auth: 'none',
    description: '',
    exposed_operations: [],
    kind: 'mcp_server',
    name: 'mcp',
    openapi_file: null,
    ...overrides,
  };
}

function agentBlockDraft(overrides: Partial<AgentBlockDraft> = {}): AgentBlockDraft {
  return {
    eval_items: [],
    kind: 'agent',
    mcp_block_names: [],
    name: 'agent',
    prompt_file: null,
    skill_files: [],
    system_prompt: '',
    ...overrides,
  };
}

function draftResponse(overrides: Partial<DraftResponse> = {}): DraftResponse {
  return { blocks: [], unused_files: [], ...overrides };
}

function proposalResponse(overrides: Partial<ProposalResponse> = {}): ProposalResponse {
  return { branch: 'studio/repo-1', files: [], pr_number: 1, pr_url: 'https://github.com/pr/1', ...overrides };
}

const TEMPLATES = {
  agent: templateView({ name: 'strands-agentcore', framework: 'strands', artifact_kind: 'agent' }),
  mcp: templateView({ name: 'mcp-gateway', artifact_kind: 'mcp_server' }),
};

function composition(): Block[] {
  const { blocks } = fromDraft(
    draftResponse({
      blocks: [
        mcpBlockDraft({ name: 'fnol-claims', description: 'FNOL claims API.', openapi_file: 'fnol-claims-openapi.yaml', exposed_operations: ['getPolicy'], auth: 'none' }),
        agentBlockDraft({ name: 'fnol_intake_agent', system_prompt: 'x', prompt_file: 'prompt.md', skill_files: ['SKILL.md'], mcp_block_names: ['fnol-claims', 'claims-api'] }),
      ],
    }),
    fnolAttachments(),
    [{ id: 'mcp-registry-1', name: 'claims-api', tools: [] }],
  );
  return blocks;
}

const done = (n: number): StepState[] => Array.from({ length: n }, (_, i) => stepState({ key: `s${i}`, label: `step ${i}`, status: 'done' }));
const pending = (n: number): StepState[] => Array.from({ length: n }, (_, i) => stepState({ key: `s${i}`, label: `step ${i}`, status: 'pending' }));

type Row = { outcome: string; started_at: string; build_id?: string; source_sha?: string; error?: string | null };
type FakeRepo = Repository & { reads: number; built: boolean; rows: Row[]; depReads: number; mergedReads: number };

function fakeApi(
  opts: {
    proposalError?: (repoId: string) => Error | null;
    templateBuild?: boolean;
    templateLate?: boolean;
    buildFails?: boolean;
    earlyStart?: boolean;
    shaLate?: boolean;
    skew?: boolean;
  } = {},
) {
  const calls: string[] = [];
  const proposals: { repoId: string; body: ProposalRequest }[] = [];
  const repos = new Map<string, FakeRepo>();
  const merged = new Set<number>();
  let seq = 0;
  let tick = 0;
  const stamp = (year = 2026) => new Date(Date.UTC(year, 0, 1) + ++tick * 1000).toISOString();
  const mergeYear = opts.skew ? 1999 : 2026;
  const tplYear = opts.skew ? 2099 : 2026;
  const sha = (r: FakeRepo) => `m-${r.id}`;
  const template = (r: FakeRepo) => {
    r.rows.unshift({ outcome: 'started', started_at: stamp(tplYear), build_id: `tpl-${r.id}`, source_sha: 'tpl-sha' });
    finish(r, 'succeeded', `tpl-${r.id}`, tplYear);
  };
  const finish = (r: FakeRepo, outcome: 'succeeded' | 'failed', build_id = `merge-${r.id}`, year = mergeYear) => {
    r.rows.unshift({ outcome, started_at: stamp(year), build_id, error: outcome === 'failed' ? 'terraform apply failed' : null });
    if (outcome === 'succeeded') {
      r.built = true;
      if (r.artifact_kind === 'mcp_server') r.gateway_arn = 'arn:gw';
    }
  };
  const mergeStart = (r: FakeRepo) => r.rows.unshift({ outcome: 'started', started_at: stamp(mergeYear), build_id: `merge-${r.id}`, source_sha: sha(r) });
  const api: StudioRunApi = {
    async createRepo(projectId, body) {
      calls.push(`create ${body.name}`);
      const id = `repo-${++seq}`;
      const isMcp = Boolean(body.mcp_name);
      const repo: FakeRepo = {
        ...repository({
          id,
          project_id: projectId,
          name: body.name,
          repo_url: `https://github.com/acme/${body.name}`,
          artifact_kind: isMcp ? 'mcp_server' : 'agent',
          agent_id: isMcp ? null : `agent-${seq}`,
          mcp_server_id: isMcp ? `mcp-${seq}` : null,
          template_name: body.template_name,
          cicd_status: 'provisioning',
          status: 'pending',
          steps: pending(5),
        }),
        reads: 0,
        built: false,
        rows: [],
        depReads: 0,
        mergedReads: 0,
      };
      repos.set(id, repo);
      return repo;
    },
    async repoStatus(_p, repoId) {
      const r = repos.get(repoId)!;
      r.reads += 1;
      if (r.reads >= 2) {
        r.steps = done(5);
        r.status = 'ready';
      }
      calls.push(`status ${r.name}`);
      return { ...r };
    },
    async retryRepo(_p, repoId) {
      calls.push(`retry ${repoId}`);
      return repos.get(repoId)!;
    },
    async propose(_p, repoId, body) {
      const r = repos.get(repoId)!;
      calls.push(`propose ${r.name}`);
      const err = opts.proposalError?.(repoId);
      if (err) throw err;
      proposals.push({ repoId, body });
      if (opts.templateBuild && !r.rows.length) template(r);
      return proposalResponse({ branch: `studio/${repoId}`, pr_number: proposals.length, pr_url: `https://github.com/pr/${proposals.length}` });
    },
    async pullRequests(repoId) {
      const r = repos.get(repoId)!;
      calls.push(`prs ${r.name}`);
      return proposals
        .map((p, i) => ({ repoId: p.repoId, number: i + 1 }))
        .filter((p) => p.repoId === repoId)
        .map((p) => {
          if (!merged.has(p.number)) return { number: p.number, state: 'open', merge_commit_sha: null };
          r.mergedReads += 1;
          return { number: p.number, state: 'merged', merge_commit_sha: opts.shaLate && r.mergedReads === 1 ? null : sha(r) };
        });
    },
    async deployments(_p, repoId) {
      const r = repos.get(repoId)!;
      calls.push(`deployments ${r.name}`);
      if (r.depReads > 0) {
        r.depReads += 1;
        if (r.depReads === 2 && !opts.earlyStart) mergeStart(r);
        if (r.depReads === 2 && opts.templateLate) template(r);
        if (r.depReads === 3) finish(r, opts.buildFails ? 'failed' : 'succeeded');
      }
      return [...r.rows];
    },
    async runtimeStatus(agentId) {
      calls.push(`runtime ${agentId}`);
      const r = [...repos.values()].find((x) => x.agent_id === agentId)!;
      return { status: r.built ? 'ready' : 'creating' };
    },
    async submitAgent(id) {
      calls.push(`submit agent ${id}`);
    },
    async submitMcp(id) {
      calls.push(`submit mcp ${id}`);
    },
  };
  const merge = () => {
    [1, 2, 3, 4].forEach((n) => merged.add(n));
    repos.forEach((r) => {
      r.depReads = 1;
      if (opts.earlyStart) mergeStart(r);
    });
  };
  return { api, calls, proposals, repos, merge };
}

async function drive(s: RunState, api: StudioRunApi, stop: (s: RunState) => boolean, keys: Record<string, string> = {}): Promise<RunState> {
  for (let i = 0; i < 200 && !stop(s); i++) {
    const a = nextAction(s);
    if (a.type === 'done' || a.type === 'stopped') break;
    s = apply(s, await runAction(s, a, api, keys));
  }
  return s;
}

const atAwaitMerge = (s: RunState) => nextAction(s).type === 'await_merge';
const finished = (s: RunState) => nextAction(s).type === 'done';

function memoryStorage() {
  const m = new Map<string, string>();
  return {
    getItem: (k: string) => m.get(k) ?? null,
    setItem: (k: string, v: string) => void m.set(k, v),
    removeItem: (k: string) => void m.delete(k),
    key: (i: number) => [...m.keys()][i] ?? null,
    get length() {
      return m.size;
    },
  };
}

describe('planRun', () => {
  it('puts MCP blocks first and never carries the API key', () => {
    const blocks = composition();
    const keyed = blocks.map((b) => (b.kind === 'mcp' ? { ...b, draft: { ...b.draft, keyMode: 'new' as const, apiKey: 'sk-secret' } } : b));
    const s = planRun('p1', [...keyed].reverse(), TEMPLATES);
    expect(s.items.map((i) => i.kind)).toEqual(['mcp_server', 'agent']);
    expect(JSON.stringify(s)).not.toContain('sk-secret');
    expect(s.items[0].create).toMatchObject({ name: 'fnol-claims', template_name: 'mcp-gateway', mcp_name: 'fnol-claims', purpose: 'FNOL claims API.' });
    expect(s.items[1].create).toMatchObject({ name: 'fnol_intake_agent', template_name: 'strands-agentcore', agent_config: { agent_name: 'fnol_intake_agent', framework: 'strands' } });
  });
});

describe('the run', () => {
  it('happy path: MCP repos, ready, proposals, agent repos, ready, proposals, review, builds, submit, done; the review wait is a poll, and the agent repo is created only after the MCP repo is ready', async () => {
    const { api, calls, merge } = fakeApi();
    let s = planRun('p1', composition(), TEMPLATES);
    s = await drive(s, api, atAwaitMerge);
    expect(nextAction(s)).toEqual({ type: 'await_merge', items: s.items.map((i) => i.blockId) });
    expect(timeline(s).find((r) => r.key === 'review')?.status).toBe('waiting');
    expect(runBadge(s)).toBe('waiting');
    s = apply(s, await runAction(s, nextAction(s), api, {}));
    expect(atAwaitMerge(s)).toBe(true);
    merge();
    s = await drive(s, api, finished);
    expect(finished(s)).toBe(true);
    expect(timeline(s).every((r) => r.status === 'done')).toBe(true);
    expect(runBadge(s)).toBe('done');

    const order = calls.filter((c) => /^(create|propose|submit)/.test(c));
    expect(order).toEqual([
      'create fnol-claims',
      'propose fnol-claims',
      'create fnol_intake_agent',
      'propose fnol_intake_agent',
      'submit mcp mcp-1',
      'submit agent agent-2',
    ]);
    expect(calls.indexOf('create fnol_intake_agent')).toBeGreaterThan(calls.lastIndexOf('status fnol-claims', calls.indexOf('propose fnol-claims')));
  });

  it('agent proposals carry the MCP ids created earlier in the same run, then registry ids', async () => {
    const { api, proposals } = fakeApi();
    await drive(planRun('p1', composition(), TEMPLATES), api, atAwaitMerge);
    const agent = proposals.find((p) => p.body.kind === 'agent')!.body;
    expect(agent).toMatchObject({ kind: 'agent', declared_mcp_server_ids: ['mcp-1', 'mcp-registry-1'] });
    const mcp = proposals.find((p) => p.body.kind === 'mcp_server')!.body;
    expect(mcp).toMatchObject({ kind: 'mcp_server', auth: 'none', exposed_operations: ['getPolicy'] });
    expect(mcp).not.toHaveProperty('api_key');
  });

  it('an API key is sent from memory only; after a reload it is gone and the step says so', async () => {
    const keyed = composition().map((b) => (b.kind === 'mcp' ? { ...b, draft: { ...b.draft, keyMode: 'new' as const, apiKey: 'sk-1' } } : b));
    const mcpId = keyed[0].id;
    const { api, proposals } = fakeApi();
    await drive(planRun('p1', keyed, TEMPLATES), api, atAwaitMerge, { [mcpId]: 'sk-1' });
    expect(proposals[0].body).toMatchObject({ auth: 'api_key', api_key: 'sk-1' });

    const lost = await drive(planRun('p1', keyed, TEMPLATES), fakeApi().api, (s) => s.error !== null, {});
    expect(lost.error).toMatchObject({ row: `pr:${mcpId}` });
    expect(lost.error?.message).toMatch(/API key/);
  });

  it('reload mid-wait resumes at the same step', async () => {
    const { api } = fakeApi();
    const storage = memoryStorage();
    const s = await drive(planRun('p1', composition(), TEMPLATES), api, atAwaitMerge);
    saveRun(storage, s);
    expect(storage.getItem(runKey('p1'))).toBeTruthy();
    const back = loadRun(storage, 'p1')!;
    expect(back).toEqual(s);
    expect(nextAction(back)).toEqual(nextAction(s));
    expect(findSavedRun(storage)?.projectId).toBe('p1');
  });

  it('findSavedRun picks the newest run across projects, so a reload lands back on the one started last', () => {
    const storage = memoryStorage();
    saveRun(storage, planRun('p-old', composition(), TEMPLATES, new Date('2026-01-01T00:00:00Z')));
    saveRun(storage, planRun('p-new', composition(), TEMPLATES, new Date('2026-06-01T00:00:00Z')));
    expect(findSavedRun(storage)?.projectId).toBe('p-new');
  });

  it('a 422 on a proposal stops the run with the message; Retry re-runs only that step', async () => {
    let fail = true;
    const { api, calls } = fakeApi({
      proposalError: () => (fail ? new ApiFailure(422, null, 'Unknown operation(s): getPolicy', 'Unknown operation(s): getPolicy') : null),
    });
    let s = await drive(planRun('p1', composition(), TEMPLATES), api, (x) => x.error !== null);
    const mcpBlock = s.items[0].blockId;
    expect(s.error).toEqual({ row: `pr:${mcpBlock}`, message: 'Unknown operation(s): getPolicy' });
    expect(nextAction(s)).toEqual({ type: 'stopped' });
    const row = timeline(s).find((r) => r.key === `pr:${mcpBlock}`)!;
    expect(row).toMatchObject({ status: 'failed', detail: 'Unknown operation(s): getPolicy' });
    expect(runBadge(s)).toBe('failed');
    expect(calls.filter((c) => c.startsWith('create'))).toEqual(['create fnol-claims']);

    fail = false;
    s = { ...s, error: null };
    expect(nextAction(s)).toMatchObject({ type: 'propose', item: mcpBlock });
    s = await drive(s, api, atAwaitMerge);
    expect(calls.filter((c) => c === 'create fnol-claims')).toHaveLength(1);
  });

  it('a failed materialize step stops the run; Retry calls the retry route', async () => {
    const { api, calls } = fakeApi();
    const failing: StudioRunApi = {
      ...api,
      async repoStatus(p, id) {
        const r = await api.repoStatus(p, id);
        return { ...r, steps: [stepState({ key: 'gh', label: 'GitHub repository created', status: 'failed', error: 'name taken' })] };
      },
    };
    let s = await drive(planRun('p1', [composition()[0]], TEMPLATES), failing, (x) => x.error !== null);
    expect(s.error?.message).toBe('GitHub repository created: name taken');
    s = { ...s, error: null };
    expect(nextAction(s).type).toBe('retry_repo');
    s = apply(s, await runAction(s, nextAction(s), api, {}));
    expect(calls).toContain(`retry ${s.items[0].repoId}`);
    expect(nextAction(s).type).toBe('poll_repo');
  });

  it('a PR closed without merging stops the run at the review row', async () => {
    const { api } = fakeApi();
    const closing: StudioRunApi = { ...api, pullRequests: async () => [{ number: 1, state: 'closed' }] };
    const s = await drive(planRun('p1', [composition()[0]], TEMPLATES), closing, (x) => x.error !== null);
    expect(s.error?.row).toBe('review');
    expect(s.error?.message).toMatch(/closed without merging/);
  });

  it('a poll that changes nothing still yields a new state, so the page schedules the next poll', () => {
    const s = planRun('p1', composition(), TEMPLATES);
    expect(apply(s, { type: 'prs', states: { [s.items[0].blockId]: 'open' }, mergeShas: {} })).not.toBe(s);
    expect(apply(s, { type: 'built', ready: [] })).not.toBe(s);
  });

  it('a build counts only if it ran the merge commit: the template build before the merge does not', async () => {
    const { api, merge, calls } = fakeApi({ templateBuild: true });
    let s = await drive(planRun('p1', composition(), TEMPLATES), api, atAwaitMerge);
    merge();
    s = await drive(s, api, (x) => nextAction(x).type === 'await_build');
    expect(s.items.map((i) => i.mergeSha)).toEqual(s.items.map((i) => `m-${i.repoId}`));
    s = apply(s, await runAction(s, nextAction(s), api, {}));
    expect(s.items.some((i) => i.built)).toBe(false);
    expect(calls.some((c) => c.startsWith('submit'))).toBe(false);
    s = await drive(s, api, finished);
    expect(finished(s)).toBe(true);
  });

  it("the template's started row landing AFTER the merge does not count", async () => {
    const { api, merge } = fakeApi({ templateLate: true });
    let s = await drive(planRun('p1', [composition()[0]], TEMPLATES), api, atAwaitMerge);
    merge();
    s = await drive(s, api, (x) => nextAction(x).type === 'await_build');
    s = apply(s, await runAction(s, nextAction(s), api, {}));
    expect(s.items[0].built).toBeFalsy();
    s = await drive(s, api, finished);
    expect(finished(s)).toBe(true);
  });

  it('timestamps do not matter: a merge build stamped in the past counts, a template stamped in the future does not', async () => {
    const { api, merge } = fakeApi({ skew: true, templateLate: true });
    let s = await drive(planRun('p1', [composition()[0]], TEMPLATES), api, atAwaitMerge);
    merge();
    s = await drive(s, api, (x) => nextAction(x).type === 'await_build');
    s = apply(s, await runAction(s, nextAction(s), api, {}));
    expect(s.items[0].built).toBeFalsy();
    s = await drive(s, api, finished);
    expect(finished(s)).toBe(true);
  });

  it('a merge build that already started when the merge is first seen still counts', async () => {
    const { api, merge } = fakeApi({ earlyStart: true });
    let s = await drive(planRun('p1', [composition()[0]], TEMPLATES), api, atAwaitMerge);
    merge();
    s = await drive(s, api, finished);
    expect(finished(s)).toBe(true);
  });

  it('no build of the merge commit ~5 min after the merge: the build row hints at GitHub Actions and keeps polling, and the hint goes once the started row lands', async () => {
    const { api, merge } = fakeApi();
    let s = await drive(planRun('p1', [composition()[0]], TEMPLATES), api, atAwaitMerge);
    merge();
    s = await drive(s, api, (x) => nextAction(x).type === 'await_build');
    const it0 = s.items[0];
    const at = (ms: number) => new Date(Date.parse(it0.mergedAt!) + ms);
    const buildRow = (now: Date) => timeline(s, now).find((r) => r.key === `build:${it0.blockId}`)!;
    expect(buildRow(at(NO_BUILD_HINT_MS - 1000)).detail).toBeUndefined();
    const late = buildRow(at(NO_BUILD_HINT_MS));
    expect(late).toMatchObject({ status: 'running', detail: NO_BUILD_HINT, href: `${it0.repoUrl}/actions` });
    expect(s.error).toBeNull();
    expect(isPoll(nextAction(s))).toBe(true);
    s = apply(s, await runAction(s, nextAction(s), api, {}));
    expect(s.items[0].buildStarted).toBe(true);
    expect(buildRow(at(NO_BUILD_HINT_MS * 2)).detail).toBeUndefined();
    s = await drive(s, api, finished);
    expect(finished(s)).toBe(true);
  });

  it('a merged PR without its merge sha yet keeps the run waiting on the reviewer row', async () => {
    const { api, merge } = fakeApi({ shaLate: true });
    let s = await drive(planRun('p1', [composition()[0]], TEMPLATES), api, atAwaitMerge);
    merge();
    s = apply(s, await runAction(s, nextAction(s), api, {}));
    expect(s.items[0].merged).toBeFalsy();
    expect(nextAction(s).type).toBe('await_merge');
    s = apply(s, await runAction(s, nextAction(s), api, {}));
    expect(s.items[0].merged).toBe(true);
  });

  it('a failed build stops the run at its row; Check again waits for a newer build of the merge commit, which a re-run of the same commit satisfies', async () => {
    const { api, merge, repos } = fakeApi({ buildFails: true });
    let s = await drive(planRun('p1', [composition()[0]], TEMPLATES), api, atAwaitMerge);
    merge();
    s = await drive(s, api, (x) => x.error !== null);
    const id = s.items[0].blockId;
    expect(s.error).toEqual({ row: `build:${id}`, message: 'terraform apply failed. Fix it in the repository, then check again.' });
    expect(timeline(s).find((r) => r.key === `build:${id}`)!.status).toBe('failed');
    s = { ...s, error: null };
    s = apply(s, await runAction(s, nextAction(s), api, {}));
    expect(s.error).toBeNull();
    expect(s.items[0].built).toBeFalsy();
    const r = [...repos.values()][0];
    r.rows.unshift({ outcome: 'started', started_at: '2027-01-01T00:00:00.000Z', build_id: 'rerun', source_sha: `m-${r.id}` });
    r.rows.unshift({ outcome: 'succeeded', started_at: '2027-01-01T00:01:00.000Z', build_id: 'rerun' });
    r.gateway_arn = 'arn:gw';
    s = await drive(s, api, finished);
    expect(finished(s)).toBe(true);
  });

  it('Check again after a failed build: a stale build from before the failure does not count, a fix commit built after it does', async () => {
    const { api, merge, repos } = fakeApi({ buildFails: true });
    let s = await drive(planRun('p1', [composition()[0]], TEMPLATES), api, atAwaitMerge);
    merge();
    s = await drive(s, api, (x) => x.error !== null);
    const failedAt = s.items[0].failedAt!;
    expect(failedAt).toBeTruthy();
    s = { ...s, error: null };
    const r = [...repos.values()][0];
    r.gateway_arn = 'arn:gw';
    r.rows.unshift({ outcome: 'started', started_at: '2025-01-01T00:00:00.000Z', build_id: 'stale', source_sha: 'old-sha' });
    r.rows.unshift({ outcome: 'succeeded', started_at: '2027-01-01T00:00:00.000Z', build_id: 'stale' });
    s = apply(s, await runAction(s, nextAction(s), api, {}));
    expect(s.error).toBeNull();
    expect(s.items[0].built).toBeFalsy();
    r.rows.unshift({ outcome: 'started', started_at: '2027-01-02T00:00:00.000Z', build_id: 'fix', source_sha: 'fix-1' });
    r.rows.unshift({ outcome: 'succeeded', started_at: '2027-01-02T00:01:00.000Z', build_id: 'fix' });
    s = await drive(s, api, finished);
    expect(s.items[0].built).toBe(true);
    expect(finished(s)).toBe(true);
  });

  it('Check again after a failed runtime: a stale build does not count, a fix commit built after it does', async () => {
    const { api, merge, repos } = fakeApi();
    let runtime: RuntimeStatus['status'] = 'failed';
    const broken: StudioRunApi = { ...api, runtimeStatus: async () => ({ status: runtime }) };
    let s = await drive(planRun('p1', composition(), TEMPLATES), broken, atAwaitMerge);
    merge();
    s = await drive(s, broken, (x) => x.error !== null);
    const agent = s.items[1];
    expect(s.error?.row).toBe(`build:${agent.blockId}`);
    expect(agent.failedAt).toBeTruthy();
    s = { ...s, error: null };
    runtime = 'ready';
    const r = repos.get(agent.repoId!)!;
    r.rows.unshift({ outcome: 'started', started_at: '2025-01-01T00:00:00.000Z', build_id: 'stale', source_sha: 'old-sha' });
    r.rows.unshift({ outcome: 'succeeded', started_at: '2027-01-01T00:00:00.000Z', build_id: 'stale' });
    s = apply(s, await runAction(s, nextAction(s), broken, {}));
    expect(s.error).toBeNull();
    expect(s.items[1].built).toBeFalsy();
    r.rows.unshift({ outcome: 'started', started_at: '2027-01-02T00:00:00.000Z', build_id: 'fix', source_sha: 'fix-1' });
    r.rows.unshift({ outcome: 'succeeded', started_at: '2027-01-02T00:01:00.000Z', build_id: 'fix' });
    s = await drive(s, broken, finished);
    expect(s.items[1].built).toBe(true);
    expect(finished(s)).toBe(true);
  });

  it('a failed runtime stops the run at the agent build row; Check again then waits', async () => {
    const { api, merge } = fakeApi();
    const broken: StudioRunApi = { ...api, runtimeStatus: async () => ({ status: 'failed' }) };
    let s = await drive(planRun('p1', composition(), TEMPLATES), broken, atAwaitMerge);
    merge();
    s = await drive(s, broken, (x) => x.error !== null);
    expect(s.error?.row).toBe(`build:${s.items[1].blockId}`);
    expect(s.error?.message).toMatch(/runtime failed.*Fix it in the repository, then check again/);
    s = { ...s, error: null };
    s = apply(s, await runAction(s, nextAction(s), broken, {}));
    expect(s.error).toBeNull();
    expect(nextAction(s).type).toBe('await_build');
  });

  it('polls are the waiting actions', () => {
    expect(isPoll({ type: 'poll_repo', item: 'x' })).toBe(true);
    expect(isPoll({ type: 'await_merge', items: [] })).toBe(true);
    expect(isPoll({ type: 'await_build', items: [] })).toBe(true);
    expect(isPoll({ type: 'make_repo', item: 'x' })).toBe(false);
  });

  it('an MCP-only run skips the agent phase', async () => {
    const { api, merge } = fakeApi();
    let s = await drive(planRun('p1', [composition()[0]], TEMPLATES), api, atAwaitMerge);
    merge();
    s = await drive(s, api, finished);
    expect(finished(s)).toBe(true);
  });

  it('an agent block without MCP servers declares none', async () => {
    const a = newBlock('agent') as AgentBlock;
    const agent: AgentBlock = { ...a, draft: { ...a.draft, name: 'solo', prompt: 'hi' } };
    const { api, proposals } = fakeApi();
    await drive(planRun('p1', [agent as Block], TEMPLATES), api, atAwaitMerge);
    expect(proposals[0].body).toEqual({ kind: 'agent', system_prompt: 'hi', skills: [], declared_mcp_server_ids: [] });
  });

  it("passes the agent's eval items to its proposal unchanged", async () => {
    const items = [{ id: 'solo-1', input: 'Open a claim', expected_tools: [{ name: 'openClaim' }], tags: ['studio'] }];
    const a = newBlock('agent') as AgentBlock;
    const agent: AgentBlock = { ...a, draft: { ...a.draft, name: 'solo', prompt: 'hi', evalItems: items } };
    const { api, proposals } = fakeApi();
    await drive(planRun('p1', [agent as Block], TEMPLATES), api, atAwaitMerge);
    expect(proposals[0].body).toEqual({ kind: 'agent', system_prompt: 'hi', skills: [], declared_mcp_server_ids: [], eval_items: items });
  });
});

describe('serverErrorMessage', () => {
  it('504 and the API Gateway 503 read as a timeout; 502 shows the detail', () => {
    expect(serverErrorMessage('draft', 504, 'The draft took too long. Compose manually or try again.')).toMatch(/too long/);
    expect(serverErrorMessage('draft', 503, undefined)).toMatch(/too long/);
    expect(serverErrorMessage('draft', 502, 'The draft failed. Compose manually or try again.')).toMatch(/failed/);
    expect(serverErrorMessage('proposal', 502, 'Storing the API key failed')).toBe('Storing the API key failed');
    expect(serverErrorMessage('proposal', 503, undefined)).toMatch(/timed out/);
  });
});

describe('awaitMergeCopy — true to both merge paths', () => {
  it('names both paths and no longer says "not here"', () => {
    const two = awaitMergeCopy(2);
    expect(two).toContain('all 2 pull requests are merged');
    expect(two).toContain('Pull requests tab');
    expect(two).toContain('GitHub');
    expect(two).not.toMatch(/not here/i);
    expect(awaitMergeCopy(1)).toContain('the pull request is merged');
  });
});

describe('the end card of a finished run', () => {
  it('the end card lists Approve before Grant: a grant on a PENDING_APPROVAL record resets it to DRAFT', () => {
    const src = stripComments(runViewRaw);
    const approve = src.indexOf('<li>Approve the ');
    const grant = src.indexOf('Then grant ');
    expect(approve).toBeGreaterThan(-1);
    expect(grant).toBeGreaterThan(-1);
    expect(approve).toBeLessThan(grant);
  });
});

describe('declaredNames and compositionLabelOf — the end card and the run heading', () => {
  it("declaredNames names an agent's MCP servers, this run's first, then the registry, each falling back to its id", () => {
    const s = planRun('p1', composition(), TEMPLATES);
    const [mcp, agent] = s.items;
    const registry = [{ id: 'mcp-registry-1', name: 'claims-api', tools: [] }];
    expect(declaredNames(agent, s, registry)).toEqual(['fnol-claims', 'claims-api']);
    expect(declaredNames(agent, { ...s, items: [agent] }, [])).toEqual([mcp.blockId, 'mcp-registry-1']);
    expect(declaredNames(mcp, s, registry)).toEqual([]);
  });
  it("compositionLabelOf counts the run's MCP servers and agents, singular or plural, and drops a kind with none", () => {
    const s = planRun('p1', composition(), TEMPLATES);
    const [mcp, agent] = s.items;
    expect(compositionLabelOf(s)).toBe('1 MCP server and 1 agent');
    expect(compositionLabelOf({ ...s, items: [mcp, agent, agent] })).toBe('1 MCP server and 2 agents');
    expect(compositionLabelOf({ ...s, items: [mcp, mcp] })).toBe('2 MCP servers');
    expect(compositionLabelOf({ ...s, items: [agent] })).toBe('1 agent');
    expect(compositionLabelOf({ ...s, items: [] })).toBe('');
  });
});
