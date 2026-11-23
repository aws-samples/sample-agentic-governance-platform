import type { Node, Edge } from '@xyflow/react';
import dagre from 'dagre';
import type { GovernanceGraph, GraphNode, GraphEdge } from '../../../api/client';

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

export const NODE_WIDTH = 180;
export const NODE_HEIGHT = 60;

const NODE_SEP = 40;
const RANK_SEP = 120;

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
      hasPolicy: e.has_policy,
      edgeType: e.type,
    },
  }));

  return { nodes, edges };
}

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
    // dagre reports a node's centre; React Flow positions a node by its top-left corner.
    const { x, y } = g.node(node.id);
    return {
      ...node,
      position: { x: x - NODE_WIDTH / 2, y: y - NODE_HEIGHT / 2 },
    };
  });

  return { nodes: positioned, edges };
}
