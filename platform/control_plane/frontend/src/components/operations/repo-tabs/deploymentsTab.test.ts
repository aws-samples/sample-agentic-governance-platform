// deploymentsTab.test.ts — the pure companions behind the three repository-detail tabs T12
// builds: Deployments (+ rollback), Resources, and Observability (E28/T12, contracts C1/C2 + D13).
//
// ONE test file for three modules, deliberately: they are one task's judgements and the plan
// pinned exactly one test path for them. vitest collects only `src/**/*.test.ts`, so anything
// decided inside a `.tsx` is unreachable here — every branch below lives in a `.ts` companion and
// the `.tsx` files are wiring, checked by the source-as-text guards at the bottom
// (`repositoryDetailTabs.test.ts` established that idiom).
//
// The five judgements pinned here, and why each is load-bearing:
//
//   • THE COLLAPSE IS IMPORTED, NOT RE-DERIVED. `collapseByBuild` already encodes the rule that
//     nothing ever closes a `started` row. A second copy here is the drift this epic exists to
//     remove, so a guard asserts this module never even MENTIONS the join key.
//   • ROLLBACK IS OWNER-GATED ON THE STRICT GATE, and a role refusal renders NOTHING.
//   • A ROLLBACK TARGET MUST BE A SUCCEEDED ATTEMPT. `started` means "we asked" and `failed`
//     means "it did not run" — neither is evidence an image ever served traffic.
//   • "NOT INSTRUMENTED" ≠ ZERO ≠ UNREADABLE (D13). Three distinct states, never merged, and a
//     zero is only reported when a successful read actually returned zero.
//   • AN ARTIFACT THE PREVIEW DID NOT REPORT IS UNKNOWN, NEVER GONE. Absence of evidence on a
//     teardown inventory must not read as "already deleted".

import { describe, expect, it } from 'vitest';

import {
  DELETED_REPO_MARKER,
  DEPLOYMENT_RETENTION_NOTE,
  ROLLBACK_BLOCKED_NOTE,
  attemptRepoRef,
  isCurrentAttempt,
  mayRollback,
  rollbackActionMessage,
  rollbackEligibility,
  stageHistories,
  type RollbackEligibility,
} from './deploymentsTab';
import {
  AGENT_ZERO_NOTE,
  METRICS_STATE_COPY,
  PROJECT_SCOPE_NOTE,
  agentSlice,
  metricsWindow,
  metricsState,
} from './observabilityTab';
import {
  INVENTORY_STATE_COPY,
  RESOURCE_ITEMS,
  RESOURCE_LABEL,
  RESOURCE_STATE_BADGE_KEY,
  RESOURCE_STATE_LABEL,
  inventoryState,
  resourceInventory,
  toResourceState,
} from './resourcesTab';
import { canDestroy, type ProjectRoleName } from '../projectRoles';
import type { Deployment } from '../../../api/client';

// Vite's `?raw`, not `node:fs`: `tsconfig.app.json` declares only `vite/client`, which declares
// `*?raw`. The explicit extension is load-bearing on a case-insensitive filesystem, where a name
// differing from a sibling only in casing resolves to the sibling.
import deploymentsPureSrc from './deploymentsTab.ts?raw';
import deploymentsTabSrc from './DeploymentsTab.tsx?raw';
import observabilityPureSrc from './observabilityTab.ts?raw';
import observabilityTabSrc from './ObservabilityTab.tsx?raw';
import resourcesPureSrc from './resourcesTab.ts?raw';
import resourcesTabSrc from './ResourcesTab.tsx?raw';
import ownSrc from './deploymentsTab.test.ts?raw';
// The teardown checklist, read ONLY as text. It is another task's file and nothing here edits it;
// the key-parity guard below needs the real other side of the join, not a restatement of this
// module's own table.
import deleteModalSrc from '../DeleteRepositoryModal.tsx?raw';

// EVERY source this task owns, and this file itself. The two blanket guards below (no stage
// literal, no account id) iterate this list rather than restating their own subset, because an
// earlier form of each named four of the six modules — omitting the two pure companions where two
// of the three sets of DECISIONS actually live. Both were clean, so nothing was violated; but a
// guard that cannot see the file it is meant to guard cannot fail, and this epic's stated primary
// risk IS the unfailable guard. One list, so a seventh module joins both guards at once.
//
// This file is included deliberately: a rule that exempts the file enforcing it has a hole in it.
// `DeleteRepositoryModal.tsx` is NOT included — it is another task's file, held to another task's
// rules, and read here only for the key-parity join.
const T12_SOURCES = [
  ['deploymentsTab.ts', deploymentsPureSrc],
  ['DeploymentsTab.tsx', deploymentsTabSrc],
  ['observabilityTab.ts', observabilityPureSrc],
  ['ObservabilityTab.tsx', observabilityTabSrc],
  ['resourcesTab.ts', resourcesPureSrc],
  ['ResourcesTab.tsx', resourcesTabSrc],
  ['deploymentsTab.test.ts', ownSrc],
] as const;

// ---------------------------------------------------------------------------
// Fixtures. `deployment()` mirrors the one in `repositoryDetailTabs.test.ts` field-for-field so
// the two suites exercise the same shape; no stage literal is written anywhere (C5), and the
// account-id-shaped strings are avoided entirely.
// ---------------------------------------------------------------------------

function deployment(over: Partial<Deployment> = {}): Deployment {
  return {
    id: 'dep-11111111',
    repo_id: 'repo-1',
    agent_id: 'a-1',
    stage: 'uat',
    seq_key: 'repo-1#uat#2026-07-31T10:00:00Z#1111',
    image_tag: 'a-1-tree000',
    outcome: 'succeeded',
    started_at: '2026-07-31T10:00:00Z',
    ...over,
  };
}

// ---------------------------------------------------------------------------
// attemptRepoRef — WHOSE REPOSITORY IS THIS ROW'S? (E28C/T7, D-C5)
//
// THE FACT BEHIND IT, confirmed twice on live data: DELETING A REPOSITORY DOES NOT DELETE ITS
// DEPLOYMENT ROWS. The E23 cascade tears down five artifacts and the registry record; the
// `deployment` partition is APPEND-ONLY and is deliberately not among them (ten rows survived the
// 2026-08-04 cascade). Retention is the right decision — the history of what reached production
// outlives the repository, which is the entire point of an append-only audit partition — but until
// now it was SILENT retention, and that is the dishonesty D-C5 names.
//
// WHERE THE DANGLING REFERENCE ACTUALLY IS, because it is not a visible "repo-1" string anywhere.
// No surface renders `Deployment.repo_id`; the history is reached only through the repository detail
// page, which calls `/agents/{id}/deployments` and lets the route resolve the repo server-side. So
// every row that comes back is IMPLICITLY ATTRIBUTED to the repository being viewed — the page's
// title, its header and its whole frame say so. That attribution is the reference, and it is the one
// that can be wrong: an agent whose repository was deleted and later re-created carries rows from
// BOTH, and the surviving rows would silently read as this repository's delivery history.
//
// So the marker is per-ROW and compares the row's own `repo_id` against the repository the page is
// showing. Same shape as `prodServingState`'s build_id join: identify by the id the row carries,
// never by position or by trusting the caller's framing.
//
// NO STAGE LITERAL, and no join key named — this module may mention neither (see the file header),
// and `collapseByBuild` is untouched, so the E28A build_id-join tests are unaffected by name.
// ---------------------------------------------------------------------------

describe('attemptRepoRef — a retained row does not silently borrow this repository’s identity', () => {
  it('reports OWN for a row belonging to the repository being viewed', () => {
    expect(attemptRepoRef('repo-1', deployment({ repo_id: 'repo-1' }))).toEqual({
      orphaned: false,
      marker: null,
    });
  });

  it('marks a row whose repository is NOT this one, with the pinned copy', () => {
    // The rows that survived a delete cascade. They are real evidence of a real deployment and are
    // kept — but they are not this repository's, and rendering them unmarked under this page's title
    // is the implicit claim D-C5 removes.
    const ref = attemptRepoRef('repo-2', deployment({ repo_id: 'repo-1' }));
    expect(ref.orphaned).toBe(true);
    expect(ref.marker).toBe(DELETED_REPO_MARKER);
    expect(DELETED_REPO_MARKER).toBe('repo deleted');
  });

  it('says NOTHING when the row carries no repository id at all', () => {
    // Absence of the id is not evidence the repository was deleted — it is a partial record, and
    // "repo deleted" is a definite claim about a destructive act. The same rule the runtime probe
    // follows (absent ⇒ unknown, never a verdict) and the reason a null join key is un-collapsible.
    for (const missing of [null, undefined, '', '   ']) {
      const ref = attemptRepoRef('repo-1', deployment({ repo_id: missing as unknown as string }));
      expect(ref.orphaned, String(missing)).toBe(false);
      expect(ref.marker).toBeNull();
    }
  });

  it('says NOTHING when the page does not know which repository it is showing', () => {
    // Symmetric, and for the same reason: with no repository to compare against, EVERY row would be
    // marked deleted — the cry-wolf failure, on the most alarming possible claim.
    for (const unknown of [null, undefined, '']) {
      const ref = attemptRepoRef(unknown as unknown as string, deployment({ repo_id: 'repo-1' }));
      expect(ref.orphaned, String(unknown)).toBe(false);
      expect(ref.marker).toBeNull();
    }
  });

  it('is a MARKER, not a filter — the row is retained and still readable', () => {
    // Retention IS the decision (D-C5). Dropping an orphaned row would destroy the evidence the
    // append-only partition exists to keep, and would make a deleted repository's production history
    // disappear from the platform entirely. Marking it is the whole change.
    expect(attemptRepoRef('repo-2', deployment({ repo_id: 'repo-1' })).marker).not.toBeNull();
  });

  it('reads as a statement of FACT, not as a fault', () => {
    // A retained row is not an error state. The copy names what happened and nothing more — no
    // alarm word, and no imperative telling an operator to go fix something that is working.
    const said = DELETED_REPO_MARKER.toLowerCase();
    for (const alarm of ['error', 'invalid', 'broken', 'orphan', 'failed', 'unknown']) {
      expect(said, alarm).not.toContain(alarm);
    }
    // Short enough for the badge it sits in, beside an image tag.
    expect(DELETED_REPO_MARKER.length).toBeLessThan(24);
  });
});

// ---------------------------------------------------------------------------
// DEPLOYMENT_RETENTION_NOTE — the same policy, stated BEFORE the teardown (E28C/T7, D-C5).
//
// The delete confirm's half. The marker above is what an operator reads AFTERWARDS, on the rows this
// sentence promised would survive; both come from this module so the promise and the evidence cannot
// be worded into disagreement. The delete modal imports it — a guard below pins that it does not
// spell out its own version.
// ---------------------------------------------------------------------------

describe('DEPLOYMENT_RETENTION_NOTE — the delete confirm says what is NOT torn down', () => {
  it('opens with the pinned sentence', () => {
    // Pinned verbatim: this is the line D-C5 specifies, and a reworded version on a destructive
    // confirm is the kind of drift the epic's copy findings were all instances of.
    expect(DEPLOYMENT_RETENTION_NOTE.startsWith('Deployment history is retained.')).toBe(true);
  });

  it('gives the REASON, so retention reads as a decision rather than an oversight', () => {
    // On a screen whose whole subject is removing things, "history is kept" alone invites the
    // question "why?" — and an unexplained exception to a teardown reads as something forgotten.
    expect(DEPLOYMENT_RETENTION_NOTE).toMatch(/outlives the repository/i);
    expect(DEPLOYMENT_RETENTION_NOTE).toMatch(/auditable/i);
  });

  it('promises exactly what the rows deliver — the marker, named', () => {
    // The join between the two halves. The confirm says retained rows are MARKED as belonging to a
    // deleted repository, and `attemptRepoRef` is what marks them; a confirm promising a treatment
    // the rows do not apply would be a new dishonesty in place of the old one.
    expect(DEPLOYMENT_RETENTION_NOTE).toMatch(/marked/i);
    expect(DEPLOYMENT_RETENTION_NOTE).toMatch(/deleted repository/i);
  });

  it('does not dress retention as a warning', () => {
    // It is not a caution and not a fault: nothing is wrong, and an alarm here would tell an
    // operator to reconsider a delete over a partition behaving exactly as designed.
    const said = DEPLOYMENT_RETENTION_NOTE.toLowerCase();
    for (const alarm of ['warning', 'error', 'cannot', 'failed', 'careful']) {
      expect(said, alarm).not.toContain(alarm);
    }
  });
});

// ---------------------------------------------------------------------------
// stageHistories — the history, grouped per stage, over COLLAPSED attempts
// ---------------------------------------------------------------------------

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
    // The two surfaces answer different questions and so read different authorities. The strip
    // asks "what is each CONFIGURED environment running?", so the tenant record is its authority
    // and a retired stage must not resurrect a row. This asks "what has this repository shipped?",
    // which is a question about HISTORY — and dropping a retired stage's deployments would delete
    // evidence that a real release happened. Deliberate divergence, not an oversight.
    const groups = stageHistories([deployment({ stage: 'retired' })]);
    expect(groups.map((g) => g.stage)).toEqual(['retired']);
  });

  it('lists each stage’s attempts newest-first', () => {
    const groups = stageHistories([
      deployment({ id: 'dep-old', started_at: '2026-07-01T10:00:00Z', image_tag: 'a-1-old' }),
      deployment({ id: 'dep-new', started_at: '2026-07-31T10:00:00Z', image_tag: 'a-1-new' }),
    ]);
    expect(groups[0].attempts.map((a) => a.row.id)).toEqual(['dep-new', 'dep-old']);
  });

  it('COLLAPSES the append-only pair — a finished deploy is not still in flight', () => {
    // THE BUG THIS TAB WOULD OTHERWISE RE-CREATE. Nothing ever closes a `started` row: AGP writes
    // one when the build is requested and the buildspec writes the terminal row SEPARATELY, so
    // `some(outcome === 'started')` is permanently true and every historical deployment renders
    // as perpetually running. `collapseByBuild` is IMPORTED for exactly this.
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
    // …and the actor/sha come off the STARTED row, which is the only row that carries them.
    expect(groups[0].attempts[0].actor).toEqual({ kind: 'github', display: '@jorge' });
    expect(groups[0].attempts[0].shortSha).toBe('3f9a1c2');
  });

  it('a genuinely running attempt IS reported as in flight', () => {
    const groups = stageHistories([deployment({ outcome: 'started', build_id: 'b:live' })]);
    expect(groups[0].attempts[0].inFlight).toBe(true);
  });

  it('names what each stage is CURRENTLY serving — its newest SUCCEEDED attempt', () => {
    const groups = stageHistories([
      deployment({ id: 'dep-broken', outcome: 'failed', image_tag: 'a-1-broken', started_at: '2026-07-31T10:00:00Z' }),
      deployment({ id: 'dep-good', outcome: 'succeeded', image_tag: 'a-1-good', started_at: '2026-07-30T10:00:00Z' }),
    ]);
    // A failed attempt is the NEWEST row and is not what the stage is running.
    expect(groups[0].currentTag).toBe('a-1-good');
  });

  it('a stage whose only attempts failed is serving nothing — null, not the failed tag', () => {
    const groups = stageHistories([deployment({ outcome: 'failed', image_tag: 'a-1-broken' })]);
    expect(groups[0].currentTag).toBeNull();
  });
});

// ---------------------------------------------------------------------------
// mayRollback — the STRICT owner gate, and the same one promote uses
// ---------------------------------------------------------------------------

describe('mayRollback — OWNER-or-admin, on the STRICT gate', () => {
  it('offers rollback to an owner and to a platform admin', () => {
    expect(mayRollback('owner', 0)).toBe(true);
    expect(mayRollback(null, 2)).toBe(true);
  });

  it('does NOT offer it to a maintainer or a viewer', () => {
    // The route gates on OWNER via `_require_project_role`, the same helper and threshold as
    // promote — anything looser here would BE a bypass of promote's gate, since a rollback is a
    // write to production.
    expect(mayRollback('maintainer', 0)).toBe(false);
    expect(mayRollback('viewer', 0)).toBe(false);
    expect(mayRollback(null, 0)).toBe(false);
  });

  it('is the SAME predicate `canDestroy` already is — never a fourth threshold', () => {
    // Delegated rather than re-derived, so "owner-or-admin, no ungoverned fallback" has ONE body.
    // Named separately because the vocabulary at the call site is a rollback, not a teardown; this
    // assertion is what stops the two drifting apart into two different real gates.
    for (const held of [null, 'viewer', 'maintainer', 'owner'] as (ProjectRoleName | null)[]) {
      for (const level of [0, 1, 2, 3]) {
        expect(mayRollback(held, level), `${held}/${level}`).toBe(canDestroy(held, level));
      }
    }
  });
});

// ---------------------------------------------------------------------------
// rollbackEligibility — the plan's FIRST required test: hidden for a non-OWNER
// ---------------------------------------------------------------------------

const OWNER_TARGET = {
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

  it('HIDES rollback from a non-OWNER (the plan’s required test)', () => {
    // A role refusal renders NOTHING — not a disabled button, and not a hint reading "you need
    // Owner". `promoteBlockedReason` reached this conclusion first: a per-row privilege hint is
    // both noise and an invitation to go asking for standing.
    for (const held of [null, 'viewer', 'maintainer'] as (ProjectRoleName | null)[]) {
      expect(rollbackEligibility({ ...OWNER_TARGET, held }), String(held)).toBe('not-owner');
    }
    expect(ROLLBACK_BLOCKED_NOTE['not-owner']).toBeNull();
  });

  it('the role refusal OUTRANKS every other reason, so no refusal leaks a privilege hint', () => {
    // If a lesser reason won, a viewer would read "a delivery is running" — a sentence implying
    // that waiting would earn them the button.
    const viewer = { ...OWNER_TARGET, held: 'viewer' as const };
    expect(rollbackEligibility({ ...viewer, cicdStatus: 'promoting' })).toBe('not-owner');
    expect(rollbackEligibility({ ...viewer, outcome: 'failed' })).toBe('not-owner');
    expect(rollbackEligibility({ ...viewer, imageTag: 'a-1-current' })).toBe('not-owner');
  });

  it('refuses a NON-SUCCEEDED attempt as a target', () => {
    // `started` means "we asked" and `failed` means "it did not run" — neither is evidence the
    // image ever served traffic, and the service refuses both with the same 409.
    for (const outcome of ['started', 'failed'] as Deployment['outcome'][]) {
      expect(rollbackEligibility({ ...OWNER_TARGET, outcome }), outcome).toBe('not-a-target');
    }
    expect(ROLLBACK_BLOCKED_NOTE['not-a-target']).toBeNull();
  });

  it('refuses an attempt with no image tag — a rollback with no target must never be sent', () => {
    // The route requires `image_tag` and answers 422 without one. Sending a blank tag would turn a
    // validation the backend performs into a request the backend rejects.
    for (const imageTag of [null, '', '   ']) {
      expect(rollbackEligibility({ ...OWNER_TARGET, imageTag }), JSON.stringify(imageTag)).toBe(
        'not-a-target',
      );
    }
  });

  it('does not offer a rollback to what the stage is ALREADY serving', () => {
    // Not a rollback: the confirm’s own sentence ("production will serve X instead of Y") would be
    // false, and it would start a production build that changes nothing.
    expect(rollbackEligibility({ ...OWNER_TARGET, imageTag: 'a-1-current' })).toBe('current');
    expect(ROLLBACK_BLOCKED_NOTE.current).toBeNull();
  });

  it('suppresses rollback while a delivery is IN FLIGHT, and says so', () => {
    // The service reuses promote's bounded in-flight guard, so the route answers 409 — an
    // affordance whose every click is refused is not an affordance. Unlike a role refusal this one
    // is actionable by waiting, so it gets a sentence.
    for (const status of ['provisioning', 'promoting']) {
      expect(rollbackEligibility({ ...OWNER_TARGET, cicdStatus: status }), status).toBe('in-flight');
    }
    expect(ROLLBACK_BLOCKED_NOTE['in-flight']).toBeTruthy();
  });

  it('narrows the delivery status through the shared boundary, never a raw compare', () => {
    // `cicd_status` is a bare wire string with five writers, one a shell helper — a raw `===`
    // would miss ` PROMOTING ` and offer a rollback the route answers with a 409.
    expect(rollbackEligibility({ ...OWNER_TARGET, cicdStatus: ' PROMOTING ' })).toBe('in-flight');
  });

  it('is NOT suppressed by a resting or unknown delivery status', () => {
    // An incident is exactly when a rollback is needed, and `failed` is the likeliest status then.
    for (const status of ['failed', 'ready', 'deployed', 'unknown', '', null, undefined]) {
      expect(rollbackEligibility({ ...OWNER_TARGET, cicdStatus: status }), String(status)).toBe('ok');
    }
  });

  it('every eligibility has an entry in the note table — no default branch', () => {
    const keys: RollbackEligibility[] = ['ok', 'not-owner', 'not-a-target', 'current', 'in-flight'];
    for (const k of keys) expect(k in ROLLBACK_BLOCKED_NOTE, k).toBe(true);
    expect(Object.keys(ROLLBACK_BLOCKED_NOTE).sort()).toEqual([...keys].sort());
  });

  it('never gates on the project’s `ungoverned` bit', () => {
    // D11's trap, and the reason `headerActions` keeps promote and retry on different predicates:
    // the rollback route rides the STRICT gate, so a role-less caller on an ungoverned project
    // must NOT be offered a button whose every click 403s.
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
    // The marker is not an affordance. Hiding it with the button would withhold a fact from
    // everyone who cannot act on it, which is the opposite of what an Ops surface is for.
    expect(isCurrentAttempt('a-1-x', 'a-1-x')).toBe(true);
    expect(rollbackEligibility({ ...OWNER_TARGET, held: 'viewer', imageTag: 'a-1-current' })).toBe(
      'not-owner',
    );
  });
});

// ---------------------------------------------------------------------------
// rollbackActionMessage — the route's FIXED literals, mapped
// ---------------------------------------------------------------------------

describe('rollbackActionMessage — the route’s literals become sentences', () => {
  it('states a REJECTED TAG as an ordinary state, not a server fault', () => {
    // The route answers 409 with a FIXED detail that never echoes the tag back. The tag was
    // validated against the succeeded rows for this repo+stage and did not match — usually because
    // the history moved under the page.
    const msg = rollbackActionMessage('no such succeeded deployment to roll back to');
    expect(msg).not.toMatch(/error|fault|failed to/i);
    expect(msg.length).toBeGreaterThan(30);
    // And it must not invent a tag: the response deliberately carries none.
    expect(msg).not.toMatch(/a-1-/);
  });

  it('distinguishes the in-flight refusal from the build-never-started one', () => {
    const inflight = rollbackActionMessage('a promotion is already in flight');
    const nostart = rollbackActionMessage('failed to start the rollback build');
    expect(inflight).not.toBe(nostart);
    expect(inflight).toMatch(/wait/i);
    // A build that never STARTED means nothing reached production, so retrying is safe — the one
    // thing an operator mid-incident needs to be told.
    expect(nostart).toMatch(/nothing/i);
  });

  it('names OWNER on the 403 and does not offer the ungoverned excuse', () => {
    const msg = rollbackActionMessage('insufficient project role');
    expect(msg).toMatch(/owner/i);
    expect(msg).not.toMatch(/maintainer/i);
  });

  it('maps a vanished repository', () => {
    expect(rollbackActionMessage('Repository not found')).toMatch(/no longer/i);
  });

  it('falls back on an empty message and passes an unrecognised one through', () => {
    expect(rollbackActionMessage('', 'fallback copy')).toBe('fallback copy');
    expect(rollbackActionMessage('   ', 'fallback copy')).toBe('fallback copy');
    // An unrecognised literal is already curated backend copy — surfaced, not swallowed.
    expect(rollbackActionMessage('some other curated detail')).toBe('some other curated detail');
  });

  it('gives every mapped literal its OWN sentence — none collapsed into a neighbour', () => {
    const literals = [
      'insufficient project role',
      'no such succeeded deployment to roll back to',
      'a promotion is already in flight',
      'failed to start the rollback build',
      'Repository not found',
    ];
    const sentences = literals.map((l) => rollbackActionMessage(l));
    expect(new Set(sentences).size).toBe(literals.length);
    // …and none of them leaks the raw lowercase fragment to the operator.
    for (const [i, s] of sentences.entries()) expect(s, literals[i]).not.toBe(literals[i]);
  });
});

// ---------------------------------------------------------------------------
// Observability — the plan's SECOND required test: zero-metrics vs NOT INSTRUMENTED (D13)
// ---------------------------------------------------------------------------

const ZERO_TOTALS = { traces: 0, cost_usd: 0, tokens: 0 };
const zeroMetrics = { totals: ZERO_TOTALS, daily: [], by_model: [], by_user: [], by_agent: [] };

describe('metricsState — not instrumented, unreadable and genuinely zero are THREE states (D13)', () => {
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

  it('a genuine ZERO and NOT INSTRUMENTED are rendered differently (the required test)', () => {
    // The whole of D13 in one assertion. `langfuse_metrics_service` degrades a failed or
    // unprovisioned read to a ZEROED payload and its behaviour is deliberately NOT changed, so the
    // distinction has to be made HERE — and it is made off `configured`, which is the only signal
    // that says whether the platform can measure this at all.
    const notInstrumented = metricsState({
      loading: false, configured: false, error: false, metrics: zeroMetrics,
    });
    const genuineZero = metricsState({
      loading: false, configured: true, error: false, metrics: zeroMetrics,
    });
    // The IDENTITY of each state, not merely that the two differ. Mutation testing caught this: with
    // the not-instrumented branch DELETED the unconfigured case fell through to `unread`, which still
    // differs from `data` — so a bare `not.toBe` passed over an implementation that had lost the
    // third state entirely. A "they are different" assertion is satisfied by the wrong difference.
    expect(notInstrumented).toBe('not-instrumented');
    expect(genuineZero).toBe('data');
    expect(notInstrumented).not.toBe(genuineZero);
    // Not instrumented gets COPY and draws no figures; a real zero draws the figures, which is
    // what `null` copy means.
    expect(METRICS_STATE_COPY[notInstrumented]).toBeTruthy();
    expect(METRICS_STATE_COPY[genuineZero]).toBeNull();
    // …and the copy must not describe an absence of activity, which is the conflation itself.
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
    // The platform Observability page degrades a failed settings read to `configured: false`,
    // which asserts that Langfuse is absent on the strength of a request that failed. Here the
    // two stay apart: "we could not ask" is not "there is nothing to ask".
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
      {
        ...zeroMetrics,
        by_agent: [
          { agent_id: 'a-1', agent_name: 'One', tenant_id: null, totals: { traces: 7, cost_usd: 1.5, tokens: 90 } },
          { agent_id: 'a-2', agent_name: 'Two', tenant_id: null, totals: ZERO_TOTALS },
        ],
      },
      'a-1',
    );
    expect(slice.kind).toBe('present');
    expect(slice.totals).toEqual({ traces: 7, cost_usd: 1.5, tokens: 90 });
  });

  it('an agent MISSING from the breakdown is ABSENT, never zero', () => {
    // The same rule one level down: a project read that did not enumerate this agent has not
    // established that the agent recorded nothing. Reporting 0 traces would be a claim.
    const slice = agentSlice({ ...zeroMetrics, by_agent: [] }, 'a-1');
    expect(slice.kind).toBe('absent');
    expect(slice.totals).toBeNull();
  });

  it('an unread project payload is absent, not zero', () => {
    expect(agentSlice(null, 'a-1')).toEqual({ kind: 'absent', totals: null });
  });

  it('a present agent reporting real zeroes IS zero — the distinction cuts both ways', () => {
    const slice = agentSlice(
      {
        ...zeroMetrics,
        by_agent: [{ agent_id: 'a-1', agent_name: 'One', tenant_id: null, totals: ZERO_TOTALS }],
      },
      'a-1',
    );
    expect(slice.kind).toBe('present');
    expect(slice.totals).toEqual(ZERO_TOTALS);
  });
});

describe('PROJECT_SCOPE_NOTE — project-wide figures are never labelled as this repository’s', () => {
  it('says out loud that the scope is the project', () => {
    // `scope=project` aggregates every agent materialized into the project. There is no
    // repo-scoped metrics route, so the honest move is to state the scope rather than to relabel
    // the number.
    expect(PROJECT_SCOPE_NOTE).toMatch(/project/i);
    expect(PROJECT_SCOPE_NOTE).toMatch(/not|whole|every/i);
  });
});

describe('AGENT_ZERO_NOTE — a zero on THIS repository’s card is not evidence of idleness', () => {
  // The last place D13's conflation survives, and it is on the one card that carries this
  // repository's name. `configured` comes from the settings probe, which reports a PLATFORM-wide
  // fact; per-agent instrumentation is a separate per-agent secret, and the metrics service returns
  // a ZEROED row with no upstream call at all when that secret is absent. The scope route appends a
  // breakdown row for every visible agent unconditionally. So: platform instrumented + THIS agent
  // never provisioned ⇒ the state is `data` ⇒ this card draws 0 traces / $0.00 / 0 tokens, which is
  // exactly "an agent nobody is watching looks identical to an agent nobody is using".
  //
  // Reading the real per-agent signal would mean widening the frontend `Agent` interface, which
  // this epic forbids, so the honest fix available here is COPY: say what a zero does and does not
  // establish. Pinned so it cannot quietly disappear and leave the tiles unqualified.
  it('says a zero came from the project read and does not establish idleness', () => {
    expect(AGENT_ZERO_NOTE.length).toBeGreaterThan(80);
    // It attributes the figure to the read, per agent…
    expect(AGENT_ZERO_NOTE).toMatch(/zero/i);
    // …and it names the other explanation for a zero, rather than leaving the tiles to imply the
    // reassuring one.
    expect(AGENT_ZERO_NOTE).toMatch(/never (been )?(set up|provisioned|configured|instrumented)/i);
    // The sentence must NOT be the reassuring reading. "This agent has been idle" is the exact
    // claim a zero cannot support.
    expect(AGENT_ZERO_NOTE).not.toMatch(/\b(is|has been|was) idle\b/i);
    expect(AGENT_ZERO_NOTE).not.toMatch(/nothing (has )?happened/i);
  });

  it('is rendered on the repo card, beside the per-agent tiles', () => {
    // Copy nobody reads is copy nobody wrote. The note must reach the card whose numbers it
    // qualifies — the same card the tiles are on, not the project-totals card below it.
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

// ---------------------------------------------------------------------------
// Resources — the 5-artifact inventory
// ---------------------------------------------------------------------------

describe('resourceInventory — the five teardown artifacts, as a read-only inventory', () => {
  it('always renders the five contract artifacts, in a fixed order', () => {
    // The five keys are the backend `RepoDeleteSelection` fields; the order matches the teardown
    // checklist so one operator does not meet two orderings of one list.
    expect(RESOURCE_ITEMS).toEqual(['record', 'github', 'image', 'runtime', 'identity']);
    const rows = resourceInventory({ items: [] });
    expect(rows.map((r) => r.key)).toEqual([...RESOURCE_ITEMS]);
  });

  it('an artifact the preview did NOT report is UNKNOWN, never gone', () => {
    // Absence of evidence is not evidence of absence, and on this panel "gone" would tell an
    // operator an artifact was already deleted — the reassuring direction, and the wrong one.
    const rows = resourceInventory({ items: [{ item: 'github', state: 'present' }] });
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
    const rows = resourceInventory({
      items: [
        { item: 'record', state: 'present' },
        { item: 'github', state: 'gone' },
        { item: 'image', state: 'unknown' },
      ],
    });
    const byKey = Object.fromEntries(rows.map((r) => [r.key, r.state]));
    expect([byKey.record, byKey.github, byKey.image]).toEqual(['present', 'gone', 'unknown']);
  });

  it('ignores an artifact the contract does not name', () => {
    // A newer backend reporting a sixth artifact must not add an unlabelled row to a panel whose
    // vocabulary is fixed; the five contract rows still render.
    const rows = resourceInventory({ items: [{ item: 'something-new', state: 'present' }] });
    expect(rows.map((r) => r.key)).toEqual([...RESOURCE_ITEMS]);
  });
});

describe('toResourceState — total, with UNKNOWN as the fallback', () => {
  it('narrows the three contract values, case- and whitespace-tolerantly', () => {
    expect(toResourceState('present')).toBe('present');
    expect(toResourceState(' GONE ')).toBe('gone');
    expect(toResourceState('unknown')).toBe('unknown');
  });

  it('anything unrecognised is UNKNOWN — never `gone`, and never `present`', () => {
    // `gone` would claim an artifact was deleted; `present` would claim it exists. Only `unknown`
    // claims nothing, which is all an unrecognised value supports.
    for (const raw of [null, undefined, '', 'deleted', 'missing', 'ok']) {
      expect(toResourceState(raw), String(raw)).toBe('unknown');
    }
  });

  it('every state has a label and a tint — no default branch', () => {
    for (const s of ['present', 'gone', 'unknown'] as const) {
      expect(RESOURCE_STATE_LABEL[s], s).toBeTruthy();
      expect(RESOURCE_STATE_BADGE_KEY[s], s).toBeTruthy();
    }
    // `unknown` wears the NEUTRAL tint and `gone` does not wear a failure tint: an already-deleted
    // artifact after a partial teardown is an ordinary state, not a fault.
    expect(RESOURCE_STATE_BADGE_KEY.unknown).toBe('unknown');
    expect(RESOURCE_STATE_BADGE_KEY.gone).not.toBe('failed');
    expect(RESOURCE_STATE_BADGE_KEY.present).toBe('ready');
    // The three labels are distinct, or the panel cannot say which state a row is in.
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
  it('reports FORBIDDEN for the OWNER-gated route’s 403 rather than an empty inventory', () => {
    // `delete-preview` is OWNER-gated (it is the delete modal's own surface). A viewer's read is
    // refused, and rendering that as "nothing here" would state that the repository has no
    // artifacts — over a repository that certainly does.
    expect(inventoryState({ loading: false, error: 'insufficient project role', preview: null })).toBe(
      'forbidden',
    );
    expect(INVENTORY_STATE_COPY.forbidden).toBeTruthy();
  });

  it('reports UNREAD for any other failure', () => {
    expect(
      inventoryState({ loading: false, error: 'Failed to preview the repository delete', preview: null }),
    ).toBe('unread');
    expect(INVENTORY_STATE_COPY.unread).not.toBe(INVENTORY_STATE_COPY.forbidden);
  });

  it('reports DATA when the probe answered', () => {
    expect(inventoryState({ loading: false, error: null, preview: { items: [] } })).toBe('data');
    expect(INVENTORY_STATE_COPY.data).toBeNull();
  });

  it('LOADING outranks everything', () => {
    expect(
      inventoryState({ loading: true, error: 'insufficient project role', preview: { items: [] } }),
    ).toBe('loading');
  });

  it('every state has a copy entry — no default branch', () => {
    expect(Object.keys(INVENTORY_STATE_COPY).sort()).toEqual([
      'data', 'forbidden', 'loading', 'unread',
    ]);
  });
});

// ---------------------------------------------------------------------------
// Source-as-text guards over the `.tsx` wiring and over this task's pure modules
// ---------------------------------------------------------------------------

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
    // The single most expensive drift available to this task. `repositoryDetailTabs` exports the
    // corrected rule because T11 shipped the naive one and review caught it.
    expect(deploymentsPureSrc).toContain('collapseByBuild');
    expect(deploymentsPureSrc).toMatch(/from '\.\.\/repositoryDetailTabs'/);
    // A re-derivation has to name the join key and has to test the outcome literal. This module
    // does NEITHER — the collapse is entirely behind the import — so both absences are checkable.
    expect(deploymentsPureSrc).not.toContain('build_id');
    expect(deploymentsPureSrc).not.toContain("'started'");
    // …and the `.tsx` renders from the shared attempt shape rather than from raw rows.
    expect(deploymentsTabSrc).toContain('DeploymentAttempt');
    expect(deploymentsTabSrc).not.toContain('build_id');
  });

  it('renders the actor CURRENCY through the shared table, never a re-derived ternary', () => {
    // A GitHub login and an Entra oid are two different currencies (E27A §6). `ACTOR_KIND_TITLE`
    // is a `Record` over all three kinds; a two-way ternary in a `.tsx` is what shipped `unknown`
    // under "GitHub login", asserting a provider identity nobody established.
    expect(deploymentsTabSrc).toContain('ACTOR_KIND_TITLE[');
    for (const literal of ['GitHub login', 'Entra object id']) {
      expect(deploymentsTabSrc, literal).not.toContain(literal);
    }
    // The TINT is table-driven too, over the same three kinds. A two-way ternary on one kind styles
    // a THREE-member union: whichever kind is not named silently inherits the other's treatment, so
    // an actor of unestablished currency is rendered as if it were a provider login — the same
    // mistake the `title` already avoided, one attribute along. A `Record` over the union makes a
    // fourth kind a `tsc` error instead of a silent default.
    expect(deploymentsTabSrc).toContain("ACTOR_KIND_CLASS: Record<DeploymentActor['kind'], string>");
    expect(deploymentsTabSrc).toContain('ACTOR_KIND_CLASS[');
    expect(deploymentsTabSrc).not.toMatch(/\.kind === '\w+' \?/);
  });

  it('marks a retained row through the MODEL, and does not re-decide whose it is', () => {
    // E28C/T7, D-C5. The comparison is a judgement — "is this row's repository the one on screen?"
    // — so it lives in the `.ts` where a test reaches it. A `.tsx` comparing the two ids itself
    // would be exactly the pattern that put a wrong tint on a live production repo.
    expect(deploymentsTabSrc).toContain('attemptRepoRef(');
    // The copy comes off the model's answer, never spelled in the markup.
    expect(deploymentsTabSrc).toContain('repoRef.marker');
    expect(deploymentsTabSrc).not.toMatch(/'repo deleted'|"repo deleted"/);
    // And the `.tsx` must not form its own verdict: an id comparison here is the re-derivation.
    expect(deploymentsTabSrc).not.toMatch(/repo\.id\s*!==\s*row\.repo_id/);
    expect(deploymentsTabSrc).not.toMatch(/row\.repo_id\s*!==/);
  });

  it('MARKS the retained row rather than filtering it out', () => {
    // Retention is the decision (D-C5): dropping the row would destroy the evidence the append-only
    // partition exists to keep, and a deleted repository's production history would vanish from the
    // platform. Mechanically: nothing filters or hides on the orphan verdict.
    expect(deploymentsTabSrc).not.toMatch(/\.filter\([^)]*orphan/i);
    expect(deploymentsTabSrc).not.toMatch(/!\s*repoRef\.orphaned\s*&&/);
    // The image tag still renders for every row, orphaned or not — it is above the marker, not
    // replaced by it.
    expect(deploymentsTabSrc).toContain('{row.image_tag}');
  });

  it('labels the actor "deployed by", never "promoted by" (P10)', () => {
    // `last_promoted_by` may now name a roller-back, so "promoted by" became false. T13 fixes the
    // other site; a third wording here would be the drift both fixes exist to remove.
    expect(deploymentsTabSrc).toMatch(/deployed by/i);
    expect(deploymentsTabSrc).not.toMatch(/promoted by/i);
  });

  it('does not derive a DURATION from a build-written row (P9)', () => {
    // A build-written terminal row sets `started_at` to the COMPLETION time, so any duration
    // computed from that row alone is 0 — a confident, wrong number.
    for (const src of [deploymentsTabSrc, deploymentsPureSrc]) {
      expect(src).not.toMatch(/duration|elapsed|getTime\(\)\s*-/i);
    }
  });

  it('never captions anything with a runtime status (the runtime is per-AGENT, not per-stage)', () => {
    // T5: one `agent_arn`, overwritten by whichever stage deployed last. The page renders the
    // runtime pill ONCE, agent-level, in the header. A per-stage section here must not repeat it,
    // which is made mechanical the same way `EnvironmentStrip` does — by not importing the tables.
    for (const src of [deploymentsTabSrc, resourcesTabSrc, observabilityTabSrc]) {
      expect(src).not.toContain('RUNTIME_LABEL');
      expect(src).not.toContain('RUNTIME_BADGE_KEY');
    }
  });

  it('uses the shared delivery LABELS rather than hand-written status strings', () => {
    expect(deploymentsPureSrc).toContain('toCicdStatus');
    expect(deploymentsPureSrc).not.toContain('as CicdStatus');
    for (const src of [deploymentsTabSrc, deploymentsPureSrc]) {
      expect(src).not.toContain("'Delivery failed'");
      expect(src).not.toContain("'Runtime failed'");
    }
  });

  it('offers NO governance verb on any of the three tabs', () => {
    // The shadow-governance failure mode. Rollback and promote are the ONLY mutations permitted on
    // this surface, and only ONE of them is this task's.
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
    // The panel surfaces the same five-artifact inventory the teardown enumerates, from the
    // READ-ONLY pre-check route. A delete from here would be a second teardown path beside the
    // E23 cascade, and it would skip the modal that owns the checklist and the per-item outcome.
    expect(resourcesTabSrc).toContain('projectsApi.deletePreview');
    expect(resourcesTabSrc).not.toContain('projectsApi.deleteRepo');
    expect(resourcesTabSrc).not.toContain('DeleteRepositoryModal');
  });

  it('writes NO zero-for-absent-data fallback, even an unreachable one (E28 final review)', () => {
    // The project-totals tiles read `metrics?.totals.x ?? 0`. Unreachable today — those tiles only
    // render in the `data` state, where the payload is non-null — but a pre-written
    // zero-for-absent-data fallback is the wrong thing to have in the ONE file whose thesis is that
    // a zero must come from a read that returned zero. Unreachable code is the code a later change
    // reaches, and a `?? 0` that becomes live is silently reassuring rather than loudly broken.
    //
    // Anchored to the operator so the guard is about the FALLBACK and not about the digit: `0` alone
    // appears in class strings and format calls throughout the file.
    expect(observabilityTabSrc).not.toMatch(/\?\?\s*0\b/);
    // `Tile` already renders `null` as the em dash, so the honest form is available and used.
    expect(observabilityTabSrc).toContain('NO_VALUE');
  });

  it('the OBSERVABILITY tab owns its rendering and touches nothing under governance/', () => {
    // `components/governance/**` is Jorge's surface — not one line, and no import either. The
    // client signature already accepts the project scope and the backend already implements it, so
    // nothing anywhere needs widening.
    expect(observabilityTabSrc).not.toContain('governance/');
    expect(observabilityTabSrc).toContain('observabilityApi.getMetrics');
    // …and it states the scope rather than relabelling project figures as this repository's.
    expect(observabilityTabSrc).toContain('PROJECT_SCOPE_NOTE');
  });

  it('the ROLLBACK confirm is a real confirm, on the ONE route, with MAPPED errors', () => {
    expect(deploymentsTabSrc).toContain('projectsApi.rollbackRepo');
    expect(deploymentsTabSrc).toContain('rollbackActionMessage');
    expect(deploymentsTabSrc).toContain('rollbackEligibility');
    // Gated by RENDERING, not `disabled` (the epic's FE constraint): `disabled` is reserved for an
    // in-flight request, so a caller without the standing is never shown a button that 403s.
    expect(deploymentsTabSrc).toContain("=== 'ok'");
    // The gate is decided in the `.ts` where the tests above reach it — not re-derived here.
    expect(deploymentsTabSrc).not.toContain('meetsRole');
    expect(deploymentsTabSrc).not.toContain('canDestroy');
  });

  it('renders the rollback FAILURE inside the confirm, not underneath its overlay', () => {
    // A placement bug no mutation of a `.ts` could ever catch, because the defect is WHERE a node
    // sits in a `.tsx` and there is no jsdom here. `error` is set only in the rollback's catch,
    // which cannot run unless a confirm is open — and the confirm is a fixed, full-viewport,
    // backdrop-blurred overlay at a raised stacking level. So an error paragraph rendered at the
    // top of the tab is painted UNDERNEATH it: the operator sees the button re-enable with no
    // stated reason, and every carefully-mapped literal (the role refusal, the rejected tag, the
    // in-flight refusal, the build that never started) is invisible. The house precedent renders
    // its action error inside its own modal.
    //
    // The `notice` is deliberately NOT covered by this: it is only ever set AFTER the confirm
    // closes, so the top of the tab is exactly where it belongs.
    const open = deploymentsTabSrc.indexOf('<ModalShell');
    const close = deploymentsTabSrc.indexOf('</ModalShell>');
    expect(open).toBeGreaterThan(-1);
    expect(close).toBeGreaterThan(open);
    const modalSpan = deploymentsTabSrc.slice(open, close);

    // VACUITY CHECK on the span itself. A span that silently resolved to empty — or to a stray
    // mention rather than the real element — would make every assertion below pass for the wrong
    // reason, which is the exact failure class this epic keeps paying for.
    expect(modalSpan.length).toBeGreaterThan(800);
    expect(modalSpan).toContain('actionPending');
    expect(modalSpan).toContain('Roll back');

    // The error block lives WITHIN that span.
    const alert = deploymentsTabSrc.indexOf('role="alert"');
    expect(alert).toBeGreaterThan(-1);
    expect(alert).toBeGreaterThan(open);
    expect(alert).toBeLessThan(close);
    expect(modalSpan).toContain('{error && (');
    // …and there is exactly one of it, so a second copy outside cannot hide behind the one inside.
    expect(deploymentsTabSrc.split('role="alert"').length - 1).toBe(1);
    expect(deploymentsTabSrc.split('{error && (').length - 1).toBe(1);
  });

  it('sends the attempt’s OWN stage and tag — never a hardcoded stage (C5)', () => {
    // The route defaults `stage` server-side and the tag is validated against that stage's
    // succeeded rows, so a frontend that named either would be guessing at the trust boundary.
    expect(deploymentsTabSrc).toMatch(/stage:\s*\w/);
    expect(deploymentsTabSrc).toMatch(/image_tag:\s*\w/);
  });

  it('every clickable element is keyboard-reachable (M-e, not inherited)', () => {
    const clickableNonInteractive = /<(tr|div|span|td|li)\b[^>]*\sonClick/;
    for (const [name, src] of [
      ['DeploymentsTab.tsx', deploymentsTabSrc],
      ['ResourcesTab.tsx', resourcesTabSrc],
      ['ObservabilityTab.tsx', observabilityTabSrc],
    ] as const) {
      expect(clickableNonInteractive.test(src), name).toBe(false);
    }
  });

  it('contains NO stage literal anywhere (C5) — quoted, member-access OR destructured', () => {
    // The same pattern `repositoryDetailTabs.test.ts` defines and applies to T11's files, applied
    // here to T12's. Matched on RAW source with no lowercasing, trimming or comment stripping: a
    // normalization step is how a guard silently stops seeing what it guards. This file is covered
    // too — a rule that exempts the file enforcing it is a rule with a hole in it.
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
    // Two surfaces name one contract's five artifacts. The LABELS differ by design (an inventory
    // names an artifact; a checklist names what will be destroyed), but a KEY renamed on one side
    // and not the other would silently give this panel five rows reading "Not established" for
    // artifacts that exist — the join is on the key, and a key that matches nothing narrows to
    // unknown by design.
    //
    // So the comparison is against the REAL other side: the teardown modal's own checklist, read as
    // raw text. An earlier form of this test compared `RESOURCE_ITEMS` against the tab's rendered
    // source OR-ed with a non-empty label check — and the label check is true by construction (the
    // table's five entries are non-empty literals in the module this file imports), so the left
    // disjunct could never decide anything. It also could never fire: the panel renders `row.label`
    // and never a key. A test that cannot fail is worse than no test, because it is counted.
    //
    // The modal is READ ONLY here. It is another task's file; this guard notices a divergence, it
    // does not create one.
    expect(deleteModalSrc.length).toBeGreaterThan(500);
    expect(deleteModalSrc).toContain('RepoDeleteSelection');
    for (const k of RESOURCE_ITEMS) {
      // The checklist's own declaration form, so a mere mention in prose cannot satisfy it.
      const declared = new RegExp(`key:\\s*'${k}'`);
      expect(declared.test(deleteModalSrc), `${k} is not on the teardown checklist`).toBe(true);
    }
    expect(new Set(RESOURCE_ITEMS).size).toBe(RESOURCE_ITEMS.length);
    // …and the modal names no SIXTH key, which would be an artifact this panel silently omits.
    const checklistKeys = [...deleteModalSrc.matchAll(/key:\s*'([a-z_]+)'/g)].map((m) => m[1]);
    expect(new Set(checklistKeys)).toEqual(new Set(RESOURCE_ITEMS));
  });

  it('gives every RESULT line-item a human label, including the ride-along ones', () => {
    // E28C/T7. The cascade REPORTS more items than it offers as choices: `langfuse` rides `identity`
    // and `exec_role` rides `runtime` (T5) — an operator keeping the runtime keeps the IAM role it
    // needs to pull images and write logs. Both are separate non-blocking line-items precisely so a
    // surviving resource is never silently absent from the report, which is how the exec-role leak
    // stayed invisible for six roles: the reclaim always answered AccessDenied and the cascade always
    // said "deleted".
    //
    // Labelled here rather than added to the checklist ABOVE, deliberately: the guard on the five
    // keys is the one that keeps the inventory and the checklist in step, and an unselectable sixth
    // checkbox would imply a choice the cascade does not offer. So the label map is a SUPERSET of the
    // checklist, and this pins the two extra entries without disturbing that.
    for (const key of ['exec_role', 'langfuse']) {
      const labelled = new RegExp(`\\b${key}:\\s*'[^']{4,}'`);
      expect(labelled.test(deleteModalSrc), `${key} has no human label`).toBe(true);
    }
    // The raw-key fallback stays — an item this build has never heard of must degrade to its key
    // rather than to a blank row on a teardown report.
    expect(deleteModalSrc).toMatch(/LABEL_BY_KEY\[[^\]]+\]\s*\?\?/);
  });

  it('states the retention policy on the confirm, from the shared constant', () => {
    // D-C5's other half. The screen that tells an operator what "permanently removes" covers must
    // also say what it does NOT: a checklist enumerating five artifacts and staying silent about the
    // append-only deployment partition implies the partition goes too.
    expect(deleteModalSrc).toContain('DEPLOYMENT_RETENTION_NOTE');
    // Imported, never re-typed — the promise here and the marker on the retained rows are one policy,
    // and a second wording is how a product comes to promise one thing and show another.
    expect(deleteModalSrc).not.toMatch(/Deployment history is retained\./);
    expect(deleteModalSrc).toMatch(/from '\.\/repo-tabs\/deploymentsTab'/);
  });
});
