// Decision: docs/adr/frontend-refusals-classified-by-code.md
import type { ProjectRoleRead } from '../../api/client';
import { type ApiFailure, type ErrorCode, isApiFailure } from '../../api/failure';
import type { OPS_BADGE } from './opsUi';
import { CICD_BADGE_KEY, CICD_LABEL, type CicdStatus } from './opsStatus';
import { refusalKind } from '../../api/refusalKind';

export interface EffectiveRoleSource {
  effective_role?: string | null;
}

export type ProjectRoleName = 'viewer' | 'maintainer' | 'owner';

export const PROJECT_ROLE_OPTIONS: { value: ProjectRoleName; label: string; hint: string }[] = [
  { value: 'viewer',     label: 'Viewer',     hint: 'Read the project and its repositories' },
  { value: 'maintainer', label: 'Maintainer', hint: 'Add and retry repositories; change agents' },
  { value: 'owner',      label: 'Owner',      hint: 'Full control, including promote to prod and delete' },
];

const RANK: Record<ProjectRoleName, number> = { viewer: 0, maintainer: 1, owner: 2 };

export function meetsRole(held: ProjectRoleName | null, required: ProjectRoleName): boolean {
  return held !== null && RANK[held] >= RANK[required];
}

/** `roleLevel` 2 is Platform.Admin. */
export function canManageRoles(held: ProjectRoleName | null, roleLevel: number): boolean {
  return roleLevel >= 2 || meetsRole(held, 'owner');
}

export function canPromote(
  held: ProjectRoleName | null,
  roleLevel: number,
  prodCandidateStatus: string | null,
  devOutcomeForCandidate?: string | null,
): boolean {
  return (
    prodCandidateStatus === 'pending' &&
    devOutcomeForCandidate !== 'failed' &&
    (roleLevel >= 2 || meetsRole(held, 'owner'))
  );
}

export interface ProdCandidateSource {
  prod_candidate_status?: string | null;
  prod_candidate_sha?: string | null;
  prod_candidate_actor?: string | null;
  prod_candidate_image_tag?: string | null;
  prod_candidate_at?: string | null;
}

export interface ProdCandidateView {
  /** The candidate's commit, git-short (7); null when the wire field is absent or blank. */
  shortSha: string | null;
  /** The GitHub login, `@`-prefixed exactly once; null when absent or blank. */
  actor: string | null;
  /** The image the candidate built; displayed, never sent. */
  imageTag: string | null;
  /** ISO-8601 UTC as delivered; the row shows the date only. */
  at: string | null;
}

export function prodCandidateView(
  repo: ProdCandidateSource | null | undefined,
): ProdCandidateView | null {
  if (repo?.prod_candidate_status !== 'pending') return null;
  const text = (v: string | null | undefined): string | null => {
    const s = (v ?? '').trim();
    return s.length > 0 ? s : null;
  };
  const sha = text(repo.prod_candidate_sha);
  const actor = text(repo.prod_candidate_actor);
  return {
    shortSha: sha === null ? null : sha.slice(0, 7),
    actor: actor === null ? null : actor.startsWith('@') ? actor : `@${actor}`,
    imageTag: text(repo.prod_candidate_image_tag),
    at: text(repo.prod_candidate_at),
  };
}

export type PromoteBlockedReason = 'ok' | 'no-candidate' | 'dev-deploy-failed' | 'not-owner';

// Decision: docs/adr/frontend-absent-standing-renders-nothing.md
export function promoteBlockedReason(
  held: ProjectRoleName | null,
  roleLevel: number,
  prodCandidateStatus: string | null | undefined,
  devOutcomeForCandidate?: string | null,
): PromoteBlockedReason {
  if (!(roleLevel >= 2 || meetsRole(held, 'owner'))) return 'not-owner';
  if (prodCandidateStatus !== 'pending') return 'no-candidate';
  if (devOutcomeForCandidate === 'failed') return 'dev-deploy-failed';
  return 'ok';
}

// Decision: docs/adr/frontend-two-status-machines.md
export function promotionStatusLabel(cicdStatus: CicdStatus): string {
  return CICD_LABEL[cicdStatus];
}

export function cicdBadgeKey(status: CicdStatus): keyof typeof OPS_BADGE {
  return CICD_BADGE_KEY[status];
}

export function isPromotionInFlight(cicdStatus: CicdStatus): boolean {
  return cicdStatus === 'promoting';
}

export function keepPromotionOverride(liveStatus: CicdStatus, parentStatus: CicdStatus | undefined): boolean {
  if (parentStatus === undefined) return false;
  return isPromotionInFlight(liveStatus) !== isPromotionInFlight(parentStatus);
}

export function canDestroy(held: ProjectRoleName | null, roleLevel: number): boolean {
  return canManageRoles(held, roleLevel);
}

export function mayMaintainProject(
  held: ProjectRoleName | null,
  roleLevel: number,
  ungoverned: boolean | null | undefined,
): boolean {
  return roleLevel >= 2 || meetsRole(held, 'maintainer') || ungoverned === true;
}

export function roleLabel(role: ProjectRoleName): string {
  return PROJECT_ROLE_OPTIONS.find((o) => o.value === role)?.label ?? role;
}

export function isProjectRoleName(v: string): v is ProjectRoleName {
  return v === 'viewer' || v === 'maintainer' || v === 'owner';
}

export const PROJECT_ROLE_BADGE: Record<ProjectRoleName, string> = {
  owner: 'bg-violet-50 text-violet-700',
  maintainer: 'bg-teal-50 text-teal-700',
  viewer: 'bg-slate-100 text-slate-600',
};

export interface ProjectRoleBadge {
  key: ProjectRoleName | 'unknown';
  label: string;
  cls: string;
}

export function projectRoleBadge(raw: string | null | undefined): ProjectRoleBadge {
  const value = (raw ?? '').trim();
  if (isProjectRoleName(value)) {
    return { key: value, label: roleLabel(value), cls: PROJECT_ROLE_BADGE[value] };
  }
  return {
    key: 'unknown',
    cls: 'bg-slate-100 text-slate-500',
    label: value || 'Unknown',
  };
}

export function effectiveRole(source: EffectiveRoleSource | null | undefined): ProjectRoleName | null {
  const raw = source?.effective_role;
  return typeof raw === 'string' && isProjectRoleName(raw) ? raw : null;
}

export function mayOfferGrant(
  held: ProjectRoleName | null,
  roleLevel: number,
  rosterLoaded: boolean,
): boolean {
  return rosterLoaded && canManageRoles(held, roleLevel);
}

export function grantRefusal(principalId: string, existingIds: Set<string>): string | null {
  return existingIds.has(principalId)
    ? 'That principal already holds a role on this project. Change it from its row in the list below — granting again would overwrite the role they already have.'
    : null;
}

export type EmptyRosterReason = 'ungoverned' | 'unknown-admin' | 'unknown';

export function emptyRosterReason(
  ungoverned: boolean | null | undefined,
  isAdmin: boolean,
): EmptyRosterReason {
  if (ungoverned === true) return 'ungoverned';
  return isAdmin ? 'unknown-admin' : 'unknown';
}

export const EMPTY_ROSTER_COPY: Record<EmptyRosterReason, { headline: string; detail: string }> = {
  ungoverned: {
    headline: 'No roles granted yet.',
    detail:
      'Until someone holds a role here, anyone in the project’s tenant can maintain it.',
  },
  'unknown-admin': {
    headline: 'No role rows on this project.',
    detail:
      'You see this project in full as a platform administrator, so this list cannot tell you whether anyone else governs it. Grant Owner to a group to make ownership explicit.',
  },
  unknown: {
    headline: 'No roles returned.',
    detail:
      'The server does not report this project as ungoverned, so retry before concluding that nobody has access.',
  },
};

export function sortRoleRows(rows: ProjectRoleRead[]): ProjectRoleRead[] {
  const rank = (r: ProjectRoleRead): number => (isProjectRoleName(r.role) ? RANK[r.role] : -1);
  return [...rows].sort((a, b) => {
    if (rank(a) !== rank(b)) return rank(b) - rank(a);
    if (a.principal_type !== b.principal_type) return a.principal_type === 'group' ? -1 : 1;
    return (a.principal_display || a.principal_id).localeCompare(b.principal_display || b.principal_id);
  });
}

export function filterNewPrincipals<T extends { id: string }>(
  hits: T[],
  existingIds: Set<string>,
): T[] {
  return hits.filter((h) => !existingIds.has(h.id));
}

export function grantedPrincipalIds(rows: ProjectRoleRead[]): Set<string> {
  return new Set(rows.map((r) => r.principal_id));
}

export type RoleRefusalKind = 'last_owner' | 'ownership_unverified' | 'insufficient' | 'validation';

export const ROLE_REFUSAL_CODES = {
  last_owner: ['project_role.last_owner'],
  ownership_unverified: ['project_role.ownership_unverified'],
  insufficient: ['project_role.insufficient'],
  validation: ['project_role.validation'],
} as const satisfies Record<RoleRefusalKind, readonly ErrorCode[]>;

export function classifyRoleFailure(failure: ApiFailure): RoleRefusalKind | null {
  return refusalKind(ROLE_REFUSAL_CODES, failure);
}

export type PromotionRefusalKind =
  | 'insufficient'
  | 'no_prod_candidate'
  | 'no_dev_build'
  | 'eval_gate'
  | 'promote_in_flight'
  | 'promote_failed'
  | 'repo_not_found';

export const PROMOTION_REFUSAL_CODES = {
  insufficient: ['project_role.insufficient'],
  no_prod_candidate: ['project.no_prod_candidate'],
  no_dev_build: ['project.no_dev_build'],
  eval_gate: ['project.eval_gate'],
  promote_in_flight: ['project.promote_in_flight'],
  promote_failed: ['project.promote_failed'],
  repo_not_found: ['project.repo_not_found'],
} as const satisfies Record<PromotionRefusalKind, readonly ErrorCode[]>;

export function classifyPromotionFailure(failure: ApiFailure): PromotionRefusalKind | null {
  return refusalKind(PROMOTION_REFUSAL_CODES, failure);
}

export function isNothingToRetry(err: unknown): boolean {
  return isApiFailure(err) && err.code === 'project.nothing_to_retry';
}

function sentOrFallback(err: unknown, fallback: string): string {
  const message = typeof err === 'string' ? err.trim() : err instanceof Error ? err.message.trim() : '';
  return message || fallback;
}

export function roleActionMessage(err: unknown, fallback = 'The change could not be saved.'): string {
  const kind = isApiFailure(err) ? classifyRoleFailure(err) : null;
  if (kind === 'last_owner') {
    return 'This project must keep at least one owner. Grant Owner to someone else first, then change or remove this one.';
  }
  if (kind === 'ownership_unverified') {
    return 'Couldn’t verify this project’s owners just now, so the change was refused rather than risk leaving the project with none. Try again in a moment.';
  }
  if (kind === 'insufficient') {
    return 'You need the Owner role on this project to change who has access.';
  }
  if (kind === 'validation') {
    return 'That role or principal isn’t valid for this project.';
  }
  return sentOrFallback(err, fallback);
}

export function rosterLoadMessage(err: unknown): string {
  if (isApiFailure(err) && classifyRoleFailure(err) === 'insufficient') {
    return 'You don’t have a role on this project, so its access list is hidden. Ask an owner to grant you one.';
  }
  return roleActionMessage(err, 'Failed to load the access list.');
}

export function destructiveActionMessage(
  err: unknown,
  subject: 'project' | 'repository',
  fallback: string,
): string {
  if (isApiFailure(err) && classifyRoleFailure(err) === 'insufficient') {
    return subject === 'project'
      ? 'You need the Owner role on this project to delete it. Ask an owner to delete it, or to grant you Owner.'
      : 'You need the Owner role on this project to delete a repository — the teardown removes its runtime, images, identity and GitHub repo. Ask an owner to run it, or to grant you Owner.';
  }
  return roleActionMessage(err, fallback);
}

export function maintainerActionMessage(err: unknown, fallback: string): string {
  if (isApiFailure(err) && classifyRoleFailure(err) === 'insufficient') {
    return 'You need the Maintainer role on this project to add or retry a repository. Ask an owner to grant you one.';
  }
  return roleActionMessage(err, fallback);
}

export function promotionActionMessage(err: unknown, fallback = 'The promotion could not be started.'): string {
  const kind = isApiFailure(err) ? classifyPromotionFailure(err) : null;
  if (kind === 'insufficient') {
    return 'You need the Owner role on this project to promote to prod — shipping to production always needs a named owner, even on a project with no roles granted yet. Ask an owner to promote it, or to grant you Owner.';
  }
  if (kind === 'no_prod_candidate') {
    return 'There is nothing waiting for approval on this repository — either it has no merge to main yet, or the candidate was already promoted. Merge to main first; the merge that lands there becomes the next candidate.';
  }
  if (kind === 'no_dev_build') {
    return 'This repository hasn’t deployed to dev yet, so there is no image to promote. Push to its dev branch and let that build finish first.';
  }
  if (kind === 'eval_gate') {
    return 'This build has no passing evaluation, so prod promotion needs an override. Give a reason in the dialog — it is recorded with your name — or run evals on dev until they pass.';
  }
  if (kind === 'promote_in_flight') {
    return 'A promotion is already running for this repository. Wait for it to finish — the status here updates on its own.';
  }
  if (kind === 'promote_failed') {
    return 'The promotion build couldn’t be started, so nothing was deployed to prod. Try again in a moment.';
  }
  if (kind === 'repo_not_found') {
    return 'This repository is no longer available — it may have been deleted. Reload the project.';
  }
  return roleActionMessage(err, fallback);
}
