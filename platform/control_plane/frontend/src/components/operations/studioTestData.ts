import { attachment, type Attachment } from './studioModel';

interface CannedOp {
  id: string;
  method: 'get' | 'post';
  path: string;
  summary: string;
}

const FNOL_OPS: CannedOp[] = [
  { id: 'getPolicy', method: 'get', path: '/policies/{policyId}', summary: 'Look up a policy and its coverage' },
  { id: 'openClaim', method: 'post', path: '/claims', summary: 'Open a first notice of loss claim' },
  { id: 'getClaimStatus', method: 'get', path: '/claims/{claimId}', summary: 'Read the status of a claim' },
  { id: 'approvePayout', method: 'post', path: '/claims/{claimId}/payout', summary: 'Approve a payout on a claim' },
];

export function cannedSpecYaml(opts: { only?: string[]; broken?: boolean } = {}): string {
  const { only, broken = false } = opts;
  const ops = FNOL_OPS.filter((o) => !only || only.includes(o.id));
  const byPath = new Map<string, CannedOp[]>();
  for (const o of ops) byPath.set(o.path, [...(byPath.get(o.path) ?? []), o]);

  const lines = [
    'openapi: 3.0.3',
    'info:',
    '  title: FNOL Claims API',
    '  version: 1.4.0',
    'servers:',
    `  - url: ${broken ? 'https://api.example.com' : 'https://claims.internal.northwind-insurance.com/v1'}`,
    'paths:',
  ];
  for (const [path, pathOps] of byPath) {
    lines.push(`  ${path}:`);
    for (const o of pathOps) {
      lines.push(`    ${o.method}:`);
      if (!(broken && o.id === 'approvePayout')) lines.push(`      operationId: ${o.id}`);
      lines.push(`      summary: ${o.summary}`);
      lines.push('      responses:');
      lines.push("        '200':");
      lines.push('          description: OK');
      lines.push('          content:');
      lines.push('            application/json:');
      lines.push('              schema:');
      if (broken && o.id === 'getClaimStatus') {
        lines.push('                oneOf:');
        lines.push('                  - type: object');
        lines.push('                  - type: string');
      } else {
        lines.push('                type: object');
      }
    }
  }
  return lines.join('\n') + '\n';
}

export const FNOL_PROMPT = `You are the FNOL intake assistant for Northwind Insurance.

Your job: take a first notice of loss from a policyholder, check their policy, and open a claim.

1. Ask for the policy number, the date and place of the loss, and what happened.
2. Call getPolicy and confirm the loss type is covered. If it is not, explain why, plainly.
3. Call openClaim with the details. Read back the claim number.
4. Tell the customer what happens next and when an adjuster will contact them.

Never promise or approve a payout. Never ask for card or bank details.`;

export const SKILL_FNOL_INTAKE = `---
name: fnol-intake
description: Collect a complete first notice of loss (who, what, when, where) before opening a claim.
---
# FNOL intake

Ask one question at a time. A claim is complete when it has: policy number, loss date,
loss location, loss type (motor, home, travel), a short description, and whether anyone was injured.
`;

export const fnolAttachments = (): Attachment[] => {
  const spec = cannedSpecYaml();
  return [
    attachment({ name: 'fnol-claims-openapi.yaml', text: spec, size: spec.length }),
    attachment({ name: 'SKILL.md', text: SKILL_FNOL_INTAKE, size: SKILL_FNOL_INTAKE.length }),
    attachment({ name: 'prompt.md', text: FNOL_PROMPT, size: FNOL_PROMPT.length }),
  ];
};
