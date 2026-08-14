import { describe, it, expect } from 'vitest';
import { SKILLS, MODELS, TENANTS, estimateCost, findSkillsForUseCase } from './demoData';

describe('demoData', () => {
  it('has the three demo model providers', () => {
    expect(MODELS.map((m) => m.provider).sort()).toEqual(['Anthropic', 'Google', 'OpenAI']);
    expect(MODELS.some((m) => m.label === 'Claude Opus 4.8')).toBe(true);
  });
  it('estimateCost computes from per-1k pricing', () => {
    const m = MODELS.find((x) => x.label === 'Claude Opus 4.8')!;
    expect(estimateCost(m, 1000, 1000)).toBeCloseTo(m.inputPer1k + m.outputPer1k, 6);
  });
  it('findSkillsForUseCase matches claims/email keywords and is never empty', () => {
    const s = findSkillsForUseCase('triage incoming claims emails');
    expect(s.length).toBeGreaterThan(0);
    expect(s.every((x) => SKILLS.some((k) => k.id === x.id))).toBe(true);
    expect(findSkillsForUseCase('something totally unrelated').length).toBeGreaterThan(0); // fallback chain
  });
  it('TENANTS include both sandbox and production accounts with aws:// ids', () => {
    expect(TENANTS.some((t) => t.kind === 'sandbox')).toBe(true);
    expect(TENANTS.some((t) => t.kind === 'production')).toBe(true);
    expect(TENANTS.every((t) => t.account.startsWith('aws://'))).toBe(true);
  });
});
