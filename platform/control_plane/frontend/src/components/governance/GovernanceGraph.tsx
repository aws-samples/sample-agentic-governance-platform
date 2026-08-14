// GovernanceGraph — the Epic 11 page that assembles the read-only node-link view
// of Users/Groups→Agents and Agents→MCP access relationships from Microsoft Entra.
//
// This page OWNS:
//  - the fetch (governanceGraphApi.get) + loading / error / empty states (the
//    AgentsList idiom: white/70 backdrop-blur cards, slate text, blue-600 primary);
//  - the API→ReactFlow transform (toFlow) + the dagre LR layout (layout), both pure
//    T7 helpers, re-derived via useMemo over the fetched graph + the active filters;
//  - the React Flow canvas (Background / Controls / MiniMap) with custom nodeTypes
//    (T8) and edgeTypes (T8);
//  - a glass legend + type/policy filter panel overlaid top-left;
//  - selection → GraphDetailDrawer (T8), building the EXACT GraphSelection payload;
//  - one-hop neighborhood highlight (getConnectedEdges + incomers/outgoers → dim
//    the rest), recomputed purely over the selection.
//
// READ-ONLY (research §5): the only network calls are governanceGraphApi.get() here
// and the drawer's own lazy reads (principal / cedar list). No mutations anywhere.
//
// React Flow gotchas (research §1.1) handled here:
//  - `@xyflow/react/dist/style.css` is imported ONCE, here (the components don't);
//  - the <ReactFlow> parent has an EXPLICIT height (h-full on a flex-sized parent
//    whose ancestor is already viewport-bounded) or the canvas collapses to 0px and
//    renders blank;
//  - ReactFlowProvider wraps the canvas so the store-backed helper hooks are safe.

import { useEffect, useMemo, useState, useCallback, useRef } from 'react';
import {
  ReactFlow,
  ReactFlowProvider,
  Background,
  Controls,
  MiniMap,
  Panel,
  useReactFlow,
  useNodesState,
  getConnectedEdges,
  getIncomers,
  getOutgoers,
} from '@xyflow/react';
import type { Node, Edge } from '@xyflow/react';
import '@xyflow/react/dist/style.css';

import { governanceGraphApi } from '../../api/client';
import type { GovernanceGraph as GovernanceGraphData, GraphNode } from '../../api/client';
import { toFlow, layout } from './graph/layout';
import type { FlowNode, FlowEdge } from './graph/layout';
import { nodeTypes } from './graph/graphNodes';
import { edgeTypes } from './graph/PolicyEdge';
import GraphDetailDrawer from './graph/GraphDetailDrawer';
import type { GraphSelection } from './graph/GraphDetailDrawer';

// ---------------------------------------------------------------------------
// Type-filter model. One toggle per node type; the chip colors mirror each
// node's accent (graphNodes.tsx) so the legend doubles as the key.
// ---------------------------------------------------------------------------
type NodeType = GraphNode['type'];

const TYPE_META: Record<NodeType, { label: string; dot: string; chipOn: string }> = {
  user: { label: 'Users', dot: 'bg-blue-500', chipOn: 'bg-blue-50 text-blue-700 border-blue-200' },
  group: { label: 'Groups', dot: 'bg-cyan-500', chipOn: 'bg-cyan-50 text-cyan-700 border-cyan-200' },
  // Agent owns the teal accent (graphNodes.tsx) — distinct from every MCP kind so
  // the legend dot is an honest key for the new Agent card color.
  agent: { label: 'Agents', dot: 'bg-teal-500', chipOn: 'bg-teal-50 text-teal-700 border-teal-200' },
  mcp: { label: 'MCP servers', dot: 'bg-violet-500', chipOn: 'bg-violet-50 text-violet-700 border-violet-200' },
};
const NODE_TYPE_ORDER: NodeType[] = ['user', 'group', 'agent', 'mcp'];

type TypeVisibility = Record<NodeType, boolean>;
const ALL_VISIBLE: TypeVisibility = { user: true, group: true, agent: true, mcp: true };

const DIMMED_NODE = 'opacity-25 transition-opacity';
const DIMMED_EDGE = 0.12; // edge opacity when outside the selected neighborhood

// prefers-reduced-motion: read once + subscribe so the selection-flow animation
// (Feature 1) is gated off for users who ask for reduced motion. The highlight
// still works via brightness/dim — only the marching-ants dash is suppressed.
function usePrefersReducedMotion(): boolean {
  const [reduced, setReduced] = useState<boolean>(() => {
    if (typeof window === 'undefined' || !window.matchMedia) return false;
    return window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  });
  useEffect(() => {
    if (typeof window === 'undefined' || !window.matchMedia) return;
    const mql = window.matchMedia('(prefers-reduced-motion: reduce)');
    const onChange = (e: MediaQueryListEvent) => setReduced(e.matches);
    mql.addEventListener('change', onChange);
    return () => mql.removeEventListener('change', onChange);
  }, []);
  return reduced;
}

export default function GovernanceGraph() {
  const [graph, setGraph] = useState<GovernanceGraphData | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [reloadNonce, setReloadNonce] = useState(0);

  // Filters (re-derive the flow arrays). Type toggles + a "policy-governed only"
  // toggle that narrows can_call edges to the Cedar-enforcing ones + their endpoints.
  const [typeVisible, setTypeVisible] = useState<TypeVisibility>(ALL_VISIBLE);
  const [policyOnly, setPolicyOnly] = useState(false);
  // "Hide unconnected": drop Agent/MCP nodes with no edge touching them (after the
  // type/policy filters run). Users/Groups only exist because of an assignment, so
  // they're connected by definition — this only ever trims isolated Agents/MCPs.
  const [hideUnconnected, setHideUnconnected] = useState(false);

  // Selection → drawer + neighborhood highlight. We keep the raw clicked node/edge
  // id; the highlight + the drawer payload are derived from the laid-out arrays.
  const [selectedNodeId, setSelectedNodeId] = useState<string | null>(null);
  const [selectedEdgeId, setSelectedEdgeId] = useState<string | null>(null);

  // Reduced-motion gate for the selection-flow animation (Feature 1).
  const prefersReducedMotion = usePrefersReducedMotion();

  // --- Fetch (AgentsList idiom: cancellable, retry via nonce) ---------------
  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    governanceGraphApi
      .get()
      .then((res) => {
        if (cancelled) return;
        setGraph(res);
        setError(null);
      })
      .catch((err: unknown) => {
        if (cancelled) return;
        setError(err instanceof Error ? err.message : 'Failed to load the governance graph.');
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [reloadNonce]);

  // --- Transform + filter + layout (pure, memoized over graph + filters) -----
  // We filter the API shape FIRST (drop hidden node types; if policyOnly, keep
  // only Cedar-enforced can_call edges + the access edges/endpoints that remain),
  // then toFlow → layout so dagre re-packs the visible subgraph tightly.
  const { nodes: laidOutNodes, edges: laidOutEdges } = useMemo(() => {
    if (!graph) return { nodes: [] as FlowNode[], edges: [] as FlowEdge[] };

    // 1) node-type visibility
    const visibleNodeIds = new Set(
      graph.nodes.filter((n) => typeVisible[n.type]).map((n) => n.id),
    );

    // 2) edges that survive the type filter (both endpoints visible)
    let edges = graph.edges.filter(
      (e) => visibleNodeIds.has(e.source) && visibleNodeIds.has(e.target),
    );

    // 3) "policy-governed only": keep can_call edges with has_policy, drop the
    //    rest of the can_call edges; access edges stay (they explain WHO reaches
    //    the policy-governed agents). Then prune to nodes still touched by an edge
    //    OR kept as a standalone agent/mcp so the focused view isn't cluttered.
    if (policyOnly) {
      edges = edges.filter((e) => e.type !== 'can_call' || e.has_policy);
    }

    // 4) which nodes to render: every visible node when not policyOnly; when
    //    policyOnly, restrict to nodes still incident to a surviving edge so the
    //    view collapses onto the governed paths.
    let nodes = graph.nodes.filter((n) => visibleNodeIds.has(n.id));
    // The set of node ids still touched by a surviving edge — computed AFTER the
    // type + policy filters so neither flag leaves a dangling node behind.
    const touched = new Set<string>();
    for (const e of edges) {
      touched.add(e.source);
      touched.add(e.target);
    }
    if (policyOnly) {
      nodes = nodes.filter((n) => touched.has(n.id));
    }

    // 5) "hide unconnected": drop Agent/MCP nodes with no surviving edge. Users/
    //    Groups are connected by definition, so they're never trimmed here.
    if (hideUnconnected) {
      nodes = nodes.filter(
        (n) => (n.type !== 'agent' && n.type !== 'mcp') || touched.has(n.id),
      );
    }

    const subgraph: GovernanceGraphData = { nodes, edges };
    const flow = toFlow(subgraph);
    return layout(flow.nodes, flow.edges);
  }, [graph, typeVisible, policyOnly, hideUnconnected]);

  // Fast lookup map for selection-payload + highlight derivation. Derived from the
  // LAID-OUT nodes (not the draggable copy) — selection neighbors + the drawer
  // payload are topology, not screen position, so a manual drag must not perturb
  // them.
  const nodeById = useMemo(() => {
    const m = new Map<string, FlowNode>();
    for (const n of laidOutNodes) m.set(n.id, n);
    return m;
  }, [laidOutNodes]);

  // --- Draggable stateful nodes (Feature 2) ---------------------------------
  // The rendered nodes must persist manual drags, so they live in their OWN state
  // (seeded FROM the dagre-laid-out array) rather than being the recomputed array
  // straight from the memo. `onNodesChange` (applyNodeChanges under the hood) folds
  // drag/position changes back into this state so a drag survives the next render.
  const [draggableNodes, setDraggableNodes, onNodesChange] = useNodesState<FlowNode>([]);

  // Re-seed ON GRAPH/FILTER CHANGE ONLY. `laidOutNodes` is memoized over
  // [graph, typeVisible, policyOnly, hideUnconnected] and NOT over selection — so
  // its identity is stable across a plain node/edge click. Keying this effect on
  // `laidOutNodes` therefore resets positions exactly when a fresh layout is
  // expected (fetch reload, type/policy/hide-unconnected toggle, Reset), and never
  // on selection — so the user's manual drags survive selection/highlight changes.
  useEffect(() => {
    setDraggableNodes(laidOutNodes);
  }, [laidOutNodes, setDraggableNodes]);

  // --- Neighborhood highlight (pure recompute over selection) ---------------
  // When a node is selected, the "kept lit" set = the node + its one-hop incomers/
  // outgoers + the edges connecting them. Everything else dims. When an edge is
  // selected, only its two endpoints + itself stay lit. No selection → nothing dims.
  const { litNodeIds, litEdgeIds } = useMemo(() => {
    if (!selectedNodeId && !selectedEdgeId) {
      return { litNodeIds: null as Set<string> | null, litEdgeIds: null as Set<string> | null };
    }

    const litNodes = new Set<string>();
    const litEdges = new Set<string>();

    if (selectedNodeId) {
      const node = nodeById.get(selectedNodeId);
      if (node) {
        litNodes.add(node.id);
        const incomers = getIncomers(node, laidOutNodes, laidOutEdges);
        const outgoers = getOutgoers(node, laidOutNodes, laidOutEdges);
        for (const n of incomers) litNodes.add(n.id);
        for (const n of outgoers) litNodes.add(n.id);
        for (const e of getConnectedEdges([node], laidOutEdges)) litEdges.add(e.id);
      }
    } else if (selectedEdgeId) {
      const edge = laidOutEdges.find((e) => e.id === selectedEdgeId);
      if (edge) {
        litEdges.add(edge.id);
        litNodes.add(edge.source);
        litNodes.add(edge.target);
      }
    }

    return { litNodeIds: litNodes, litEdgeIds: litEdges };
  }, [selectedNodeId, selectedEdgeId, nodeById, laidOutNodes, laidOutEdges]);

  // Apply the dim flag over the STATEFUL (possibly-dragged) nodes — NOT the raw
  // laid-out array — so dragging + dimming coexist: a node keeps its dragged
  // position AND picks up the dim className when it falls outside the lit set.
  // No selection → pass the stateful nodes through untouched.
  // Typed FlowNode[] (not the wider Node[]) so the <ReactFlow> node generic infers
  // as FlowNode — matching onNodesChange: OnNodesChange<FlowNode> from useNodesState.
  const renderNodes: FlowNode[] = useMemo(() => {
    if (!litNodeIds) return draggableNodes;
    return draggableNodes.map((n) =>
      litNodeIds.has(n.id) ? n : { ...n, className: DIMMED_NODE },
    );
  }, [draggableNodes, litNodeIds]);

  // Edges stay derived off the laid-out array + the lit set (they re-route to the
  // moved node handles automatically — no need to make them stateful). Feature 1:
  // a lit edge also gets `animated: true` so the selected access path "flows"
  // (marching-ants on both default + PolicyEdge via the built-in `.animated` CSS).
  // Gated on a live selection (litEdgeIds non-null) AND prefers-reduced-motion off.
  const renderEdges: Edge[] = useMemo(() => {
    if (!litEdgeIds) return laidOutEdges;
    const allowAnimation = !prefersReducedMotion;
    return laidOutEdges.map((e) =>
      litEdgeIds.has(e.id)
        ? allowAnimation
          ? { ...e, animated: true }
          : e
        : { ...e, style: { ...(e.style ?? {}), opacity: DIMMED_EDGE } },
    );
  }, [laidOutEdges, litEdgeIds, prefersReducedMotion]);

  // --- Selection → drawer payload (EXACT GraphSelection contract) -----------
  // node click → { kind:'node', node }. edge click → { kind:'edge', edge, source/
  // targetLabel resolved from the node map, targetMcpRefId stripped from the
  // "mcp:<refId>" target id when the edge targets an MCP (the drawer's cedar list).
  const selection: GraphSelection | null = useMemo(() => {
    if (selectedNodeId) {
      const node = nodeById.get(selectedNodeId);
      return node ? { kind: 'node', node } : null;
    }
    if (selectedEdgeId) {
      const edge = laidOutEdges.find((e) => e.id === selectedEdgeId);
      if (!edge) return null;
      const target = nodeById.get(edge.target);
      const targetMcpRefId =
        target?.data.nodeType === 'mcp' ? target.data.refId : undefined;
      return {
        kind: 'edge',
        edge,
        sourceLabel: nodeById.get(edge.source)?.data.label,
        targetLabel: target?.data.label,
        targetMcpRefId,
      };
    }
    return null;
  }, [selectedNodeId, selectedEdgeId, nodeById, laidOutEdges]);

  const clearSelection = useCallback(() => {
    setSelectedNodeId(null);
    setSelectedEdgeId(null);
  }, []);

  // Reset layout (Feature 3): re-seed the stateful nodes back to the dagre
  // positions (discarding manual drags) and bump a nonce the in-canvas controller
  // watches to re-run fitView. Doesn't touch filters/selection or hit the network.
  const [fitNonce, setFitNonce] = useState(0);
  const onResetLayout = useCallback(() => {
    setDraggableNodes(laidOutNodes);
    setFitNonce((n) => n + 1);
  }, [laidOutNodes, setDraggableNodes]);

  const onNodeClick = useCallback((_: unknown, node: Node) => {
    setSelectedEdgeId(null);
    setSelectedNodeId(node.id);
  }, []);

  const onEdgeClick = useCallback((_: unknown, edge: Edge) => {
    setSelectedNodeId(null);
    setSelectedEdgeId(edge.id);
  }, []);

  const toggleType = (t: NodeType) =>
    setTypeVisible((prev) => ({ ...prev, [t]: !prev[t] }));

  const totalNodes = graph?.nodes.length ?? 0;
  const isEmpty = !loading && !error && totalNodes === 0;

  return (
    <div className="h-full flex flex-col">
      {/* Page header — governance idiom (title + one-line subtitle). */}
      <div className="shrink-0 px-6 pt-6 pb-3">
        <h1 className="text-xl font-semibold text-slate-900">Governance Graph</h1>
        <p className="text-xs text-slate-500 mt-0.5">
          Who can reach what — Users and Groups → Agents → MCP servers, drawn from
          Microsoft&nbsp;Entra access. Edges to a Cedar-enforcing gateway carry a
          policy badge.
        </p>
      </div>

      {/* Body — the canvas fills the remaining height (the explicit-height fix). */}
      <div className="flex-1 min-h-0 px-6 pb-6">
        {error ? (
          <div className="bg-white/70 backdrop-blur rounded-2xl border border-red-200/70 shadow-sm p-6 max-w-xl">
            <h3 className="text-sm font-semibold text-red-700">Couldn’t load the governance graph</h3>
            <p className="text-sm text-slate-600 mt-1">{error}</p>
            <button
              onClick={() => setReloadNonce((n) => n + 1)}
              className="mt-3 px-3 py-1.5 rounded-lg bg-white border border-slate-300 text-slate-700 text-xs font-medium hover:bg-slate-50 transition-colors"
            >
              Retry
            </button>
          </div>
        ) : loading ? (
          <div className="h-full flex items-center justify-center bg-white/60 backdrop-blur rounded-2xl border border-slate-200/60 shadow-sm">
            <div className="text-slate-400 text-sm">Loading the governance graph…</div>
          </div>
        ) : isEmpty ? (
          <div className="h-full flex items-center justify-center bg-white/60 backdrop-blur rounded-2xl border border-slate-200/60 shadow-sm">
            <div className="text-center max-w-md px-6">
              <div
                aria-hidden="true"
                className="mx-auto mb-4 h-12 w-12 rounded-2xl bg-blue-50 text-blue-500 flex items-center justify-center"
              >
                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={1.6} className="h-6 w-6">
                  <circle cx="6" cy="6" r="2.4" />
                  <circle cx="18" cy="6" r="2.4" />
                  <circle cx="12" cy="18" r="2.4" />
                  <path strokeLinecap="round" d="M7.7 7.6 10.6 16M16.3 7.6 13.4 16" />
                </svg>
              </div>
              <h3 className="text-sm font-semibold text-slate-900">No governance relationships yet</h3>
              <p className="text-xs text-slate-500 mt-1.5 leading-relaxed">
                Once agents and MCP servers are provisioned and Users or Groups are
                granted access in Microsoft&nbsp;Entra, their relationships will be
                drawn here.
              </p>
            </div>
          </div>
        ) : (
          // The sized parent: rounded glass frame; ReactFlow fills it (h-full).
          <div className="h-full rounded-2xl border border-slate-200/60 bg-white/40 backdrop-blur shadow-sm overflow-hidden">
            <ReactFlowProvider>
              <ReactFlow
                nodes={renderNodes}
                edges={renderEdges}
                onNodesChange={onNodesChange}
                nodeTypes={nodeTypes}
                edgeTypes={edgeTypes}
                fitView
                fitViewOptions={{ padding: 0.2 }}
                minZoom={0.2}
                proOptions={{ hideAttribution: true }}
                nodesDraggable
                nodesConnectable={false}
                elementsSelectable
                onNodeClick={onNodeClick}
                onEdgeClick={onEdgeClick}
                onPaneClick={clearSelection}
                className="!bg-transparent"
              >
                {/* Re-runs fitView when Reset layout bumps the nonce (needs the
                    instance, so it lives inside the provider). */}
                <FitViewOnReset nonce={fitNonce} padding={0.2} />
                <Background color="#cbd5e1" gap={20} size={1} />
                <Controls showInteractive={false} className="!shadow-sm !border !border-slate-200 !rounded-lg" />
                <MiniMap
                  pannable
                  zoomable
                  nodeColor={miniMapNodeColor}
                  maskColor="rgba(241,245,249,0.6)"
                  className="!rounded-lg !border !border-slate-200"
                />
                <Panel position="top-left">
                  <LegendFilters
                    typeVisible={typeVisible}
                    onToggleType={toggleType}
                    policyOnly={policyOnly}
                    onTogglePolicyOnly={() => setPolicyOnly((v) => !v)}
                    hideUnconnected={hideUnconnected}
                    onToggleHideUnconnected={() => setHideUnconnected((v) => !v)}
                    onResetLayout={onResetLayout}
                  />
                </Panel>
              </ReactFlow>
            </ReactFlowProvider>
          </div>
        )}
      </div>

      {/* Selection → detail drawer (node/edge). Pane click clears selection. */}
      <GraphDetailDrawer selected={selection} onClose={clearSelection} />
    </div>
  );
}

// Re-frames the canvas (fitView) when the Reset-layout nonce changes. Lives inside
// <ReactFlowProvider> so it can grab the instance. Skips the FIRST run so it never
// fights the <ReactFlow fitView> prop's initial framing — only later Reset clicks
// (nonce 0 → 1 → …) re-fit; manual drags in between are left untouched.
function FitViewOnReset({ nonce, padding }: { nonce: number; padding: number }) {
  const { fitView } = useReactFlow();
  const isFirst = useRef(true);
  useEffect(() => {
    if (isFirst.current) {
      isFirst.current = false;
      return;
    }
    fitView({ padding });
  }, [nonce, fitView, padding]);
  return null;
}

// MiniMap dot color per node type — mirrors the legend dots / node accents.
function miniMapNodeColor(node: Node): string {
  switch (node.type) {
    case 'user':
      return '#3b82f6'; // blue-500
    case 'group':
      return '#06b6d4'; // cyan-500
    case 'mcp':
      return '#8b5cf6'; // violet-500
    default:
      return '#14b8a6'; // teal-500 (agent)
  }
}

// ---------------------------------------------------------------------------
// LegendFilters — a glass card overlaid top-left. The colored dots ARE the
// legend; clicking a row toggles that node type. A second block toggles the
// "policy-governed only" lens with the honest (user,tool)-scope caveat.
// ---------------------------------------------------------------------------
function LegendFilters({
  typeVisible,
  onToggleType,
  policyOnly,
  onTogglePolicyOnly,
  hideUnconnected,
  onToggleHideUnconnected,
  onResetLayout,
}: {
  typeVisible: TypeVisibility;
  onToggleType: (t: NodeType) => void;
  policyOnly: boolean;
  onTogglePolicyOnly: () => void;
  hideUnconnected: boolean;
  onToggleHideUnconnected: () => void;
  onResetLayout: () => void;
}) {
  return (
    <div className="w-56 rounded-2xl border border-slate-200/80 bg-white/90 backdrop-blur-md shadow-sm p-3 text-left">
      <p className="text-[11px] uppercase tracking-wide text-slate-400 font-semibold mb-2">
        Legend &amp; filters
      </p>

      <div className="space-y-1">
        {NODE_TYPE_ORDER.map((t) => {
          const meta = TYPE_META[t];
          const on = typeVisible[t];
          return (
            <button
              key={t}
              type="button"
              aria-pressed={on}
              onClick={() => onToggleType(t)}
              className={`group/row w-full flex items-center gap-2 px-2 py-1 rounded-lg border text-xs font-medium transition-colors ${
                on
                  ? meta.chipOn
                  : 'bg-transparent text-slate-400 border-transparent hover:bg-slate-50'
              }`}
            >
              <span
                aria-hidden="true"
                className={`h-2.5 w-2.5 rounded-full shrink-0 ${on ? meta.dot : 'bg-slate-300'}`}
              />
              <span className="flex-1 text-left">{meta.label}</span>
              <span className="text-[10px] tabular-nums opacity-60">{on ? 'shown' : 'hidden'}</span>
            </button>
          );
        })}
      </div>

      <div className="mt-2.5 pt-2.5 border-t border-slate-200/70">
        <button
          type="button"
          aria-pressed={hideUnconnected}
          onClick={onToggleHideUnconnected}
          className={`w-full flex items-center gap-2 px-2 py-1 rounded-lg border text-xs font-medium transition-colors ${
            hideUnconnected
              ? 'bg-slate-100 text-slate-700 border-slate-300'
              : 'bg-transparent text-slate-500 border-transparent hover:bg-slate-50'
          }`}
        >
          <span
            aria-hidden="true"
            className={`h-2.5 w-2.5 rounded-full shrink-0 ${
              hideUnconnected ? 'bg-slate-500' : 'bg-slate-300'
            }`}
          />
          <span className="flex-1 text-left">Hide unconnected</span>
          <span className="text-[10px] opacity-60">{hideUnconnected ? 'on' : 'off'}</span>
        </button>
        <p className="mt-1.5 text-[10px] leading-snug text-slate-400">
          Hides Agents and MCP servers with no access edge. Users and Groups are
          always connected.
        </p>
      </div>

      <div className="mt-2.5 pt-2.5 border-t border-slate-200/70">
        <button
          type="button"
          aria-pressed={policyOnly}
          onClick={onTogglePolicyOnly}
          className={`w-full flex items-center gap-2 px-2 py-1 rounded-lg border text-xs font-medium transition-colors ${
            policyOnly
              ? 'bg-amber-50 text-amber-800 border-amber-200'
              : 'bg-transparent text-slate-500 border-transparent hover:bg-slate-50'
          }`}
        >
          <span aria-hidden="true" className="h-2.5 w-2.5 rounded-full bg-amber-500 shrink-0" />
          <span className="flex-1 text-left">Policy-governed only</span>
          <span className="text-[10px] opacity-60">{policyOnly ? 'on' : 'off'}</span>
        </button>
        <p className="mt-1.5 text-[10px] leading-snug text-slate-400">
          Amber edges reach an MCP gateway with Cedar enforcement on. Policies are
          scoped to (user,&nbsp;tool) — not to a specific agent.
        </p>
      </div>

      {/* Reset layout — re-packs the nodes to the auto (dagre) positions and
          re-frames the view, discarding any manual drags. Neutral slate action
          button matching the Retry / toggle idiom. */}
      <div className="mt-2.5 pt-2.5 border-t border-slate-200/70">
        <button
          type="button"
          onClick={onResetLayout}
          className="w-full flex items-center justify-center gap-1.5 px-2 py-1.5 rounded-lg bg-white border border-slate-300 text-slate-700 text-xs font-medium hover:bg-slate-50 transition-colors"
        >
          <svg
            aria-hidden="true"
            viewBox="0 0 20 20"
            fill="none"
            stroke="currentColor"
            strokeWidth={1.6}
            className="h-3.5 w-3.5"
          >
            <path strokeLinecap="round" strokeLinejoin="round" d="M4 10a6 6 0 1 1 1.8 4.3" />
            <path strokeLinecap="round" strokeLinejoin="round" d="M3.5 14.5v-3h3" />
          </svg>
          Reset layout
        </button>
      </div>
    </div>
  );
}
