// cedarPolicyForm — pure helpers for the Cedar Policies tab (Epic 8, T6).
//
// Kept out of CedarPoliciesTab.tsx so the add-policy payload shaping + the row
// label mapping are unit-testable in vitest (the tab itself is build-gated; only
// src/**/*.test.ts is picked up by the runner). No React, no I/O — pure TS.

import type { CedarCondition, CedarEffect, CedarPolicyRow, PrincipalHit } from '../../api/client';

/** A top-level tool parameter with a coarse type derived from its JSON schema. */
export interface SchemaParam {
  name: string;
  type: 'number' | 'string' | 'other';
}

/**
 * Parse a tool's JSON-schema input_schema → its top-level params with a coarse
 * type: JSON-schema integer/number → 'number', string → 'string', anything else
 * → 'other'. A non-object / missing schema (or missing/non-object properties) → [].
 * The condition builder only offers number/string params; 'other' params are shown
 * but yield no operators (operatorsForType('other') === []).
 */
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

/**
 * Operators legal for a param type. value = the ASCII wire token sent to the API;
 * label = the display glyph. number → the full six; string → '=' / '!='; other → [].
 */
export function operatorsForType(type: string): { value: string; label: string }[] {
  if (type === 'number') return NUMBER_OPS;
  if (type === 'string') return STRING_OPS;
  return [];
}

/** Map an ASCII wire op token to its display glyph ('!='→'≠', '<='→'≤', '>='→'≥'); else passthrough. */
export function displayOp(op: string): string {
  if (op === '!=') return '≠';
  if (op === '<=') return '≤';
  if (op === '>=') return '≥';
  return op;
}

/** Human-readable label for a policy-row condition chip, e.g. "amount < 1000" / "client_id ≠ id1". */
export function conditionLabel(c: CedarCondition): string {
  return `${c.param} ${displayOp(c.op)} ${c.value}`;
}

/**
 * Build the POST body for cedarPoliciesApi.add from a picked Entra user (or null for
 * an all-users Deny "Everyone") + a tool selection + effect/conditions. all_tools
 * collapses the tool to none (the backend ignores tool_name when all_tools is true;
 * we force it null here so the wire shape is unambiguous) AND drops conditions
 * (conditions require a specific tool whose param schema is known).
 *
 * PrincipalHit.id IS the Entra oid (the search yields the user's object id), so it
 * maps straight to principal_oid. The label falls back display_name → mail → id so
 * the policy row always has a human-readable identifier; a null hit is "Everyone".
 */
export function buildAddPolicyBody(
  hit: PrincipalHit | null,          // null === "Everyone (all users)"
  toolName: string | null,
  allTools: boolean,
  effect: CedarEffect = 'allow',
  conditions: CedarCondition[] = [],
): { principal_oid: string | null; principal_label: string; tool_name: string | null;
     all_tools: boolean; effect: CedarEffect; conditions: CedarCondition[] } {
  return {
    principal_oid: hit ? hit.id : null, // PrincipalHit.id IS the Entra oid
    principal_label: hit ? (hit.display_name || hit.mail || hit.id) : 'Everyone',
    tool_name: allTools ? null : (toolName || null),
    all_tools: allTools,
    effect,
    conditions: allTools ? [] : conditions,   // conditions require a specific tool
  };
}

/** The friendly tool label for a policy row — null tool (all-tools / foreign) → "All tools". */
export function policyToolLabel(row: CedarPolicyRow): string {
  return row.tool ?? 'All tools';
}
