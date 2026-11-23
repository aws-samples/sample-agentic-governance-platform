import { describe, it, expect } from 'vitest';
import { toFlow, layout, NODE_WIDTH, NODE_HEIGHT } from './layout';
import type { GovernanceGraph, GraphEdge, GraphNode } from '../../../api/openapi';
import { stripComments } from '../../../rawSource';
import graphNodesRaw from './graphNodes.tsx?raw';

function graphNode(overrides: Partial<GraphNode> = {}): GraphNode {
  return {
    type: 'agent',
    id: 'agent:rec-a',
    label: 'claims-triage-de',
    ref_id: 'rec-a',
    metadata: {},
    ...overrides,
  };
}

function graphEdge(overrides: Partial<GraphEdge> = {}): GraphEdge {
  return {
    id: 'asg-1',
    source: 'user:oid-1',
    target: 'agent:rec-a',
    type: 'access',
    role: 'Invoker',
    has_policy: false,
    ...overrides,
  };
}

function governanceGraph(overrides: Partial<GovernanceGraph> = {}): GovernanceGraph {
  return { nodes: [], edges: [], ...overrides };
}

const graph = (): GovernanceGraph =>
  governanceGraph({
    nodes: [
      graphNode({ type: 'user', id: 'user:oid-1', label: 'Maria Bauer', ref_id: 'oid-1', metadata: { principal_type: 'User' } }),
      graphNode({ type: 'group', id: 'group:gid-1', label: 'Contoso-Claims-Officers', ref_id: 'gid-1', metadata: { principal_type: 'Group' } }),
      graphNode({ type: 'agent', id: 'agent:rec-a', label: 'claims-triage-de', ref_id: 'rec-a', metadata: { origin: 'Registered', platform: 'aws_bedrock' } }),
      graphNode({ type: 'mcp', id: 'mcp:rec-m', label: 'internal-claims-mcp', ref_id: 'rec-m', metadata: { kind: 'gateway', cedar_enforcement_mode: 'enforce' } }),
    ],
    edges: [
      graphEdge({ id: 'asg-1', source: 'user:oid-1', target: 'agent:rec-a', type: 'access', role: 'Invoker' }),
      graphEdge({ id: 'asg-2', source: 'group:gid-1', target: 'agent:rec-a', type: 'access', role: 'Admin' }),
      graphEdge({ id: 'asg-3', source: 'agent:rec-a', target: 'mcp:rec-m', type: 'can_call', role: 'Invoker', has_policy: true }),
    ],
  });

describe('toFlow', () => {
  it('maps each API node to a React Flow node carrying id, label and metadata, typed by the API node type React Flow resolves the component with, at a placeholder position until layout() runs', () => {
    const { nodes } = toFlow(graph());
    expect(nodes).toHaveLength(4);

    const agent = nodes.find((n) => n.id === 'agent:rec-a');
    expect(agent).toBeDefined();
    expect(agent!.type).toBe('agent');
    expect(agent!.position).toEqual({ x: 0, y: 0 });
    expect(agent!.data.label).toBe('claims-triage-de');
    expect(agent!.data.refId).toBe('rec-a');
    expect(agent!.data.nodeType).toBe('agent');
    expect(agent!.data.metadata).toEqual({ origin: 'Registered', platform: 'aws_bedrock' });

    const mcp = nodes.find((n) => n.id === 'mcp:rec-m');
    expect(mcp!.type).toBe('mcp');
    expect(mcp!.data.metadata).toEqual({ kind: 'gateway', cedar_enforcement_mode: 'enforce' });
  });

  it('preserves input node order (deterministic)', () => {
    const { nodes } = toFlow(graph());
    expect(nodes.map((n) => n.id)).toEqual(['user:oid-1', 'group:gid-1', 'agent:rec-a', 'mcp:rec-m']);
  });

  it('routes can_call edges to the policy edge type and access edges to default', () => {
    const { edges } = toFlow(graph());
    const access = edges.find((e) => e.id === 'asg-1');
    const canCall = edges.find((e) => e.id === 'asg-3');

    expect(access!.type).toBe('default');
    expect(canCall!.type).toBe('policy');
  });

  it('carries role and the snake→camel has_policy boundary into edge data', () => {
    const { edges } = toFlow(graph());
    const canCall = edges.find((e) => e.id === 'asg-3')!;
    expect(canCall.source).toBe('agent:rec-a');
    expect(canCall.target).toBe('mcp:rec-m');
    expect(canCall.data!.role).toBe('Invoker');
    expect(canCall.data!.edgeType).toBe('can_call');
    expect(canCall.data!.hasPolicy).toBe(true);
  });
});

describe('layout', () => {
  it('assigns numeric positions to every node', () => {
    const { nodes, edges } = toFlow(graph());
    const out = layout(nodes, edges);
    expect(out.nodes).toHaveLength(4);
    for (const n of out.nodes) {
      expect(typeof n.position.x).toBe('number');
      expect(typeof n.position.y).toBe('number');
      expect(Number.isFinite(n.position.x)).toBe(true);
      expect(Number.isFinite(n.position.y)).toBe(true);
    }
  });

  it('lays out left→right by rank: x(user) < x(agent) < x(mcp)', () => {
    const { nodes, edges } = toFlow(graph());
    const out = layout(nodes, edges);
    const xOf = (id: string) => out.nodes.find((n) => n.id === id)!.position.x;
    expect(xOf('user:oid-1')).toBeLessThan(xOf('agent:rec-a'));
    expect(xOf('group:gid-1')).toBeLessThan(xOf('agent:rec-a'));
    expect(xOf('agent:rec-a')).toBeLessThan(xOf('mcp:rec-m'));
  });

  it('returns edges unchanged', () => {
    const { nodes, edges } = toFlow(graph());
    const out = layout(nodes, edges);
    expect(out.edges).toEqual(edges);
  });

  it('does not mutate the input nodes', () => {
    const { nodes, edges } = toFlow(graph());
    const before = nodes.map((n) => ({ ...n.position }));
    layout(nodes, edges);
    expect(nodes.map((n) => n.position)).toEqual(before);
  });
});

describe('node cards', () => {
  it('the card is laid out with the same box dagre positions: graphNodes.tsx sizes it from NODE_WIDTH and NODE_HEIGHT, never from a hand-typed size class', () => {
    const src = stripComments(graphNodesRaw);
    expect(src).toContain('width: NODE_WIDTH, height: NODE_HEIGHT');
    expect(src).not.toMatch(/\b[wh]-\[\d+px\]/);
    expect(NODE_WIDTH).toBeGreaterThan(0);
    expect(NODE_HEIGHT).toBeGreaterThan(0);
  });
});
