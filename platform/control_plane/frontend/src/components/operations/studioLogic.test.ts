import { describe, it, expect } from 'vitest';
import {
  synthesize, initialReadiness, resolveItem, runEval, isDeployReady, EVAL_THRESHOLD,
} from './studioLogic';

describe('synthesize', () => {
  it('builds an agent plan with input→…→output and at least one skill node', () => {
    const p = synthesize('triage incoming claims emails', 'agent');
    expect(p.kind).toBe('agent');
    expect(p.nodes[0].type).toBe('input');
    expect(p.nodes.at(-1)!.type).toBe('output');
    expect(p.nodes.some((n) => n.type === 'skill')).toBe(true);
    expect(p.edges.length).toBeGreaterThanOrEqual(p.nodes.length - 1);
    expect(p.costPerRun).toBeGreaterThan(0);
    expect(p.model.length).toBeGreaterThan(0);
  });
  it('workflow kind has NO mcp/router nodes (chain of LLMs)', () => {
    const p = synthesize('summarize then translate a policy doc', 'workflow');
    expect(p.kind).toBe('workflow');
    expect(p.nodes.some((n) => n.type === 'mcp' || n.type === 'router')).toBe(false);
    expect(p.mcps).toHaveLength(0);
  });
});

describe('readiness', () => {
  it('starts with three warnings (permissions, guardrail, costOwner) plus a failing eval', () => {
    const items = initialReadiness();
    expect(items.find((i) => i.key === 'permissions')!.status).toBe('warn');
    expect(items.find((i) => i.key === 'guardrail')!.status).toBe('warn');
    expect(items.find((i) => i.key === 'costOwner')!.status).toBe('warn');
    expect(items.find((i) => i.key === 'eval')!.status).toBe('warn');
    expect(isDeployReady(items)).toBe(false);
  });
  it('resolveItem flips a single key to ok, immutably', () => {
    const items = initialReadiness();
    const next = resolveItem(items, 'permissions');
    expect(next.find((i) => i.key === 'permissions')!.status).toBe('ok');
    expect(items.find((i) => i.key === 'permissions')!.status).toBe('warn'); // original untouched
  });
  it('runEval passes only at/above threshold', () => {
    expect(runEval(initialReadiness(), EVAL_THRESHOLD - 1).find((i) => i.key === 'eval')!.status).toBe('warn');
    expect(runEval(initialReadiness(), EVAL_THRESHOLD + 7).find((i) => i.key === 'eval')!.status).toBe('ok');
  });
  it('isDeployReady true only when every item resolved', () => {
    let items = initialReadiness();
    for (const k of ['permissions', 'guardrail', 'costOwner'] as const) items = resolveItem(items, k);
    items = runEval(items, 92);
    expect(isDeployReady(items)).toBe(true);
  });
});
