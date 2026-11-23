// Decision: docs/adr/frontend-absent-standing-renders-nothing.md
// Rationale: frontend/README.md "Tests are node-environment only, by design"

import type { Deployment } from '../../../api/client';
import { type ApiFailure, type ErrorCode, isApiFailure } from '../../../api/failure';
import { isCicdInFlight, type CicdStatus } from '../opsStatus';
import { canDestroy, roleActionMessage, type ProjectRoleName } from '../projectRoles';
import { refusalKind } from '../../../api/refusalKind';
import { collapseByBuild, type DeploymentAttempt } from '../repositoryDetailTabs';
import { isCommitShaTag } from '../repoRowModel';

function text(value: string | null | undefined): string | null {
  const trimmed = (value ?? '').trim();
  return trimmed.length > 0 ? trimmed : null;
}

export interface StageHistory {
  stage: string;
  attempts: DeploymentAttempt[];
  currentTag: string | null;
  currentAttemptId: string | null;
}

export function stageHistories(rows: readonly Deployment[], now: number = Date.now()): StageHistory[] {
  const attempts = collapseByBuild(rows, now);
  const stages = [...new Set(attempts.map((a) => a.row.stage))].sort((a, b) => a.localeCompare(b));
  return stages.map((stage) => {
    const mine = attempts.filter((a) => a.row.stage === stage);
    const current = mine.find((a) => a.outcome === 'succeeded') ?? null;
    return {
      stage,
      attempts: mine,
      currentTag: current === null ? null : text(current.row.image_tag),
      currentAttemptId: current === null ? null : current.row.id,
    };
  });
}

export function imageColumnLabel(attempts: readonly DeploymentAttempt[]): string {
  const newest = attempts[0];
  if (newest === undefined) return 'Image';
  return isCommitShaTag(newest.row.image_tag) ? 'Revision' : 'Image';
}

export const DELETED_REPO_MARKER = 'repo deleted';

export const DEPLOYMENT_RETENTION_NOTE =
  'Deployment history is retained. The record of what reached production outlives the repository, so past deliveries stay auditable and are marked as belonging to a deleted repository.';

export interface AttemptRepoRef {
  orphaned: boolean;
  marker: string | null;
}

export function attemptRepoRef(
  viewingRepoId: string | null | undefined,
  row: Pick<Deployment, 'repo_id'>,
): AttemptRepoRef {
  const mine = text(viewingRepoId);
  const theirs = text(row.repo_id);
  if (mine === null || theirs === null || mine === theirs) {
    return { orphaned: false, marker: null };
  }
  return { orphaned: true, marker: DELETED_REPO_MARKER };
}

export function isCurrentAttempt(
  imageTag: string | null | undefined,
  currentTag: string | null | undefined,
): boolean {
  const tag = text(imageTag);
  const current = text(currentTag);
  return tag !== null && current !== null && tag === current;
}

export function mayRollback(held: ProjectRoleName | null, roleLevel: number): boolean {
  return canDestroy(held, roleLevel);
}

export type RollbackEligibility = 'ok' | 'not-owner' | 'not-a-target' | 'current' | 'in-flight';

export interface RollbackTarget {
  held: ProjectRoleName | null;
  roleLevel: number;
  cicdStatus: CicdStatus;
  outcome: Deployment['outcome'];
  imageTag: string | null | undefined;
  currentTag: string | null | undefined;
}

export function rollbackEligibility(target: RollbackTarget): RollbackEligibility {
  if (!mayRollback(target.held, target.roleLevel)) return 'not-owner';
  if (target.outcome !== 'succeeded' || text(target.imageTag) === null) return 'not-a-target';
  if (isCurrentAttempt(target.imageTag, target.currentTag)) return 'current';
  if (isCicdInFlight(target.cicdStatus)) return 'in-flight';
  return 'ok';
}

export const ROLLBACK_BLOCKED_NOTE: Record<RollbackEligibility, string | null> = {
  ok: null,
  'not-owner': null,
  'not-a-target': null,
  current: null,
  'in-flight':
    'A delivery is running for this repository. Wait for it to finish — a rollback started now would be refused.',
};

// Decision: docs/adr/frontend-refusals-classified-by-code.md
export type RollbackRefusalKind =
  | 'insufficient'
  | 'unknown_rollback_target'
  | 'promote_in_flight'
  | 'rollback_failed'
  | 'repo_not_found';

export const ROLLBACK_REFUSAL_CODES = {
  insufficient: ['project_role.insufficient'],
  unknown_rollback_target: ['project.unknown_rollback_target'],
  promote_in_flight: ['project.promote_in_flight'],
  rollback_failed: ['project.rollback_failed'],
  repo_not_found: ['project.repo_not_found'],
} as const satisfies Record<RollbackRefusalKind, readonly ErrorCode[]>;

export function classifyRollbackFailure(failure: ApiFailure): RollbackRefusalKind | null {
  return refusalKind(ROLLBACK_REFUSAL_CODES, failure);
}

export function rollbackActionMessage(
  err: unknown,
  fallback = 'The rollback could not be started.',
): string {
  const kind = isApiFailure(err) ? classifyRollbackFailure(err) : null;
  if (kind === 'insufficient') {
    return 'You need the Owner role on this project to roll back — a rollback deploys to production, so it is gated exactly as promotion is. Ask an owner to run it, or to grant you Owner.';
  }
  if (kind === 'unknown_rollback_target') {
    return 'That image is no longer a valid rollback target for this stage — only a deployment that previously SUCCEEDED here can be redeployed. Reload this page to see the current history.';
  }
  if (kind === 'promote_in_flight') {
    return 'A delivery is already running for this repository, so the rollback was refused rather than allowed to race it. Wait for it to finish and try again.';
  }
  if (kind === 'rollback_failed') {
    return 'The rollback build couldn’t be started, so nothing changed in production. Try again in a moment.';
  }
  if (kind === 'repo_not_found') {
    return 'This repository is no longer available — it may have been deleted. Reload the page.';
  }
  return roleActionMessage(err, fallback);
}
