// Decision: docs/adr/frontend-absent-standing-renders-nothing.md
// Rationale: frontend/README.md "Tests are node-environment only, by design"

import { isCicdInFlight, type CicdStatus, type RuntimeStatusKey } from './opsStatus';
import { NO_VALUE } from './opsLabels';
import { initialsFor } from '../shared/initials';

export interface RepoRowSource {
  cicd_status: CicdStatus;
  last_dev_image_tag?: string | null;
  last_promoted_image_tag?: string | null;
  prod_candidate_status?: string | null;
  created_by?: string | null;
  /** The backend's dev outcome for the candidate's tag, `'started' | 'succeeded' | 'failed'`; null or absent means no dev row names it, which is unknown rather than failed. */
  dev_outcome_for_candidate?: string | null;
}

function text(value: string | null | undefined): string | null {
  const trimmed = (value ?? '').trim();
  return trimmed.length > 0 ? trimmed : null;
}

export const REPO_ROW_COLUMNS = 6;

export const REPO_ACTIONS = [
  'delivery-failed',
  'runtime-failed',
  'approval-pending',
  'drift',
  'never-deployed',
  'status-unknown',
  'none',
] as const;
export type RepoActionKey = typeof REPO_ACTIONS[number];

export function repoAction(repo: RepoRowSource, runtime: RuntimeStatusKey): RepoActionKey {
  const cicd = repo.cicd_status;

  if (cicd === 'failed') return 'delivery-failed';
  if (runtime === 'failed') return 'runtime-failed';

  if (isCicdInFlight(cicd)) return 'none';

  if (repo.prod_candidate_status === 'pending') return 'approval-pending';

  if (prodVersion(repo).drifted) return 'drift';

  if (text(repo.last_promoted_image_tag) === null) return 'never-deployed';

  if (cicd === 'unknown') return 'status-unknown';

  return 'none';
}

export const REPO_ACTION_RANK: Record<RepoActionKey, number> = {
  'delivery-failed': 0,
  'runtime-failed': 1,
  'approval-pending': 2,
  drift: 3,
  'never-deployed': 4,
  'status-unknown': 5,
  none: 6,
};

export const REPO_ACTION_LABEL: Record<RepoActionKey, string> = {
  'delivery-failed': 'Delivery failed',
  'runtime-failed': 'Runtime down',
  'approval-pending': 'Awaiting owner approval',
  drift: 'Prod behind dev',
  'never-deployed': 'Never deployed',
  'status-unknown': 'Status not reported',
  none: NO_VALUE,
};

export const REPO_ACTION_TONE: Record<RepoActionKey, 'fault' | 'attention' | 'quiet'> = {
  'delivery-failed': 'fault',
  'runtime-failed': 'fault',
  'approval-pending': 'attention',
  drift: 'attention',
  'never-deployed': 'quiet',
  'status-unknown': 'quiet',
  none: 'quiet',
};

export function sortRepoRows<T extends RepoRowSource & { id: string; name: string }>(
  rows: readonly T[],
  runtimeFor: (repo: T) => RuntimeStatusKey = () => 'unknown',
): T[] {
  return [...rows].sort((a, b) => {
    const rankA = REPO_ACTION_RANK[repoAction(a, runtimeFor(a))];
    const rankB = REPO_ACTION_RANK[repoAction(b, runtimeFor(b))];
    return rankA !== rankB ? rankA - rankB : a.name.localeCompare(b.name);
  });
}

export interface ProdVersion {
  tag: string | null;
  drifted: boolean;
  devTag: string | null;
}

export function prodVersion(repo: RepoRowSource): ProdVersion {
  const tag = text(repo.last_promoted_image_tag);
  const dev = text(repo.last_dev_image_tag);
  const drifted = tag !== null && dev !== null && tag !== dev;
  return { tag, drifted, devTag: drifted ? dev : null };
}

export function ownerLabel(createdBy: string | null | undefined): { name: string; initials: string } {
  const name = text(createdBy);
  return name === null ? { name: 'Unknown', initials: '?' } : { name, initials: initialsFor(name) };
}

export const CANDIDATE_ACTOR_VERB = 'pushed by';

export const LAST_DEPLOYED_LABEL = 'Last deployed';
export const LAST_DEPLOYED_ACTOR_VERB = 'last deployed by';

export const PROMOTION_READY_LABEL = 'Ready for promotion';

export function promotionReadiness(repo: RepoRowSource): boolean {
  return (
    repo.prod_candidate_status === 'pending' && repo.dev_outcome_for_candidate !== 'failed'
  );
}

export function repoActionBand(
  repo: RepoRowSource & { artifact_kind?: string | null },
  promoteState: string,
  inFlight: boolean,
): 'awaiting-approval' | 'dev-deploy-failed' | null {
  if (inFlight || repo.artifact_kind === 'mcp_server') return null;
  if (promoteState === 'dev-deploy-failed') return 'dev-deploy-failed';
  return promotionReadiness(repo) ? 'awaiting-approval' : null;
}

const IMAGE_DIGEST_RE = /^sha256:[0-9a-f]{64}$/;

const DIGEST_HEX_SHOWN = 7;

export function shortDigest(digest: string | null | undefined): string | null {
  const value = text(digest);
  if (value === null) return null;
  if (!IMAGE_DIGEST_RE.test(value)) return value;
  return `sha256:${value.slice('sha256:'.length, 'sha256:'.length + DIGEST_HEX_SHOWN)}…`;
}

export const PROMOTION_TAG_ONLY_LABEL = 'Tag-only — mutable';
export const PROMOTION_TAG_ONLY_NOTE =
  'This candidate records no image digest, so approving it approves a mutable tag rather than exact bytes: what the tag points at can change between this approval and the deployment. The repository’s build workflow predates digest recording — updating it from the template makes later candidates digest-pinned.';

const COMMIT_SHA_RE = /^[0-9a-f]{40}$/;

export function isCommitShaTag(tag: string | null | undefined): boolean {
  return COMMIT_SHA_RE.test(text(tag) ?? '');
}

export function shortCommitSha(sha: string): string {
  return `commit ${sha.slice(0, 7)}`;
}
