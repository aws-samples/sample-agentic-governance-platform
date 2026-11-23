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
  it('advance is immutable and returns fresh stage objects', () => {
    const s = experimentStages();
    const next = advance(s);
    expect(s[0].status).toBe('idle');
    expect(next[0]).not.toBe(s[0]);
  });
  it('advance is idempotent at completion, so a loop converges', () => {
    let s = experimentStages();
    for (let i = 0; i < 50 && !isComplete(s); i++) s = advance(s);
    expect(advance(s)).toEqual(s);
  });
  it('experimentStages beats are allocate, directory, collaborators, ready, all idle', () => {
    expect(experimentStages().map((x) => x.key)).toEqual(['allocate', 'directory', 'collaborators', 'ready']);
    expect(experimentStages().every((x) => x.status === 'idle')).toBe(true);
  });
  it('deploymentStages leads with the scaffolding beats into the CI/CD trio build, scan, deploy', () => {
    expect(deploymentStages().map((x) => x.key)).toEqual(['template', 'repository', 'entra', 'build', 'scan', 'deploy']);
  });
  it('mockAccountId is deterministic and aws://-prefixed', () => {
    expect(mockAccountId(1)).toMatch(/^aws:\/\/\d{4}-\d{4}-\d{4}$/);
    expect(mockAccountId(1)).toBe(mockAccountId(1));
  });
});
