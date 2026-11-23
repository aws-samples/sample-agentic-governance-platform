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
import { useLoad } from '../../api/useLoad';
import { toFlow, layout } from './graph/layout';
import type { FlowNode, FlowEdge } from './graph/layout';
import { nodeTypes, edgeTypes } from './graph/flowTypes';
import { CEDAR_LEGEND } from './graph/cedarCopy';
import GraphDetailDrawer from './graph/GraphDetailDrawer';
import type { GraphSelection } from './graph/GraphDetailDrawer';

type NodeType = GraphNode['type'];

const TYPE_META: Record<NodeType, { label: string; dot: string; chipOn: string }> = {
  user: { label: 'Users', dot: 'bg-blue-500', chipOn: 'bg-blue-50 text-blue-700 border-blue-200' },
  group: { label: 'Groups', dot: 'bg-cyan-500', chipOn: 'bg-cyan-50 text-cyan-700 border-cyan-200' },
  agent: { label: 'Agents', dot: 'bg-teal-500', chipOn: 'bg-teal-50 text-teal-700 border-teal-200' },
  mcp: { label: 'MCP servers', dot: 'bg-violet-500', chipOn: 'bg-violet-50 text-violet-700 border-violet-200' },
};
const NODE_TYPE_ORDER: NodeType[] = ['user', 'group', 'agent', 'mcp'];

type TypeVisibility = Record<NodeType, boolean>;
const ALL_VISIBLE: TypeVisibility = { user: true, group: true, agent: true, mcp: true };

const DIMMED_NODE = 'opacity-25 transition-opacity';
const DIMMED_EDGE = 0.12;

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
  const { state, reload } = useLoad(
    () => governanceGraphApi.get(),
    [],
    'Failed to load the governance graph.',
  );
  const graph = state.data;

  const [typeVisible, setTypeVisible] = useState<TypeVisibility>(ALL_VISIBLE);
  const [policyOnly, setPolicyOnly] = useState(false);
  const [hideUnconnected, setHideUnconnected] = useState(false);

  const [selectedNodeId, setSelectedNodeId] = useState<string | null>(null);
  const [selectedEdgeId, setSelectedEdgeId] = useState<string | null>(null);

  const prefersReducedMotion = usePrefersReducedMotion();

  const { nodes: laidOutNodes, edges: laidOutEdges } = useMemo(() => {
    if (!graph) return { nodes: [] as FlowNode[], edges: [] as FlowEdge[] };

    const visibleNodeIds = new Set(
      graph.nodes.filter((n) => typeVisible[n.type]).map((n) => n.id),
    );

    let edges = graph.edges.filter(
      (e) => visibleNodeIds.has(e.source) && visibleNodeIds.has(e.target),
    );

    if (policyOnly) {
      edges = edges.filter((e) => e.type !== 'can_call' || e.has_policy);
    }

    let nodes = graph.nodes.filter((n) => visibleNodeIds.has(n.id));
    const touched = new Set<string>();
    for (const e of edges) {
      touched.add(e.source);
      touched.add(e.target);
    }
    if (policyOnly) {
      nodes = nodes.filter((n) => touched.has(n.id));
    }

    if (hideUnconnected) {
      nodes = nodes.filter(
        (n) => (n.type !== 'agent' && n.type !== 'mcp') || touched.has(n.id),
      );
    }

    const subgraph: GovernanceGraphData = { nodes, edges };
    const flow = toFlow(subgraph);
    return layout(flow.nodes, flow.edges);
  }, [graph, typeVisible, policyOnly, hideUnconnected]);

  const nodeById = useMemo(() => {
    const m = new Map<string, FlowNode>();
    for (const n of laidOutNodes) m.set(n.id, n);
    return m;
  }, [laidOutNodes]);

  const [draggableNodes, setDraggableNodes, onNodesChange] = useNodesState<FlowNode>(laidOutNodes);
  const [laidOutFor, setLaidOutFor] = useState(laidOutNodes);
  if (laidOutFor !== laidOutNodes) {
    setLaidOutFor(laidOutNodes);
    setDraggableNodes(laidOutNodes);
  }

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

  const renderNodes: FlowNode[] = useMemo(() => {
    if (!litNodeIds) return draggableNodes;
    return draggableNodes.map((n) =>
      litNodeIds.has(n.id) ? n : { ...n, className: DIMMED_NODE },
    );
  }, [draggableNodes, litNodeIds]);

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
  const isEmpty = state.kind === 'ready' && totalNodes === 0;

  return (
    <div className="h-full flex flex-col">
      <div className="shrink-0 px-6 pt-6 pb-3">
        <h1 className="text-xl font-semibold text-slate-900">Governance Graph</h1>
        <p className="text-xs text-slate-500 mt-0.5">
          Who can reach what — Users and Groups → Agents → MCP servers, drawn from
          Microsoft&nbsp;Entra access. Edges to a Cedar-enforcing gateway carry a
          policy badge.
        </p>
      </div>

      <div className="flex-1 min-h-0 px-6 pb-6">
        {state.kind === 'error' ? (
          <div className="bg-white/70 backdrop-blur rounded-2xl border border-red-200/70 shadow-sm p-6 max-w-xl">
            <h3 className="text-sm font-semibold text-red-700">Couldn’t load the governance graph</h3>
            <p className="text-sm text-slate-600 mt-1">{state.message}</p>
            <button
              onClick={reload}
              className="mt-3 px-3 py-1.5 rounded-lg bg-white border border-slate-300 text-slate-700 text-xs font-medium hover:bg-slate-50 transition-colors"
            >
              Retry
            </button>
          </div>
        ) : state.kind === 'loading' ? (
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
          // React Flow collapses the canvas to 0px unless its parent has an explicit height.
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

      <GraphDetailDrawer selected={selection} onClose={clearSelection} />
    </div>
  );
}

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

function miniMapNodeColor(node: Node): string {
  switch (node.type) {
    case 'user':
      return '#3b82f6';
    case 'group':
      return '#06b6d4';
    case 'mcp':
      return '#8b5cf6';
    case undefined:
    default:
      return '#14b8a6';
  }
}

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
        <p className="mt-1.5 text-[10px] leading-snug text-slate-400">{CEDAR_LEGEND}</p>
      </div>

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
