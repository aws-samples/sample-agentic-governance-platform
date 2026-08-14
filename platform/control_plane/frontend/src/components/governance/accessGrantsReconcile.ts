// accessGrantsReconcile — pure read-your-writes overlay for the Access tab (Epic 6).
//
// WHY: grants are live Entra app-role assignments and Microsoft Graph's
// appRoleAssignedTo (the LIST/read path) is eventually consistent. A grant's
// authoritative POST/DELETE response lands immediately, but an immediate re-list
// usually has NOT replicated yet — so blindly replacing the table with a fresh
// grantsApi.list() clobbers a just-added row (it vanishes) or resurrects a
// just-removed one. An optimistic row born from a 2xx is more trustworthy than an
// immediate stale read, so it MUST survive a reconcile that doesn't yet reflect it.
//
// These helpers overlay the operator's recent *successful* mutations on top of the
// server read until the read actually catches up, then drop the now-confirmed
// overlay. Kept out of AccessTab.tsx so the merge/confirmation logic is
// unit-testable in vitest (only src/**/*.test.ts is picked up by the runner). No
// React, no I/O — pure TS; imports only the Grant *type*.

import type { Grant } from '../../api/client';

/**
 * A successful (2xx) optimistic mutation awaiting confirmation by the server read.
 * `add` carries the full authoritative grant the POST returned (the row to keep
 * visible); `remove` carries the assignment_id the DELETE targeted (the row to
 * keep hidden). Discriminated on `kind`.
 */
export type PendingMutation =
  | { kind: 'add'; grant: Grant }
  | { kind: 'remove'; assignmentId: string };

/**
 * Overlay pending mutations on a server read (read-your-writes). Pure — never
 * mutates its inputs. Order is deterministic: server rows first (in their server
 * order), then any pending adds the server read does not yet contain (in pending
 * order). Pending removes filter their assignment_id out of the result. De-dupes
 * by assignment_id, so an add already reflected by the server read is NOT appended
 * twice (the server row wins its position).
 */
export function mergeGrants(serverGrants: Grant[], pending: PendingMutation[]): Grant[] {
  const serverIds = new Set(serverGrants.map((g) => g.assignment_id));
  const removedIds = new Set(
    pending.filter((m): m is { kind: 'remove'; assignmentId: string } => m.kind === 'remove').map((m) => m.assignmentId),
  );

  // Server rows first, with optimistically-removed rows filtered out.
  const result = serverGrants.filter((g) => !removedIds.has(g.assignment_id));

  // Then append optimistic adds the server read does not yet reflect (de-duped).
  for (const m of pending) {
    if (m.kind === 'add' && !serverIds.has(m.grant.assignment_id)) {
      result.push(m.grant);
    }
  }
  return result;
}

/**
 * The subset of `pending` the server read does NOT yet reflect — i.e. the still-
 * unconfirmed overlay the caller keeps applying and keeps polling for. An add is
 * confirmed (dropped) once serverGrants contains its assignment_id; a remove is
 * confirmed (dropped) once serverGrants no longer contains its assignment_id. When
 * the returned list is empty, the server read has caught up and polling can stop.
 * Pure — does not mutate its inputs.
 */
export function unconfirmedMutations(serverGrants: Grant[], pending: PendingMutation[]): PendingMutation[] {
  const serverIds = new Set(serverGrants.map((g) => g.assignment_id));
  return pending.filter((m) =>
    m.kind === 'add' ? !serverIds.has(m.grant.assignment_id) : serverIds.has(m.assignmentId),
  );
}
