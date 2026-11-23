import { describe, it, expect } from 'vitest';
import { probeNotice, probeTime, type ProbeOutcome } from './tenantProbeNotice';

const AT = new Date(2026, 9, 5, 9, 7);

describe('probeTime', () => {
  it('formats the local wall clock as a zero-padded 24h hh:mm', () => {
    expect(probeTime(AT)).toBe('09:07');
    expect(probeTime(new Date(2026, 9, 5, 23, 59))).toBe('23:59');
  });
});

describe('probeNotice', () => {
  it('says all ready when no verb is unavailable', () => {
    expect(
      probeNotice(AT, {
        ok: true,
        tenant: {
          readiness: {
            deploy: { verb: 'deploy', state: 'ready', reason: '', stage: null },
            guardrails: { verb: 'guardrails', state: 'not_applicable', reason: '', stage: null },
          },
        },
      }),
    ).toBe('Probed 09:07 — all ready');
  });

  it('names each unavailable verb, with its stage', () => {
    expect(
      probeNotice(AT, {
        ok: true,
        tenant: {
          readiness: {
            invoke: { verb: 'invoke', state: 'unavailable', reason: 'invoke_unavailable', stage: null },
            deploy: { verb: 'deploy', state: 'unavailable', reason: 'deploy_role_not_assumable', stage: 'prod' },
            publish: { verb: 'publish', state: 'ready', reason: '', stage: null },
          },
        },
      }),
    ).toBe('Probed 09:07 — unavailable: Invoke, Deploy (prod)');
  });

  it('fails closed, as readinessBadge does: a state this build does not know reads unavailable', () => {
    const outcome: unknown = { ok: true, tenant: { readiness: { deploy: { state: 'weird', reason: '' } } } };
    expect(probeNotice(AT, outcome as ProbeOutcome)).toBe('Probed 09:07 — unavailable: Deploy');
  });

  it('says so when the backend reported no readiness', () => {
    expect(probeNotice(AT, { ok: true, tenant: { readiness: {} } })).toBe(
      'Probed 09:07 — no readiness reported',
    );
  });

  it('carries the failure message', () => {
    expect(probeNotice(AT, { ok: false, message: 'platform unreachable' })).toBe(
      'Probed 09:07 — failed: platform unreachable',
    );
  });
});
