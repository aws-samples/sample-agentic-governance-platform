// Studio — the Operations Build & Run centerpiece (Epic 18, Task 5).
//
// The user types a plain-language use case, picks a build kind (Agent | LLM
// Workflow), and hits Synthesize. The platform then *performs* the build in
// staged beats driven by a `phase` state machine:
//
//   idle → searching → graph → running → done
//
//   • searching (~700ms): "Searching registry…" then reveal the matched skill +
//     MCP chips pulled from the SynthPlan.
//   • graph: render the plan as an @xyflow/react node-link flow, laid out
//     left→right with the SAME dagre pattern the Governance Graph uses
//     (governance/graph/layout.ts). Every llm/router node carries a per-node
//     model <select> (options = MODELS).
//   • running: light the nodes one-by-one (sequential setTimeout), then surface
//     tokens/run + $/run from the plan.
//   • done: reveal the Readiness panel — the deploy gate.
//
// The Readiness panel renders initialReadiness(): ok items show an emerald check;
// warn items show an amber badge + an inline action that flips them to ok via the
// pure resolveItem/runEval helpers. Deploy stays disabled until isDeployReady().
// On Deploy we PROMOTE_AGENT into the shared demo store and route to Deployments.
//
// All pure logic (synthesize / readiness gate) is Task 4 (studioLogic.ts); the
// seed data + store are Task 3; the page frame + tokens are Task 1. This file is
// only the React composition + the staged interaction on top of those.
//
// House style: emerald-on-glass Ops tokens (opsUi.ts), inline-SVG glyphs (no icon
// lib), Tailwind v4 utility strings, 2-space indent — matching the other Ops pages.

import { useCallback, useEffect, useMemo, useRef, useState, type JSX } from 'react';
import { useNavigate } from 'react-router-dom';
import {
  ReactFlow,
  ReactFlowProvider,
  Background,
  Handle,
  Position,
  useReactFlow,
  type Node,
  type Edge,
  type NodeProps,
} from '@xyflow/react';
import dagre from 'dagre';
import '@xyflow/react/dist/style.css';

import { ComingSoonBanner } from '../shared/comingSoon';
import OpsPage from './OpsPage';
import { OPS_CARD, OPS_BADGE, OPS_PRIMARY_BTN } from './opsUi';
import { MODELS } from './demoData';
import { useDemoStore } from './demoStore';
import {
  synthesize,
  initialReadiness,
  resolveItem,
  runEval,
  isDeployReady,
  EVAL_THRESHOLD,
  type SynthPlan,
  type SynthNode,
  type NodeType,
  type BuildKind,
  type ReadinessItem,
  type ReadinessKey,
} from './studioLogic';

// ───────────────────────────── constants ─────────────────────────────

const DEFAULT_USE_CASE = 'Triage incoming claims emails and route to the right adjuster';
const PROMOTED_AGENT_NAME = 'Claims Triage Agent';
const PROMOTED_ACCOUNT = 'aws://9001-0002-0003';

// Phase machine that drives the whole synthesis spectacle.
type Phase = 'idle' | 'searching' | 'graph' | 'running' | 'done';

// Per-beat timings (ms). Kept here so the staged reveal reads at a glance.
const SEARCHING_MS = 700; // registry scan before the chips land
const GRAPH_TO_RUN_MS = 650; // graph settles, then the run kicks off
const NODE_LIGHT_MS = 360; // gap between each node lighting up
const RUN_TO_DONE_MS = 600; // metrics linger, then readiness reveals
const EVAL_PASS_SCORE = 92; // the score the re-run lands on (≥ EVAL_THRESHOLD)

// dagre layout footprint — mirrors governance/graph/layout.ts (180×60 boxes, LR).
const NODE_WIDTH = 184;
const NODE_HEIGHT = 64;
const NODE_SEP = 36;
const RANK_SEP = 96;

// ───────────────────────────── flow node data + layout ─────────────────────────────
// We carry the plan node + a couple of render flags through React Flow's node
// `data`. The dagre pass below mirrors the governance idiom: LR ranking, dagre
// returns CENTER coords so we shift to React Flow's TOP-LEFT origin.

type StudioNodeData = {
  node: SynthNode;
  lit: boolean;
  // per-node model selection lives on the page; the node only needs the value +
  // a setter so the <select> is controlled. Undefined for non-model nodes.
  model?: string;
  onModelChange?: (value: string) => void;
};
type StudioFlowNode = Node<StudioNodeData>;

/** Map a SynthPlan → React Flow nodes/edges (positions filled by `layoutFlow`). */
function toFlow(
  plan: SynthPlan,
  litIds: Set<string>,
  models: Record<string, string>,
  onModelChange: (nodeId: string, value: string) => void,
): { nodes: StudioFlowNode[]; edges: Edge[] } {
  const nodes: StudioFlowNode[] = plan.nodes.map((node) => {
    const isModelNode = node.type === 'llm' || node.type === 'router';
    return {
      id: node.id,
      type: 'studio',
      position: { x: 0, y: 0 },
      data: {
        node,
        lit: litIds.has(node.id),
        model: isModelNode ? (models[node.id] ?? node.model ?? MODELS[0].label) : undefined,
        onModelChange: isModelNode ? (value: string) => onModelChange(node.id, value) : undefined,
      },
    };
  });

  const edges: Edge[] = plan.edges.map((e) => {
    const lit = litIds.has(e.from) && litIds.has(e.to);
    return {
      id: `${e.from}->${e.to}`,
      source: e.from,
      target: e.to,
      animated: lit,
      style: {
        stroke: lit ? '#059669' : '#cbd5e1', // emerald-600 lit / slate-300 idle
        strokeWidth: lit ? 2 : 1.5,
        transition: 'stroke 0.3s ease, stroke-width 0.3s ease',
      },
    };
  });

  return { nodes, edges };
}

/** Run dagre LR — same pattern as governance/graph/layout.ts. */
function layoutFlow(nodes: StudioFlowNode[], edges: Edge[]): StudioFlowNode[] {
  const g = new dagre.graphlib.Graph();
  g.setGraph({ rankdir: 'LR', nodesep: NODE_SEP, ranksep: RANK_SEP });
  g.setDefaultEdgeLabel(() => ({}));

  for (const node of nodes) g.setNode(node.id, { width: NODE_WIDTH, height: NODE_HEIGHT });
  for (const edge of edges) g.setEdge(edge.source, edge.target);

  dagre.layout(g);

  return nodes.map((node) => {
    const { x, y } = g.node(node.id);
    return { ...node, position: { x: x - NODE_WIDTH / 2, y: y - NODE_HEIGHT / 2 } };
  });
}

// ───────────────────────────── inline glyphs (no icon lib) ─────────────────────────────

function CheckGlyph({ className }: { className?: string }) {
  return (
    <svg viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth={2.2} aria-hidden="true" className={className}>
      <path strokeLinecap="round" strokeLinejoin="round" d="M3.5 8.5 6.5 11.5 12.5 5" />
    </svg>
  );
}

function SparkGlyph({ className }: { className?: string }) {
  return (
    <svg viewBox="0 0 20 20" fill="currentColor" aria-hidden="true" className={className}>
      <path d="M10 1.5l1.6 4.4 4.4 1.6-4.4 1.6L10 13.5 8.4 9.1 4 7.5l4.4-1.6L10 1.5Zm6 9.5l.8 2.2 2.2.8-2.2.8L16 17l-.8-2.2-2.2-.8 2.2-.8.8-2.2Z" />
    </svg>
  );
}

// Per-node-type glyph + accent tint. The type — not the instance — drives color,
// echoing the governance graph's per-type-accent approach but in the Ops emerald
// family (skills/mcp/router keep their own restrained hues so the chain reads).
function NodeGlyph({ type, className }: { type: NodeType; className?: string }) {
  switch (type) {
    case 'input':
      return (
        <svg viewBox="0 0 20 20" fill="currentColor" aria-hidden="true" className={className}>
          <path d="M3 4.5A1.5 1.5 0 0 1 4.5 3h11A1.5 1.5 0 0 1 17 4.5v11A1.5 1.5 0 0 1 15.5 17h-11A1.5 1.5 0 0 1 3 15.5v-11Zm2 1.2 5 3.4 5-3.4V5.2H5v.5Zm10 1.9-5 3.4-5-3.4v6.3h10V7.6Z" />
        </svg>
      );
    case 'output':
      return (
        <svg viewBox="0 0 20 20" fill="currentColor" aria-hidden="true" className={className}>
          <path d="M10 1.8a8.2 8.2 0 1 0 0 16.4 8.2 8.2 0 0 0 0-16.4Zm-1 11.7-3.2-3.2 1.3-1.3L9 10.8l4-4 1.3 1.3L9 13.5Z" />
        </svg>
      );
    case 'mcp':
      return (
        <svg viewBox="0 0 20 20" fill="currentColor" aria-hidden="true" className={className}>
          <path d="M4 3h12a1 1 0 0 1 1 1v3a1 1 0 0 1-1 1H4a1 1 0 0 1-1-1V4a1 1 0 0 1 1-1Zm0 9h12a1 1 0 0 1 1 1v3a1 1 0 0 1-1 1H4a1 1 0 0 1-1-1v-3a1 1 0 0 1 1-1Zm2-7.5a.75.75 0 1 0 0 1.5.75.75 0 0 0 0-1.5Zm0 9a.75.75 0 1 0 0 1.5.75.75 0 0 0 0-1.5Z" />
        </svg>
      );
    case 'router':
      return (
        <svg viewBox="0 0 20 20" fill="currentColor" aria-hidden="true" className={className}>
          <path d="M6 3a2.5 2.5 0 0 0-.5 4.95V9a3 3 0 0 0 3 3h1v1.05a2.5 2.5 0 1 0 1.5 0V12h1a3 3 0 0 0 3-3V7.95A2.5 2.5 0 1 0 14 3a2.5 2.5 0 0 0-.5 4.95V9a1.5 1.5 0 0 1-1.5 1.5h-4A1.5 1.5 0 0 1 6.5 9V7.95A2.5 2.5 0 0 0 6 3Z" />
        </svg>
      );
    case 'llm':
      return (
        <svg viewBox="0 0 20 20" fill="currentColor" aria-hidden="true" className={className}>
          <path d="M9 1.5a1 1 0 0 1 2 0V3h1.5A3.5 3.5 0 0 1 16 6.5v6A3.5 3.5 0 0 1 12.5 16h-5A3.5 3.5 0 0 1 4 12.5v-6A3.5 3.5 0 0 1 7.5 3H9V1.5ZM7.5 7a1.25 1.25 0 1 0 0 2.5 1.25 1.25 0 0 0 0-2.5Zm5 0a1.25 1.25 0 1 0 0 2.5 1.25 1.25 0 0 0 0-2.5ZM7 12a.75.75 0 0 0 0 1.5h6a.75.75 0 0 0 0-1.5H7Z" />
        </svg>
      );
    case 'skill':
    default:
      return (
        <svg viewBox="0 0 20 20" fill="currentColor" aria-hidden="true" className={className}>
          <path d="M10 1.8 12.4 7l5.6.5-4.3 3.7 1.3 5.5L10 13.9 4.9 16.7l1.3-5.5L1.9 7.5 7.6 7 10 1.8Z" />
        </svg>
      );
  }
}

const NODE_TINT: Record<NodeType, { chip: string; rail: string; label: string }> = {
  input: { chip: 'bg-slate-100 text-slate-600', rail: 'bg-slate-400', label: 'Input' },
  skill: { chip: 'bg-teal-50 text-teal-600', rail: 'bg-teal-500', label: 'Skill' },
  mcp: { chip: 'bg-violet-50 text-violet-600', rail: 'bg-violet-500', label: 'MCP' },
  llm: { chip: 'bg-emerald-50 text-emerald-600', rail: 'bg-emerald-500', label: 'LLM' },
  router: { chip: 'bg-emerald-50 text-emerald-600', rail: 'bg-emerald-500', label: 'Router' },
  output: { chip: 'bg-slate-100 text-slate-600', rail: 'bg-slate-400', label: 'Output' },
};

// ───────────────────────────── custom flow node ─────────────────────────────
// A small rounded card mirroring the governance node footprint (184×64 to match
// the dagre box). It lights up — emerald ring + brightened chip — when `lit`, and
// model nodes carry an inline per-node <select>. Handles are invisible edge
// anchors (read-only flow, like the governance nodes).

const FLOW_HANDLE = '!w-1 !h-1 !min-w-0 !border-0 !bg-transparent';

function StudioNode({ data }: NodeProps<StudioFlowNode>): JSX.Element {
  const { node, lit, model, onModelChange } = data;
  const tint = NODE_TINT[node.type];
  const isModelNode = node.type === 'llm' || node.type === 'router';

  return (
    <div
      title={node.label}
      className={`group/node relative flex items-center gap-2.5 w-[184px] h-[64px] px-3 rounded-2xl border bg-white shadow-sm overflow-hidden transition-all duration-300 ${
        lit ? 'border-emerald-400 ring-2 ring-emerald-300/60 shadow-md' : 'border-slate-200'
      }`}
    >
      <span aria-hidden="true" className={`absolute inset-y-0 left-0 w-1 transition-colors ${lit ? 'bg-emerald-500' : tint.rail}`} />
      <span
        aria-hidden="true"
        className={`shrink-0 h-8 w-8 rounded-lg flex items-center justify-center transition-colors ${
          lit ? 'bg-emerald-100 text-emerald-700' : tint.chip
        }`}
      >
        <NodeGlyph type={node.type} className="h-4.5 w-4.5" />
      </span>
      <div className="min-w-0 flex-1">
        <div className="text-[13px] font-semibold text-slate-900 leading-tight truncate">{node.label}</div>
        {isModelNode && onModelChange ? (
          <select
            value={model}
            onChange={(e) => onModelChange(e.target.value)}
            aria-label={`Model for ${node.label}`}
            className="nodrag mt-0.5 w-full max-w-[136px] bg-transparent text-[11px] text-slate-500 font-medium leading-tight focus:outline-none focus:text-emerald-700 cursor-pointer"
          >
            {MODELS.map((m) => (
              <option key={m.id} value={m.label}>
                {m.label}
              </option>
            ))}
          </select>
        ) : (
          <div className="text-[11px] text-slate-400 leading-tight">{tint.label}</div>
        )}
      </div>
      <Handle type="target" position={Position.Left} isConnectable={false} className={FLOW_HANDLE} />
      <Handle type="source" position={Position.Right} isConnectable={false} className={FLOW_HANDLE} />
    </div>
  );
}

const nodeTypes = { studio: StudioNode };

// Re-frame the canvas whenever the layout identity changes (re-synthesis). Lives
// inside <ReactFlowProvider> so it can grab the instance — the FitViewOnReset idiom
// from GovernanceGraph.tsx, keyed on a nonce.
function FitOnChange({ nonce }: { nonce: number }): null {
  const { fitView } = useReactFlow();
  useEffect(() => {
    // rAF lets React Flow measure the freshly-mounted nodes before we frame them.
    const id = requestAnimationFrame(() => fitView({ padding: 0.18, duration: 300 }));
    return () => cancelAnimationFrame(id);
  }, [nonce, fitView]);
  return null;
}

// ───────────────────────────── the page ─────────────────────────────

export default function Studio(): JSX.Element {
  const navigate = useNavigate();
  const { dispatch } = useDemoStore();

  // --- inputs -------------------------------------------------------------
  const [useCase, setUseCase] = useState(DEFAULT_USE_CASE);
  const [kind, setKind] = useState<BuildKind>('agent');

  // --- synthesis state ----------------------------------------------------
  const [phase, setPhase] = useState<Phase>('idle');
  const [plan, setPlan] = useState<SynthPlan | null>(null);
  const [litIds, setLitIds] = useState<Set<string>>(new Set());
  // per-node model overrides keyed by node id (controlled <select> on llm/router).
  const [nodeModels, setNodeModels] = useState<Record<string, string>>({});
  const [fitNonce, setFitNonce] = useState(0);

  // --- readiness gate -----------------------------------------------------
  const [readiness, setReadiness] = useState<ReadinessItem[]>(initialReadiness);
  const [evalRunning, setEvalRunning] = useState(false);

  // Every pending timer lives here so we can cancel them on unmount OR whenever a
  // fresh synthesis / input change resets the flow (no stale beats firing late).
  const timers = useRef<ReturnType<typeof setTimeout>[]>([]);
  const clearTimers = useCallback(() => {
    timers.current.forEach(clearTimeout);
    timers.current = [];
  }, []);
  const schedule = useCallback((fn: () => void, ms: number) => {
    timers.current.push(setTimeout(fn, ms));
  }, []);
  useEffect(() => clearTimers, [clearTimers]);

  // Reset the whole flow back to idle. Editing the use case or flipping the kind
  // after a synthesis must reset so the graph/readiness reflect the live selection.
  const resetFlow = useCallback(() => {
    clearTimers();
    setPhase('idle');
    setPlan(null);
    setLitIds(new Set());
    setNodeModels({});
    setReadiness(initialReadiness());
    setEvalRunning(false);
  }, [clearTimers]);

  const onUseCaseChange = (value: string) => {
    setUseCase(value);
    if (phase !== 'idle') resetFlow();
  };
  const onKindChange = (next: BuildKind) => {
    if (next === kind) return;
    setKind(next);
    if (phase !== 'idle') resetFlow();
  };

  // --- the staged synthesis -----------------------------------------------
  const onSynthesize = useCallback(() => {
    clearTimers();
    const nextPlan = synthesize(useCase, kind);
    // seed per-node model selects from the plan's node.model (Claude Opus 4.8).
    const seededModels: Record<string, string> = {};
    for (const n of nextPlan.nodes) {
      if ((n.type === 'llm' || n.type === 'router') && n.model) seededModels[n.id] = n.model;
    }

    setPlan(nextPlan);
    setNodeModels(seededModels);
    setReadiness(initialReadiness());
    setEvalRunning(false);
    setLitIds(new Set());
    setPhase('searching');

    // searching → graph (chips have landed; render the workflow)
    schedule(() => {
      setPhase('graph');
      setFitNonce((n) => n + 1);
    }, SEARCHING_MS);

    // graph → running (kick off the node-by-node light-up)
    schedule(() => {
      setPhase('running');
      // light nodes sequentially in plan order.
      nextPlan.nodes.forEach((n, i) => {
        schedule(() => {
          setLitIds((prev) => {
            const next = new Set(prev);
            next.add(n.id);
            return next;
          });
        }, i * NODE_LIGHT_MS);
      });
      // running → done after the last node lights + the metrics linger.
      const runTail = nextPlan.nodes.length * NODE_LIGHT_MS + RUN_TO_DONE_MS;
      schedule(() => setPhase('done'), runTail);
    }, SEARCHING_MS + GRAPH_TO_RUN_MS);
  }, [useCase, kind, clearTimers, schedule]);

  // --- readiness actions --------------------------------------------------
  const onResolveItem = (key: ReadinessKey) => {
    if (key === 'eval') {
      // animate the eval 78→92 over a short beat, then commit the pass.
      setEvalRunning(true);
      schedule(() => {
        setReadiness((prev) => runEval(prev, EVAL_PASS_SCORE));
        setEvalRunning(false);
      }, 900);
      return;
    }
    setReadiness((prev) => resolveItem(prev, key));
  };

  const deployReady = isDeployReady(readiness);

  const onDeploy = () => {
    if (!plan || !deployReady) return;
    dispatch({
      type: 'PROMOTE_AGENT',
      agent: {
        name: PROMOTED_AGENT_NAME,
        useCase,
        kind,
        model: plan.model,
        account: PROMOTED_ACCOUNT,
        costPerRun: plan.costPerRun,
      },
    });
    navigate('/ops/deployments');
  };

  // --- flow arrays (laid out once per plan/light change) ------------------
  const onNodeModelChange = useCallback((nodeId: string, value: string) => {
    setNodeModels((prev) => ({ ...prev, [nodeId]: value }));
  }, []);

  const { nodes: flowNodes, edges: flowEdges } = useMemo(() => {
    if (!plan) return { nodes: [] as StudioFlowNode[], edges: [] as Edge[] };
    const flow = toFlow(plan, litIds, nodeModels, onNodeModelChange);
    return { nodes: layoutFlow(flow.nodes, flow.edges), edges: flow.edges };
  }, [plan, litIds, nodeModels, onNodeModelChange]);

  const showChips = plan && phase !== 'idle';
  const showGraph = plan && (phase === 'graph' || phase === 'running' || phase === 'done');
  const showMetrics = plan && (phase === 'running' || phase === 'done');
  const showReadiness = plan && phase === 'done';

  return (
    <OpsPage
      backTo="/ops"
      title="Studio"
      subtitle="Describe a use case and the platform synthesizes a governed agent or workflow from registered skills, MCPs, and models."
    >
      <ComingSoonBanner />

      {/* ── Brief: use case + kind + synthesize ───────────────────────── */}
      <div className={`${OPS_CARD} p-5 mb-6`}>
        <label htmlFor="studio-usecase" className="block text-[11px] font-medium text-slate-500 uppercase tracking-wide mb-2">
          What should it do?
        </label>
        <textarea
          id="studio-usecase"
          value={useCase}
          onChange={(e) => onUseCaseChange(e.target.value)}
          rows={2}
          className="w-full resize-none rounded-lg border border-emerald-200/70 bg-white/70 px-3.5 py-2.5 text-sm text-slate-800 placeholder:text-slate-400 focus:outline-none focus:ring-2 focus:ring-emerald-400/50 focus:border-emerald-400 transition-shadow"
          placeholder="Describe the task in plain language…"
        />

        <div className="mt-4 flex flex-wrap items-center justify-between gap-3">
          <KindToggle kind={kind} onChange={onKindChange} />
          <button type="button" onClick={onSynthesize} className={`${OPS_PRIMARY_BTN} inline-flex items-center gap-1.5`}>
            <SparkGlyph className="h-4 w-4" />
            {phase === 'idle' ? 'Synthesize' : 'Re-synthesize'}
          </button>
        </div>
      </div>

      {/* ── Searching → chips ──────────────────────────────────────────── */}
      {showChips && plan && (
        <div className={`${OPS_CARD} p-5 mb-6`}>
          <div className="flex items-center gap-2 text-[11px] font-medium uppercase tracking-wide mb-3">
            {phase === 'searching' ? (
              <span className="inline-flex items-center gap-2 text-emerald-700">
                <Spinner />
                Searching registry…
              </span>
            ) : (
              <span className="inline-flex items-center gap-1.5 text-slate-500">
                <CheckGlyph className="h-3.5 w-3.5 text-emerald-600" />
                Matched from the registry
              </span>
            )}
          </div>

          {phase !== 'searching' && (
            <div className="flex flex-col gap-3">
              <ChipRow label="Skills" items={plan.skills.map((s) => s.name)} tint="teal" />
              {plan.mcps.length > 0 && (
                <ChipRow label="MCP servers" items={plan.mcps.map((m) => m.name)} tint="violet" />
              )}
            </div>
          )}
        </div>
      )}

      {/* ── Graph ──────────────────────────────────────────────────────── */}
      {showGraph && (
        <div className={`${OPS_CARD} mb-6 overflow-hidden`}>
          <div className="flex items-center justify-between px-5 py-3 border-b border-emerald-100/70">
            <div className="text-[11px] font-medium text-slate-500 uppercase tracking-wide">
              {kind === 'agent' ? 'Agent workflow' : 'LLM workflow'}
            </div>
            {phase === 'running' && (
              <span className="inline-flex items-center gap-2 text-[11px] font-semibold text-emerald-700">
                <Spinner />
                Tracing the graph…
              </span>
            )}
          </div>
          <div className="h-[300px] bg-gradient-to-br from-emerald-50/40 to-white">
            <ReactFlowProvider>
              <ReactFlow
                nodes={flowNodes}
                edges={flowEdges}
                nodeTypes={nodeTypes}
                fitView
                fitViewOptions={{ padding: 0.18 }}
                minZoom={0.3}
                maxZoom={1.4}
                proOptions={{ hideAttribution: true }}
                nodesDraggable={false}
                nodesConnectable={false}
                elementsSelectable={false}
                panOnScroll={false}
                zoomOnScroll={false}
                preventScrolling={false}
                className="!bg-transparent"
              >
                <FitOnChange nonce={fitNonce} />
                <Background color="#a7f3d0" gap={22} size={1} />
              </ReactFlow>
            </ReactFlowProvider>
          </div>

          {/* metrics strip — tokens/run + $/run from the plan */}
          {showMetrics && plan && (
            <div className="flex flex-wrap items-center gap-x-8 gap-y-2 px-5 py-3 border-t border-emerald-100/70 bg-white/50">
              <Metric label="Model" value={plan.model} />
              <Metric label="Tokens / run" value={plan.tokensPerRun.toLocaleString()} />
              <Metric label="Cost / run" value={`$${plan.costPerRun.toFixed(4)}`} />
            </div>
          )}
        </div>
      )}

      {/* ── Readiness gate ─────────────────────────────────────────────── */}
      {showReadiness && plan && (
        <div className={`${OPS_CARD} p-5`}>
          <div className="flex items-end justify-between gap-4 mb-4">
            <div>
              <div className="text-sm font-semibold text-slate-900">Readiness</div>
              <p className="text-xs text-slate-500 mt-0.5">
                Resolve every check to unlock deployment. Eval must reach {EVAL_THRESHOLD}%.
              </p>
            </div>
            <button
              type="button"
              onClick={onDeploy}
              disabled={!deployReady}
              className={`shrink-0 inline-flex items-center gap-1.5 px-3.5 py-1.5 rounded-lg text-sm font-medium transition-colors ${
                deployReady
                  ? 'bg-emerald-600 text-white hover:bg-emerald-700'
                  : 'bg-slate-100 text-slate-400 cursor-not-allowed'
              }`}
            >
              <CheckGlyph className="h-4 w-4" />
              Deploy
            </button>
          </div>

          <ul className="divide-y divide-emerald-100/70">
            {readiness.map((item) => (
              <ReadinessRow
                key={item.key}
                item={item}
                busy={item.key === 'eval' && evalRunning}
                onAction={() => onResolveItem(item.key)}
              />
            ))}
          </ul>
        </div>
      )}
    </OpsPage>
  );
}

// ───────────────────────────── sub-components ─────────────────────────────

function KindToggle({ kind, onChange }: { kind: BuildKind; onChange: (k: BuildKind) => void }): JSX.Element {
  const options: { value: BuildKind; label: string }[] = [
    { value: 'agent', label: 'Agent' },
    { value: 'workflow', label: 'LLM Workflow' },
  ];
  return (
    <div role="radiogroup" aria-label="Build kind" className="inline-flex rounded-lg border border-emerald-200/70 bg-white/60 p-0.5">
      {options.map((o) => {
        const active = kind === o.value;
        return (
          <button
            key={o.value}
            type="button"
            role="radio"
            aria-checked={active}
            onClick={() => onChange(o.value)}
            className={`px-3 py-1.5 rounded-md text-sm font-medium transition-colors ${
              active ? 'bg-emerald-600 text-white shadow-sm' : 'text-slate-600 hover:text-slate-900'
            }`}
          >
            {o.label}
          </button>
        );
      })}
    </div>
  );
}

function ChipRow({ label, items, tint }: { label: string; items: string[]; tint: 'teal' | 'violet' }): JSX.Element {
  const chip =
    tint === 'teal' ? 'bg-teal-50 text-teal-700 border-teal-200/70' : 'bg-violet-50 text-violet-700 border-violet-200/70';
  return (
    <div className="flex items-baseline gap-3">
      <span className="shrink-0 w-24 text-[11px] font-medium text-slate-400 uppercase tracking-wide pt-1">{label}</span>
      <div className="flex flex-wrap gap-1.5">
        {items.map((name, i) => (
          <span
            key={name}
            // a brief staggered fade-in so the chips "land" one after another.
            style={{ animation: `studioChipIn 0.35s ease both`, animationDelay: `${i * 70}ms` }}
            className={`inline-flex items-center gap-1.5 text-xs font-medium px-2.5 py-1 rounded-full border ${chip}`}
          >
            <span aria-hidden="true" className="h-1.5 w-1.5 rounded-full bg-current opacity-70" />
            {name}
          </span>
        ))}
      </div>
      {/* keyframes scoped via a style tag — Tailwind v4 has no util for a custom stagger. */}
      <style>{`@keyframes studioChipIn{from{opacity:0;transform:translateY(4px)}to{opacity:1;transform:translateY(0)}}`}</style>
    </div>
  );
}

function Metric({ label, value }: { label: string; value: string }): JSX.Element {
  return (
    <div className="flex flex-col">
      <span className="text-[10px] font-medium text-slate-400 uppercase tracking-wide">{label}</span>
      <span className="text-sm font-semibold text-slate-900 tabular-nums">{value}</span>
    </div>
  );
}

function Spinner(): JSX.Element {
  return (
    <svg viewBox="0 0 16 16" aria-hidden="true" className="h-3.5 w-3.5 animate-spin text-emerald-600">
      <circle cx="8" cy="8" r="6" fill="none" stroke="currentColor" strokeWidth="2.5" strokeOpacity="0.25" />
      <path d="M8 2a6 6 0 0 1 6 6" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" />
    </svg>
  );
}

function ReadinessRow({
  item,
  busy,
  onAction,
}: {
  item: ReadinessItem;
  busy: boolean;
  onAction: () => void;
}): JSX.Element {
  const ok = item.status === 'ok';
  return (
    <li className="flex items-center gap-3 py-2.5">
      {/* status icon */}
      <span
        aria-hidden="true"
        className={`shrink-0 h-6 w-6 rounded-full flex items-center justify-center transition-colors ${
          ok ? 'bg-emerald-100 text-emerald-700' : 'bg-amber-100 text-amber-700'
        }`}
      >
        {ok ? <CheckGlyph className="h-3.5 w-3.5" /> : <span className="h-2 w-2 rounded-full bg-current" />}
      </span>

      <div className="min-w-0 flex-1">
        <div className="text-sm font-medium text-slate-800">{item.label}</div>
      </div>

      {/* detail (eval %) — tabular so the 78→92 swap doesn't jump */}
      {item.detail && (
        <span className={`text-xs font-semibold tabular-nums ${ok ? 'text-emerald-700' : 'text-amber-700'}`}>
          {item.detail}
        </span>
      )}

      {/* status pill — emerald ok / amber warn (shared OPS_BADGE color pairs) */}
      <span
        className={`inline-flex items-center gap-1.5 text-[11px] font-semibold px-2 py-0.5 rounded-full ${
          ok ? OPS_BADGE.ok : OPS_BADGE.warn
        }`}
      >
        <span aria-hidden="true">●</span>
        {ok ? 'Ready' : 'Attention'}
      </span>

      {/* inline action for warn items */}
      {!ok && item.action && (
        <button
          type="button"
          onClick={onAction}
          disabled={busy}
          className="shrink-0 inline-flex items-center gap-1.5 px-2.5 py-1 rounded-lg border border-emerald-300 bg-white text-emerald-700 text-xs font-medium hover:bg-emerald-50 transition-colors disabled:opacity-60 disabled:cursor-wait"
        >
          {busy && <Spinner />}
          {busy ? 'Running…' : item.action}
        </button>
      )}
    </li>
  );
}
