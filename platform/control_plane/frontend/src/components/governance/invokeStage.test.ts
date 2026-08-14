// invokeStage.test.ts — the Test-invoke panel's stage decision (E36/T2, fix round 1).
//
// WHY THIS FILE IS THE CONTRACT. The thing under test decides WHICH RUNTIME a prompt reaches,
// and one of the reachable runtimes is production. `tsc -b` proves the types of that decision
// and nothing about the decision itself, which is how round 1 shipped a default of
// "alphabetically first stage" for a panel whose previous default was "whichever stage deployed
// last" — for a `{"prod","staging"}` agent, a silent re-point from staging to PROD.
//
// So the facts a reviewer would otherwise have to hold in their head are pinned here:
//
//   • the default is the stage the `agent_arn` scalar NAMES — not the first key, and provably
//     not the first key, because every multi-stage fixture below is ordered so that the two
//     answers differ;
//   • the options are alphabetical (the selector's rendering order is a UI promise);
//   • absent evidence yields NO `?stage=` rather than a guess — a legacy record, a single-stage
//     record, and an unattributable scalar all keep the backend's pre-E36 resolution.
//
// The ARNs are realistic and CROSS-REGION on purpose (the backend fixture for this feature does
// the same): a helper that returned the wrong stage would return an ARN in the wrong region too,
// so an assertion on the resolved value is an assertion about the runtime, not about a label.

import { describe, expect, it } from 'vitest';

import { invokeStageChoice } from './invokeStage';

const PROD_ARN = 'arn:aws:bedrock-agentcore:us-west-2:111122223333:runtime/acme_prod-AbCd';
const STAGING_ARN = 'arn:aws:bedrock-agentcore:us-east-1:111122223333:runtime/acme_staging-EfGh';
const DEV_ARN = 'arn:aws:bedrock-agentcore:us-east-1:111122223333:runtime/acme_dev-IjKl';

describe('invokeStageChoice — the no-interaction default', () => {
  it('defaults to the stage the scalar names, NOT the alphabetically first one', () => {
    // The regression this fix exists for. `prod` sorts first; the scalar names `staging`
    // (staging deployed last), so a Run the operator never configured must reach STAGING.
    const choice = invokeStageChoice(
      { prod: PROD_ARN, staging: STAGING_ARN },
      STAGING_ARN,
    );
    expect(choice.stages[0]).toBe('prod'); // the alphabetical accident round 1 used…
    expect(choice.defaultStage).toBe('staging'); // …and the evidence-based answer instead
  });

  it('defaults to prod only when prod is genuinely the last deployed stage', () => {
    // The same shape with the scalar moved: the default follows the SCALAR, not a preference
    // about which stage is safer. Pinned so the fix cannot be mistaken for "never prod".
    const choice = invokeStageChoice({ prod: PROD_ARN, staging: STAGING_ARN }, PROD_ARN);
    expect(choice.defaultStage).toBe('prod');
  });

  it('is insensitive to the insertion order of the map', () => {
    // The map arrives as JSON, so key order is the server's, not a contract. Same two stages
    // inserted the other way round must still resolve to the scalar's stage.
    const choice = invokeStageChoice(
      { staging: STAGING_ARN, prod: PROD_ARN },
      STAGING_ARN,
    );
    expect(choice.defaultStage).toBe('staging');
    expect(choice.stages).toEqual(['prod', 'staging']);
  });

  it('resolves the default to that stage’s own ARN', () => {
    // The assertion that makes this about a RUNTIME rather than a string: the chosen stage's
    // ARN is the scalar's, in the scalar's region.
    const arns = { prod: PROD_ARN, staging: STAGING_ARN };
    const choice = invokeStageChoice(arns, STAGING_ARN);
    expect(arns[choice.defaultStage as keyof typeof arns]).toBe(STAGING_ARN);
  });
});

describe('invokeStageChoice — the options and when they are offered', () => {
  it('orders the stages alphabetically, whatever the map order', () => {
    const choice = invokeStageChoice(
      { staging: STAGING_ARN, dev: DEV_ARN, prod: PROD_ARN },
      DEV_ARN,
    );
    expect(choice.stages).toEqual(['dev', 'prod', 'staging']);
    expect(choice.showSelector).toBe(true);
    expect(choice.defaultStage).toBe('dev');
  });

  it('offers the selector only for MORE THAN ONE runtime', () => {
    expect(invokeStageChoice({ dev: DEV_ARN, prod: PROD_ARN }, PROD_ARN).showSelector).toBe(true);
    expect(invokeStageChoice({ dev: DEV_ARN }, DEV_ARN).showSelector).toBe(false);
    expect(invokeStageChoice({}, DEV_ARN).showSelector).toBe(false);
    expect(invokeStageChoice(undefined, DEV_ARN).showSelector).toBe(false);
  });

  it('single stage: that stage is the only option, and the call stays stage-less', () => {
    // One runtime has nothing to CHOOSE, and the published contract is that the panel passes a
    // stage when the agent owns more than one runtime — so `defaultStage` is absent and the
    // request is the pre-E36 one byte for byte. The backend resolves the same ARN either way
    // (the map entry holding the scalar IS the scalar), so this costs no precision.
    const choice = invokeStageChoice({ dev: DEV_ARN }, DEV_ARN);
    expect(choice.stages).toEqual(['dev']);
    expect(choice.showSelector).toBe(false);
    expect(choice.defaultStage).toBeUndefined();
  });

  it('legacy record (no agent_arns): no options, no selector, no stage param', () => {
    // Pre-E28A, or an agent whose next deploy has not run under T1b's buildspec. Its one
    // runtime cannot be attributed to a stage, and the route answers `404 unknown stage` for
    // any stage such a record is asked for.
    for (const arns of [undefined, null, {}]) {
      const choice = invokeStageChoice(arns, DEV_ARN);
      expect(choice.stages).toEqual([]);
      expect(choice.showSelector).toBe(false);
      expect(choice.defaultStage).toBeUndefined();
    }
  });
});

describe('invokeStageChoice — when the scalar attributes nothing', () => {
  it('sends no stage when the scalar matches none of the stages', () => {
    // Reachable: a stage whose runtime was replaced, or a scalar written by a path the map
    // never saw. Falling back to a key would reinstate the alphabetical guess, so the panel
    // omits the parameter and the backend resolves the scalar itself — the pre-T2 target.
    const choice = invokeStageChoice(
      { prod: PROD_ARN, staging: STAGING_ARN },
      'arn:aws:bedrock-agentcore:eu-west-1:111122223333:runtime/acme_gone-MnOp',
    );
    expect(choice.showSelector).toBe(true); // the operator may still pick one
    expect(choice.stages).toEqual(['prod', 'staging']);
    expect(choice.defaultStage).toBeUndefined(); // but nothing is chosen FOR them
  });

  it.each([undefined, null, ''])('sends no stage when the scalar is %o', (scalar) => {
    // An absent or empty scalar names no runtime. `''` matters on its own: an equally empty map
    // entry would otherwise "match" it and caption a corrupt record with a stage, where the
    // stage-less path reports it honestly as a malformed ARN.
    const choice = invokeStageChoice({ prod: PROD_ARN, staging: '' }, scalar);
    expect(choice.defaultStage).toBeUndefined();
  });
});
