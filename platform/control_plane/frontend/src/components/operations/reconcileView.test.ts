import { afterEach, describe, expect, expectTypeOf, it, vi } from 'vitest';

import type { ReconcileItemView } from '../../api/client';
import { ApiFailure, type ErrorCode } from '../../api/failure';
import { AUTH_401_HALT_MESSAGE } from '../../api/authRetry';
import { failureOf } from '../../api/loadState';
import { stripComments } from '../../rawSource';

import {
  ADOPT_CONFIRM_NO_PROVENANCE,
  ADOPT_NAME_RULE_HINT,
  RECONCILE_STATE_LABEL,
  RECONCILE_STATE_TONE,
  ROLLOUT_ERROR_CODES,
  SEED_CONSENT_PROMPT,
  adoptConfirm,
  batchConfirmLines,
  batchConfirmTitle,
  batchPlan,
  classifyRolloutError,
  classifyRolloutFailure,
  deregisterConfirm,
  infraIntentFor,
  infraNeedsCreate,
  isAdoptableName,
  reconcileRowModel,
  reconcileRows,
  recreateConfirm,
  repushConfirm,
  rolloutActionLabel,
  wireFlagNote,
  toggleQueuedAction,
  type BatchPlan,
  type InfraIntent,
  type ReconcileAction,
  type ReconcileItemLike,
  type RolloutErrorKind,
} from './reconcileView.ts';
import { TEMPLATE_ERROR_CODES, classifyTemplateError } from './templatesView.ts';
import rolloutModalSrc from './RolloutTemplatesModal.tsx?raw';

export const NON_TEST_SOURCE_FLOOR = 20;

const row = (over: Partial<ReconcileItemLike> = {}): ReconcileItemLike => ({
  name: 'strands-agentcore',
  origin: 'seed',
  state: 'seed_absent',
  default_branch: null,
  head_sha: null,
  ...over,
});

const q = (...entries: [string, ReconcileAction][]): ReadonlyMap<string, ReconcileAction> =>
  new Map(entries);

afterEach(() => vi.restoreAllMocks());

describe('reconcileRowModel: each state offers one honest action, the client half of the server’s state table', () => {
  it('seed_absent offers create, and nothing else', () => {
    const m = reconcileRowModel(row({ state: 'seed_absent' }));
    expect(m.actions).toEqual(['create']);
  });

  it('unregistered_present offers adopt and never create or repush: rollout refuses the row server-side whatever overwrite says', () => {
    const m = reconcileRowModel(row({ name: 'their-repo', origin: 'org', state: 'unregistered_present' }));
    expect(m.actions).toEqual(['adopt']);
    expect(m.actions).not.toContain('create');
    expect(m.actions).not.toContain('repush');
  });

  it('registered_present on a SEED row offers repush only', () => {
    const m = reconcileRowModel(row({ state: 'registered_present' }));
    expect(m.actions).toEqual(['repush']);
  });

  it('registered_present on a registry row offers no action: no seed on disk, so re-push would 404 on every click', () => {
    const m = reconcileRowModel(row({ name: 'uploaded-thing', origin: 'registry', state: 'registered_present' }));
    expect(m.actions).toEqual([]);
  });

  it('registered_missing on a SEED row offers recreate THEN deregister, in that order', () => {
    const m = reconcileRowModel(row({ state: 'registered_missing' }));
    expect(m.actions).toEqual(['recreate', 'deregister']);
  });

  it('registered_missing on a registry row offers deregister only: there is no seed to re-create from', () => {
    const m = reconcileRowModel(row({ origin: 'registry', state: 'registered_missing' }));
    expect(m.actions).toEqual(['deregister']);
  });

  it('renders the raw word with no action for a state outside the union, without throwing', () => {
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => undefined);
    const m = reconcileRowModel(row({ state: 'archived' as never }));
    expect(m.label).toContain('"archived"');
    expect(m.note).toContain('"archived"');
    expect(m.actions).toEqual([]);
    expect(m.queueAction).toBeNull();
    expect(m.actionDisabled).toBe(true);
    expect(m.unknown).toBe(true);
    reconcileRowModel(row({ state: 'archived' as never }));
    expect(warn).toHaveBeenCalledTimes(1);
  });

  it('marks every known state as not unknown, the blocked adopt included: its actionDisabled is the name rule, not a skew', () => {
    for (const state of ['registered_present', 'registered_missing', 'unregistered_present', 'seed_absent'] as const) {
      expect(reconcileRowModel(row({ state })).unknown).toBe(false);
    }
    const blocked = reconcileRowModel(row({ name: 'Legacy_Repo', origin: 'org', state: 'unregistered_present' }));
    expect(blocked.actionDisabled).toBe(true);
    expect(blocked.unknown).toBe(false);
  });

  it('gives every state a label and a tone, and no two states share a label', () => {
    const states = [
      'registered_present',
      'registered_missing',
      'unregistered_present',
      'seed_absent',
    ] as const;
    const labels = states.map((s) => RECONCILE_STATE_LABEL[s]);
    expect(new Set(labels).size).toBe(states.length);
    for (const s of states) {
      expect(RECONCILE_STATE_LABEL[s].length).toBeGreaterThan(0);
      expect(RECONCILE_STATE_TONE[s].length).toBeGreaterThan(0);
    }
  });

  it('tones the two action-needed states apart from the in-sync one', () => {
    expect(RECONCILE_STATE_TONE.registered_present).not.toBe(RECONCILE_STATE_TONE.registered_missing);
    expect(RECONCILE_STATE_TONE.registered_present).not.toBe(RECONCILE_STATE_TONE.unregistered_present);
  });

  it('uses no rose tone, and tints unregistered_present slate: no state here is an error, and someone else’s repository is neither a fault nor AGP’s pending work', () => {
    for (const tone of Object.values(RECONCILE_STATE_TONE)) expect(tone).not.toMatch(/rose/);
    expect(RECONCILE_STATE_TONE.unregistered_present).toMatch(/slate/);
    expect(RECONCILE_STATE_TONE.unregistered_present).not.toMatch(/amber|emerald/);
    expect(RECONCILE_STATE_TONE.registered_present).toMatch(/emerald/);
  });

  it('never labels an unregistered_present row as ours', () => {
    const label = RECONCILE_STATE_LABEL.unregistered_present.toLowerCase();
    expect(label).not.toContain('registered');
    expect(label).not.toContain('in sync');
  });
});

describe('reconcileRowModel — provenance', () => {
  it('renders branch @ short-sha when read_repo found both', () => {
    const m = reconcileRowModel(
      row({ state: 'registered_present', default_branch: 'main', head_sha: 'abcdef1234567890' }),
    );
    expect(m.provenance).toBe('main @ abcdef1');
  });

  it('renders the branch alone for a repo that exists with no commit yet, never "unknown" or a dash', () => {
    const m = reconcileRowModel(row({ state: 'registered_present', default_branch: 'main', head_sha: null }));
    expect(m.provenance).toBe('main');
  });

  it('renders no provenance for an org-origin adopt row: org rows are listed, never re-probed, so both fields are null', () => {
    const m = reconcileRowModel(row({ origin: 'org', state: 'unregistered_present' }));
    expect(m.provenance).toBeNull();
  });

  it('renders no provenance for a row whose repo is gone', () => {
    const m = reconcileRowModel(row({ state: 'registered_missing' }));
    expect(m.provenance).toBeNull();
  });
});

describe('isAdoptableName mirrors the backend name rule ^[a-z][a-z0-9-]{0,63}$, which adopt applies before anything else', () => {
  it('accepts lowercase alphanumerics and hyphens after a leading letter', () => {
    expect(isAdoptableName('strands-agentcore')).toBe(true);
    expect(isAdoptableName('a')).toBe(true);
    expect(isAdoptableName('a1-2-3')).toBe(true);
  });

  it('refuses the shapes GitHub allows and AGP does not: uppercase, underscore, dot, leading digit, leading hyphen, empty, over 64', () => {
    expect(isAdoptableName('MyRepo')).toBe(false);
    expect(isAdoptableName('my_repo')).toBe(false);
    expect(isAdoptableName('my.repo')).toBe(false);
    expect(isAdoptableName('1repo')).toBe(false);
    expect(isAdoptableName('-repo')).toBe(false);
    expect(isAdoptableName('')).toBe(false);
    expect(isAdoptableName('a'.repeat(65))).toBe(false);
  });

  it('accepts exactly 64 characters and refuses 65', () => {
    expect(isAdoptableName('a'.repeat(64))).toBe(true);
    expect(isAdoptableName('a'.repeat(65))).toBe(false);
  });

  it('refuses a name carrying a path separator', () => {
    expect(isAdoptableName('../etc')).toBe(false);
    expect(isAdoptableName('a/b')).toBe(false);
  });
});

describe('reconcileRowModel — the un-adoptable org row', () => {
  it('disables the adopt action and says why', () => {
    const m = reconcileRowModel(row({ name: 'Legacy_Repo', origin: 'org', state: 'unregistered_present' }));
    expect(m.actions).toEqual(['adopt']);
    expect(m.actionDisabled).toBe(true);
    expect(m.note).toBe(ADOPT_NAME_RULE_HINT);
  });

  it('leaves an adoptable org row enabled and un-annotated', () => {
    const m = reconcileRowModel(row({ name: 'legacy-repo', origin: 'org', state: 'unregistered_present' }));
    expect(m.actionDisabled).toBe(false);
    expect(m.note).toBeNull();
  });

  it('does not annotate a non-adopt row even when its name would fail the rule: the rule bears only on the offered action', () => {
    const m = reconcileRowModel(row({ name: 'Legacy_Repo', origin: 'registry', state: 'registered_missing' }));
    expect(m.actionDisabled).toBe(false);
    expect(m.note).toBeNull();
  });

  it('names the rule in terms an operator can act on, without a regex', () => {
    expect(ADOPT_NAME_RULE_HINT).toMatch(/lowercase/i);
    expect(ADOPT_NAME_RULE_HINT).toMatch(/hyphen/i);
    expect(ADOPT_NAME_RULE_HINT).not.toContain('^[a-z]');
  });
});

describe('ReconcileItemLike is the wire row', () => {
  it('the client’s ReconcileItemView assigns to it, so the surface reads the wire shape and nothing more', () => {
    expectTypeOf<ReconcileItemView>().toExtend<ReconcileItemLike>();
    const wire: ReconcileItemView = { name: 'x', origin: 'seed', state: 'seed_absent', default_branch: null, head_sha: null, infra_template_id: null };
    const item: ReconcileItemLike = wire;
    expect(item).toBe(wire);
  });
});

describe('reconcileRows — the surface\'s row list', () => {
  it('keeps the wire order and adds one model per row', () => {
    const rows = reconcileRows([
      row({ name: 'b', state: 'registered_present', default_branch: 'main', head_sha: 'f'.repeat(40) }),
      row({ name: 'a', origin: 'org', state: 'unregistered_present' }),
    ]);
    expect(rows.map((r) => r.item.name)).toEqual(['b', 'a']);
    expect(rows[0].actions).toEqual(['repush']);
    expect(rows[1].actions).toEqual(['adopt']);
  });

  it('reports the counts the consent screen states', () => {
    const rows = reconcileRows([
      row({ name: 'a', state: 'seed_absent' }),
      row({ name: 'b', state: 'seed_absent' }),
      row({ name: 'c', origin: 'org', state: 'unregistered_present' }),
      row({ name: 'd', state: 'registered_present', default_branch: 'main', head_sha: 'abc1234' }),
      row({ name: 'e', state: 'registered_missing' }),
    ]);
    const { createCount, adoptCount, inSyncCount, missingCount } = reconcileRows.summarize(rows);
    expect(createCount).toBe(2);
    expect(adoptCount).toBe(1);
    expect(inSyncCount).toBe(1);
    expect(missingCount).toBe(1);
  });
});

describe('queueAction: the three rollout-batch verbs are queueable, adopt and deregister are single-repo verbs on other routes', () => {
  it('marks create, repush and recreate as the queueing actions', () => {
    expect(reconcileRowModel(row({ state: 'seed_absent' })).queueAction).toBe('create');
    expect(reconcileRowModel(row({ state: 'registered_present' })).queueAction).toBe('repush');
    expect(reconcileRowModel(row({ state: 'registered_missing' })).queueAction).toBe('recreate');
  });

  it('leaves adopt-only and deregister-only rows unqueueable', () => {
    expect(
      reconcileRowModel(row({ origin: 'org', state: 'unregistered_present' })).queueAction,
    ).toBeNull();
    expect(
      reconcileRowModel(row({ origin: 'registry', state: 'registered_missing' })).queueAction,
    ).toBeNull();
  });

  it('leaves an in-sync registry row unqueueable — it has no action at all', () => {
    expect(
      reconcileRowModel(row({ origin: 'registry', state: 'registered_present' })).queueAction,
    ).toBeNull();
  });

  it('never makes an un-adoptable name queueable — the two gates are independent', () => {
    const m = reconcileRowModel(row({ name: 'Bad_Name', origin: 'org', state: 'unregistered_present' }));
    expect(m.queueAction).toBeNull();
    expect(m.actionDisabled).toBe(true);
  });
});

describe('batchPlan — what the wire carries, and the two overwrite flags', () => {
  const ROWS = reconcileRows([
    row({ name: 'fresh', state: 'seed_absent' }),
    row({ name: 'insync', state: 'registered_present', default_branch: 'main', head_sha: 'abc1234' }),
    row({ name: 'gone', state: 'registered_missing' }),
    row({ name: 'theirs', origin: 'org', state: 'unregistered_present' }),
    row({ name: 'uploaded', origin: 'registry', state: 'registered_present' }),
  ]);

  it('carries a queued create with overwrite=false', () => {
    const plan = batchPlan(ROWS, q(['fresh', 'create']), 'none');
    expect(plan.names).toEqual(['fresh']);
    expect(plan.overwrite).toBe(false);
    expect(plan.empty).toBe(false);
  });

  it('sets overwrite when a re-push is queued, exactly when a row whose queueAction is repush is in the batch', () => {
    const plan = batchPlan(ROWS, q(['insync', 'repush']), 'none');
    expect(plan.names).toEqual(['insync']);
    expect(plan.overwrite).toBe(true);
    expect(plan.repushes).toEqual(['insync']);
  });

  it('carries creates and re-pushes together, with overwrite set by the re-push', () => {
    const plan = batchPlan(ROWS, q(['fresh', 'create'], ['insync', 'repush']), 'none');
    expect(plan.names).toEqual(['fresh', 'insync']);
    expect(plan.overwrite).toBe(true);
    expect(plan.creates).toEqual(['fresh']);
    expect(plan.repushes).toEqual(['insync']);
  });

  it('does not set overwrite for a re-create: the server re-creates regardless of the flag, and the flag would authorise re-pushing every other in-sync template in the batch', () => {
    const plan = batchPlan(ROWS, q(['gone', 'recreate']), 'none');
    expect(plan.names).toEqual(['gone']);
    expect(plan.overwrite).toBe(false);
    expect(plan.recreates).toEqual(['gone']);
  });

  it('emits names in ROW order, not in the map\'s insertion order', () => {
    const plan = batchPlan(ROWS, q(['gone', 'recreate'], ['fresh', 'create'], ['insync', 'repush']), 'none');
    expect(plan.names).toEqual(['fresh', 'insync', 'gone']);
  });

  it('self-prunes a queued name whose row no longer offers that action, so a submit never acts on a comparison the operator did not see', () => {
    const plan = batchPlan(
      ROWS,
      q(['fresh', 'create'], ['vanished', 'create'], ['theirs', 'create'], ['uploaded', 'repush']),
      'none',
    );
    expect(plan.names).toEqual(['fresh']);
  });

  it('can never carry an unregistered_present name, even when it is queued', () => {
    const plan = batchPlan(ROWS, q(['theirs', 'adopt']), 'none');
    expect(plan.names).toEqual([]);
    expect(plan.empty).toBe(true);
    expect(plan.overwrite).toBe(false);
  });

  it('reports empty for an empty selection', () => {
    const plan = batchPlan(ROWS, q(), 'none');
    expect(plan.names).toEqual([]);
    expect(plan.empty).toBe(true);
  });

  it('is a pure function of (rows, queued, infraIntent), with no hidden ordering state', () => {
    const a = batchPlan(ROWS, q(['insync', 'repush'], ['fresh', 'create']), 'none');
    const b = batchPlan(ROWS, q(['fresh', 'create'], ['insync', 'repush']), 'none');
    expect(a).toEqual(b);
  });
});

describe('batchPlan: a tick carries the verb it was placed against, so a verb change across a Refresh drops it', () => {
  const CREATE_ROWS = reconcileRows([row({ name: 'strands-agentcore', state: 'seed_absent' })]);
  const NOW_PRESENT = reconcileRows([
    row({
      name: 'strands-agentcore',
      state: 'registered_present',
      default_branch: 'main',
      head_sha: 'abc1234',
    }),
  ]);
  const TICKED_AS_CREATE = q(['strands-agentcore', 'create']);

  it('carries the tick while the verb still matches', () => {
    const plan = batchPlan(CREATE_ROWS, TICKED_AS_CREATE, 'none');
    expect(plan.names).toEqual(['strands-agentcore']);
    expect(plan.creates).toEqual(['strands-agentcore']);
  });

  it('drops the tick when the row comes back offering a different verb', () => {
    const plan = batchPlan(NOW_PRESENT, TICKED_AS_CREATE, 'none');
    expect(plan.names).toEqual([]);
    expect(plan.empty).toBe(true);
  });

  it('and therefore never escalates a create tick into an overwrite', () => {
    const plan = batchPlan(NOW_PRESENT, TICKED_AS_CREATE, 'none');
    expect(plan.overwrite).toBe(false);
    expect(plan.repushes).toEqual([]);
  });

  it('carries a re-push that was ticked as a re-push: a deliberate tick still reaches the wire and sets the flag', () => {
    const plan = batchPlan(NOW_PRESENT, q(['strands-agentcore', 'repush']), 'none');
    expect(plan.names).toEqual(['strands-agentcore']);
    expect(plan.overwrite).toBe(true);
  });

  it('reports every dropped tick, so the surface can say the selection changed rather than silently doing less than authorised', () => {
    const ROWS = reconcileRows([
      row({ name: 'fresh', state: 'seed_absent' }),
      row({ name: 'insync', state: 'registered_present', default_branch: 'main', head_sha: 'abc1234' }),
      row({ name: 'theirs', origin: 'org', state: 'unregistered_present' }),
    ]);
    const plan = batchPlan(
      ROWS,
      q(['fresh', 'create'], ['insync', 'create'], ['theirs', 'create'], ['vanished', 'create']),
      'none',
    );
    expect(plan.names).toEqual(['fresh']);
    expect(plan.dropped).toEqual(['insync', 'theirs', 'vanished']);
  });

  it('reports no drops when every tick still matches', () => {
    expect(batchPlan(CREATE_ROWS, TICKED_AS_CREATE, 'none').dropped).toEqual([]);
  });

  it('sorts the dropped names, so the notice does not vary with click order', () => {
    const rows = reconcileRows([row({ name: 'a', origin: 'org', state: 'unregistered_present' })]);
    const a = batchPlan(rows, q(['zeta', 'create'], ['a', 'create'], ['alpha', 'create']), 'none').dropped;
    const b = batchPlan(rows, q(['alpha', 'create'], ['a', 'create'], ['zeta', 'create']), 'none').dropped;
    expect(a).toEqual(['a', 'alpha', 'zeta']);
    expect(a).toEqual(b);
  });
});

describe('batchPlan: the infra consent is the third argument, never derived from the rows', () => {
  const ROWS = reconcileRows([
    row({ name: 'fresh', state: 'seed_absent' }),
    row({ name: 'insync', state: 'registered_present', default_branch: 'main', head_sha: 'abc1234' }),
  ]);

  it('leaves overwriteInfra false when the operator has not ticked it', () => {
    expect(batchPlan(ROWS, q(['fresh', 'create']), 'none').overwriteInfra).toBe(false);
  });

  it('does not let a template re-push authorise the infra push: the two flags are independent in both directions', () => {
    const plan = batchPlan(ROWS, q(['insync', 'repush']), 'none');
    expect(plan.overwrite).toBe(true);
    expect(plan.overwriteInfra).toBe(false);
  });

  it('carries the infra consent alone, with no template queued at all', () => {
    const plan = batchPlan(ROWS, q(), 'repush');
    expect(plan.empty).toBe(true);
    expect(plan.names).toEqual([]);
    expect(plan.overwrite).toBe(false);
    expect(plan.overwriteInfra).toBe(true);
  });

  it('keeps the infra tick independent of the template flag in the other direction too', () => {
    const plan = batchPlan(ROWS, q(['fresh', 'create']), 'repush');
    expect(plan.overwrite).toBe(false);
    expect(plan.overwriteInfra).toBe(true);
  });

  it('never sends overwrite_infra for a repo that is not there: a tick that outlived its checkbox cannot reach the body', () => {
    const intent = infraIntentFor(row({ name: 'agp-runtime-infra', state: 'seed_absent' }), true);
    expect(intent).toBe('create');
    const plan = batchPlan(ROWS, q(), intent);
    expect(plan.overwriteInfra).toBe(false);
  });

  it('and the re-read clears the infra tick in the reconcile effect, so no consent outlives its checkbox', () => {
    const src = stripComments(rolloutModalSrc);
    const effect = src.slice(
      src.indexOf('.reconcile(connection.id)'),
      src.indexOf('const refetch'),
    );
    expect(effect.length).toBeGreaterThan(200);
    expect(effect).toMatch(/setOverwriteInfra\(false\)/);
  });

  it('and the confirm body agrees with the title in that case', () => {
    const intent = infraIntentFor(row({ name: 'agp-runtime-infra', state: 'seed_absent' }), true);
    const infraLine = batchConfirmLines(ROWS, q(), intent).filter((l) => /runtime-infra/i.test(l));
    expect(infraLine).toHaveLength(1);
    expect(infraLine[0]).not.toMatch(/authorised re-pushing/i);
    expect(batchConfirmTitle(batchPlan(ROWS, q(), intent), intent, 'myorg')).toMatch(/creat/i);
  });
});

describe('toggleQueuedAction: one click does what the box shows', () => {
  const CREATE_ROW = reconcileRows([row({ name: 'a', state: 'seed_absent' })])[0];
  const REPUSH_ROW = reconcileRows([
    row({ name: 'a', state: 'registered_present', default_branch: 'main', head_sha: 'abc1234' }),
  ])[0];

  it('ticks an untouched row', () => {
    expect([...toggleQueuedAction(new Map(), CREATE_ROW)]).toEqual([['a', 'create']]);
  });

  it('un-ticks a row whose tick matches the verb on offer', () => {
    expect([...toggleQueuedAction(q(['a', 'create']), CREATE_ROW)]).toEqual([]);
  });

  it('re-ticks a dropped tick in one call, against the row\'s new verb: a presence-keyed toggle needed two clicks', () => {
    const after = toggleQueuedAction(q(['a', 'create']), REPUSH_ROW);
    expect(after.get('a')).toBe('repush');
    expect(batchPlan([REPUSH_ROW], after, 'none').names).toEqual(['a']);
  });

  it('still round-trips: a second click on the re-selected row clears it', () => {
    const on = toggleQueuedAction(q(['a', 'create']), REPUSH_ROW);
    expect([...toggleQueuedAction(on, REPUSH_ROW)]).toEqual([]);
  });

  it('refuses to queue a row with no queueable action, unchanged', () => {
    const adoptRow = reconcileRows([row({ name: 'theirs', origin: 'org', state: 'unregistered_present' })])[0];
    const before = q(['a', 'create']);
    expect(toggleQueuedAction(before, adoptRow)).toBe(before);
  });

  it('leaves the other rows\' ticks alone', () => {
    const rows = reconcileRows([
      row({ name: 'a', state: 'seed_absent' }),
      row({ name: 'b', state: 'seed_absent' }),
    ]);
    const after = toggleQueuedAction(q(['b', 'create']), rows[0]);
    expect(after.get('b')).toBe('create');
    expect(after.get('a')).toBe('create');
  });

  it('is the modal\'s writer, so the dead click cannot come back inline', () => {
    const src = stripComments(rolloutModalSrc);
    expect(src).toMatch(/toggleQueuedAction\(prev, row\)/);
    expect(src).not.toMatch(/next\.has\(name\)/);
  });
});

describe('infraIntentFor: what the click will do to the forced repo, as one value', () => {
  const infra = (over: Partial<ReconcileItemLike> = {}) =>
    row({ name: 'agp-runtime-infra', origin: 'seed', ...over });

  it('is create when the repo is absent, whatever the tick says: an absent repo being re-pushed is unrepresentable', () => {
    expect(infraIntentFor(infra({ state: 'seed_absent' }), false)).toBe('create');
    expect(infraIntentFor(infra({ state: 'seed_absent' }), true)).toBe('create');
  });

  it('is none for a present repo the operator has not ticked', () => {
    expect(infraIntentFor(infra({ state: 'unregistered_present', default_branch: 'main' }), false)).toBe(
      'none',
    );
  });

  it('is repush for a present repo the operator did tick', () => {
    expect(infraIntentFor(infra({ state: 'unregistered_present', default_branch: 'main' }), true)).toBe(
      'repush',
    );
  });

  it('is none when there is no infra row at all, even with the tick set: a view that could not report the repo is not evidence about it', () => {
    expect(infraIntentFor(null, true)).toBe('none');
    expect(infraIntentFor(undefined, true)).toBe('none');
  });
});

describe('batchConfirmLines — the one consent gate states every write', () => {
  const ROWS = reconcileRows([
    row({ name: 'fresh', state: 'seed_absent' }),
    row({ name: 'insync', state: 'registered_present', default_branch: 'main', head_sha: 'abc1234' }),
    row({ name: 'gone', state: 'registered_missing' }),
  ]);

  const rowLines = (lines: string[]) => lines.filter((l) => !/runtime-infra/i.test(l));

  const ALL_THREE = q(['fresh', 'create'], ['insync', 'repush'], ['gone', 'recreate']);

  it('states what a re-push writes, in the row\'s own line', () => {
    const lines = rowLines(batchConfirmLines(ROWS, q(['insync', 'repush']), 'none'));
    expect(lines).toHaveLength(1);
    expect(lines[0]).toContain('insync');
    expect(lines[0]).toMatch(/one commit|a single commit/i);
    expect(lines[0]).toMatch(/history/i);
  });

  it('distinguishes a create, a re-push and a re-create in the same confirm', () => {
    const lines = rowLines(batchConfirmLines(ROWS, ALL_THREE, 'none'));
    expect(lines).toHaveLength(3);
    expect(new Set(lines).size).toBe(3);
    expect(lines[0]).toContain('fresh');
    expect(lines[1]).toContain('insync');
    expect(lines[2]).toContain('gone');
  });

  it('never promises a deletion or a replacement', () => {
    for (const infraIntent of ['none', 'create', 'repush'] as const) {
      for (const line of batchConfirmLines(ROWS, ALL_THREE, infraIntent)) {
        expect(line.toLowerCase()).not.toContain('delet');
        expect(line.toLowerCase()).not.toContain('destroy');
        expect(line).not.toMatch(/replaces the .*contents/i);
      }
    }
  });

  it('names the forced infra repo, because every rollout ensures it', () => {
    expect(batchConfirmLines(ROWS, q(['fresh', 'create']), 'none').join(' ')).toMatch(
      /runtime-infra|infra repo/i,
    );
  });

  it('says the infra module is left alone when its own box is unticked, and no longer that a template re-push authorises it', () => {
    const infra = batchConfirmLines(ROWS, q(['insync', 'repush']), 'none').filter((l) =>
      /runtime-infra/i.test(l),
    );
    expect(infra).toHaveLength(1);
    expect(infra[0]).toMatch(/if this org does not have it yet|created/i);
    expect(infra[0]).toMatch(/left exactly as it is|otherwise left/i);
    expect(infra[0]).not.toMatch(/every roll-out .{0,40}re-pushes/i);
    expect(infra[0]).not.toMatch(/only when a re-push/i);
    expect(infra[0]).not.toMatch(/authoris/i);
  });

  it('says the module is re-pushed into every existing infra repo when the infra box is ticked, and only then', () => {
    const ticked = batchConfirmLines(ROWS, q(), 'repush').filter((l) => /runtime-infra/i.test(l));
    expect(ticked).toHaveLength(1);
    expect(ticked[0]).toMatch(/re-push/i);
    expect(ticked[0]).toMatch(/idempotent|writes no commit/i);
    const unticked = batchConfirmLines(ROWS, q(), 'none').filter((l) => /runtime-infra/i.test(l));
    expect(ticked[0]).not.toBe(unticked[0]);
    expect(ticked[0]).toMatch(/every one of them that already exists/i);
    expect(ticked[0]).toMatch(/not only the row the box sits on/i);
    expect(ticked[0]).toMatch(/one commit per repository/i);
    expect(ticked[0]).not.toMatch(/into an existing one/i);
    expect(ticked[0]).not.toMatch(/the forced runtime-infra repo\b/i);
  });

  it('the modal’s own wire-flag paragraph agrees with this line: plural, never "that one repository"', () => {
    const para = wireFlagNote({ overwrite: false, overwriteInfra: true }) ?? '';
    expect(para).not.toMatch(/that one repository/i);
    expect(para).toMatch(/each runtime-infra repo that already exists/i);
    expect(stripComments(rolloutModalSrc)).toContain('wireFlagNote(plan)');
  });

  it('and the title agrees with it rather than naming one repository', () => {
    const repush = batchConfirmTitle(batchPlan(ROWS, q(), 'repush'), 'repush', 'myorg');
    expect(repush).toMatch(/modules/);
    expect(repush).not.toMatch(/the runtime infra module\b/i);
    const create = batchConfirmTitle(batchPlan(ROWS, q(), 'create'), 'create', 'myorg');
    expect(create).toMatch(/repos/);
    expect(create).not.toMatch(/the runtime infra repo\b/i);
  });

  it('lists nothing for an empty selection', () => {
    const lines = batchConfirmLines(ROWS, q(), 'none');
    expect(lines.filter((l) => /fresh|insync|gone/.test(l))).toEqual([]);
  });

  it('drops a line for a tick whose verb changed, exactly as batchPlan drops the name', () => {
    const lines = batchConfirmLines(ROWS, q(['insync', 'create']), 'none');
    expect(lines.filter((l) => /insync/.test(l))).toEqual([]);
  });

  it('does not let the modal\'s infra hint restate the overstatement this line dropped', () => {
    const src = stripComments(rolloutModalSrc);
    const start = src.indexOf('Runtime infra repo');
    const hint = src.slice(start, src.indexOf('actionError &&', start));
    expect(hint.length).toBeGreaterThan(200);
    expect(hint).toMatch(/leaves it in place/i);
    expect(hint).not.toMatch(/Every roll-out re-pushes/i);
    expect(hint).not.toMatch(/only when a re-push/i);
    expect(hint).not.toMatch(/a re-push above/i);
    expect(hint).toMatch(/Not in the org yet\..{0,60}creates it/is);
  });

  it('gives the modal\'s infra hint its own branch for a present repo in a state this frontend does not know', () => {
    const src = stripComments(rolloutModalSrc);
    const start = src.indexOf('Runtime infra repo');
    const hint = src.slice(start, src.indexOf('actionError &&', start));
    expect(hint).toMatch(/infraRow\.provenance && !infraRow\.unknown\s*\?/);
    expect(hint).toMatch(
      /:\s*infraRow\.provenance\s*\?\s*`Present at \$\{infraRow\.provenance\}\. \$\{infraRow\.label\}, so nothing will be written to it\.`/,
    );
  });

  it('gives the modal\'s infra checkbox the present branch only, bound to overwriteInfra', () => {
    const src = stripComments(rolloutModalSrc);
    const start = src.indexOf('Runtime infra repo');
    const section = src.slice(start, src.indexOf('actionError &&', start));
    expect(section).toContain('type="checkbox"');
    expect(section).toMatch(/checked=\{overwriteInfra\}/);
    expect(section).toMatch(/infraRow\.provenance &&/);
  });

  it('hides the modal\'s infra overwrite consent on a state this frontend does not know, gated on unknown and never on actionDisabled', () => {
    const src = stripComments(rolloutModalSrc);
    const start = src.indexOf('Runtime infra repo');
    const section = src.slice(start, src.indexOf('actionError &&', start));
    expect(section).toMatch(/\{infraRow\.provenance && !infraRow\.unknown && \(\s*<label[^>]*>\s*<input\s+type="checkbox"/);
    expect(section).not.toMatch(/infraRow\.actionDisabled/);
  });

  it('gives that checkbox a label derived from every repo the tick writes to, never the singular literal', () => {
    const src = stripComments(rolloutModalSrc);
    const start = src.indexOf('Runtime infra repos');
    const section = src.slice(start, src.indexOf('actionError &&', start));
    expect(section.length).toBeGreaterThan(400);
    expect(section).toMatch(/overwriteInfraConsent\(overwriteTargets\)/);
    expect(src).toMatch(/overwriteInfraTargets\(infraRows\)/);
    expect(src).not.toMatch(/into this existing repository/i);
  });

  it('lists one row per registered infra template rather than one hardcoded repo', () => {
    const src = stripComments(rolloutModalSrc);
    const start = src.indexOf('Runtime infra repos');
    expect(start).toBeGreaterThan(-1);
    const section = src.slice(start, src.indexOf('actionError &&', start));
    expect(section.length).toBeGreaterThan(400);
    expect(section).toMatch(/infraRows\.map\(/);
    expect(src).toMatch(/infraRolloutRows\(\s*withInfraSeeds\(infraCatalog,/);
    expect(section).not.toMatch(/reconcileRows\(\[view\.infra_repo\]\)/);
  });

  it('shows a state only for the row the comparison actually read', () => {
    const src = stripComments(rolloutModalSrc);
    const start = src.indexOf('Runtime infra repos');
    const section = src.slice(start, src.indexOf('actionError &&', start));
    expect(section).toMatch(/infra\.reconcile && infraRow &&/);
    expect(section).toMatch(/!infra\.reconcile &&/);
    expect(section).toMatch(/its state in\s*\n?\s*the org is not shown/);
    const evidenceBranch = section.slice(section.indexOf('infra.reconcile && infraRow &&'));
    const unprobedBranch = section.indexOf('!infra.reconcile &&');
    expect(evidenceBranch.indexOf('type="checkbox"')).toBeGreaterThan(-1);
    expect(section.indexOf('type="checkbox"')).toBeLessThan(unprobedBranch);
  });

  it('pills and promises only the mechanisms a roll-out will ensure, and says "Not referenced" for the rest', () => {
    const src = stripComments(rolloutModalSrc);
    const start = src.indexOf('Runtime infra repos');
    const section = src.slice(start, src.indexOf('actionError &&', start));
    expect(section.length).toBeGreaterThan(400);
    expect(section).toMatch(/infra\.ensured \?/);
    expect(section).toMatch(/Not referenced/);
    expect(section).not.toMatch(/>\s*Not in use\s*</);
    expect(section).toMatch(/infra\.ensured\s*\n?\s*\?/);
    expect(section).not.toMatch(/ensures it either way\.\s*<\/p>/);
    expect(section).toMatch(/ensures an infra repo once one does/);
    expect(section).toMatch(/in the same roll-out/);
  });

  it('says so when the reference set could not be read, in amber, above the rows', () => {
    const src = stripComments(rolloutModalSrc);
    expect(src).toMatch(/console\.warn\('\[RolloutTemplatesModal\]/);
    expect(src).toMatch(/ensuredUnknown: true/);
    expect(src).toMatch(/ensuredUnknown: false/);
    const start = src.indexOf('Runtime infra repos');
    const section = src.slice(start, src.indexOf('actionError &&', start));
    expect(section).toMatch(/\{ensuredUnknown &&/);
    expect(section).toMatch(/could not be read/);
    expect(section).toMatch(/text-amber-800/);
    expect(section.indexOf('ensuredUnknown')).toBeLessThan(section.indexOf('infraRows.map('));
  });

  it('derives the ensured set from the agent catalog, degrading to the default only on a failed read', () => {
    const src = stripComments(rolloutModalSrc);
    expect(src).toMatch(/ensuredInfraIds\(agentCatalog,/);
    const read = src.slice(src.indexOf('const agentCatalogLoad'), src.indexOf('const agentCatalog ='));
    expect(read).toMatch(/githubTemplatesApi\s*\n?\s*\.list\(/);
    expect(read).toMatch(/\.catch\([\s\S]*rows: \[\] as TemplateView\[\], ensuredUnknown: true/);
  });

  it('reads the infra catalog as a registry call that degrades to the wire\'s single row, never a provider one', () => {
    const src = stripComments(rolloutModalSrc);
    expect(src).toMatch(/infraTemplatesApi\s*\n?\s*\.list\(/);
    const read = src.slice(src.indexOf('const infraCatalogLoad'), src.indexOf('const infraCatalog ='));
    expect(read.length).toBeGreaterThan(100);
    expect(read).toMatch(/\.catch\(\(\) => \[\] as InfraTemplateView\[\]\)/);
    expect(read).not.toMatch(/classifyRolloutError|RECONCILE_FALLBACK/);
  });
});

describe('batchConfirmTitle: the confirm names what it is about to do', () => {
  const plan = (over: Partial<BatchPlan> = {}): BatchPlan => ({
    names: [],
    overwrite: false,
    overwriteInfra: false,
    creates: [],
    repushes: [],
    recreates: [],
    dropped: [],
    empty: true,
    ...over,
  });

  it('names one template in the singular', () => {
    expect(batchConfirmTitle(plan({ names: ['a'], empty: false }), 'none', 'myorg')).toBe(
      'Roll out 1 template — myorg',
    );
  });

  it('names several templates with the count', () => {
    expect(batchConfirmTitle(plan({ names: ['a', 'b'], empty: false }), 'none', 'myorg')).toBe(
      'Roll out 2 templates — myorg',
    );
  });

  it('keeps the template count even when the infra repo is also being created', () => {
    expect(batchConfirmTitle(plan({ names: ['a'], empty: false }), 'create', 'myorg')).toBe(
      'Roll out 1 template — myorg',
    );
  });

  it('never says "0 templates"', () => {
    for (const intent of ['none', 'create', 'repush'] as const) {
      expect(batchConfirmTitle(plan(), intent, 'myorg')).not.toMatch(/\b0 templates\b/);
    }
  });

  it('names the create when that is the only write the click performs', () => {
    const title = batchConfirmTitle(plan(), 'create', 'myorg');
    expect(title).toContain('myorg');
    expect(title).toMatch(/creat/i);
    expect(title).toMatch(/infra/i);
  });

  it('names the re-push when the infra tick is the only write', () => {
    const title = batchConfirmTitle(plan({ overwriteInfra: true }), 'repush', 'myorg');
    expect(title).toContain('myorg');
    expect(title).toMatch(/re-push/i);
    expect(title).not.toMatch(/creat/i);
  });

  it('distinguishes the create and the re-push titles', () => {
    expect(batchConfirmTitle(plan(), 'create', 'org')).not.toBe(
      batchConfirmTitle(plan(), 'repush', 'org'),
    );
  });

  it('still returns a sentence for the footer-unreachable empty case', () => {
    const title = batchConfirmTitle(plan(), 'none', 'myorg');
    expect(title.length).toBeGreaterThan(0);
    expect(title).toContain('myorg');
  });

  it('interpolates the org into every case', () => {
    const intents: InfraIntent[] = ['none', 'create', 'repush'];
    for (const intent of intents) {
      expect(batchConfirmTitle(plan(), intent, 'acme-corp')).toContain('acme-corp');
      expect(batchConfirmTitle(plan({ names: ['a'], empty: false }), intent, 'acme-corp')).toContain(
        'acme-corp',
      );
    }
  });

  it('is the modal\'s title, not a template literal built from the count', () => {
    const src = stripComments(rolloutModalSrc);
    expect(src).toMatch(/batchConfirmTitle\(\s*plan/);
    expect(src).not.toMatch(/title=\{`Roll out \$\{/);
    expect(src).not.toMatch(/`Roll out \$\{plan\.names\.length/);
  });
});

describe('infraNeedsCreate: does the forced repo still need creating', () => {
  it('is true only when the infra repo is absent', () => {
    expect(infraNeedsCreate(row({ name: 'agp-runtime-infra', state: 'seed_absent' }))).toBe(true);
    expect(
      infraNeedsCreate(row({ name: 'agp-runtime-infra', state: 'unregistered_present', default_branch: 'main' })),
    ).toBe(false);
  });

  it('is false when there is no infra row at all', () => {
    expect(infraNeedsCreate(null)).toBe(false);
  });

  it('only ever sees the two states the infra repo can reach', () => {
    expect(infraNeedsCreate(row({ name: 'agp-runtime-infra', state: 'registered_present' }))).toBe(false);
    expect(infraNeedsCreate(row({ name: 'agp-runtime-infra', state: 'registered_missing' }))).toBe(false);
  });
});

describe('wireFlagNote: one sentence per combination of the two wire flags', () => {
  it('never pairs "does not authorise the runtime-infra repo" with an infra overwrite', () => {
    const both = wireFlagNote({ overwrite: true, overwriteInfra: true }) ?? '';
    expect(both).toMatch(/two separate overwrites/i);
    expect(both).toMatch(/registered templates/i);
    expect(both).toMatch(/runtime-infra/i);
    expect(both).not.toMatch(/does not authorise/i);
  });

  it('states each single flag with its own scope, and nothing when neither is sent', () => {
    expect(wireFlagNote({ overwrite: true, overwriteInfra: false })).toMatch(/left as they are/);
    expect(wireFlagNote({ overwrite: false, overwriteInfra: true })).toMatch(/never a template/);
    expect(wireFlagNote({ overwrite: false, overwriteInfra: false })).toBeNull();
  });
});

describe('rolloutActionLabel: the result vocabulary, total over a wire string', () => {
  it('labels every action the backend can report', () => {
    for (const action of [
      'created',
      'overwritten',
      'unchanged',
      'recreated',
      'skipped',
      'adopted',
      'failed',
    ] as const) {
      expect(rolloutActionLabel(action).label.length).toBeGreaterThan(0);
      expect(rolloutActionLabel(action).cls.length).toBeGreaterThan(0);
    }
  });

  it('tints a failed infra push rose, not the neutral slate of a skip', () => {
    const failed = rolloutActionLabel('failed');
    expect(failed.known).toBe(true);
    expect(failed.cls).toContain('rose');
    expect(failed.cls).not.toContain('emerald');
    expect(rolloutActionLabel('skipped').cls).toContain('slate');
  });

  it('degrades an unknown word to the word itself instead of throwing: the receipt for writes that already happened must render', () => {
    const view = rolloutActionLabel('quarantined');
    expect(view.label).toBe('quarantined');
    expect(view.cls.length).toBeGreaterThan(0);
    expect(view.known).toBe(false);
  });

  it('reports a pinned word as known, the discriminator a label alone cannot carry', () => {
    expect(rolloutActionLabel('created').known).toBe(true);
    expect(rolloutActionLabel('skipped').known).toBe(true);
  });

  it('renders an unknown word muted: slate, since it must claim neither success nor failure', () => {
    const view = rolloutActionLabel('something-new');
    expect(view.cls).toContain('slate');
    expect(view.cls).not.toContain('emerald');
    expect(view.cls).not.toContain('rose');
  });

  it('survives the empty string and prototype member names: the key is server-supplied, so the table is a Map', () => {
    for (const key of ['toString', 'constructor', '__proto__', 'hasOwnProperty']) {
      expect(rolloutActionLabel(key).known, key).toBe(false);
      expect(typeof rolloutActionLabel(key).cls).toBe('string');
    }
    expect(rolloutActionLabel('').label.length).toBeGreaterThan(0);
  });

  it('distinguishes recreated from created and from overwritten: three facts, three words', () => {
    const words = new Set([
      rolloutActionLabel('created').label,
      rolloutActionLabel('recreated').label,
      rolloutActionLabel('overwritten').label,
    ]);
    expect(words.size).toBe(3);
  });

  it('a re-push that wrote no commit reads "Already up to date", not "Re-pushed"', () => {
    const view = rolloutActionLabel('unchanged');
    expect(view.known).toBe(true);
    expect(view.label).toBe('Already up to date');
    expect(view.label).not.toBe(rolloutActionLabel('overwritten').label);
    expect(view.cls).toContain('slate');
  });

  it('never says an overwritten row was destroyed or replaced: a re-push is one idempotent commit on top', () => {
    const label = rolloutActionLabel('overwritten').label.toLowerCase();
    expect(label).not.toContain('replac');
    expect(label).not.toContain('destroy');
    expect(label).not.toContain('delet');
  });
});

describe('repushConfirm — a destructive-adjacent action states what it writes', () => {
  const text = repushConfirm('strands-agentcore');

  it('names the repository and the single commit it adds', () => {
    expect(text).toContain('strands-agentcore');
    expect(text).toMatch(/one commit|a single commit/i);
  });

  it('states that history is preserved and nothing is deleted', () => {
    expect(text).toMatch(/history/i);
    expect(text).not.toMatch(/\breplaces the .*contents\b/i);
    expect(text).not.toMatch(/permanently|destroy/i);
  });

  it('does not claim the repository is deleted or recreated', () => {
    expect(text.toLowerCase()).not.toContain('deleted');
  });
});

describe('recreateConfirm: the record exists, the repository behind it does not', () => {
  const text = recreateConfirm('strands-agentcore');

  it('names the repository, says it no longer exists in the org and that the record is kept', () => {
    expect(text).toContain('strands-agentcore');
    expect(text).toMatch(/no longer exists/i);
    expect(text).toMatch(/record is kept/i);
  });

  it('promises no deletion or replacement, since nothing is at risk', () => {
    expect(text.toLowerCase()).not.toContain('delet');
    expect(text.toLowerCase()).not.toContain('replac');
    expect(text.toLowerCase()).not.toContain('overwrit');
  });
});

describe('deregisterConfirm: the record goes, the repository is never touched', () => {
  const text = deregisterConfirm('strands-agentcore');

  it('names the repository and says the record is removed so the name can no longer be materialized', () => {
    expect(text).toContain('strands-agentcore');
    expect(text).toMatch(/removes AGP’s record/);
    expect(text).toMatch(/no longer be materialized/);
  });

  it('states that AGP never deletes the repository behind a template', () => {
    expect(text).toMatch(/never deletes the repository/);
  });
});

describe('adoptConfirm — register-as-is, with and without provenance', () => {
  it('shows what read_repo found when the row carries it', () => {
    const text = adoptConfirm(row({ name: 'x', default_branch: 'develop', head_sha: 'deadbeefcafe' }));
    expect(text).toContain('develop');
    expect(text).toContain('deadbee');
  });

  it('says what will happen, without provenance, for an org-origin row: never "unknown", never an invented branch', () => {
    const text = adoptConfirm(row({ name: 'theirs', origin: 'org', state: 'unregistered_present' }));
    expect(text).toContain('theirs');
    expect(text).toContain(ADOPT_CONFIRM_NO_PROVENANCE);
    expect(text.toLowerCase()).not.toContain('unknown');
  });

  it('promises no push and no content change, in both shapes', () => {
    for (const text of [
      adoptConfirm(row({ name: 'x', default_branch: 'main', head_sha: 'abc1234' })),
      adoptConfirm(row({ name: 'x', origin: 'org', state: 'unregistered_present' })),
    ]) {
      expect(text).toMatch(/as[- ]is|nothing is (written|pushed)|no (files|content) are/i);
      expect(text.toLowerCase()).not.toContain('overwrit');
    }
  });
});

describe('SEED_CONSENT_PROMPT — the post-finalize prompt, pinned verbatim', () => {
  it('is the exact sentence the design pins, with the org interpolated', () => {
    expect(SEED_CONSENT_PROMPT('acme-corp')).toBe(
      'Seed starter templates into acme-corp? Nothing is created until you confirm below.',
    );
  });

  it('promises that navigation alone executes nothing', () => {
    expect(SEED_CONSENT_PROMPT('org')).toContain('Nothing is created until you confirm');
  });
});

function apiFailure({ status, code, detail = '', message = detail }: { status: number; code: ErrorCode | null; detail?: string; message?: string }): ApiFailure {
  return new ApiFailure(status, code, detail, message);
}

const ROUTE_REFUSALS: Record<RolloutErrorKind, ApiFailure> = {
  not_found: apiFailure({ status: 404, code: 'rollout.not_found', detail: 'Unknown base template' }),
  repo_not_found: apiFailure({ status: 404, code: 'rollout.repo_not_found', detail: 'Repository not found in the org' }),
  conflict: apiFailure({ status: 409, code: 'rollout.conflict', detail: 'Template already registered' }),
  rollout_error: apiFailure({ status: 502, code: 'rollout.rollout_error', detail: 'Template rollout failed' }),
  validation: apiFailure({ status: 422, code: 'rollout.validation', detail: 'Invalid template name or connection id' }),
};

describe('ROLLOUT_ERROR_CODES — typed on the generated ErrorCode', () => {
  it('satisfies Record<RolloutErrorKind, readonly ErrorCode[]>, so a renamed backend code fails tsc where this table names it', () => {
    expectTypeOf(ROLLOUT_ERROR_CODES).toExtend<Record<RolloutErrorKind, readonly ErrorCode[]>>();
  });

  it('names at least one code per kind and no code under two kinds', () => {
    const all = Object.values(ROLLOUT_ERROR_CODES).flat();
    for (const codes of Object.values(ROLLOUT_ERROR_CODES)) expect(codes.length).toBeGreaterThan(0);
    expect(new Set(all).size).toBe(all.length);
  });

  it('keys rollout_error on both provider faults: the rollout service\'s own and the repo service\'s', () => {
    expect(classifyRolloutFailure(ROUTE_REFUSALS.rollout_error)).toBe('rollout_error');
    expect(
      classifyRolloutFailure(apiFailure({ status: 502, code: 'github_repo.provider_error', detail: 'Template rollout failed' })),
    ).toBe('rollout_error');
  });

  it('classifies by code alone: the detail under no code is unrecognised, a reworded detail under the code is not', () => {
    expect(classifyRolloutFailure(apiFailure({ status: 409, code: null, detail: 'Template already registered' }))).toBeNull();
    expect(classifyRolloutFailure(apiFailure({ status: 409, code: 'rollout.conflict', detail: 'reworded' }))).toBe('conflict');
  });
});

describe('classifyRolloutError — the route\'s codes → a sentence and a verdict', () => {
  it('recognizes every code the route answers with', () => {
    for (const [kind, failure] of Object.entries(ROUTE_REFUSALS)) {
      expect(classifyRolloutError(failure, 'fallback').kind, kind).toBe(kind);
    }
  });

  it('treats only rollout_error as retryable: the other four answer the same to the same request', () => {
    expect(classifyRolloutError(ROUTE_REFUSALS.rollout_error, 'f').retryable).toBe(true);
    for (const kind of ['not_found', 'repo_not_found', 'conflict', 'validation'] as const) {
      expect(classifyRolloutError(ROUTE_REFUSALS[kind], 'f').retryable, kind).toBe(false);
    }
  });

  it('appends the wait hint to the retryable kind only', () => {
    const retryable = classifyRolloutError(ROUTE_REFUSALS.rollout_error, 'f').message;
    const terminal = classifyRolloutError(ROUTE_REFUSALS.conflict, 'f').message;
    expect(retryable).toMatch(/try again/i);
    expect(terminal).not.toMatch(/try again/i);
  });

  it('tells 404-repo apart from 404-seed in the sentence, not only in the kind', () => {
    const seed = classifyRolloutError(ROUTE_REFUSALS.not_found, 'f').message;
    const repo = classifyRolloutError(ROUTE_REFUSALS.repo_not_found, 'f').message;
    expect(seed).not.toBe(repo);
    expect(repo).toMatch(/repositor/i);
  });

  it('points a conflict at the rule that refused, not just at "already registered"', () => {
    const msg = classifyRolloutError(ROUTE_REFUSALS.conflict, 'f').message;
    expect(msg).toMatch(/already|platform|agent/i);
  });

  it('names the name rule for a validation failure', () => {
    const msg = classifyRolloutError(ROUTE_REFUSALS.validation, 'f').message;
    expect(msg).toMatch(/lowercase/i);
  });

  it('passes the raw message through, terminal, for anything unrecognised: an unknown failure is more likely a bug than contention', () => {
    const view = classifyRolloutError(new Error('HTTP 500'), 'fallback');
    expect(view.kind).toBeNull();
    expect(view.retryable).toBe(false);
    expect(view.message).toBe('HTTP 500');
    const other = classifyRolloutError(
      apiFailure({ status: 404, code: 'connection.not_found', detail: 'Connection not found' }),
      'fallback',
    );
    expect(other.kind).toBeNull();
    expect(other.message).toBe('Connection not found');
  });

  it('uses the fallback for an empty or non-Error rejection', () => {
    expect(classifyRolloutError(new Error(''), 'fallback').message).toBe('fallback');
    expect(classifyRolloutError(new Error('   '), 'fallback').message).toBe('fallback');
    expect(classifyRolloutError(null, 'fallback').message).toBe('fallback');
    expect(classifyRolloutError(undefined, 'fallback').message).toBe('fallback');
  });

  it('shows the interceptor’s 401 copy as sent when the load state carries its codeless 401 failure', () => {
    const state = failureOf(new ApiFailure(401, null, undefined, AUTH_401_HALT_MESSAGE), 'fallback');
    expect(state.failure).toMatchObject({ status: 401, code: null, message: AUTH_401_HALT_MESSAGE });
    expect(classifyRolloutError(state.failure ?? state.message, 'fallback')).toEqual({
      message: AUTH_401_HALT_MESSAGE,
      retryable: false,
      kind: null,
    });
    expect(stripComments(rolloutModalSrc)).toContain(
      'classifyRolloutError(viewLoad.state.failure ?? viewLoad.state.message, RECONCILE_FALLBACK)',
    );
  });
});

describe('deregister failures belong to the catalog route\'s classifier: a 503 store fault is retryable there and unknown to the rollout one', () => {
  const CATALOG_REFUSALS = [
    apiFailure({ status: 404, code: 'github_template.not_found', detail: 'Template not found' }),
    apiFailure({ status: 503, code: 'github_template.store_error', detail: 'Template catalog is temporarily unavailable' }),
    apiFailure({ status: 502, code: 'github_template.github_error', detail: 'GitHub template operation failed' }),
    apiFailure({ status: 422, code: 'github_template.invalid_input', detail: 'Invalid template metadata' }),
    apiFailure({ status: 422, code: 'github_template.invalid_zip', detail: 'Invalid template zip' }),
  ];

  it('the rollout classifier recognises none of the catalog route\'s codes', () => {
    for (const failure of CATALOG_REFUSALS) {
      expect(classifyRolloutError(failure, 'f').kind, failure.message).toBeNull();
    }
  });

  it('the catalog classifier does recognize them, and calls the store fault retryable', () => {
    for (const failure of CATALOG_REFUSALS) {
      expect(classifyTemplateError(failure, 'f').kind, failure.message).not.toBeNull();
    }
    const store = classifyTemplateError(CATALOG_REFUSALS[1], 'f');
    expect(store.retryable).toBe(true);
    expect(store.message).toMatch(/try again/i);
  });

  it('and the two tables are disjoint, so neither route can claim the other\'s errors', () => {
    const rollout = new Set<string>(Object.values(ROLLOUT_ERROR_CODES).flat());
    for (const code of Object.values(TEMPLATE_ERROR_CODES).flat()) expect(rollout.has(code), code).toBe(false);
    for (const failure of Object.values(ROUTE_REFUSALS)) {
      expect(classifyTemplateError(failure, 'f').kind, failure.message).toBeNull();
    }
  });

  it('the modal hands its deregister handler the catalog classifier, and the failure itself rather than a rewrapped message', () => {
    const src = stripComments(rolloutModalSrc);
    expect(src).toContain('classifyTemplateError');
    expect(src).not.toMatch(/classifyRolloutError\(new Error\(/);
    const handler = src.slice(src.indexOf('const runDeregister'), src.indexOf('const askConfirm'));
    expect(handler.length).toBeGreaterThan(200);
    expect(handler).toContain('githubTemplatesApi.remove');
    expect(handler).toMatch(/classifyTemplateError\(/);
    expect(handler).not.toMatch(/classifyRolloutError\(/);
  });
});

describe('the cost model — which surfaces pay for a provider call', () => {
  it('leaves the Templates page free of any reconcile, rollout or adopt call: provider calls happen only on the modal\'s open and Refresh', () => {
    const pages = import.meta.glob<string>('./Templates.tsx', {
      query: '?raw',
      import: 'default',
      eager: true,
    });
    const src = stripComments(pages['./Templates.tsx']);
    expect(typeof src).toBe('string');
    expect(src.length).toBeGreaterThan(1000);
    expect(src).not.toMatch(/rolloutApi\s*\.\s*(reconcile|rollout|adopt)\s*\(/);
  });

  it('keeps the Templates page\'s api call set closed: three registry reads and three registry writes, no provider call', () => {
    const src = stripComments(
      import.meta.glob<string>('./Templates.tsx', {
        query: '?raw',
        import: 'default',
        eager: true,
      })['./Templates.tsx'],
    );
    const apiCalls = [...src.matchAll(/\b(\w+Api)\s*\n?\s*\.\s*(\w+)\s*\(/g)].map(
      (m) => `${m[1]}.${m[2]}`,
    );
    const distinct = [...new Set(apiCalls)].sort();
    expect(distinct).toEqual([
      'connectionsApi.list',
      'githubTemplatesApi.list',
      'githubTemplatesApi.patch',
      'githubTemplatesApi.remove',
      'githubTemplatesApi.upload',
      'infraTemplatesApi.list',
    ]);
  });
});

describe('the deleted wire fields exists_in_org and base_templates survive nowhere in src/ code', () => {
  it('mentions neither name in any non-test .ts or .tsx, comments stripped', () => {
    const DELETED = ['exists_in_org', 'base_templates'];
    const sources = Object.entries(
      import.meta.glob<string>('../../**/*.{ts,tsx}', { query: '?raw', import: 'default', eager: true }),
    ).filter(([path]) => !path.endsWith('.test.ts') && !path.endsWith('.test.tsx'));
    expect(sources.length).toBeGreaterThan(NON_TEST_SOURCE_FLOOR);
    expect(sources.map(([p]) => p).filter((p) => p.endsWith('api/client.ts'))).toHaveLength(1);
    for (const [path, raw] of sources) {
      const src = stripComments(raw);
      for (const name of DELETED) {
        expect(src, `${path} still mentions the deleted field ${name}`).not.toContain(name);
      }
    }
  }, 30_000);
});
