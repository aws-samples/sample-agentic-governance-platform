import type { AddPolicyRequest, ConditionIn, CedarPolicyRow, PrincipalHit } from '../../api/client';

export interface SchemaParam {
  name: string;
  type: 'number' | 'string' | 'other';
}

export function paramsFromSchema(input_schema: unknown): SchemaParam[] {
  if (!input_schema || typeof input_schema !== 'object') return [];
  const props = (input_schema as Record<string, unknown>).properties;
  if (!props || typeof props !== 'object') return [];
  return Object.entries(props as Record<string, unknown>).map(([name, spec]) => {
    const schemaType = spec && typeof spec === 'object' ? (spec as Record<string, unknown>).type : undefined;
    let type: SchemaParam['type'] = 'other';
    if (schemaType === 'integer' || schemaType === 'number') type = 'number';
    else if (schemaType === 'string') type = 'string';
    return { name, type };
  });
}

const NUMBER_OPS: { value: string; label: string }[] = [
  { value: '=', label: '=' },
  { value: '!=', label: '≠' },
  { value: '<', label: '<' },
  { value: '<=', label: '≤' },
  { value: '>', label: '>' },
  { value: '>=', label: '≥' },
];
const STRING_OPS: { value: string; label: string }[] = [
  { value: '=', label: '=' },
  { value: '!=', label: '≠' },
];

/** `value` is the ASCII token the API accepts; `label` is the glyph shown. */
export function operatorsForType(type: string): { value: string; label: string }[] {
  if (type === 'number') return NUMBER_OPS;
  if (type === 'string') return STRING_OPS;
  return [];
}

export function displayOp(op: string): string {
  if (op === '!=') return '≠';
  if (op === '<=') return '≤';
  if (op === '>=') return '≥';
  return op;
}

export function conditionLabel(c: ConditionIn): string {
  return `${c.param} ${displayOp(c.op)} ${c.value}`;
}

export function buildAddPolicyBody(
  hit: PrincipalHit | null,
  toolName: string | null,
  allTools: boolean,
  effect: CedarPolicyRow['effect'] = 'allow',
  conditions: ConditionIn[] = [],
): Required<AddPolicyRequest> {
  return {
    principal_oid: hit ? hit.id : null,
    principal_label: hit ? (hit.display_name || hit.mail || hit.id) : 'Everyone',
    tool_name: allTools ? null : (toolName || null),
    all_tools: allTools,
    effect,
    conditions: allTools ? [] : conditions,
  };
}

export function isWholeGatewayDeny(
  effect: CedarPolicyRow['effect'],
  allUsers: boolean,
  allTools: boolean,
  conditions: ConditionIn[],
): boolean {
  return effect === 'deny' && allUsers && allTools && conditions.length === 0;
}

export function policyToolLabel(row: CedarPolicyRow): string {
  return row.tool ?? 'All tools';
}

export function isContainmentRow(row: CedarPolicyRow): boolean {
  return row.kind === 'contain';
}
