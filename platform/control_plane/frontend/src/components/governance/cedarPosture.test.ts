import { describe, expect, it } from 'vitest';

import { ENFORCEMENT_COPY, ENFORCE_FLIP_WARNING, ENFORCE_POSTURE_WARNING, postureWarning } from './cedarPosture';

const NON_BLOCKING_MODES = ['none', 'log_only'] as const;

describe('postureWarning — which postures warn', () => {
  it('warns on enforce, with the single-sourced copy', () => {
    expect(postureWarning('enforce')).toBe(ENFORCE_POSTURE_WARNING);
  });

  it.each(NON_BLOCKING_MODES)(
    'does not warn on %s: an amber panel over a posture that blocks nobody would train operators to ignore the one that does',
    (mode) => {
      expect(postureWarning(mode)).toBeNull();
    },
  );

  it('returns null for an absent mode (a request that failed or was never made) rather than claiming a posture on no evidence', () => {
    expect(postureWarning(undefined)).toBeNull();
  });

  it('returns null for an empty or unrecognised mode', () => {
    expect(postureWarning('')).toBeNull();
    expect(postureWarning('   ')).toBeNull();
    expect(postureWarning('enforcing')).toBeNull();
    expect(postureWarning('ENFORCE_ALL')).toBeNull();
  });

  it('returns null for every Object.prototype member name instead of the inherited function an object-literal index would hand React', () => {
    expect(postureWarning('toString')).toBeNull();
    expect(postureWarning('constructor')).toBeNull();
    expect(postureWarning('hasOwnProperty')).toBeNull();
    expect(postureWarning('valueOf')).toBeNull();
    expect(postureWarning('__proto__')).toBeNull();
  });
});

describe('postureWarning — enforcement_mode is a bare str on the wire that the live engine spells ENFORCE, so narrowing trims and lower-cases', () => {
  it.each(['ENFORCE', 'Enforce', ' enforce', 'enforce\n', '  ENFORCE  '])(
    'still warns for %o: a strict === "enforce" would withhold the warning from the one gateway that most needs it',
    (raw) => {
      expect(postureWarning(raw)).toBe(ENFORCE_POSTURE_WARNING);
    },
  );

  it.each(['LOG_ONLY', ' none ', 'None'])('still stays silent for %o', (raw) => {
    expect(postureWarning(raw)).toBeNull();
  });
});

describe('ENFORCE_POSTURE_WARNING — the copy itself, pinned because copy has no compiler', () => {
  it('is the reviewed copy, verbatim', () => {
    expect(ENFORCE_POSTURE_WARNING).toBe(
      'Every tool call not covered by a policy below is blocked — for every agent connected ' +
        'to this gateway. A blocked call is not recorded here: it reaches the agent as ' +
        'a tool error ("Tool Execution Denied") naming the policy id. Removing the last policy does not lift ' +
        'enforcement — only changing the mode does.',
    );
  });

  it('shares "blocked" and "not covered by a policy" with the ENFORCE-flip dialog and the enforce banner, each term asserted against every surface', () => {
    const OTHER_SURFACES = [ENFORCE_FLIP_WARNING, ENFORCEMENT_COPY.enforce.body];
    for (const term of ['not covered by a policy', 'blocked']) {
      expect(ENFORCE_POSTURE_WARNING).toContain(term);
      for (const surface of OTHER_SURFACES) expect(surface).toContain(term);
    }
  });

  it('states the three facts no other surface on the tab states: the blast radius is every agent on the gateway, the deny is not recorded here, removing the last policy does not lift enforcement', () => {
    expect(ENFORCE_POSTURE_WARNING).toContain('every agent connected to this gateway');
    expect(ENFORCE_POSTURE_WARNING).toContain('not recorded here');
    expect(ENFORCE_POSTURE_WARNING).toContain('tool error');
    expect(ENFORCE_POSTURE_WARNING).toContain('Removing the last policy does not lift enforcement');
  });

  it('never names the Log only / Disable buttons, which render only for a manager', () => {
    expect(ENFORCE_POSTURE_WARNING).not.toContain('Log only');
    expect(ENFORCE_POSTURE_WARNING).not.toContain('Disable');
  });

  it('names "Tool Execution Denied" and the policy id as what the agent sees, and promises no blocked screen', () => {
    expect(ENFORCE_POSTURE_WARNING).toContain('Tool Execution Denied');
    expect(ENFORCE_POSTURE_WARNING).toContain('naming the policy id');
    expect(ENFORCE_POSTURE_WARNING).not.toContain('no policy id');
    expect(ENFORCE_POSTURE_WARNING).not.toContain('blocked screen');
  });
});

describe('ENFORCEMENT_COPY and ENFORCE_FLIP_WARNING — the banner and dialog copy, owned once for the Policies tab and the graph drawer', () => {
  it('is the reviewed copy, verbatim', () => {
    expect(ENFORCEMENT_COPY).toEqual({
      none: {
        heading: 'No policy engine attached',
        body: 'Every assigned user can call any tool on this gateway. Add a policy to switch to default-deny.',
      },
      log_only: {
        heading: 'Logging only',
        body: 'Policies are evaluated and logged to CloudWatch, but not enforced — every assigned user can still call any tool.',
      },
      enforce: {
        heading: 'Enforcing — default-deny',
        body: 'Only users named in a policy can call tools. Assigned users not covered by a policy are blocked.',
      },
    });
    expect(ENFORCE_FLIP_WARNING).toBe(
      'Applying a policy engine switches this gateway to default-deny — only users named in ' +
        'policies will be able to call its tools. Agents and users not covered by a policy ' +
        'will be blocked.',
    );
  });
});
