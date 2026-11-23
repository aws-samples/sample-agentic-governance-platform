import type { ArtifactKind, DatasetItemInput, Repository, RepositoryCreate, RuntimeStatus, StepState } from '../../api/client';
import type { McpProposalRequest, ProposalRequest, ProposalResponse } from '../../api/client';
import { parseSkill, type Block, type RegistryMcp, type StudioTemplates } from './studioModel';

export const POLL_MS = 3000;
export const RUN_KEY_PREFIX = 'agp.studio.run.';
export const runKey = (projectId: string): string => `${RUN_KEY_PREFIX}${projectId}`;
export const STUDIO_AGENT_MODEL_ID = 'us.anthropic.claude-sonnet-4-6';

export const NO_BUILD_HINT_MS = 5 * 60 * 1000;
export const NO_BUILD_HINT = "No build has started for the merged commit yet. Check the repository's GitHub Actions run.";

export function awaitMergeCopy(prCount: number): string {
  const what = prCount === 1 ? 'the pull request is' : `all ${prCount} pull requests are`;
  return `Waiting on review. Builds start once ${what} merged, in AGP's Pull requests tab or on GitHub. This page checks every few seconds.`;
}

export const KEY_LOST_MESSAGE = 'The API key is kept in this tab only and was lost on reload. Re-enter it to continue.';

export interface RunItem {
  blockId: string;
  kind: ArtifactKind;
  name: string;
  create: RepositoryCreate;
  /** The proposal body without the API key; `needsKey` means the key comes from `keys[blockId]` at proposal time. */
  mcp?: { body: Omit<McpProposalRequest, 'api_key'>; needsKey: boolean };
  /** The proposal body; declared MCP ids resolve at proposal time from this run's MCP items. */
  agent?: {
    systemPrompt: string;
    skills: { name: string; content: string }[];
    mcpBlockIds: string[];
    registryIds: string[];
    evalItems?: DatasetItemInput[];
  };
  repoId?: string;
  repoUrl?: string | null;
  /** The MCP server record id (MCP repos) or the agent id (agent repos). */
  recordId?: string | null;
  repoReady?: boolean;
  repoFailed?: boolean;
  pr?: { number: number; url: string; branch: string };
  merged?: boolean;
  mergeSha?: string;
  /** When Studio first saw the merge (browser clock): drives the no-build hint only. */
  mergedAt?: string;
  buildStarted?: boolean;
  /** After a failed build: only a terminal row newer than this (server time) counts. */
  failedAt?: string;
  built?: boolean;
  submitted?: boolean;
}

export interface RunError {
  row: string;
  message: string;
}

export interface RunState {
  version: 1;
  projectId: string;
  startedAt: string;
  items: RunItem[];
  error: RunError | null;
}

export type RunAction =
  | { type: 'make_repo'; item: string }
  | { type: 'poll_repo'; item: string }
  | { type: 'retry_repo'; item: string }
  | { type: 'propose'; item: string }
  | { type: 'await_merge'; items: string[] }
  | { type: 'await_build'; items: string[] }
  | { type: 'submit'; item: string }
  | { type: 'stopped' }
  | { type: 'done' };

export type RepoSnapshot = Pick<Repository, 'id' | 'steps' | 'status'> &
  Partial<Pick<Repository, 'repo_url' | 'agent_id' | 'mcp_server_id' | 'gateway_arn'>>;

export type RunResult =
  | { type: 'repo'; item: string; repo: RepoSnapshot }
  | { type: 'proposed'; item: string; pr: ProposalResponse }
  | { type: 'prs'; states: Record<string, string>; mergeShas: Record<string, string>; at?: string }
  | { type: 'built'; ready: string[]; started?: string[] }
  | { type: 'build_failed'; item: string; message: string; at: string }
  | { type: 'submitted'; item: string }
  | { type: 'failed'; message: string; row?: string };

export type DeploymentRow = {
  outcome: string;
  started_at: string;
  build_id?: string | null;
  source_sha?: string | null;
  error?: string | null;
};

export interface StudioRunApi {
  createRepo(projectId: string, body: RepositoryCreate): Promise<RepoSnapshot>;
  repoStatus(projectId: string, repoId: string): Promise<RepoSnapshot>;
  retryRepo(projectId: string, repoId: string): Promise<RepoSnapshot>;
  propose(projectId: string, repoId: string, body: ProposalRequest): Promise<ProposalResponse>;
  pullRequests(repoId: string): Promise<{ number: number; state: string; merge_commit_sha?: string | null }[]>;
  /** The repo's deployment rows, newest first. */
  deployments(projectId: string, repoId: string): Promise<DeploymentRow[]>;
  runtimeStatus(agentId: string): Promise<Pick<RuntimeStatus, 'status'>>;
  submitAgent(id: string): Promise<unknown>;
  submitMcp(id: string): Promise<unknown>;
}

export function planRun(projectId: string, blocks: Block[], templates: StudioTemplates, now = new Date()): RunState {
  const ordered = [...blocks.filter((b) => b.kind === 'mcp'), ...blocks.filter((b) => b.kind === 'agent')];
  const mcpIds = new Set(blocks.filter((b) => b.kind === 'mcp').map((b) => b.id));
  const items = ordered.map((b): RunItem => {
    if (b.kind === 'mcp') {
      const d = b.draft;
      const auth = d.keyMode === 'none' ? 'none' : 'api_key';
      return {
        blockId: b.id,
        kind: 'mcp_server',
        name: d.name,
        create: { name: d.name, template_name: templates.mcp?.name ?? '', mcp_name: d.name, purpose: d.description },
        mcp: {
          body: {
            kind: 'mcp_server',
            openapi_text: d.spec?.text ?? '',
            exposed_operations: d.exposed,
            auth,
            ...(d.keyMode === 'existing' ? { existing_secret_arn: d.secretArn } : {}),
          },
          needsKey: d.keyMode === 'new',
        },
      };
    }
    const d = b.draft;
    return {
      blockId: b.id,
      kind: 'agent',
      name: d.name,
      create: {
        name: d.name,
        template_name: templates.agent?.name ?? '',
        agent_config: { agent_name: d.name, framework: templates.agent?.framework ?? '', model_id: STUDIO_AGENT_MODEL_ID },
      },
      agent: {
        systemPrompt: d.prompt,
        skills: d.skills.map((s) => ({ name: parseSkill(s.text).name, content: s.text })),
        mcpBlockIds: d.mcpIds.filter((id) => mcpIds.has(id)),
        registryIds: d.mcpIds.filter((id) => !mcpIds.has(id)),
        ...(d.evalItems ? { evalItems: d.evalItems } : {}),
      },
    };
  });
  return { version: 1, projectId, startedAt: now.toISOString(), items, error: null };
}

export function nextAction(s: RunState): RunAction {
  if (s.error) return { type: 'stopped' };
  for (const kind of ['mcp_server', 'agent'] as const) {
    const of = s.items.filter((i) => i.kind === kind);
    const uncreated = of.find((i) => !i.repoId);
    if (uncreated) return { type: 'make_repo', item: uncreated.blockId };
    const failed = of.find((i) => i.repoFailed);
    if (failed) return { type: 'retry_repo', item: failed.blockId };
    const waiting = of.find((i) => !i.repoReady);
    if (waiting) return { type: 'poll_repo', item: waiting.blockId };
    const unproposed = of.find((i) => !i.pr);
    if (unproposed) return { type: 'propose', item: unproposed.blockId };
  }
  const open = s.items.filter((i) => !i.merged).map((i) => i.blockId);
  if (open.length) return { type: 'await_merge', items: open };
  const building = s.items.filter((i) => !i.built).map((i) => i.blockId);
  if (building.length) return { type: 'await_build', items: building };
  const unsubmitted = s.items.find((i) => !i.submitted);
  if (unsubmitted) return { type: 'submit', item: unsubmitted.blockId };
  return { type: 'done' };
}

export const isPoll = (a: RunAction): boolean => a.type === 'poll_repo' || a.type === 'await_merge' || a.type === 'await_build';

function rowOf(a: RunAction): string {
  switch (a.type) {
    case 'make_repo':
    case 'poll_repo':
    case 'retry_repo':
      return `repo:${a.item}`;
    case 'propose':
      return `pr:${a.item}`;
    case 'await_merge':
      return 'review';
    case 'await_build':
      return `build:${a.items[0]}`;
    case 'submit':
      return `submit:${a.item}`;
    case 'stopped':
    case 'done':
      return '';
  }
}

const patch = (s: RunState, id: string, p: Partial<RunItem>): RunState => ({
  ...s,
  items: s.items.map((i) => (i.blockId === id ? { ...i, ...p } : i)),
});

const stepFailure = (steps: StepState[]): string | null => {
  const f = steps.find((x) => x.status === 'failed');
  return f ? (f.error ? `${f.label}: ${f.error}` : `${f.label} failed.`) : null;
};

export function apply(s: RunState, r: RunResult): RunState {
  return { ...fold(s, r) };
}

function fold(s: RunState, r: RunResult): RunState {
  switch (r.type) {
    case 'repo': {
      const item = s.items.find((i) => i.blockId === r.item);
      const failure = stepFailure(r.repo.steps) ?? (r.repo.status === 'failed' ? 'Creating the repository failed.' : null);
      const recordId = (item?.kind === 'mcp_server' ? r.repo.mcp_server_id : r.repo.agent_id) ?? item?.recordId ?? null;
      const next = patch(s, r.item, {
        repoId: r.repo.id,
        repoUrl: r.repo.repo_url ?? item?.repoUrl ?? null,
        recordId,
        repoReady: !failure && r.repo.status === 'ready',
        repoFailed: Boolean(failure),
      });
      return failure ? { ...next, error: { row: `repo:${r.item}`, message: failure } } : next;
    }
    case 'proposed':
      return patch(s, r.item, { pr: { number: r.pr.pr_number, url: r.pr.pr_url, branch: r.pr.branch } });
    case 'prs': {
      let next = s;
      for (const [id, state] of Object.entries(r.states)) {
        if (state === 'merged' && r.mergeShas[id]) {
          const mergedAt = next.items.find((i) => i.blockId === id)?.mergedAt ?? r.at;
          next = patch(next, id, { merged: true, mergeSha: r.mergeShas[id], mergedAt });
        }
        else if (state === 'closed') {
          const name = s.items.find((i) => i.blockId === id)?.name ?? id;
          next = { ...next, error: { row: 'review', message: `The pull request for ${name} was closed without merging. Reopen it in GitHub, then retry.` } };
        }
      }
      return next;
    }
    case 'built': {
      const next = (r.started ?? []).reduce((acc, id) => patch(acc, id, { buildStarted: true }), s);
      return r.ready.reduce((acc, id) => patch(acc, id, { built: true }), next);
    }
    case 'build_failed':
      return { ...patch(s, r.item, { failedAt: r.at }), error: { row: `build:${r.item}`, message: r.message } };
    case 'submitted':
      return patch(s, r.item, { submitted: true });
    case 'failed':
      return { ...s, error: { row: r.row ?? rowOf(nextAction(s)), message: r.message } };
  }
}

export const FIX_IN_REPO = 'Fix it in the repository, then check again.';
const fixHint = (msg: string) => `${msg.replace(/[.\s]+$/, '')}. ${FIX_IN_REPO}`;

export function buildVerdict(
  rows: DeploymentRow[],
  mergeSha: string,
  failedAt = '',
): { state: 'pending' | 'succeeded' | 'failed'; at: string; error?: string } {
  const ids = new Set(
    rows
      .filter((r) => r.outcome === 'started' && r.build_id && (r.source_sha === mergeSha || (failedAt && r.started_at > failedAt)))
      .map((r) => r.build_id),
  );
  const last = rows
    .filter((r) => r.outcome !== 'started' && r.started_at > failedAt && (r.source_sha === mergeSha || (r.build_id && ids.has(r.build_id))))
    .sort((x, y) => (x.started_at < y.started_at ? 1 : -1))[0];
  if (!last) return { state: 'pending', at: failedAt };
  if (last.outcome === 'failed') return { state: 'failed', at: last.started_at, error: last.error || 'The build failed.' };
  return last.outcome === 'succeeded' ? { state: 'succeeded', at: last.started_at } : { state: 'pending', at: failedAt };
}

const message = (e: unknown): string => (e instanceof Error ? e.message : String(e));

function proposalBody(s: RunState, item: RunItem, keys: Record<string, string>): ProposalRequest | string {
  if (item.mcp) {
    if (!item.mcp.needsKey) return item.mcp.body;
    const key = keys[item.blockId];
    return key ? { ...item.mcp.body, api_key: key } : KEY_LOST_MESSAGE;
  }
  const a = item.agent!;
  const created = a.mcpBlockIds.map((id) => s.items.find((i) => i.blockId === id)?.recordId).filter((x): x is string => Boolean(x));
  return {
    kind: 'agent',
    system_prompt: a.systemPrompt,
    skills: a.skills,
    declared_mcp_server_ids: [...new Set([...created, ...a.registryIds])],
    ...(a.evalItems ? { eval_items: a.evalItems } : {}),
  };
}

export async function runAction(
  s: RunState,
  a: RunAction,
  api: StudioRunApi,
  keys: Record<string, string>,
): Promise<RunResult> {
  const item = (id: string) => s.items.find((i) => i.blockId === id)!;
  try {
    switch (a.type) {
      case 'make_repo':
        return { type: 'repo', item: a.item, repo: await api.createRepo(s.projectId, item(a.item).create) };
      case 'poll_repo':
        return { type: 'repo', item: a.item, repo: await api.repoStatus(s.projectId, item(a.item).repoId!) };
      case 'retry_repo':
        return { type: 'repo', item: a.item, repo: await api.retryRepo(s.projectId, item(a.item).repoId!) };
      case 'propose': {
        const it = item(a.item);
        const body = proposalBody(s, it, keys);
        if (typeof body === 'string') return { type: 'failed', message: body };
        const pr = await api.propose(s.projectId, it.repoId!, body);
        return { type: 'proposed', item: a.item, pr };
      }
      case 'await_merge': {
        const states: Record<string, string> = {};
        const mergeShas: Record<string, string> = {};
        for (const id of a.items) {
          const it = item(id);
          const pr = (await api.pullRequests(it.repoId!)).find((p) => p.number === it.pr?.number);
          states[id] = pr?.state ?? 'open';
          if (pr?.merge_commit_sha) mergeShas[id] = pr.merge_commit_sha;
        }
        return { type: 'prs', states, mergeShas, at: new Date().toISOString() };
      }
      case 'await_build': {
        const ready: string[] = [];
        const started: string[] = [];
        for (const id of a.items) {
          const it = item(id);
          try {
            const rows = await api.deployments(s.projectId, it.repoId!);
            if (rows.some((r) => r.outcome === 'started' && r.source_sha === it.mergeSha)) started.push(id);
            const v = buildVerdict(rows, it.mergeSha ?? '', it.failedAt);
            if (v.state === 'failed') return { type: 'build_failed', item: id, message: fixHint(v.error ?? 'The build failed'), at: v.at };
            if (v.state !== 'succeeded') continue;
            if (it.kind === 'mcp_server') {
              if ((await api.repoStatus(s.projectId, it.repoId!)).gateway_arn) ready.push(id);
              continue;
            }
            const rt = (await api.runtimeStatus(it.recordId!)).status;
            if (rt === 'failed') return { type: 'build_failed', item: id, message: fixHint('The agent runtime failed to start'), at: v.at };
            if (rt === 'ready') ready.push(id);
          } catch (e) {
            return { type: 'failed', message: message(e), row: `build:${id}` };
          }
        }
        return { type: 'built', ready, started };
      }
      case 'submit': {
        const it = item(a.item);
        await (it.kind === 'mcp_server' ? api.submitMcp(it.recordId!) : api.submitAgent(it.recordId!));
        return { type: 'submitted', item: a.item };
      }
      case 'stopped':
      case 'done':
        return { type: 'failed', message: 'Nothing to run.' };
    }
  } catch (e) {
    return { type: 'failed', message: message(e) };
  }
}

export type RowStatus = 'pending' | 'running' | 'waiting' | 'done' | 'failed';

export interface TimelineRow {
  key: string;
  label: string;
  status: RowStatus;
  detail?: string;
  href?: string;
}

export function timeline(s: RunState, now = new Date()): TimelineRow[] {
  const a = nextAction(s);
  const current = new Set(
    a.type === 'await_build' ? a.items.map((id) => `build:${id}`) : a.type === 'stopped' || a.type === 'done' ? [] : [rowOf(a)],
  );
  const row = (key: string, label: string, isDone: boolean, href?: string): TimelineRow => {
    if (s.error?.row === key) return { key, label, status: 'failed', detail: s.error.message, href };
    if (isDone) return { key, label, status: 'done', href };
    if (current.has(key)) return { key, label, status: key === 'review' ? 'waiting' : 'running', href };
    return { key, label, status: 'pending', href };
  };
  const rows: TimelineRow[] = [];
  for (const kind of ['mcp_server', 'agent'] as const) {
    const of = s.items.filter((i) => i.kind === kind);
    for (const i of of) rows.push(row(`repo:${i.blockId}`, `Repository ${i.name} created`, Boolean(i.repoReady), i.repoUrl ?? undefined));
    for (const i of of) rows.push(row(`pr:${i.blockId}`, `Pull request opened (${i.name})`, Boolean(i.pr), i.pr?.url));
  }
  const merged = s.items.filter((i) => i.merged).length;
  const n = s.items.length;
  rows.push(row('review', n > 1 ? `Waiting on reviewer in GitHub (${merged} of ${n} merged)` : 'Waiting on reviewer in GitHub', merged === n));
  for (const i of s.items) {
    const r = row(`build:${i.blockId}`, i.kind === 'mcp_server' ? `MCP gateway provisioned (${i.name})` : `Agent runtime ready (${i.name})`, Boolean(i.built));
    const late = i.mergedAt && !i.buildStarted && now.getTime() - Date.parse(i.mergedAt) >= NO_BUILD_HINT_MS;
    rows.push(r.status === 'running' && late ? { ...r, detail: NO_BUILD_HINT, href: i.repoUrl ? `${i.repoUrl}/actions` : undefined } : r);
  }
  for (const i of s.items) rows.push(row(`submit:${i.blockId}`, `Submitted for approval (${i.name})`, Boolean(i.submitted)));
  return rows;
}

export type RunBadge = 'running' | 'waiting' | 'failed' | 'done';

export function runBadge(s: RunState): RunBadge {
  if (s.error) return 'failed';
  const a = nextAction(s);
  return a.type === 'done' ? 'done' : a.type === 'await_merge' ? 'waiting' : 'running';
}

export function declaredNames(item: RunItem, run: RunState, registry: RegistryMcp[]): string[] {
  const a = item.agent;
  if (!a) return [];
  return [
    ...a.mcpBlockIds.map((id) => run.items.find((i) => i.blockId === id)?.name ?? id),
    ...a.registryIds.map((id) => registry.find((r) => r.id === id)?.name ?? id),
  ];
}

export function compositionLabelOf(run: RunState): string {
  const n = (k: RunItem['kind']) => run.items.filter((i) => i.kind === k).length;
  const part = (c: number, one: string, many: string) => (c ? `${c} ${c === 1 ? one : many}` : '');
  return [part(n('mcp_server'), 'MCP server', 'MCP servers'), part(n('agent'), 'agent', 'agents')].filter(Boolean).join(' and ');
}

type RunStorage = Pick<Storage, 'getItem' | 'setItem' | 'removeItem' | 'key' | 'length'>;

const parse = (raw: string | null): RunState | null => {
  if (!raw) return null;
  try {
    const s = JSON.parse(raw) as RunState;
    return s && s.version === 1 && Array.isArray(s.items) ? s : null;
  } catch {
    return null;
  }
};

export function saveRun(storage: RunStorage, s: RunState): void {
  try {
    storage.setItem(runKey(s.projectId), JSON.stringify(s));
  } catch {
    // Private mode or quota: the run goes on, it just will not survive a reload.
  }
}

export function loadRun(storage: RunStorage, projectId: string): RunState | null {
  try {
    return parse(storage.getItem(runKey(projectId)));
  } catch {
    return null;
  }
}

export function clearRun(storage: RunStorage, projectId: string): void {
  try {
    storage.removeItem(runKey(projectId));
  } catch {
    // Private mode or quota.
  }
}

export function findSavedRun(storage: RunStorage): RunState | null {
  let best: RunState | null = null;
  try {
    for (let i = 0; i < storage.length; i++) {
      const k = storage.key(i);
      if (!k?.startsWith(RUN_KEY_PREFIX)) continue;
      const s = parse(storage.getItem(k));
      if (s && (!best || s.startedAt > best.startedAt)) best = s;
    }
  } catch {
    return best;
  }
  return best;
}
