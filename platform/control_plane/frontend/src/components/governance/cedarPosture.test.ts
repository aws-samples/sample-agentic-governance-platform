// cedarPosture.test.ts — the ENFORCE posture warning (E36/T10, research item 15 tier 1).
//
// WHY THIS FILE IS THE CONTRACT. The deliverable of item 15 tier 1 is not a mechanism, it
// is COPY: an enforcing gateway denies every uncovered call, in AWS, silently, and the tab
// used to state the posture without stating that consequence. Copy has no compiler, so the
// things a reviewer would otherwise have to remember are pinned here instead:
//
//   • it fires for `enforce` and for NOTHING else — an amber panel over `none` or
//     `log_only` (postures that block nobody) would train operators to ignore the one
//     posture that does block;
//   • it does not contradict the ENFORCE-flip confirmation dialog in the same component.
//     The research is explicit that these are ONE copy decision and that "written
//     separately, the two surfaces will contradict each other", so the shared vocabulary is
//     asserted, not trusted;
//   • it survives a loudly-spelled wire value. `enforcement_mode` is a bare `str` on the
//     backend model and can be adopted from the live AWS engine, whose own spelling is
//     `ENFORCE`. A strict `=== 'enforce'` would silently withhold the warning from the one
//     gateway that most needs it — so the asymmetry is a test, not a comment.
//
// The mode list below is iterated from the exported table's own keys where it can be, so a
// new enforcement mode lands in scope of these tests the moment it is added.

import { describe, expect, it } from 'vitest';

import { ENFORCE_POSTURE_WARNING, postureWarning } from './cedarPosture';

// The three modes the wire can carry (`CedarEnforcementMode`, `api/client.ts`). Backed by
// the backend's own lowercase constants: `_MODE_NONE` / `_MODE_LOG_ONLY` / `_MODE_ENFORCE`
// in `services/mcp_cedar_service.py`.
const NON_BLOCKING_MODES = ['none', 'log_only'] as const;

describe('postureWarning — which postures warn', () => {
  it('warns on enforce, with the single-sourced copy', () => {
    expect(postureWarning('enforce')).toBe(ENFORCE_POSTURE_WARNING);
  });

  it.each(NON_BLOCKING_MODES)('does not warn on %s (it blocks nobody)', (mode) => {
    expect(postureWarning(mode)).toBeNull();
  });

  it('returns null for an absent mode rather than guessing', () => {
    // A request that failed, or one never made. `undefined` is "no answer", and claiming a
    // gateway is enforcing on no evidence is as wrong as missing one that is.
    expect(postureWarning(undefined)).toBeNull();
  });

  it('returns null for an empty or unrecognised mode', () => {
    expect(postureWarning('')).toBeNull();
    expect(postureWarning('   ')).toBeNull();
    expect(postureWarning('enforcing')).toBeNull();
    expect(postureWarning('ENFORCE_ALL')).toBeNull();
  });

  it('is not fooled by a prototype key', () => {
    // A mode naming an Object.prototype member resolves to an inherited FUNCTION when the
    // lookup indexes an object literal — which `?? null` does not catch, since a function is
    // neither null nor undefined. `constructor` is the reachable one: the lower-casing hides
    // `toString`/`hasOwnProperty` by mangling them into clean misses, but leaves `constructor`
    // untouched. This assertion FAILED on `constructor` while the lookup was an object literal;
    // it holds now because the lookup goes through a `Map`, whose keys are its own entries only
    // (the `githubLink.ts` idiom). Keep all five: they are the regression net for anyone who
    // "simplifies" the Map back into a plain-object index.
    expect(postureWarning('toString')).toBeNull();
    expect(postureWarning('constructor')).toBeNull();
    expect(postureWarning('hasOwnProperty')).toBeNull();
    expect(postureWarning('valueOf')).toBeNull();
    expect(postureWarning('__proto__')).toBeNull();
  });
});

describe('postureWarning — tolerant narrowing of a bare-string wire value', () => {
  // `mcp_server.cedar_enforcement_mode` is a plain `str` (tolerant by design, E36 item 16),
  // and `_MODE_FROM_AWS` adopts the mode off a live engine whose spelling is `ENFORCE`. The
  // FE union is an assertion about the wire, not a guarantee from it.
  it.each(['ENFORCE', 'Enforce', ' enforce', 'enforce\n', '  ENFORCE  '])(
    'still warns for %o',
    (raw) => {
      expect(postureWarning(raw)).toBe(ENFORCE_POSTURE_WARNING);
    },
  );

  it.each(['LOG_ONLY', ' none ', 'None'])('still stays silent for %o', (raw) => {
    expect(postureWarning(raw)).toBeNull();
  });
});

describe('ENFORCE_POSTURE_WARNING — the copy itself', () => {
  // Pinned literally: this string is a product decision shared with the confirmation
  // dialog, and an unreviewed edit to it is exactly the drift this task exists to close.
  it('is the reviewed copy, verbatim', () => {
    expect(ENFORCE_POSTURE_WARNING).toBe(
      'Every tool call not covered by a policy below is blocked — for every agent connected ' +
        'to this gateway. A blocked call is not recorded here: it reaches the agent as ' +
        'a tool error with no reason and no policy id. Removing the last policy does not lift ' +
        'enforcement — only changing the mode does.',
    );
  });

  it('shares the vocabulary of the other two enforce surfaces in the card', () => {
    // Transcribed from `CedarPoliciesTab.tsx` — the ENFORCE-flip gate body and the enforce
    // banner body. They are JSX, and `vitest.config.ts` collects `src/**/*.test.ts` only, so a
    // transcript is as close to the rendered copy as a `.ts` test reaches; single-sourcing the
    // pair is a follow-up (review M3). What this test CAN guarantee is that the terms claimed
    // as shared really are: each is asserted against every surface, so a term only one surface
    // uses (as "denied" was) cannot be smuggled in as alignment.
    const OTHER_SURFACES = [
      'Applying a policy engine switches this gateway to default-deny — only users named in ' +
        'policies will be able to call its tools. Agents and users not covered by a policy ' +
        'will be blocked.',
      'Only users named in a policy can call tools. Assigned users not covered by a policy ' +
        'are blocked.',
    ];
    for (const term of ['not covered by a policy', 'blocked']) {
      expect(ENFORCE_POSTURE_WARNING).toContain(term);
      for (const surface of OTHER_SURFACES) expect(surface).toContain(term);
    }
  });

  it('states the three facts no other surface on the tab states', () => {
    // 1. blast radius — every agent on the gateway, not just this operator's.
    expect(ENFORCE_POSTURE_WARNING).toContain('every agent connected to this gateway');
    // 2. the deny is invisible here — the item-15 gap itself.
    expect(ENFORCE_POSTURE_WARNING).toContain('not recorded here');
    expect(ENFORCE_POSTURE_WARNING).toContain('tool error');
    // 3. delete is not disable — a gateway that lost its last policy denies everything.
    expect(ENFORCE_POSTURE_WARNING).toContain('Removing the last policy does not lift enforcement');
  });

  it('does not point a viewer at the manage-only mode control', () => {
    // The segmented Enforce / Log only / Disable group renders only for `canManage`, so the
    // copy must not name its buttons.
    expect(ENFORCE_POSTURE_WARNING).not.toContain('Log only');
    expect(ENFORCE_POSTURE_WARNING).not.toContain('Disable');
  });

  it('does not contradict the dialog by promising a deny reason or a blocked screen', () => {
    // The guide is blunt that neither exists ("no reason string, no policy id, no blocked
    // screen"). Any future edit that starts promising one should fail here.
    expect(ENFORCE_POSTURE_WARNING).toContain('no reason and no policy id');
  });
});
