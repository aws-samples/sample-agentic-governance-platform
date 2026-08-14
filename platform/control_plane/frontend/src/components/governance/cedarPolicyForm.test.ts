import { describe, it, expect } from 'vitest';
import {
  buildAddPolicyBody,
  policyToolLabel,
  paramsFromSchema,
  operatorsForType,
  displayOp,
  conditionLabel,
} from './cedarPolicyForm';
import type { CedarPolicyRow, PrincipalHit } from '../../api/client';

const userHit = (over: Partial<PrincipalHit> = {}): PrincipalHit => ({
  id: 'eb3da-oid',
  display_name: 'Lars Svensson',
  type: 'user',
  mail: 'lars@acme.com',
  ...over,
});

const row = (over: Partial<CedarPolicyRow> = {}): CedarPolicyRow => ({
  policy_id: 'pol-1',
  effect: 'allow',
  cedar_text: 'permit(...)',
  ...over,
});

describe('buildAddPolicyBody', () => {
  it('specific tool passes the tool through', () => {
    const body = buildAddPolicyBody(userHit(), 'T___get_claim', false);
    expect(body).toEqual({
      principal_oid: 'eb3da-oid',
      principal_label: 'Lars Svensson',
      tool_name: 'T___get_claim',
      all_tools: false,
      effect: 'allow',       // defaulted (E10)
      conditions: [],        // defaulted (E10)
    });
  });

  it('all-tools forces tool_name null', () => {
    // Even with a real tool name supplied, all_tools collapses it to null.
    const body = buildAddPolicyBody(userHit(), 'T___get_claim', true);
    expect(body.tool_name).toBeNull();
    expect(body.all_tools).toBe(true);
  });

  it('falls back to mail then id for label', () => {
    // No display_name → label is the mail.
    const noName = buildAddPolicyBody(userHit({ display_name: '' }), null, false);
    expect(noName.principal_label).toBe('lars@acme.com');
    // No display_name and no mail → label is the id (the oid).
    const idOnly = buildAddPolicyBody(userHit({ display_name: '', mail: null }), null, false);
    expect(idOnly.principal_label).toBe('eb3da-oid');
  });
});

describe('policyToolLabel', () => {
  it('maps null tool to All tools', () => {
    expect(policyToolLabel(row({ tool: null }))).toBe('All tools');
    expect(policyToolLabel(row({ tool: undefined }))).toBe('All tools');
    expect(policyToolLabel(row({ tool: 'X___y' }))).toBe('X___y');
  });
});

// Epic 10 — conditional Cedar policies (parameter conditions + Deny).

describe('paramsFromSchema', () => {
  it('parses top-level properties into coarse types', () => {
    expect(
      paramsFromSchema({
        properties: {
          amount: { type: 'number' },
          client_id: { type: 'string' },
          meta: { type: 'object' },
        },
      }),
    ).toEqual([
      { name: 'amount', type: 'number' },
      { name: 'client_id', type: 'string' },
      { name: 'meta', type: 'other' },
    ]);
  });

  it('returns [] for a non-object or missing schema', () => {
    expect(paramsFromSchema(null)).toEqual([]);
    expect(paramsFromSchema(undefined)).toEqual([]);
    expect(paramsFromSchema('nope')).toEqual([]);
    expect(paramsFromSchema(42)).toEqual([]);
    expect(paramsFromSchema({})).toEqual([]);
    expect(paramsFromSchema({ properties: null })).toEqual([]);
  });
});

describe('operatorsForType', () => {
  it('number has the full six-operator set', () => {
    const ops = operatorsForType('number');
    expect(ops).toHaveLength(6);
    expect(ops).toContainEqual({ value: '>=', label: '≥' });
  });

  it('string allows exactly = and !=', () => {
    expect(operatorsForType('string')).toEqual([
      { value: '=', label: '=' },
      { value: '!=', label: '≠' },
    ]);
  });

  it('other has no operators', () => {
    expect(operatorsForType('other')).toEqual([]);
  });
});

describe('displayOp', () => {
  it('maps ASCII wire tokens to display glyphs', () => {
    expect(displayOp('!=')).toBe('≠');
    expect(displayOp('<=')).toBe('≤');
    expect(displayOp('>=')).toBe('≥');
    expect(displayOp('<')).toBe('<');
  });
});

describe('conditionLabel', () => {
  it('renders a numeric condition with a bare value', () => {
    expect(conditionLabel({ param: 'amount', op: '<', value: '1000', type: 'number' })).toBe('amount < 1000');
  });

  it('renders a string condition with the display glyph', () => {
    expect(conditionLabel({ param: 'client_id', op: '!=', value: 'id1', type: 'string' })).toBe('client_id ≠ id1');
  });
});

describe('buildAddPolicyBody — effect + conditions', () => {
  it('carries a per-user deny with one condition', () => {
    const body = buildAddPolicyBody(userHit(), 'transfer', false, 'deny', [
      { param: 'amount', op: '>', value: '10000', type: 'number' },
    ]);
    expect(body.principal_oid).toBe('eb3da-oid');
    expect(body.effect).toBe('deny');
    expect(body.conditions).toHaveLength(1);
    expect(body.all_tools).toBe(false);
    expect(body.tool_name).toBe('transfer');
  });

  it('null hit === Everyone (all users)', () => {
    const body = buildAddPolicyBody(null, 'transfer', false, 'deny', [
      { param: 'amount', op: '>', value: '10000', type: 'number' },
    ]);
    expect(body.principal_oid).toBeNull();
    expect(body.principal_label).toBe('Everyone');
  });

  it('drops conditions for all-tools', () => {
    const body = buildAddPolicyBody(userHit(), 'transfer', true, 'allow', [
      { param: 'amount', op: '>', value: '10000', type: 'number' },
    ]);
    expect(body.tool_name).toBeNull();
    expect(body.conditions).toEqual([]);
    expect(body.all_tools).toBe(true);
  });
});
