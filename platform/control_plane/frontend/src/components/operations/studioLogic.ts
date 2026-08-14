// Pure logic for the Operations Studio centerpiece (Epic 18). `synthesize` turns
// a plain-language use case + a build kind into a plan graph (nodes + edges over
// the shared Claims-Triage seed data), and the readiness-gate helpers model the
// deploy checklist. No UI here — Task 5 renders these exact shapes. House style
// mirrors demoData.ts: TS interfaces + pure functions (map/spread, no mutation).

import {
  type Skill,
  type McpServer,
  type ModelOption,
  findSkillsForUseCase,
  MODELS,
  MCP_SERVERS,
  estimateCost,
} from './demoData';

// ─────────────────────────── Synthesis ───────────────────────────
export type BuildKind = 'agent' | 'workflow';
export type NodeType = 'input' | 'skill' | 'mcp' | 'llm' | 'router' | 'output';

export interface SynthNode {
  id: string;
  label: string;
  type: NodeType;
  model?: string;
}

export interface SynthEdge {
  from: string;
  to: string;
}

export interface SynthPlan {
  kind: BuildKind;
  nodes: SynthNode[];
  edges: SynthEdge[];
  skills: Skill[];
  mcps: McpServer[];
  model: string;
  tokensPerRun: number;
  costPerRun: number;
}

// Fixed, realistic per-run token budget for the demo cost estimate.
const INPUT_TOKENS_PER_RUN = 1800;
const OUTPUT_TOKENS_PER_RUN = 600;

/** Chain a list of node ids into sequential edges (n - 1 edges). */
function chainEdges(ids: string[]): SynthEdge[] {
  const edges: SynthEdge[] = [];
  for (let i = 0; i < ids.length - 1; i++) {
    edges.push({ from: ids[i], to: ids[i + 1] });
  }
  return edges;
}

/**
 * Turn a use case + kind into a plan graph over the shared seed data.
 *
 * - `agent`: input → one `skill` node per matched skill → a `mcp` node (the first
 *   MCP server, claims-db) → a `router` node (LLM-backed) → output.
 * - `workflow`: input → one `llm` step per matched skill (no mcp/router) → output,
 *   with `mcps: []`.
 *
 * The model is Claude Opus 4.8 from MODELS; per-node `model` is set on llm/router
 * nodes. `costPerRun` is derived via estimateCost from a fixed token budget.
 */
export function synthesize(useCase: string, kind: BuildKind): SynthPlan {
  const skills = findSkillsForUseCase(useCase);
  const modelOption: ModelOption = MODELS[0]; // Claude Opus 4.8
  const model = modelOption.label;

  const inputNode: SynthNode = { id: 'input', label: 'Inbound claim', type: 'input' };
  const outputNode: SynthNode = { id: 'output', label: 'Triaged result', type: 'output' };

  let middleNodes: SynthNode[];
  let mcps: McpServer[];

  if (kind === 'agent') {
    const skillNodes: SynthNode[] = skills.map((s) => ({
      id: `skill-${s.id}`,
      label: s.name,
      type: 'skill',
    }));
    const mcp = MCP_SERVERS[0]; // claims-db
    mcps = [mcp];
    const mcpNode: SynthNode = { id: `mcp-${mcp.id}`, label: mcp.name, type: 'mcp' };
    const routerNode: SynthNode = { id: 'router', label: 'Adjuster router', type: 'router', model };
    middleNodes = [...skillNodes, mcpNode, routerNode];
  } else {
    mcps = [];
    middleNodes = skills.map((s) => ({
      id: `llm-${s.id}`,
      label: s.name,
      type: 'llm',
      model,
    }));
  }

  const nodes: SynthNode[] = [inputNode, ...middleNodes, outputNode];
  const edges = chainEdges(nodes.map((n) => n.id));

  const tokensPerRun = INPUT_TOKENS_PER_RUN + OUTPUT_TOKENS_PER_RUN;
  const costPerRun = estimateCost(modelOption, INPUT_TOKENS_PER_RUN, OUTPUT_TOKENS_PER_RUN);

  return { kind, nodes, edges, skills, mcps, model, tokensPerRun, costPerRun };
}

// ─────────────────────────── Readiness gate ───────────────────────────
export type ReadinessKey =
  | 'repo'
  | 'identity'
  | 'permissions'
  | 'guardrail'
  | 'registry'
  | 'costOwner'
  | 'eval';

export interface ReadinessItem {
  key: ReadinessKey;
  label: string;
  status: 'ok' | 'warn';
  action?: string;
  detail?: string;
}

export const EVAL_THRESHOLD = 85;

/**
 * The initial deploy checklist: repo/identity/registry already satisfied;
 * permissions/guardrail/costOwner pending with action labels; eval failing at 78%.
 */
export function initialReadiness(): ReadinessItem[] {
  return [
    { key: 'repo', label: 'Repository created', status: 'ok' },
    { key: 'identity', label: 'Workload identity issued', status: 'ok' },
    { key: 'permissions', label: 'Access roles assigned', status: 'warn', action: 'Assign roles' },
    { key: 'guardrail', label: 'Guardrail policy attached', status: 'warn', action: 'Attach policy' },
    { key: 'registry', label: 'Registered in catalog', status: 'ok' },
    { key: 'costOwner', label: 'Cost owner approved', status: 'warn', action: 'Request approval' },
    { key: 'eval', label: 'Eval suite passing', status: 'warn', action: 'Run eval suite', detail: '78%' },
  ];
}

/** Pure: return a new list with the given key flipped to `ok` (action cleared). */
export function resolveItem(items: ReadinessItem[], key: ReadinessKey): ReadinessItem[] {
  return items.map((item) =>
    item.key === key ? { ...item, status: 'ok' as const, action: undefined } : item,
  );
}

/**
 * Pure: re-run the eval. The `eval` item becomes `ok` iff score >= EVAL_THRESHOLD;
 * `detail` always reflects the latest score as a percentage.
 */
export function runEval(items: ReadinessItem[], score: number): ReadinessItem[] {
  return items.map((item) =>
    item.key === 'eval'
      ? {
          ...item,
          status: score >= EVAL_THRESHOLD ? ('ok' as const) : ('warn' as const),
          detail: `${score}%`,
          action: score >= EVAL_THRESHOLD ? undefined : item.action,
        }
      : item,
  );
}

/** True only when every readiness item is `ok`. */
export function isDeployReady(items: ReadinessItem[]): boolean {
  return items.every((item) => item.status === 'ok');
}
