import { describe, it, expect } from 'vitest';
import { toFlow, layout } from './layout';
import type { GovernanceGraph } from '../../../api/client';

// A small user/group → agent → mcp fixture exercising both edge directions,
// the snake→camel boundary (has_policy → data.hasPolicy), and the LR layering.
const graph = (): GovernanceGraph => ({
  nodes: [
    { type: 'user', id: 'user:oid-1', label: 'Maria Bauer', ref_id: 'oid-1', metadata: { principal_type: 'User' } },
    { type: 'group', id: 'group:gid-1', label: 'Contoso-Claims-Officers', ref_id: 'gid-1', metadata: { principal_type: 'Group' } },
    { type: 'agent', id: 'agent:rec-a', label: 'claims-triage-de', ref_id: 'rec-a', metadata: { origin: 'Registered', platform: 'aws_bedrock' } },
    { type: 'mcp', id: 'mcp:rec-m', label: 'internal-claims-mcp', ref_id: 'rec-m', metadata: { kind: 'gateway', cedar_enforcement_mode: 'enforce' } },
  ],
  edges: [
    { id: 'asg-1', source: 'user:oid-1', target: 'agent:rec-a', type: 'access', role: 'Invoker' },
    { id: 'asg-2', source: 'group:gid-1', target: 'agent:rec-a', type: 'access', role: 'Admin' },
    { id: 'asg-3', source: 'agent:rec-a', target: 'mcp:rec-m', type: 'can_call', role: 'Invoker', has_policy: true },
  ],
});

describe('toFlow', () => {
  it('maps each API node to a React Flow node carrying id, type, label and metadata', () => {
    const { nodes } = toFlow(graph());
    expect(nodes).toHaveLength(4);

    const agent = nodes.find((n) => n.id === 'agent:rec-a');
    expect(agent).toBeDefined();
    expect(agent!.type).toBe('agent'); // RF node type = API node type (so T8 registers the right custom component)
    expect(agent!.position).toEqual({ x: 0, y: 0 }); // placeholder until layout() runs
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

    expect(access!.type).toBe('default'); // access edges → styled default edge
    expect(canCall!.type).toBe('policy'); // can_call edges → custom PolicyEdge (T8)
  });

  it('carries role and the snake→camel has_policy boundary into edge data', () => {
    const { edges } = toFlow(graph());
    const canCall = edges.find((e) => e.id === 'asg-3')!;
    expect(canCall.source).toBe('agent:rec-a');
    expect(canCall.target).toBe('mcp:rec-m');
    expect(canCall.data!.role).toBe('Invoker');
    expect(canCall.data!.edgeType).toBe('can_call');
    // THE snake→camel boundary: API has_policy → data.hasPolicy
    expect(canCall.data!.hasPolicy).toBe(true);
  });

  it('defaults hasPolicy to false when has_policy is absent', () => {
    const { edges } = toFlow(graph());
    const access = edges.find((e) => e.id === 'asg-1')!;
    expect(access.data!.hasPolicy).toBe(false);
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
