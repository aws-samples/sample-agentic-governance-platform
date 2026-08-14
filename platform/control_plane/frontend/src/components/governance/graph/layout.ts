// Pure, framework-free transforms for the Governance Graph (Epic 11, Task 7).
// (a) toFlow: map the backend GovernanceGraph wire shape → React Flow nodes/edges.
// (b) layout: run dagre once to assign LEFT→RIGHT layered positions.
// No React / no DOM here so vitest runs these headless.
import type { Node, Edge } from '@xyflow/react';
import dagre from 'dagre';
import type { GovernanceGraph, GraphNode, GraphEdge } from '../../../api/client';

// Typed data shapes for the React Flow node/edge generics.
// Exported so T8 (custom node/edge components) can import them directly instead
// of re-declaring the same shapes — the compiler enforces the snake→camel boundary
// (API has_policy → hasPolicy) at every call site.
export type GraphNodeData = {
  label: string;
  refId: string;
  nodeType: GraphNode['type'];
  metadata: Record<string, unknown>;
};
export type GraphEdgeData = {
  role: string;
  hasPolicy: boolean;
  edgeType: GraphEdge['type'];
};
export type FlowNode = Node<GraphNodeData>;
export type FlowEdge = Edge<GraphEdgeData>;

// Node box used both as the React Flow node footprint AND the dagre node size
// (so we can convert dagre's CENTER coords back to React Flow's TOP-LEFT).
const NODE_WIDTH = 180;
const NODE_HEIGHT = 60;

// Dagre spacing for the layered LR flow.
const NODE_SEP = 40; // gap between nodes in the same rank
const RANK_SEP = 120; // gap between ranks (user/group | agent | mcp)

/**
 * Map the API GovernanceGraph shape to React Flow's `{ nodes, edges }`.
 *
 * - Each API node → a React Flow Node. `type` = the API node type so React Flow
 *   picks the matching custom node component (registered in T8). `position` is a
 *   placeholder filled in by `layout()`. `data` carries label/refId/metadata.
 * - Each API edge → a React Flow Edge. can_call edges get `type:'policy'` (custom
 *   PolicyEdge, T8); access edges get `type:'default'`. THE snake→camel boundary
 *   lives here: API `has_policy` → `data.hasPolicy`.
 *
 * Deterministic: preserves input order.
 */
export function toFlow(graph: GovernanceGraph): { nodes: FlowNode[]; edges: FlowEdge[] } {
  const nodes: FlowNode[] = graph.nodes.map((n) => ({
    id: n.id,
    type: n.type,
    position: { x: 0, y: 0 },
    data: {
      label: n.label,
      refId: n.ref_id,
      nodeType: n.type,
      metadata: n.metadata,
    },
  }));

  const edges: FlowEdge[] = graph.edges.map((e) => ({
    id: e.id,
    source: e.source,
    target: e.target,
    type: e.type === 'can_call' ? 'policy' : 'default',
    data: {
      role: e.role,
      hasPolicy: e.has_policy ?? false,
      edgeType: e.type,
    },
  }));

  return { nodes, edges };
}

/**
 * Run dagre to assign `position{x,y}` for a LEFT→RIGHT layered flow. Because the
 * edges run user/group→agent→mcp, dagre's LR ranking naturally places users/
 * groups left, agents middle, mcps right — no manual rank forcing needed.
 *
 * dagre returns node CENTER coords; React Flow expects TOP-LEFT, so we subtract
 * half the box. Returns NEW node objects (inputs are not mutated). Edges pass
 * through unchanged.
 */
export function layout(nodes: FlowNode[], edges: FlowEdge[]): { nodes: FlowNode[]; edges: FlowEdge[] } {
  const g = new dagre.graphlib.Graph();
  g.setGraph({ rankdir: 'LR', nodesep: NODE_SEP, ranksep: RANK_SEP });
  g.setDefaultEdgeLabel(() => ({}));

  for (const node of nodes) {
    g.setNode(node.id, { width: NODE_WIDTH, height: NODE_HEIGHT });
  }
  for (const edge of edges) {
    g.setEdge(edge.source, edge.target);
  }

  dagre.layout(g);

  const positioned: FlowNode[] = nodes.map((node) => {
    const { x, y } = g.node(node.id);
    return {
      ...node,
      position: { x: x - NODE_WIDTH / 2, y: y - NODE_HEIGHT / 2 },
    };
  });

  return { nodes: positioned, edges };
}
