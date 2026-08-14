import { describe, it, expect } from 'vitest';
import { experimentStages, deploymentStages, advance, isComplete, mockAccountId } from './provisioning';

describe('provisioning stage machine', () => {
  it('advance walks idle→running→done deterministically to completion', () => {
    let s = experimentStages();
    expect(isComplete(s)).toBe(false);
    for (let i = 0; i < 50 && !isComplete(s); i++) s = advance(s);
    expect(isComplete(s)).toBe(true);
    expect(s.every((x) => x.status === 'done')).toBe(true);
  });
  it('advance is immutable', () => {
    const s = experimentStages();
    advance(s);
    expect(s[0].status).toBe('idle');
  });
  it('deploymentStages has the CI/CD trio', () => {
    expect(deploymentStages().map((x) => x.key)).toContain('build');
    expect(deploymentStages().map((x) => x.key)).toContain('scan');
    expect(deploymentStages().map((x) => x.key)).toContain('deploy');
  });
  it('mockAccountId is deterministic and aws://-prefixed', () => {
    expect(mockAccountId(1)).toMatch(/^aws:\/\/\d{4}-\d{4}-\d{4}$/);
    expect(mockAccountId(1)).toBe(mockAccountId(1));
  });
});
