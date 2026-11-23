import { describe, expect, it } from 'vitest';

import { OPS_BADGE } from './opsUi';
import {
  CICD_BADGE_KEY,
  CICD_LABEL,
  RUNTIME_BADGE_KEY,
  RUNTIME_LABEL,
  isCicdInFlight,
  type CicdStatus,
  type RuntimeStatusKey,
} from './opsStatus';

function statuses<K extends string>(table: Record<K, unknown>): K[] {
  const out: K[] = [];
  for (const key in table) out.push(key);
  return out;
}

const CICD_STATUSES = statuses(CICD_LABEL);
const RUNTIME_STATUSES = statuses(RUNTIME_LABEL);

describe('the closed unions', () => {
  it('both unions carry unknown, so an absent answer is never forced into a state that makes a claim', () => {
    expect(CICD_STATUSES).toContain('unknown');
    expect(RUNTIME_STATUSES).toContain('unknown');
  });
});

describe('lookup-table exhaustiveness', () => {
  it('every cicd status has a badge key and a label, and no table carries an extra key', () => {
    for (const status of CICD_STATUSES) {
      expect(Object.keys(CICD_BADGE_KEY)).toContain(status);
      expect(Object.keys(CICD_LABEL)).toContain(status);
      expect(CICD_LABEL[status].length).toBeGreaterThan(0);
    }
    expect(Object.keys(CICD_BADGE_KEY).sort()).toEqual([...CICD_STATUSES].sort());
    expect(Object.keys(CICD_LABEL).sort()).toEqual([...CICD_STATUSES].sort());
  });

  it('every runtime status has a badge key and a label, and no table carries an extra key', () => {
    for (const status of RUNTIME_STATUSES) {
      expect(Object.keys(RUNTIME_BADGE_KEY)).toContain(status);
      expect(Object.keys(RUNTIME_LABEL)).toContain(status);
      expect(RUNTIME_LABEL[status].length).toBeGreaterThan(0);
    }
    expect(Object.keys(RUNTIME_BADGE_KEY).sort()).toEqual([...RUNTIME_STATUSES].sort());
    expect(Object.keys(RUNTIME_LABEL).sort()).toEqual([...RUNTIME_STATUSES].sort());
  });

  it('every badge key names a real OPS_BADGE tint, so no pill renders unstyled', () => {
    for (const status of CICD_STATUSES) {
      expect(OPS_BADGE[CICD_BADGE_KEY[status]]).toBeTruthy();
    }
    for (const status of RUNTIME_STATUSES) {
      expect(OPS_BADGE[RUNTIME_BADGE_KEY[status]]).toBeTruthy();
    }
  });

  it('no real cicd status wears the unknown tint or the unknown label', () => {
    for (const status of CICD_STATUSES) {
      if (status === 'unknown') continue;
      expect(CICD_BADGE_KEY[status]).not.toBe('unknown');
      expect(CICD_LABEL[status]).not.toBe(CICD_LABEL.unknown);
    }
  });

  it('every runtime status is distinguishable by label, even where not_deployed and unknown share the neutral tint', () => {
    const labels = RUNTIME_STATUSES.map((s) => RUNTIME_LABEL[s]);
    expect(new Set(labels).size).toBe(RUNTIME_STATUSES.length);
    const neutral: RuntimeStatusKey[] = ['not_deployed', 'unknown'];
    for (const status of neutral) {
      expect(RUNTIME_BADGE_KEY[status]).toBe('unknown');
      expect(OPS_BADGE[RUNTIME_BADGE_KEY[status]]).not.toBe(OPS_BADGE.ready);
      expect(OPS_BADGE[RUNTIME_BADGE_KEY[status]]).not.toBe(OPS_BADGE.failed);
    }
    const tinted: RuntimeStatusKey[] = ['ready', 'creating', 'updating', 'failed'];
    for (const status of tinted) {
      expect(RUNTIME_BADGE_KEY[status]).not.toBe('unknown');
    }
  });

  it('the cicd labels are all distinct', () => {
    const labels = CICD_STATUSES.map((s) => CICD_LABEL[s]);
    expect(new Set(labels).size).toBe(CICD_STATUSES.length);
  });

  it('no label is the raw lowercase wire value, and every label is sentence case', () => {
    for (const status of CICD_STATUSES) {
      expect(CICD_LABEL[status]).not.toBe(status);
      expect(CICD_LABEL[status]).toMatch(/^[A-Z]/);
    }
    for (const status of RUNTIME_STATUSES) {
      expect(RUNTIME_LABEL[status]).not.toBe(status);
      expect(RUNTIME_LABEL[status]).toMatch(/^[A-Z]/);
    }
  });

  it('unknown describes our read, never the outcome of a probe: the label names the runtime and no attempt', () => {
    for (const word of [/unreachable/i, /timed? ?out/i, /no response/i, /could not (be )?reach/i]) {
      expect(RUNTIME_LABEL.unknown, String(word)).not.toMatch(word);
    }
    expect(RUNTIME_LABEL.unknown).toMatch(/runtime/i);
    expect(RUNTIME_LABEL.unknown).toMatch(/unknown|not reported|no status/i);
  });

  it('unknown is not confusable with a real verdict or a known absence', () => {
    expect(RUNTIME_LABEL.unknown).not.toBe(RUNTIME_LABEL.failed);
    expect(RUNTIME_LABEL.unknown).not.toBe(RUNTIME_LABEL.not_deployed);
    expect(RUNTIME_LABEL.unknown).not.toMatch(/\bfail(ed|ure)?\b/i);
    expect(RUNTIME_LABEL.unknown).not.toMatch(/deployed/i);
  });

  it('the commonest runtime pill stays short: unknown is at most one character wider than the widest other label', () => {
    const widest = Math.max(
      ...RUNTIME_STATUSES.filter((s) => s !== 'unknown').map((s) => RUNTIME_LABEL[s].length),
    );
    expect(RUNTIME_LABEL.unknown.length).toBeLessThanOrEqual(widest + 1);
  });
});

describe('tint semantics', () => {
  it('deployed is the only terminal-success cicd tint', () => {
    expect(CICD_BADGE_KEY.deployed).toBe('ready');
    for (const status of CICD_STATUSES) {
      if (status === 'deployed') continue;
      expect(OPS_BADGE[CICD_BADGE_KEY[status]]).not.toBe(OPS_BADGE.ready);
    }
  });

  it('promoting is not emerald: in flight must never read as already in prod', () => {
    expect(CICD_BADGE_KEY.promoting).toBe('provisioning');
    expect(OPS_BADGE[CICD_BADGE_KEY.promoting]).not.toBe(OPS_BADGE[CICD_BADGE_KEY.deployed]);
  });

  it('delivery ready is not emerald: scaffolded but never built is not a success', () => {
    expect(CICD_BADGE_KEY.ready).toBe('pending');
    expect(OPS_BADGE[CICD_BADGE_KEY.ready]).not.toBe(OPS_BADGE.ready);
    expect(OPS_BADGE[CICD_BADGE_KEY.ready]).not.toBe(OPS_BADGE[CICD_BADGE_KEY.deployed]);
  });

  it('a repo live in production never renders amber', () => {
    expect(OPS_BADGE[CICD_BADGE_KEY.deployed]).not.toBe(OPS_BADGE[CICD_BADGE_KEY.provisioning]);
    expect(OPS_BADGE[CICD_BADGE_KEY.deployed]).not.toBe(OPS_BADGE[CICD_BADGE_KEY.promoting]);
    expect(OPS_BADGE[CICD_BADGE_KEY.deployed]).not.toBe(OPS_BADGE[CICD_BADGE_KEY.ready]);
    expect(OPS_BADGE[CICD_BADGE_KEY.deployed]).not.toMatch(/amber/);
  });

  it('failed is the only failure tint, on both machines', () => {
    expect(CICD_BADGE_KEY.failed).toBe('failed');
    expect(RUNTIME_BADGE_KEY.failed).toBe('failed');
  });

  it('unknown is visually distinct from failed, on both machines', () => {
    expect(OPS_BADGE[CICD_BADGE_KEY.unknown]).not.toBe(OPS_BADGE[CICD_BADGE_KEY.failed]);
    expect(OPS_BADGE[RUNTIME_BADGE_KEY.unknown]).not.toBe(OPS_BADGE[RUNTIME_BADGE_KEY.failed]);
  });

  it('unknown is distinct from in flight on both machines: no answer is not working on it', () => {
    expect(OPS_BADGE[CICD_BADGE_KEY.unknown]).not.toBe(OPS_BADGE[CICD_BADGE_KEY.provisioning]);
    expect(OPS_BADGE[RUNTIME_BADGE_KEY.unknown]).not.toBe(OPS_BADGE[RUNTIME_BADGE_KEY.creating]);
  });

  it('runtime not_deployed is neither a failure nor a success', () => {
    expect(OPS_BADGE[RUNTIME_BADGE_KEY.not_deployed]).not.toBe(OPS_BADGE.failed);
    expect(OPS_BADGE[RUNTIME_BADGE_KEY.not_deployed]).not.toBe(OPS_BADGE.ready);
  });

  it('runtime ready is the only emerald runtime tint, and keeps the plain word', () => {
    expect(RUNTIME_BADGE_KEY.ready).toBe('ready');
    expect(RUNTIME_LABEL.ready).toBe('Ready');
    for (const status of RUNTIME_STATUSES) {
      if (status === 'ready') continue;
      expect(OPS_BADGE[RUNTIME_BADGE_KEY[status]]).not.toBe(OPS_BADGE.ready);
    }
  });

  it('the two machines share no label, since the row shows them side by side: delivery ready reads Not built yet and each failed names its machine', () => {
    const cicdLabels = new Set(CICD_STATUSES.map((s) => CICD_LABEL[s]));
    for (const runtime of RUNTIME_STATUSES) {
      expect(cicdLabels).not.toContain(RUNTIME_LABEL[runtime]);
    }
    expect(CICD_LABEL.ready).not.toBe(RUNTIME_LABEL.ready);
    expect(CICD_LABEL.ready).toBe('Not built yet');
    expect(CICD_LABEL.ready).not.toBe(CICD_LABEL.deployed);
    expect(CICD_LABEL.ready).toMatch(/\bnot\b/i);
    expect(CICD_LABEL.failed).not.toBe(RUNTIME_LABEL.failed);
    expect(CICD_LABEL.failed).toMatch(/delivery/i);
    expect(RUNTIME_LABEL.failed).toMatch(/runtime/i);
  });

  it('the promoting label names no stage: a prod promote, a rollback to any stage and a dev Build now all write it', () => {
    expect(CICD_LABEL.promoting).not.toMatch(/\b(prod|production|dev|uat|stage)\b/i);
    expect(CICD_LABEL.promoting).toMatch(/…$/);
  });

  it('the two machines are independent: a deployed repo with no runtime answer is not green', () => {
    const cicd: CicdStatus = 'deployed';
    const runtime: RuntimeStatusKey = 'unknown';
    expect(CICD_BADGE_KEY[cicd]).toBe('ready');
    expect(RUNTIME_BADGE_KEY[runtime]).toBe('unknown');
    expect(OPS_BADGE[RUNTIME_BADGE_KEY[runtime]]).not.toBe(OPS_BADGE.ready);
  });
});

describe('isCicdInFlight', () => {
  it('is true for exactly provisioning and promoting, the two states a writer parks a repo in while work runs', () => {
    const inFlight = CICD_STATUSES.filter((s) => isCicdInFlight(s));
    expect([...inFlight].sort()).toEqual(['promoting', 'provisioning']);
  });

  it('is false for every terminal, resting or unestablished member', () => {
    const resting: CicdStatus[] = ['failed', 'deployed', 'ready', 'unknown'];
    for (const status of resting) {
      expect(isCicdInFlight(status)).toBe(false);
    }
  });

  it('does not treat ready as in flight: it rests between materialize and the first build, and wears the pending amber rather than the in-flight one', () => {
    expect(isCicdInFlight('ready')).toBe(false);
    expect(CICD_BADGE_KEY.ready).not.toBe('provisioning');
  });

  it('agrees with the in-flight tint', () => {
    for (const status of CICD_STATUSES) {
      if (isCicdInFlight(status)) expect(CICD_BADGE_KEY[status]).toBe('provisioning');
    }
  });
});

describe('the unions stay closed', () => {
  it('the tables are keyed by the literal unions, not by string', () => {
    type AssertClosed<T extends CicdStatus> = T;
    type _Cicd = AssertClosed<keyof typeof CICD_LABEL>;
    type AssertRuntimeClosed<T extends RuntimeStatusKey> = T;
    type _Runtime = AssertRuntimeClosed<keyof typeof RUNTIME_LABEL>;
    const notAStatus = 'definitely-not-a-status';
    expect(CICD_STATUSES).not.toContain(notAStatus);
    const _typecheck: [_Cicd, _Runtime] = ['deployed', 'ready'];
    expect(_typecheck).toHaveLength(2);
  });
});
