import { describe, expect, expectTypeOf, it } from 'vitest';

import {
  DELETED_REPO_MARKER,
  DEPLOYMENT_RETENTION_NOTE,
  ROLLBACK_BLOCKED_NOTE,
  ROLLBACK_REFUSAL_CODES,
  attemptRepoRef,
  classifyRollbackFailure,
  imageColumnLabel,
  isCurrentAttempt,
  mayRollback,
  rollbackActionMessage,
  rollbackEligibility,
  stageHistories,
  type RollbackEligibility,
  type RollbackRefusalKind,
} from './deploymentsTabModel';
import type { CicdStatus } from '../opsStatus';
import {
  AGENT_ZERO_NOTE,
  METRICS_STATE_COPY,
  PROJECT_SCOPE_NOTE,
  agentSlice,
  metricsWindow,
  metricsState,
} from './observabilityTabModel';
import {
  INVENTORY_STATE_COPY,
  RESOURCE_ITEMS,
  RESOURCE_LABEL,
  RESOURCE_STATE_BADGE_KEY,
  RESOURCE_STATE_LABEL,
  inventoryState,
  resourceInventory,
  resourceItemsForKind,
  toResourceState,
} from './resourcesTabModel';
import { offersRollback } from '../repositoryDetailTabs';
import { canDestroy, roleActionMessage, type ProjectRoleName } from '../projectRoles';
import type {
  AgentScopeRow,
  Deployment,
  MetricTotals,
  RepoDeletePreview,
  RepoDeletePreviewItem,
  ScopeMetrics,
} from '../../../api/openapi';
import { ApiFailure, type ErrorCode } from '../../../api/failure';
import { deployment } from '../../../test/wire';

import deploymentsPureRaw from './deploymentsTabModel.ts?raw';
import deploymentsTabRaw from './DeploymentsTab.tsx?raw';
import observabilityPureRaw from './observabilityTabModel.ts?raw';
import observabilityTabRaw from './ObservabilityTab.tsx?raw';
import resourcesPureRaw from './resourcesTabModel.ts?raw';
import resourcesTabRaw from './ResourcesTab.tsx?raw';
import ownRaw from './deploymentsTabModel.test.ts?raw';
import deleteModalRaw from '../DeleteRepositoryModal.tsx?raw';
import { stripComments } from '../../../rawSource';

const deploymentsPureSrc = stripComments(deploymentsPureRaw);
const deploymentsTabSrc = stripComments(deploymentsTabRaw);
const observabilityPureSrc = stripComments(observabilityPureRaw);
const observabilityTabSrc = stripComments(observabilityTabRaw);
const resourcesPureSrc = stripComments(resourcesPureRaw);
const resourcesTabSrc = stripComments(resourcesTabRaw);
const ownSrc = stripComments(ownRaw);
const deleteModalSrc = stripComments(deleteModalRaw);

const T12_SOURCES = [
  ['deploymentsTabModel.ts', deploymentsPureSrc],
  ['DeploymentsTab.tsx', deploymentsTabSrc],
  ['observabilityTabModel.ts', observabilityPureSrc],
  ['ObservabilityTab.tsx', observabilityTabSrc],
  ['resourcesTabModel.ts', resourcesPureSrc],
  ['ResourcesTab.tsx', resourcesTabSrc],
  ['deploymentsTabModel.test.ts', ownSrc],
] as const;

function metricTotals(overrides: Partial<MetricTotals> = {}): MetricTotals {
  return { cost_usd: 0, tokens: 0, traces: 0, ...overrides };
}

function agentScopeRow(overrides: Partial<AgentScopeRow> = {}): AgentScopeRow {
  return { agent_id: 'a-1', agent_name: 'One', tenant_id: null, totals: metricTotals(), ...overrides };
}

function scopeMetrics(overrides: Partial<ScopeMetrics> = {}): ScopeMetrics {
  return { by_agent: [], by_model: [], by_user: [], daily: [], totals: metricTotals(), ...overrides };
}

function repoDeletePreviewItem(overrides: Partial<RepoDeletePreviewItem> = {}): RepoDeletePreviewItem {
  return { item: 'record', note: null, state: 'present', ...overrides };
}

function repoDeletePreview(overrides: Partial<RepoDeletePreview> = {}): RepoDeletePreview {
  return { items: [], ...overrides };
}

describe('attemptRepoRef — a retained row does not silently borrow this repository’s identity', () => {
  it('reports OWN for a row belonging to the repository being viewed', () => {
    expect(attemptRepoRef('repo-1', deployment({ repo_id: 'repo-1' }))).toEqual({
      orphaned: false,
      marker: null,
    });
  });

  it('marks a row whose repository is NOT this one, with the pinned copy', () => {
    const ref = attemptRepoRef('repo-2', deployment({ repo_id: 'repo-1' }));
    expect(ref.orphaned).toBe(true);
    expect(ref.marker).toBe(DELETED_REPO_MARKER);
    expect(DELETED_REPO_MARKER).toBe('repo deleted');
  });

  it('says NOTHING when the row carries no repository id at all', () => {
    for (const missing of [null, undefined, '', '   ']) {
      const ref = attemptRepoRef('repo-1', deployment({ repo_id: missing as unknown as string }));
      expect(ref.orphaned, String(missing)).toBe(false);
      expect(ref.marker).toBeNull();
    }
  });

  it('says NOTHING when the page does not know which repository it is showing', () => {
    for (const unknown of [null, undefined, '']) {
      const ref = attemptRepoRef(unknown as unknown as string, deployment({ repo_id: 'repo-1' }));
      expect(ref.orphaned, String(unknown)).toBe(false);
      expect(ref.marker).toBeNull();
    }
  });

  it('is a MARKER, not a filter — the row is retained and still readable', () => {
    expect(attemptRepoRef('repo-2', deployment({ repo_id: 'repo-1' })).marker).not.toBeNull();
  });

  it('reads as a statement of FACT, not as a fault', () => {
    const said = DELETED_REPO_MARKER.toLowerCase();
    for (const alarm of ['error', 'invalid', 'broken', 'orphan', 'failed', 'unknown']) {
      expect(said, alarm).not.toContain(alarm);
    }
    expect(DELETED_REPO_MARKER.length).toBeLessThan(24);
  });
});

describe('DEPLOYMENT_RETENTION_NOTE — the delete confirm says what is NOT torn down', () => {
  it('opens with the pinned sentence', () => {
    expect(DEPLOYMENT_RETENTION_NOTE.startsWith('Deployment history is retained.')).toBe(true);
  });

  it('gives the REASON, so retention reads as a decision rather than an oversight', () => {
    expect(DEPLOYMENT_RETENTION_NOTE).toMatch(/outlives the repository/i);
    expect(DEPLOYMENT_RETENTION_NOTE).toMatch(/auditable/i);
  });

  it('promises exactly what the rows deliver — the marker, named', () => {
    expect(DEPLOYMENT_RETENTION_NOTE).toMatch(/marked/i);
    expect(DEPLOYMENT_RETENTION_NOTE).toMatch(/deleted repository/i);
  });

  it('does not dress retention as a warning', () => {
    const said = DEPLOYMENT_RETENTION_NOTE.toLowerCase();
    for (const alarm of ['warning', 'error', 'cannot', 'failed', 'careful']) {
      expect(said, alarm).not.toContain(alarm);
    }
  });
});

describe('stageHistories — one group per stage THE HISTORY carries', () => {
  it('groups by the stage each row reports, alphabetically', () => {
    const groups = stageHistories([
      deployment({ id: 'dep-1', stage: 'sandbox' }),
      deployment({ id: 'dep-2', stage: 'alpha' }),
      deployment({ id: 'dep-3', stage: 'uat' }),
    ]);
    expect(groups.map((g) => g.stage)).toEqual(['alpha', 'sandbox', 'uat']);
  });

  it('an empty history is zero groups — never a fabricated stage', () => {
    expect(stageHistories([])).toEqual([]);
  });

  it('reports a stage the TENANT no longer carries, unlike the environment strip', () => {
    const groups = stageHistories([deployment({ stage: 'retired' })]);
    expect(groups.map((g) => g.stage)).toEqual(['retired']);
  });

  it('marks ONE attempt as serving — never a failed one sharing its commit', () => {
    const groups = stageHistories([
      deployment({ id: 'dep-ok-old', started_at: '2026-07-01T10:00:00Z', image_tag: 'a-1-same' }),
      deployment({ id: 'dep-ok', started_at: '2026-07-02T10:00:00Z', image_tag: 'a-1-same' }),
      deployment({ id: 'dep-bad', started_at: '2026-07-03T10:00:00Z', image_tag: 'a-1-same', outcome: 'failed' }),
    ]);
    expect(groups[0].currentAttemptId).toBe('dep-ok');
    expect(stageHistories([deployment({ outcome: 'failed' })])[0].currentAttemptId).toBeNull();
  });

  it('after a rollback to A, only the rollback row serves and B is the rollback target', () => {
    const [group] = stageHistories([
      deployment({ id: 'dep-a', started_at: '2026-07-01T10:00:00Z', image_tag: 'a-1-aaa' }),
      deployment({ id: 'dep-b', started_at: '2026-07-02T10:00:00Z', image_tag: 'a-1-bbb' }),
      deployment({ id: 'dep-rb', started_at: '2026-07-03T10:00:00Z', image_tag: 'a-1-aaa' }),
    ]);
    expect(group.currentAttemptId).toBe('dep-rb');
    expect(group.currentTag).toBe('a-1-aaa');
    const target = (imageTag: string) =>
      rollbackEligibility({ ...OWNER_TARGET, imageTag, currentTag: group.currentTag });
    expect(target('a-1-bbb')).toBe('ok');
    expect(target('a-1-aaa')).toBe('current');
  });

  it('lists each stage’s attempts newest-first', () => {
    const groups = stageHistories([
      deployment({ id: 'dep-old', started_at: '2026-07-01T10:00:00Z', image_tag: 'a-1-old' }),
      deployment({ id: 'dep-new', started_at: '2026-07-31T10:00:00Z', image_tag: 'a-1-new' }),
    ]);
    expect(groups[0].attempts.map((a) => a.row.id)).toEqual(['dep-new', 'dep-old']);
  });

  it('COLLAPSES the append-only pair — a finished deploy is not still in flight', () => {
    const groups = stageHistories([
      deployment({
        id: 'dep-started', outcome: 'started', build_id: 'b:1', actor: 'jorge',
        actor_kind: 'github', source_sha: '3f9a1c2b4d', started_at: '2026-07-31T10:00:00Z',
      }),
      deployment({
        id: 'dep-done', outcome: 'succeeded', build_id: 'b:1', actor: null, actor_kind: null,
        source_sha: null, started_at: '2026-07-31T10:06:00Z',
      }),
    ]);
    expect(groups[0].attempts).toHaveLength(1);
    expect(groups[0].attempts[0].inFlight).toBe(false);
    expect(groups[0].attempts[0].outcome).toBe('succeeded');
    expect(groups[0].attempts[0].actor).toEqual({ kind: 'github', display: '@jorge' });
    expect(groups[0].attempts[0].shortSha).toBe('3f9a1c2');
  });

  it('a genuinely running attempt IS reported as in flight', () => {
    const now = Date.parse('2026-07-31T10:10:00Z');
    const groups = stageHistories([deployment({ outcome: 'started', build_id: 'b:live' })], now);
    expect(groups[0].attempts[0].inFlight).toBe(true);
  });

  it('a started row past the 60-min window is NOT in flight (lost callback)', () => {
    const now = Date.parse('2026-07-31T11:00:00Z');
    const groups = stageHistories([deployment({ outcome: 'started', build_id: 'b:lost' })], now);
    expect(groups[0].attempts[0].inFlight).toBe(false);
  });

  it('names what each stage is CURRENTLY serving — its newest SUCCEEDED attempt', () => {
    const groups = stageHistories([
      deployment({ id: 'dep-broken', outcome: 'failed', image_tag: 'a-1-broken', started_at: '2026-07-31T10:00:00Z' }),
      deployment({ id: 'dep-good', outcome: 'succeeded', image_tag: 'a-1-good', started_at: '2026-07-30T10:00:00Z' }),
    ]);
    expect(groups[0].currentTag).toBe('a-1-good');
  });

  it('a stage whose only attempts failed is serving nothing — null, not the failed tag', () => {
    const groups = stageHistories([deployment({ outcome: 'failed', image_tag: 'a-1-broken' })]);
    expect(groups[0].currentTag).toBeNull();
  });
});

describe('imageColumnLabel', () => {
  it('reads "Image" for an ordinary container-tag history', () => {
    const groups = stageHistories([deployment({ image_tag: 'a-1-tree000' })]);
    expect(imageColumnLabel(groups[0].attempts)).toBe('Image');
  });

  it('reads "Revision" when the newest attempt holds a bare commit sha', () => {
    const sha = '0123456789abcdef0123456789abcdef01234567';
    const groups = stageHistories([deployment({ image_tag: sha })]);
    expect(imageColumnLabel(groups[0].attempts)).toBe('Revision');
  });

  it('decides from the NEWEST attempt, not a vote across history', () => {
    const sha = '0123456789abcdef0123456789abcdef01234567';
    const groups = stageHistories([
      deployment({ id: 'dep-old', image_tag: 'a-1-old', started_at: '2026-07-01T10:00:00Z' }),
      deployment({ id: 'dep-new', image_tag: sha, started_at: '2026-07-31T10:00:00Z' }),
    ]);
    expect(groups[0].attempts[0].row.id).toBe('dep-new');
    expect(imageColumnLabel(groups[0].attempts)).toBe('Revision');
  });

  it('falls back to "Image" with no attempts — nothing to judge', () => {
    expect(imageColumnLabel([])).toBe('Image');
  });
});

describe('mayRollback — OWNER-or-admin, on the STRICT gate', () => {
  it('offers rollback to an owner and to a platform admin', () => {
    expect(mayRollback('owner', 0)).toBe(true);
    expect(mayRollback(null, 2)).toBe(true);
  });

  it('does NOT offer it to a maintainer or a viewer', () => {
    expect(mayRollback('maintainer', 0)).toBe(false);
    expect(mayRollback('viewer', 0)).toBe(false);
    expect(mayRollback(null, 0)).toBe(false);
  });

  it('is the SAME predicate `canDestroy` already is — never a fourth threshold', () => {
    for (const held of [null, 'viewer', 'maintainer', 'owner'] as (ProjectRoleName | null)[]) {
      for (const level of [0, 1, 2, 3]) {
        expect(mayRollback(held, level), `${held}/${level}`).toBe(canDestroy(held, level));
      }
    }
  });
});

const OWNER_TARGET: Parameters<typeof rollbackEligibility>[0] = {
  held: 'owner' as ProjectRoleName | null,
  roleLevel: 0,
  cicdStatus: 'deployed',
  outcome: 'succeeded' as Deployment['outcome'],
  imageTag: 'a-1-older',
  currentTag: 'a-1-current',
};

describe('rollbackEligibility — who may be OFFERED a rollback, and to what', () => {
  it('offers it to an owner for an older succeeded attempt', () => {
    expect(rollbackEligibility(OWNER_TARGET)).toBe('ok');
  });

  it('HIDES rollback from a non-OWNER — nothing rendered, no privilege hint', () => {
    for (const held of [null, 'viewer', 'maintainer'] as (ProjectRoleName | null)[]) {
      expect(rollbackEligibility({ ...OWNER_TARGET, held }), String(held)).toBe('not-owner');
    }
    expect(ROLLBACK_BLOCKED_NOTE['not-owner']).toBeNull();
  });

  it('the role refusal OUTRANKS every other reason, so no refusal leaks a privilege hint', () => {
    const viewer = { ...OWNER_TARGET, held: 'viewer' as const };
    expect(rollbackEligibility({ ...viewer, cicdStatus: 'promoting' })).toBe('not-owner');
    expect(rollbackEligibility({ ...viewer, outcome: 'failed' })).toBe('not-owner');
    expect(rollbackEligibility({ ...viewer, imageTag: 'a-1-current' })).toBe('not-owner');
  });

  it('refuses a NON-SUCCEEDED attempt as a target', () => {
    for (const outcome of ['started', 'failed'] as Deployment['outcome'][]) {
      expect(rollbackEligibility({ ...OWNER_TARGET, outcome }), outcome).toBe('not-a-target');
    }
    expect(ROLLBACK_BLOCKED_NOTE['not-a-target']).toBeNull();
  });

  it('refuses an attempt with no image tag — a rollback with no target must never be sent', () => {
    for (const imageTag of [null, '', '   ']) {
      expect(rollbackEligibility({ ...OWNER_TARGET, imageTag }), JSON.stringify(imageTag)).toBe(
        'not-a-target',
      );
    }
  });

  it('does not offer a rollback to what the stage is ALREADY serving', () => {
    expect(rollbackEligibility({ ...OWNER_TARGET, imageTag: 'a-1-current' })).toBe('current');
    expect(ROLLBACK_BLOCKED_NOTE.current).toBeNull();
  });

  it('suppresses rollback while a delivery is IN FLIGHT, and says so', () => {
    const inFlight: CicdStatus[] = ['provisioning', 'promoting'];
    for (const status of inFlight) {
      expect(rollbackEligibility({ ...OWNER_TARGET, cicdStatus: status }), status).toBe('in-flight');
    }
    expect(ROLLBACK_BLOCKED_NOTE['in-flight']).toBeTruthy();
  });

  it('is NOT suppressed by a resting or unknown delivery status', () => {
    const resting: CicdStatus[] = ['failed', 'ready', 'deployed', 'unknown'];
    for (const status of resting) {
      expect(rollbackEligibility({ ...OWNER_TARGET, cicdStatus: status }), status).toBe('ok');
    }
  });

  it('every eligibility has an entry in the note table — no default branch', () => {
    const keys: RollbackEligibility[] = ['ok', 'not-owner', 'not-a-target', 'current', 'in-flight'];
    for (const k of keys) expect(k in ROLLBACK_BLOCKED_NOTE, k).toBe(true);
    expect(Object.keys(ROLLBACK_BLOCKED_NOTE).sort()).toEqual([...keys].sort());
  });

  it('never gates on the project’s `ungoverned` bit', () => {
    expect(deploymentsPureSrc).not.toContain('ungoverned');
  });
});

describe('isCurrentAttempt — a FACT about the stage, rendered for everyone', () => {
  it('is true only for the tag the stage is serving', () => {
    expect(isCurrentAttempt('a-1-x', 'a-1-x')).toBe(true);
    expect(isCurrentAttempt('a-1-x', 'a-1-y')).toBe(false);
  });

  it('is false when either side is absent — never "current" by default', () => {
    expect(isCurrentAttempt(null, 'a-1-x')).toBe(false);
    expect(isCurrentAttempt('a-1-x', null)).toBe(false);
    expect(isCurrentAttempt(null, null)).toBe(false);
    expect(isCurrentAttempt('', '')).toBe(false);
  });

  it('is independent of the ROLE gate, so a viewer still learns what is live', () => {
    expect(isCurrentAttempt('a-1-x', 'a-1-x')).toBe(true);
    expect(rollbackEligibility({ ...OWNER_TARGET, held: 'viewer', imageTag: 'a-1-current' })).toBe(
      'not-owner',
    );
  });
});

function apiFailure({ status, code, detail = '', message = detail }: { status: number; code: ErrorCode | null; detail?: string; message?: string }): ApiFailure {
  return new ApiFailure(status, code, detail, message);
}

const ROLLBACK_REFUSAL: Record<RollbackRefusalKind, ApiFailure> = {
  insufficient: apiFailure({ status: 403, code: 'project_role.insufficient', detail: 'insufficient project role' }),
  unknown_rollback_target: apiFailure({
    status: 409,
    code: 'project.unknown_rollback_target',
    detail: 'no such succeeded deployment to roll back to',
  }),
  promote_in_flight: apiFailure({ status: 409, code: 'project.promote_in_flight', detail: 'a promotion is already in flight' }),
  rollback_failed: apiFailure({ status: 502, code: 'project.rollback_failed', detail: 'failed to start the rollback build' }),
  repo_not_found: apiFailure({ status: 404, code: 'project.repo_not_found', detail: 'Repository not found' }),
};

describe('ROLLBACK_REFUSAL_CODES — typed on the generated ErrorCode', () => {
  it('satisfies Record<RollbackRefusalKind, readonly ErrorCode[]>, so a renamed backend code fails tsc where this table names it', () => {
    expectTypeOf(ROLLBACK_REFUSAL_CODES).toExtend<Record<RollbackRefusalKind, readonly ErrorCode[]>>();
  });

  it('names at least one code per kind and no code under two kinds', () => {
    const all = Object.values(ROLLBACK_REFUSAL_CODES).flat();
    for (const codes of Object.values(ROLLBACK_REFUSAL_CODES)) expect(codes.length).toBeGreaterThan(0);
    expect(new Set(all).size).toBe(all.length);
  });

  it('classifies by code alone: the detail under no code is unrecognised, a reworded detail under the code is not', () => {
    expect(
      classifyRollbackFailure(apiFailure({ status: 409, code: null, detail: 'no such succeeded deployment to roll back to' })),
    ).toBeNull();
    expect(classifyRollbackFailure(apiFailure({ status: 409, code: 'project.unknown_rollback_target', detail: 'reworded' }))).toBe(
      'unknown_rollback_target',
    );
  });
});

describe('rollbackActionMessage — the route’s refusals become sentences', () => {
  it('states a REJECTED TAG as an ordinary state, not a server fault', () => {
    const msg = rollbackActionMessage(ROLLBACK_REFUSAL.unknown_rollback_target);
    expect(msg).not.toMatch(/error|fault|failed to/i);
    expect(msg.length).toBeGreaterThan(30);
    expect(msg).not.toMatch(/a-1-/);
  });

  it('distinguishes the in-flight refusal from the build-never-started one', () => {
    const inflight = rollbackActionMessage(ROLLBACK_REFUSAL.promote_in_flight);
    const nostart = rollbackActionMessage(ROLLBACK_REFUSAL.rollback_failed);
    expect(inflight).not.toBe(nostart);
    expect(inflight).toMatch(/wait/i);
    expect(nostart).toMatch(/nothing/i);
  });

  it('names OWNER on the 403 and does not offer the ungoverned excuse', () => {
    const msg = rollbackActionMessage(ROLLBACK_REFUSAL.insufficient);
    expect(msg).toMatch(/owner/i);
    expect(msg).not.toMatch(/maintainer/i);
  });

  it('maps a vanished repository', () => {
    expect(rollbackActionMessage(ROLLBACK_REFUSAL.repo_not_found)).toMatch(/no longer/i);
  });

  it('delegates a role refusal it does not name itself to the one role table', () => {
    const unverified = apiFailure({
      status: 503,
      code: 'project_role.ownership_unverified',
      detail: 'could not verify project ownership',
    });
    expect(rollbackActionMessage(unverified)).toBe(roleActionMessage(unverified));
    expect(rollbackActionMessage(unverified)).not.toBe(unverified.message);
  });

  it('falls back on an empty message and passes an unrecognised one through as sent', () => {
    expect(rollbackActionMessage(new Error(''), 'fallback copy')).toBe('fallback copy');
    expect(rollbackActionMessage(new Error('   '), 'fallback copy')).toBe('fallback copy');
    expect(rollbackActionMessage(null, 'fallback copy')).toBe('fallback copy');
    expect(rollbackActionMessage(new Error('some other curated detail'))).toBe('some other curated detail');
    expect(
      rollbackActionMessage(apiFailure({ status: 409, code: null, detail: 'a promotion is already in flight' })),
    ).toBe('a promotion is already in flight');
  });

  it('gives every mapped refusal its OWN sentence — none collapsed into a neighbour, none the raw detail', () => {
    const failures = Object.values(ROLLBACK_REFUSAL);
    const sentences = failures.map((f) => rollbackActionMessage(f));
    expect(new Set(sentences).size).toBe(failures.length);
    for (const [i, sentence] of sentences.entries()) expect(sentence, failures[i].message).not.toBe(failures[i].message);
  });
});

const ZERO_TOTALS = metricTotals();
const zeroMetrics = scopeMetrics();

describe('metricsState — not instrumented, unreadable and genuinely zero are THREE states', () => {
  it('reports NOT INSTRUMENTED when Langfuse is not wired into the environment', () => {
    expect(
      metricsState({ loading: false, configured: false, error: false, metrics: null }),
    ).toBe('not-instrumented');
  });

  it('reports DATA for a successful read that genuinely returned zero', () => {
    expect(
      metricsState({ loading: false, configured: true, error: false, metrics: zeroMetrics }),
    ).toBe('data');
  });

  it('a genuine ZERO and NOT INSTRUMENTED are two named states, not merely different ones', () => {
    const notInstrumented = metricsState({
      loading: false, configured: false, error: false, metrics: zeroMetrics,
    });
    const genuineZero = metricsState({
      loading: false, configured: true, error: false, metrics: zeroMetrics,
    });
    expect(notInstrumented).toBe('not-instrumented');
    expect(genuineZero).toBe('data');
    expect(notInstrumented).not.toBe(genuineZero);
    expect(METRICS_STATE_COPY[notInstrumented]).toBeTruthy();
    expect(METRICS_STATE_COPY[genuineZero]).toBeNull();
    expect(METRICS_STATE_COPY['not-instrumented']?.headline).not.toMatch(/no (traces|activity|data)/i);
  });

  it('a FAILED read is UNREAD — not a zero either', () => {
    expect(
      metricsState({ loading: false, configured: true, error: true, metrics: zeroMetrics }),
    ).toBe('unread');
    expect(METRICS_STATE_COPY.unread).toBeTruthy();
    expect(METRICS_STATE_COPY.unread).not.toBe(METRICS_STATE_COPY['not-instrumented']);
  });

  it('an UNESTABLISHED `configured` probe is UNREAD, never "not instrumented"', () => {
    for (const configured of [null, undefined]) {
      expect(
        metricsState({ loading: false, configured, error: false, metrics: zeroMetrics }),
        String(configured),
      ).toBe('unread');
    }
  });

  it('a successful read that returned NOTHING AT ALL is unread, not zero', () => {
    expect(
      metricsState({ loading: false, configured: true, error: false, metrics: null }),
    ).toBe('unread');
  });

  it('LOADING outranks every other state — nothing is claimed before the read lands', () => {
    for (const configured of [true, false, null]) {
      expect(
        metricsState({ loading: true, configured, error: true, metrics: null }),
        String(configured),
      ).toBe('loading');
    }
    expect(METRICS_STATE_COPY.loading).toBeTruthy();
  });

  it('every state has an entry in the copy table — no default branch', () => {
    expect(Object.keys(METRICS_STATE_COPY).sort()).toEqual([
      'data', 'loading', 'not-instrumented', 'unread',
    ]);
  });
});

describe('agentSlice — this repository’s agent, pulled out of PROJECT-wide figures', () => {
  it('finds the agent’s own totals in `by_agent`', () => {
    const slice = agentSlice(
      scopeMetrics({
        by_agent: [
          agentScopeRow({ agent_id: 'a-1', totals: metricTotals({ traces: 7, cost_usd: 1.5, tokens: 90 }) }),
          agentScopeRow({ agent_id: 'a-2', agent_name: 'Two' }),
        ],
      }),
      'a-1',
    );
    expect(slice.kind).toBe('present');
    expect(slice.totals).toEqual({ traces: 7, cost_usd: 1.5, tokens: 90 });
  });

  it('an agent MISSING from the breakdown is ABSENT, never zero', () => {
    const slice = agentSlice(scopeMetrics({ by_agent: [] }), 'a-1');
    expect(slice.kind).toBe('absent');
    expect(slice.totals).toBeNull();
  });

  it('an unread project payload is absent, not zero', () => {
    expect(agentSlice(null, 'a-1')).toEqual({ kind: 'absent', totals: null });
  });

  it('a present agent reporting real zeroes IS zero — the distinction cuts both ways', () => {
    const slice = agentSlice(scopeMetrics({ by_agent: [agentScopeRow({ agent_id: 'a-1', totals: ZERO_TOTALS })] }), 'a-1');
    expect(slice.kind).toBe('present');
    expect(slice.totals).toEqual(ZERO_TOTALS);
  });
});

describe('PROJECT_SCOPE_NOTE — project-wide figures are never labelled as this repository’s', () => {
  it('says out loud that the scope is the project', () => {
    expect(PROJECT_SCOPE_NOTE).toMatch(/project/i);
    expect(PROJECT_SCOPE_NOTE).toMatch(/not|whole|every/i);
  });
});

describe('AGENT_ZERO_NOTE — a zero on THIS repository’s card is not evidence of idleness', () => {
  it('says a zero came from the project read and does not establish idleness', () => {
    expect(AGENT_ZERO_NOTE.length).toBeGreaterThan(80);
    expect(AGENT_ZERO_NOTE).toMatch(/zero/i);
    expect(AGENT_ZERO_NOTE).toMatch(/never (been )?(set up|provisioned|configured|instrumented)/i);
    expect(AGENT_ZERO_NOTE).not.toMatch(/\b(is|has been|was) idle\b/i);
    expect(AGENT_ZERO_NOTE).not.toMatch(/nothing (has )?happened/i);
  });

  it('is rendered on the repo card, beside the per-agent tiles', () => {
    expect(observabilityTabSrc).toContain('AGENT_ZERO_NOTE');
    const card = observabilityTabSrc.indexOf('This repository’s agent');
    const totals = observabilityTabSrc.indexOf('Project totals');
    expect(card).toBeGreaterThan(-1);
    expect(totals).toBeGreaterThan(card);
    const note = observabilityTabSrc.lastIndexOf('AGENT_ZERO_NOTE');
    expect(note).toBeGreaterThan(card);
    expect(note).toBeLessThan(totals);
  });
});

describe('metricsWindow — the wire date range', () => {
  it('is an inclusive N-day window ending today, in the API’s YYYY-MM-DD UTC form', () => {
    const w = metricsWindow(new Date('2026-07-31T09:00:00Z'), 30);
    expect(w.dateTo).toBe('2026-07-31');
    expect(w.dateFrom).toBe('2026-07-02');
  });

  it('a one-day window is a single day, not an empty range', () => {
    const w = metricsWindow(new Date('2026-07-31T09:00:00Z'), 1);
    expect(w.dateFrom).toBe('2026-07-31');
    expect(w.dateTo).toBe('2026-07-31');
  });

  it('crosses a month and a year boundary correctly', () => {
    expect(metricsWindow(new Date('2026-01-02T00:00:00Z'), 7).dateFrom).toBe('2025-12-27');
  });
});

describe('resourceInventory — the five teardown artifacts, as a read-only inventory', () => {
  it('always renders the five contract artifacts, in a fixed order', () => {
    expect(RESOURCE_ITEMS).toEqual(['record', 'github', 'image', 'runtime', 'identity']);
    const rows = resourceInventory(repoDeletePreview());
    expect(rows.map((r) => r.key)).toEqual([...RESOURCE_ITEMS]);
  });

  it('an artifact the preview did NOT report is UNKNOWN, never gone', () => {
    const rows = resourceInventory(
      repoDeletePreview({ items: [repoDeletePreviewItem({ item: 'github', state: 'present' })] }),
    );
    const byKey = Object.fromEntries(rows.map((r) => [r.key, r.state]));
    expect(byKey.github).toBe('present');
    for (const k of ['record', 'image', 'runtime', 'identity']) {
      expect(byKey[k], k).toBe('unknown');
    }
  });

  it('an unread preview is five UNKNOWN rows, not five gone ones', () => {
    const rows = resourceInventory(null);
    expect(rows).toHaveLength(5);
    for (const r of rows) expect(r.state, r.key).toBe('unknown');
  });

  it('round-trips the three states the probe reports', () => {
    const rows = resourceInventory(
      repoDeletePreview({
        items: [
          repoDeletePreviewItem({ item: 'record', state: 'present' }),
          repoDeletePreviewItem({ item: 'github', state: 'gone' }),
          repoDeletePreviewItem({ item: 'image', state: 'unknown' }),
        ],
      }),
    );
    const byKey = Object.fromEntries(rows.map((r) => [r.key, r.state]));
    expect([byKey.record, byKey.github, byKey.image]).toEqual(['present', 'gone', 'unknown']);
  });

  it('ignores an artifact the contract does not name', () => {
    const rows = resourceInventory(
      repoDeletePreview({ items: [repoDeletePreviewItem({ item: 'something-new', state: 'present' })] }),
    );
    expect(rows.map((r) => r.key)).toEqual([...RESOURCE_ITEMS]);
  });
});

describe('toResourceState — total, with UNKNOWN for anything unrecognised', () => {
  it('narrows the three contract values, case- and whitespace-tolerantly', () => {
    expect(toResourceState('present')).toBe('present');
    expect(toResourceState(' GONE ')).toBe('gone');
    expect(toResourceState('unknown')).toBe('unknown');
  });

  it('anything unrecognised is UNKNOWN — never `gone`, and never `present`', () => {
    for (const raw of [null, undefined, '', 'deleted', 'missing', 'ok']) {
      expect(toResourceState(raw), String(raw)).toBe('unknown');
    }
  });

  it('every state has a label and a tint — no default branch', () => {
    for (const s of ['present', 'gone', 'unknown'] as const) {
      expect(RESOURCE_STATE_LABEL[s], s).toBeTruthy();
      expect(RESOURCE_STATE_BADGE_KEY[s], s).toBeTruthy();
    }
    expect(RESOURCE_STATE_BADGE_KEY.unknown).toBe('unknown');
    expect(RESOURCE_STATE_BADGE_KEY.gone).not.toBe('failed');
    expect(RESOURCE_STATE_BADGE_KEY.present).toBe('ready');
    const labels = (['present', 'gone', 'unknown'] as const).map((s) => RESOURCE_STATE_LABEL[s]);
    expect(new Set(labels).size).toBe(3);
  });

  it('every artifact has a label — and none of them is the raw key', () => {
    for (const k of RESOURCE_ITEMS) {
      expect(RESOURCE_LABEL[k], k).toBeTruthy();
      expect(RESOURCE_LABEL[k], k).not.toBe(k);
    }
  });
});

describe('inventoryState — the panel’s own three-state read', () => {
  const forbidden = apiFailure({ status: 403, code: 'project_role.insufficient', detail: 'insufficient project role' });

  it('reports FORBIDDEN for the OWNER-gated route’s project_role.insufficient rather than an empty inventory', () => {
    expect(inventoryState({ loading: false, failure: forbidden, preview: null })).toBe('forbidden');
    expect(INVENTORY_STATE_COPY.forbidden).toBeTruthy();
  });

  it('reports UNREAD for any other failure, by code not by sentence', () => {
    expect(
      inventoryState({
        loading: false,
        failure: apiFailure({ status: 500, code: null, detail: 'Failed to preview the repository delete' }),
        preview: null,
      }),
    ).toBe('unread');
    expect(
      inventoryState({
        loading: false,
        failure: apiFailure({ status: 403, code: null, detail: 'insufficient project role' }),
        preview: null,
      }),
    ).toBe('unread');
    expect(inventoryState({ loading: false, failure: null, preview: null })).toBe('unread');
    expect(INVENTORY_STATE_COPY.unread).not.toBe(INVENTORY_STATE_COPY.forbidden);
  });

  it('reports DATA when the probe answered', () => {
    expect(inventoryState({ loading: false, failure: null, preview: repoDeletePreview() })).toBe('data');
    expect(INVENTORY_STATE_COPY.data).toBeNull();
  });

  it('LOADING outranks everything', () => {
    expect(inventoryState({ loading: true, failure: forbidden, preview: repoDeletePreview() })).toBe('loading');
  });

  it('every state has a copy entry — no default branch', () => {
    expect(Object.keys(INVENTORY_STATE_COPY).sort()).toEqual([
      'data', 'forbidden', 'loading', 'unread',
    ]);
  });
});

describe('the .tsx surfaces obey the rules a .ts cannot enforce for them', () => {
  it('parses all six sources (guards every assertion below against an empty read)', () => {
    expect(deploymentsPureSrc.length).toBeGreaterThan(500);
    expect(deploymentsTabSrc.length).toBeGreaterThan(500);
    expect(observabilityPureSrc.length).toBeGreaterThan(500);
    expect(observabilityTabSrc.length).toBeGreaterThan(500);
    expect(resourcesPureSrc.length).toBeGreaterThan(500);
    expect(resourcesTabSrc.length).toBeGreaterThan(500);
  });

  it('IMPORTS `collapseByBuild` and does not re-derive it', () => {
    expect(deploymentsPureSrc).toContain('collapseByBuild');
    expect(deploymentsPureSrc).toMatch(/from '\.\.\/repositoryDetailTabs'/);
    expect(deploymentsPureSrc).not.toContain('build_id');
    expect(deploymentsPureSrc).not.toContain("'started'");
    expect(deploymentsTabSrc).toContain('DeploymentAttempt');
    expect(deploymentsTabSrc).not.toContain('build_id');
  });

  it('renders the actor CURRENCY through the shared table, never a re-derived ternary', () => {
    expect(deploymentsTabSrc).toContain('actorTitle(attempt.actor)');
    for (const literal of ['GitHub login', 'Entra object id']) {
      expect(deploymentsTabSrc, literal).not.toContain(literal);
    }
    expect(deploymentsTabSrc).toContain("ACTOR_KIND_CLASS: Record<DeploymentActor['kind'], string>");
    expect(deploymentsTabSrc).toContain('ACTOR_KIND_CLASS[');
    expect(deploymentsTabSrc).not.toMatch(/\.kind === '\w+' \?/);
  });

  it('marks a retained row through the MODEL, and does not re-decide whose it is', () => {
    expect(deploymentsTabSrc).toContain('attemptRepoRef(');
    expect(deploymentsTabSrc).toContain('repoRef.marker');
    expect(deploymentsTabSrc).not.toMatch(/'repo deleted'|"repo deleted"/);
    expect(deploymentsTabSrc).not.toMatch(/repo\.id\s*!==\s*row\.repo_id/);
    expect(deploymentsTabSrc).not.toMatch(/row\.repo_id\s*!==/);
  });

  it('MARKS the retained row rather than filtering it out', () => {
    expect(deploymentsTabSrc).not.toMatch(/\.filter\([^)]*orphan/i);
    expect(deploymentsTabSrc).not.toMatch(/!\s*repoRef\.orphaned\s*&&/);
    expect(deploymentsTabSrc).toContain('{shortBuildTag(row.image_tag)}');
  });

  it('labels the actor "deployed by", never "promoted by"', () => {
    expect(deploymentsTabSrc).toMatch(/deployed by/i);
    expect(deploymentsTabSrc).not.toMatch(/promoted by/i);
  });

  it('does not derive a DURATION, because a build-written row sets started_at to the completion time', () => {
    for (const src of [deploymentsTabSrc, deploymentsPureSrc]) {
      expect(src).not.toMatch(/duration|elapsed|getTime\(\)\s*-/i);
    }
  });

  it('never captions anything with a runtime status (the runtime is per-AGENT, not per-stage)', () => {
    for (const src of [deploymentsTabSrc, resourcesTabSrc, observabilityTabSrc]) {
      expect(src).not.toContain('RUNTIME_LABEL');
      expect(src).not.toContain('RUNTIME_BADGE_KEY');
    }
  });

  it('uses the shared delivery LABELS rather than hand-written status strings', () => {
    expect(deploymentsPureSrc).toContain('isCicdInFlight');
    expect(deploymentsPureSrc).not.toContain('as CicdStatus');
    for (const src of [deploymentsTabSrc, deploymentsPureSrc]) {
      expect(src).not.toContain("'Delivery failed'");
      expect(src).not.toContain("'Runtime failed'");
    }
  });

  it('offers NO governance verb on any of the three tabs', () => {
    for (const [name, src] of [
      ['DeploymentsTab.tsx', deploymentsTabSrc],
      ['ResourcesTab.tsx', resourcesTabSrc],
      ['ObservabilityTab.tsx', observabilityTabSrc],
    ] as const) {
      for (const forbidden of [
        'agentsApi.transition', 'agentsApi.submit', 'grantsApi.add', 'grantsApi.remove',
        'projectRolesApi.grant', 'projectRolesApi.revoke', 'marketplaceApi',
      ]) {
        expect(src, `${name} / ${forbidden}`).not.toContain(forbidden);
      }
    }
  });

  it('the RESOURCES tab reads the preview and can never DELETE anything', () => {
    expect(resourcesTabSrc).toContain('projectsApi.deletePreview');
    expect(resourcesTabSrc).not.toContain('projectsApi.deleteRepo');
    expect(resourcesTabSrc).not.toContain('DeleteRepositoryModal');
  });

  it('writes no `?? 0` for absent data, even an unreachable one', () => {
    expect(observabilityTabSrc).not.toMatch(/\?\?\s*0\b/);
    expect(observabilityTabSrc).toContain('NO_VALUE');
  });

  it('the OBSERVABILITY tab owns its rendering and touches nothing under governance/', () => {
    expect(observabilityTabSrc).not.toContain('governance/');
    expect(observabilityPureSrc).not.toContain('governance/');
    expect(observabilityTabSrc).toContain('observabilityApi.getMetrics');
    expect(observabilityTabSrc).toContain('PROJECT_SCOPE_NOTE');
  });

  it('the ROLLBACK confirm is a real confirm, on the ONE route, with MAPPED errors', () => {
    expect(deploymentsTabSrc).toContain('projectsApi.rollbackRepo');
    expect(deploymentsTabSrc).toContain('rollbackActionMessage');
    expect(deploymentsTabSrc).toContain('rollbackEligibility');
    expect(deploymentsTabSrc).toContain("=== 'ok'");
    expect(deploymentsTabSrc).not.toContain('meetsRole');
    expect(deploymentsTabSrc).not.toContain('canDestroy');
  });

  it('renders the rollback FAILURE inside the confirm, where the full-viewport overlay cannot hide it', () => {
    const open = deploymentsTabSrc.indexOf('<ModalShell');
    const close = deploymentsTabSrc.indexOf('</ModalShell>');
    expect(open).toBeGreaterThan(-1);
    expect(close).toBeGreaterThan(open);
    const modalSpan = deploymentsTabSrc.slice(open, close);

    expect(modalSpan.length).toBeGreaterThan(800);
    expect(modalSpan).toContain('actionPending');
    expect(modalSpan).toContain('Roll back');

    const alert = deploymentsTabSrc.indexOf('role="alert"');
    expect(alert).toBeGreaterThan(-1);
    expect(alert).toBeGreaterThan(open);
    expect(alert).toBeLessThan(close);
    expect(modalSpan).toContain('{error && (');
    expect(deploymentsTabSrc.split('role="alert"').length - 1).toBe(1);
    expect(deploymentsTabSrc.split('{error && (').length - 1).toBe(1);
  });

  it('sends the attempt’s OWN stage and tag — never a hardcoded stage', () => {
    expect(deploymentsTabSrc).toMatch(/stage:\s*\w/);
    expect(deploymentsTabSrc).toMatch(/image_tag:\s*\w/);
  });

  it('every clickable element is keyboard-reachable', () => {
    const clickableNonInteractive = /<(tr|div|span|td|li)\b[^>]*\sonClick/;
    for (const [name, src] of [
      ['DeploymentsTab.tsx', deploymentsTabSrc],
      ['ResourcesTab.tsx', resourcesTabSrc],
      ['ObservabilityTab.tsx', observabilityTabSrc],
    ] as const) {
      expect(clickableNonInteractive.test(src), name).toBe(false);
    }
  });

  it('contains NO stage literal anywhere — quoted, member-access OR destructured', () => {
    const stageLiteral =
      /(['"`])(dev|prod)\1|[.[]\s*['"`]?(dev|prod)['"`]?(?![\w$])|[{,;(]\s*(dev|prod)\s*[:,}]/;
    for (const [name, src] of T12_SOURCES) {
      expect(stageLiteral.test(src), `${name} contains a stage literal`).toBe(false);
    }
  });

  it('contains no AWS account id (a hard project rule)', () => {
    const twelveDigits = /(?<!\d)\d{12}(?!\d)/;
    for (const [name, src] of T12_SOURCES) {
      expect(twelveDigits.test(src), `${name} contains a 12-digit literal`).toBe(false);
    }
  });

  it('keeps the five inventory keys in step with the teardown checklist’s', () => {
    expect(deleteModalSrc.length).toBeGreaterThan(500);
    expect(deleteModalSrc).toContain('RepoDeleteSelection');
    for (const k of RESOURCE_ITEMS) {
      const declared = new RegExp(`key:\\s*'${k}'`);
      expect(declared.test(deleteModalSrc), `${k} is not on the teardown checklist`).toBe(true);
    }
    expect(new Set(RESOURCE_ITEMS).size).toBe(RESOURCE_ITEMS.length);
    const checklistKeys = [...deleteModalSrc.matchAll(/key:\s*'([a-z_]+)'/g)].map((m) => m[1]);
    expect(new Set(checklistKeys)).toEqual(new Set(RESOURCE_ITEMS));
  });

  it('gives every RESULT line-item a human label, including the ride-along ones, and degrades an unknown key to the key itself', () => {
    for (const key of ['exec_role', 'langfuse']) {
      const labelled = new RegExp(`\\b${key}:\\s*'[^']{4,}'`);
      expect(labelled.test(deleteModalSrc), `${key} has no human label`).toBe(true);
    }
    expect(deleteModalSrc).toMatch(/labelByKey\[[^\]]+\]\s*\?\?/);
  });

  it('states the retention policy on the confirm, from the shared constant', () => {
    expect(deleteModalSrc).toContain('DEPLOYMENT_RETENTION_NOTE');
    expect(deleteModalSrc).not.toMatch(/Deployment history is retained\./);
    expect(deleteModalSrc).toMatch(/from '\.\/repo-tabs\/deploymentsTabModel'/);
  });
});

describe('MCP-server repos — resources and deployments per kind', () => {
  it('agent inventory is unchanged; MCP inventory drops the image and relabels', () => {
    expect(resourceItemsForKind()).toEqual(RESOURCE_ITEMS);
    expect(resourceInventory(null).map((r) => r.label)).toEqual(RESOURCE_ITEMS.map((k) => RESOURCE_LABEL[k]));
    const mcp = resourceInventory(
      repoDeletePreview({
        items: [
          repoDeletePreviewItem({ item: 'runtime', state: 'present' }),
          repoDeletePreviewItem({ item: 'image', state: 'gone' }),
        ],
      }),
      'mcp_server',
    );
    expect(mcp.map((r) => r.key)).toEqual(['record', 'github', 'runtime', 'identity']);
    const label = Object.fromEntries(mcp.map((r) => [r.key, r.label]));
    expect(label.runtime).toBe('AgentCore gateway (Terraform-managed)');
    expect(label.record).toMatch(/MCP registry entry/);
    expect(label.identity).toBe('Entra MCP app');
    expect(mcp.find((r) => r.key === 'runtime')?.state).toBe('present');
  });

  it('only agent repos offer a rollback', () => {
    expect(offersRollback({})).toBe(true);
    expect(offersRollback({ artifact_kind: 'agent' })).toBe(true);
    expect(offersRollback({ artifact_kind: 'mcp_server' })).toBe(false);
    expect(deploymentsTabSrc).toMatch(/offersRollback\(repo\)/);
  });
});
