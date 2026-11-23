import { describe, expect, it } from 'vitest';

import { invokeStageChoice } from './invokeStage';

const PROD_ARN = 'arn:aws:bedrock-agentcore:us-west-2:111122223333:runtime/acme_prod-AbCd';
const STAGING_ARN = 'arn:aws:bedrock-agentcore:us-east-1:111122223333:runtime/acme_staging-EfGh';
const DEV_ARN = 'arn:aws:bedrock-agentcore:us-east-1:111122223333:runtime/acme_dev-IjKl';

describe('invokeStageChoice — the no-interaction default', () => {
  it('defaults to the stage the scalar names (the last deployed), not the alphabetically first one, so a Run the operator never configured reaches staging here', () => {
    const choice = invokeStageChoice(
      { prod: PROD_ARN, staging: STAGING_ARN },
      STAGING_ARN,
    );
    expect(choice.stages[0]).toBe('prod');
    expect(choice.defaultStage).toBe('staging');
  });

  it('defaults to prod when prod is what the scalar names: the default follows the scalar, not a preference for a safer stage', () => {
    const choice = invokeStageChoice({ prod: PROD_ARN, staging: STAGING_ARN }, PROD_ARN);
    expect(choice.defaultStage).toBe('prod');
  });

  it('is insensitive to the insertion order of the map, which is the server\'s JSON order and not a contract', () => {
    const choice = invokeStageChoice(
      { staging: STAGING_ARN, prod: PROD_ARN },
      STAGING_ARN,
    );
    expect(choice.defaultStage).toBe('staging');
    expect(choice.stages).toEqual(['prod', 'staging']);
  });

  it('resolves the default to that stage\'s own ARN, in the scalar\'s region: an assertion about the runtime, not a label', () => {
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

  it('single stage: that stage is the only option and the call stays stage-less, since one runtime has nothing to choose and the backend resolves the same ARN either way', () => {
    const choice = invokeStageChoice({ dev: DEV_ARN }, DEV_ARN);
    expect(choice.stages).toEqual(['dev']);
    expect(choice.showSelector).toBe(false);
    expect(choice.defaultStage).toBeUndefined();
  });

  it('legacy record (no agent_arns): no options, no selector, no stage param, since its one runtime cannot be attributed to a stage and the route answers 404 unknown stage for any it is asked for', () => {
    for (const arns of [undefined, null, {}]) {
      const choice = invokeStageChoice(arns, DEV_ARN);
      expect(choice.stages).toEqual([]);
      expect(choice.showSelector).toBe(false);
      expect(choice.defaultStage).toBeUndefined();
    }
  });
});

describe('invokeStageChoice — when the scalar attributes nothing', () => {
  it('sends no stage when the scalar matches none of the stages: the operator may still pick one, but a key would reinstate the alphabetical guess, so the backend resolves the scalar itself', () => {
    const choice = invokeStageChoice(
      { prod: PROD_ARN, staging: STAGING_ARN },
      'arn:aws:bedrock-agentcore:eu-west-1:111122223333:runtime/acme_gone-MnOp',
    );
    expect(choice.showSelector).toBe(true);
    expect(choice.stages).toEqual(['prod', 'staging']);
    expect(choice.defaultStage).toBeUndefined();
  });

  it.each([undefined, null, ''])('sends no stage when the scalar is %o: it names no runtime, and an empty one would otherwise match an equally empty map entry and caption a corrupt record', (scalar) => {
    const choice = invokeStageChoice({ prod: PROD_ARN, staging: '' }, scalar);
    expect(choice.defaultStage).toBeUndefined();
  });
});
