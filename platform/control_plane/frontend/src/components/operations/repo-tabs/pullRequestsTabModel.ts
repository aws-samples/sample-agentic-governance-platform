// Decision: docs/adr/frontend-absent-standing-renders-nothing.md
// Rationale: frontend/README.md "Tests are node-environment only, by design"

import { type ApiFailure, type ErrorCode, isApiFailure } from '../../../api/failure';
import type { LoadState } from '../../../api/loadState';
import { mayMaintainProject, type ProjectRoleName } from '../projectRoles';
import { refusalKind } from '../../../api/refusalKind';

export { NO_VALUE } from '../opsLabels';

/** The structural subset of `client.ts`'s `PullRequestView` (the backend's `models/repository.py`) this tab reads. */
export interface PullRequestRow {
  number: number;
  title: string;
  /** `'open' | 'closed' | 'merged'`, derived server-side; GitHub's own state has no `merged`. */
  state: string;
  /** A GITHUB login. Not an AGP principal, and never joined to an Entra oid. */
  author: string;
  head_sha: string;
  url: string;
  can_approve: boolean;
  approve_blocked_reason?: string | null;
  /** `false` means the provider says it cannot be merged; anything else establishes nothing, since the list endpoint omits the key. */
  mergeable?: boolean | null;
}

export type PrProbe =
  | { status: 'loading' }
  | { status: 'ok' }
  | { status: 'error'; message?: string | null };

export type PrTabVisibility = 'pending' | 'visible' | 'hidden';

// Decision: docs/adr/frontend-refusals-classified-by-code.md
export type PullRequestRefusalKind =
  | 'self_approval'
  | 'not_approvable'
  | 'not_mergeable'
  | 'not_linked'
  | 'capability_missing'
  | 'no_commits'
  | 'conflict'
  | 'not_found'
  | 'insufficient'
  | 'provider_error';

export const PULL_REQUEST_REFUSAL_CODES = {
  self_approval: ['github_pr.self_approval'],
  not_approvable: ['github_pr.not_approvable'],
  not_mergeable: ['github_pr.not_mergeable'],
  not_linked: ['github_link.not_linked'],
  capability_missing: ['github_pr.capability_missing'],
  no_commits: ['github_pr.no_commits'],
  conflict: ['github_pr.conflict'],
  not_found: ['github_pr.not_found'],
  insufficient: ['project_role.insufficient'],
  provider_error: ['github_pr.provider_error', 'github_pr.connection_unavailable', 'github_link.provider_error'],
} as const satisfies Record<PullRequestRefusalKind, readonly ErrorCode[]>;

export function classifyPullRequestFailure(failure: ApiFailure): PullRequestRefusalKind | null {
  return refusalKind(PULL_REQUEST_REFUSAL_CODES, failure);
}

export function prProbeFrom(state: LoadState<readonly PullRequestRow[] | null>): PrProbe {
  if (state.kind === 'error') return { status: 'error', message: state.message };
  if (state.kind === 'loading' && state.data === null) return { status: 'loading' };
  return { status: 'ok' };
}

export function prTabVisibility(probe: PrProbe): PrTabVisibility {
  if (probe.status === 'loading') return 'pending';
  if (probe.status === 'ok') return 'visible';
  return 'hidden';
}

export function sortPullRequests(rows: readonly PullRequestRow[]): PullRequestRow[] {
  return [...rows].sort((a, b) => {
    const aOpen = a.state === 'open' ? 0 : 1;
    const bOpen = b.state === 'open' ? 0 : 1;
    return aOpen === bOpen ? b.number - a.number : aOpen - bOpen;
  });
}

export function withLocalMerges<T extends PullRequestRow>(
  rows: readonly T[],
  mergedHere: ReadonlySet<number>,
): T[] {
  return rows.map((r) =>
    r.state === 'open' && mergedHere.has(r.number) ? { ...r, state: 'merged' } : r,
  );
}

export interface PrRowActions {
  approve: boolean;
  merge: boolean;
  blockedReason: string | null;
}

export function prRowActions(
  row: PullRequestRow,
  held: ProjectRoleName | null,
  roleLevel: number,
  ungoverned: boolean | null | undefined,
): PrRowActions {
  const mayWrite = mayMaintainProject(held, roleLevel, ungoverned);
  const open = row.state === 'open';
  return {
    approve: mayWrite && open && row.can_approve,
    merge: mayWrite && open && row.mergeable !== false,
    blockedReason:
      mayWrite && open && !row.can_approve ? (row.approve_blocked_reason ?? null) : null,
  };
}

export function authorDisplay(author: string | null | undefined): string {
  const trimmed = (author ?? '').trim();
  if (!trimmed) return '—';
  return `@${trimmed.replace(/^@/, '')}`;
}

export function shortHeadSha(sha: string | null | undefined): string {
  return (sha ?? '').trim().slice(0, 7);
}

export function prStateBadgeKey(state: string | null | undefined): 'pending' | 'ok' | 'unknown' {
  const value = (state ?? '').trim().toLowerCase();
  if (value === 'open') return 'pending';
  if (value === 'merged') return 'ok';
  return 'unknown';
}

export function prStateLabel(state: string | null | undefined): string {
  const value = (state ?? '').trim().toLowerCase();
  if (value === 'open') return 'Open';
  if (value === 'merged') return 'Merged';
  if (value === 'closed') return 'Closed';
  return (state ?? '').trim() || 'Unknown';
}

export function createPullRequestBody(
  title: string,
  head: string,
  base: string,
): { title: string; head: string; base?: string } {
  const body = { title: title.trim(), head: head.trim() };
  const target = base.trim();
  return target ? { ...body, base: target } : body;
}

export function pullRequestActionMessage(err: unknown, fallback: string): string {
  const kind = isApiFailure(err) ? classifyPullRequestFailure(err) : null;
  if (kind === 'self_approval') {
    return 'You opened this pull request, so GitHub will not accept your own review. Someone else has to approve it.';
  }
  if (kind === 'not_approvable') {
    return 'This pull request cannot be approved — it may have been closed or merged since this page loaded.';
  }
  if (kind === 'not_mergeable') {
    return 'GitHub refused to merge this pull request — open it on GitHub to see what is blocking the merge.';
  }
  if (kind === 'not_linked') {
    return 'Connect your GitHub account in Settings to act on pull requests as yourself.';
  }
  if (kind === 'capability_missing') {
    return 'Pull requests are not enabled for this organization. An org admin has to grant the AGP app pull-request access.';
  }
  if (kind === 'no_commits') {
    return 'There is nothing to open a pull request for — this branch has no commits the target branch does not already have. Push a commit first.';
  }
  if (kind === 'conflict') {
    return 'GitHub declined the request and nothing was changed. Check the pull request on GitHub for the reason.';
  }
  if (kind === 'not_found') {
    return 'That pull request no longer exists.';
  }
  if (kind === 'insufficient') {
    return 'You need the Maintainer role on this project to act on its pull requests.';
  }
  if (kind === 'provider_error') {
    return 'GitHub could not be reached. Nothing was changed — try again in a moment.';
  }
  return fallback;
}

export function prReadOnlyNote(
  held: ProjectRoleName | null,
  roleLevel: number,
  ungoverned: boolean | null | undefined,
): string | null {
  return mayMaintainProject(held, roleLevel, ungoverned)
    ? null
    : 'You can read this repository’s pull requests. Opening, approving and merging need the Maintainer role on this project.';
}
