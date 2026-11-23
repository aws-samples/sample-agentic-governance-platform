import { describe, it, expect } from 'vitest';
import { MODELS, TENANTS, estimateCost } from './demoData';

describe('demoData', () => {
  it('has the three demo model providers', () => {
    expect(MODELS.map((m) => m.provider).sort()).toEqual(['Anthropic', 'Google', 'OpenAI']);
    expect(MODELS.some((m) => m.label === 'Claude Opus 4.8')).toBe(true);
  });
  it('estimateCost computes from per-1k pricing', () => {
    const m = MODELS.find((x) => x.label === 'Claude Opus 4.8')!;
    expect(estimateCost(m, 1000, 1000)).toBeCloseTo(m.inputPer1k + m.outputPer1k, 6);
  });
  it('TENANTS include both sandbox and production accounts with aws:// ids', () => {
    expect(TENANTS.some((t) => t.kind === 'sandbox')).toBe(true);
    expect(TENANTS.some((t) => t.kind === 'production')).toBe(true);
    expect(TENANTS.every((t) => t.account.startsWith('aws://'))).toBe(true);
  });
});
