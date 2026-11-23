// Microsoft Graph's appRoleAssignedTo list is eventually consistent: a re-list right after a 2xx POST or DELETE may not reflect it yet.
import type { AgentGrantRead } from '../../api/client';

export type PendingMutation =
  | { kind: 'add'; grant: AgentGrantRead }
  | { kind: 'remove'; assignmentId: string };

export function mergeGrants(serverGrants: AgentGrantRead[], pending: PendingMutation[]): AgentGrantRead[] {
  const serverIds = new Set(serverGrants.map((g) => g.assignment_id));
  const removedIds = new Set(
    pending.filter((m): m is { kind: 'remove'; assignmentId: string } => m.kind === 'remove').map((m) => m.assignmentId),
  );

  const result = serverGrants.filter((g) => !removedIds.has(g.assignment_id));

  for (const m of pending) {
    if (m.kind === 'add' && !serverIds.has(m.grant.assignment_id)) {
      result.push(m.grant);
    }
  }
  return result;
}

export function unconfirmedMutations(serverGrants: AgentGrantRead[], pending: PendingMutation[]): PendingMutation[] {
  const serverIds = new Set(serverGrants.map((g) => g.assignment_id));
  return pending.filter((m) =>
    m.kind === 'add' ? !serverIds.has(m.grant.assignment_id) : serverIds.has(m.assignmentId),
  );
}
