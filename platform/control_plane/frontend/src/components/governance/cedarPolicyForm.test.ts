import { describe, it, expect } from 'vitest';
import {
  buildAddPolicyBody,
  policyToolLabel,
  paramsFromSchema,
  operatorsForType,
  displayOp,
  conditionLabel,
  isContainmentRow,
  isWholeGatewayDeny,
} from './cedarPolicyForm';
import { stripComments } from '../../rawSource';
import cedarTabRaw from './CedarPoliciesTab.tsx?raw';
import type { CedarPolicyRow, PrincipalHit } from '../../api/client';
import { principalHit } from '../../test/wire';

const cedarTabSrc = stripComments(cedarTabRaw);

const LARS: Partial<PrincipalHit> = { id: 'eb3da-oid', display_name: 'Lars Svensson', mail: 'lars@acme.com' };

const row = (over: Partial<CedarPolicyRow> = {}): CedarPolicyRow => ({
  policy_id: 'pol-1',
  effect: 'allow',
  cedar_text: 'permit(...)',
  user_oid: null,
  user_label: null,
  tool: null,
  conditions: [],
  managed: false,
  kind: null,
  agent_app_id: null,
  ...over,
});

describe('buildAddPolicyBody', () => {
  it('specific tool passes the tool through, with effect allow and no conditions by default', () => {
    const body = buildAddPolicyBody(principalHit(LARS), 'T___get_claim', false);
    expect(body).toEqual({
      principal_oid: 'eb3da-oid',
      principal_label: 'Lars Svensson',
      tool_name: 'T___get_claim',
      all_tools: false,
      effect: 'allow',
      conditions: [],
    });
  });

  it('all-tools forces tool_name null even when a real tool name is supplied, so the wire shape is unambiguous', () => {
    const body = buildAddPolicyBody(principalHit(LARS), 'T___get_claim', true);
    expect(body.tool_name).toBeNull();
    expect(body.all_tools).toBe(true);
  });

  it('labels the principal by display_name, then mail, then the id (the Entra oid)', () => {
    const noName = buildAddPolicyBody(principalHit({ ...LARS, display_name: '' }), null, false);
    expect(noName.principal_label).toBe('lars@acme.com');
    const idOnly = buildAddPolicyBody(principalHit({ ...LARS, display_name: '', mail: null }), null, false);
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
    const body = buildAddPolicyBody(principalHit(LARS), 'transfer', false, 'deny', [
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

  it('drops conditions for all-tools, since a condition needs a specific tool whose param schema is known', () => {
    const body = buildAddPolicyBody(principalHit(LARS), 'transfer', true, 'allow', [
      { param: 'amount', op: '>', value: '10000', type: 'number' },
    ]);
    expect(body.tool_name).toBeNull();
    expect(body.conditions).toEqual([]);
    expect(body.all_tools).toBe(true);
  });
});

describe('isWholeGatewayDeny', () => {
  const amount = { param: 'amount', op: '>', value: '10000', type: 'number' } as const;

  it('refuses an unconditional all-users all-tools Deny, which would block the whole gateway', () => {
    expect(isWholeGatewayDeny('deny', true, true, [])).toBe(true);
  });

  it('lets through every Deny that names a user, a tool or a condition, and every Allow', () => {
    expect(isWholeGatewayDeny('deny', false, true, [])).toBe(false);
    expect(isWholeGatewayDeny('deny', true, false, [])).toBe(false);
    expect(isWholeGatewayDeny('deny', true, true, [amount])).toBe(false);
    expect(isWholeGatewayDeny('allow', true, true, [])).toBe(false);
  });
});

describe('agent-containment rows', () => {
  const row = (over: Partial<CedarPolicyRow> = {}): CedarPolicyRow => ({
    policy_id: 'p-1', effect: 'deny', tool: null, managed: true, cedar_text: 'forbid(...)',
    user_oid: null, user_label: null, conditions: [], kind: null, agent_app_id: null, ...over,
  });

  it('recognises only kind "contain"', () => {
    expect(isContainmentRow(row({ kind: 'contain', agent_app_id: 'app-1' }))).toBe(true);
    expect(isContainmentRow(row({ kind: null }))).toBe(false);
    expect(isContainmentRow(row())).toBe(false);
  });

  it('renders as "Agent containment" with no Remove button and a hint to restore: the forbid is owned by the containment and only Restore removes it', () => {
    expect(cedarTabSrc).toContain('Agent containment');
    expect(cedarTabSrc).toContain('Restore the agent on its Containment tab');
    expect(cedarTabSrc).toMatch(/canManage && !isContain && \(/);
  });
});
