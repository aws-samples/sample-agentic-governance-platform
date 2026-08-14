// reconcileView.test.ts — the reconcile surface's decisions, as UNITS (E28C/T6).
//
// WHY THESE ARE THE TESTS THAT EXIST. vitest here collects `src/**/*.test.ts` in a node
// environment with NO jsdom, so nothing in `RolloutTemplatesModal.tsx` can be mounted, clicked
// or rendered by any test in this project. Every judgement the surface makes therefore lives in
// `reconcileView.ts` as a pure function, and this file tests the function's OUTPUT for given
// inputs.
//
// E28B's lesson (nine vacuous guards) is the shape rule here: a test that asserts a constant's
// text back to itself, or greps the source for a class name, proves nothing about behaviour. So
// there are no source-shape assertions below EXCEPT the two deliberate sweeps at the bottom —
// the deleted-field grep and the page-load fetch set — which are testing an ABSENCE across files
// that no unit call can reach, and both are proven able to fail.

import { describe, expect, it } from 'vitest';

import {
  ADOPT_CONFIRM_NO_PROVENANCE,
  ADOPT_NAME_RULE_HINT,
  PAGE_LOAD_PROVIDER_CALLS,
  RECONCILE_STATE_LABEL,
  RECONCILE_STATE_TONE,
  RECONCILE_EFFECT_DEPS,
  SEED_CONSENT_PROMPT,
  adoptConfirm,
  batchConfirmLines,
  batchConfirmTitle,
  batchPlan,
  classifyRolloutError,
  infraIntentFor,
  infraNeedsCreate,
  isAdoptableName,
  reconcileRowModel,
  reconcileRows,
  repushConfirm,
  rolloutActionLabel,
  toggleQueuedAction,
  type BatchPlan,
  type InfraIntent,
  type ReconcileAction,
  type ReconcileItemLike,
  type RolloutErrorKind,
} from './reconcileView.ts';
// The DEREGISTER classifier (F3). Deregister is `githubTemplatesApi.remove` — a different route with
// its own `detail` literals — so the reconcile surface must classify its failures with that route's
// own classifier. Imported here to pin that the two classifiers do NOT overlap.
import { classifyTemplateError } from './templatesView.ts';
import rolloutModalSrc from './RolloutTemplatesModal.tsx?raw';

// --- Row fixtures ----------------------------------------------------------
// Shaped exactly like `ReconcileItemView` on the wire (connections.py:463-481).

const row = (over: Partial<ReconcileItemLike> = {}): ReconcileItemLike => ({
  name: 'strands-agentcore',
  origin: 'seed',
  state: 'seed_absent',
  default_branch: null,
  head_sha: null,
  ...over,
});

/**
 * A SELECTION, as the surface now holds it: name → the verb that was visible when it was ticked.
 *
 * It was a `Set<string>` of bare names, and that is the T6-L3 defect in one type: a tick with no
 * verb cannot be checked against the verb the row offers NOW, so a create tick silently became a
 * re-push consent across a Refresh. Written as a helper because every `batchPlan` call below needs
 * one, and a literal `new Map([...])` at each site would bury the interesting argument.
 */
const q = (...entries: [string, ReconcileAction][]): ReadonlyMap<string, ReconcileAction> =>
  new Map(entries);

/**
 * The modal source with its COMMENTS REMOVED, for the source assertions below.
 *
 * Necessary, not tidy, and the deregister-classifier test at the bottom of this file already names
 * the reason in its own words: this codebase documents a fix by QUOTING the wrong thing it replaced.
 * `RolloutTemplatesModal.tsx` explains the retired infra sentence by reciting it, so an assertion
 * that the retired wording is gone fails on correct code unless the prose is stripped first — which
 * is a test that punishes the comment rather than the copy. The same trap caught this test twice.
 *
 * `templates.test.ts:657` is the same helper for the same reason; duplicated rather than exported
 * because a shared test util that two files import is a third thing to keep in sync for four lines.
 */
const stripComments = (src: string): string =>
  src
    .replace(/\/\*[\s\S]*?\*\//g, '')
    .split('\n')
    .filter((line) => !line.trim().startsWith('//'))
    .join('\n');

describe('reconcileRowModel — state → the ONE honest action', () => {
  it('seed_absent offers create, and nothing else', () => {
    const m = reconcileRowModel(row({ state: 'seed_absent' }));
    expect(m.actions).toEqual(['create']);
  });

  it('unregistered_present offers adopt, and NEVER create or repush', () => {
    // The state that cost a live repository in the 2026-08-04 test. Rollout refuses it
    // server-side (`_rollout_template` returns action="skipped" naming adopt), so a surface
    // that offered "create" here would be offering a click the backend is guaranteed to refuse.
    const m = reconcileRowModel(row({ name: 'their-repo', origin: 'org', state: 'unregistered_present' }));
    expect(m.actions).toEqual(['adopt']);
    expect(m.actions).not.toContain('create');
    expect(m.actions).not.toContain('repush');
  });

  it('registered_present on a SEED row offers repush only', () => {
    const m = reconcileRowModel(row({ state: 'registered_present' }));
    expect(m.actions).toEqual(['repush']);
  });

  it('registered_present on a REGISTRY row offers NO action — there is no seed to push', () => {
    // An uploaded or adopted template has no scaffold on disk. `_rollout_template` raises
    // `not_found` ("Unknown base template") for such a name, so "Re-push seed" would be an
    // affordance whose every click 404s. In sync, with nothing to do, is a legitimate row.
    const m = reconcileRowModel(row({ name: 'uploaded-thing', origin: 'registry', state: 'registered_present' }));
    expect(m.actions).toEqual([]);
  });

  it('registered_missing on a SEED row offers recreate THEN deregister, in that order', () => {
    // Order is the recommendation: the repo is gone and AGP ships the bytes to rebuild it, so
    // re-create is the constructive answer and deregister the giving-up one.
    const m = reconcileRowModel(row({ state: 'registered_missing' }));
    expect(m.actions).toEqual(['recreate', 'deregister']);
  });

  it('registered_missing on a REGISTRY row offers deregister ONLY', () => {
    // Same reasoning as the repush gate: there is no seed on disk to re-create from, so the
    // only honest action is to drop the record that points at nothing.
    const m = reconcileRowModel(row({ origin: 'registry', state: 'registered_missing' }));
    expect(m.actions).toEqual(['deregister']);
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
    // Colour is not the only channel (labels above carry it too), but it must not LIE: a row
    // whose repo is gone must not wear the same tint as a row that is in sync.
    expect(RECONCILE_STATE_TONE.registered_present).not.toBe(RECONCILE_STATE_TONE.registered_missing);
    expect(RECONCILE_STATE_TONE.registered_present).not.toBe(RECONCILE_STATE_TONE.unregistered_present);
  });

  it('never labels an unregistered_present row as ours', () => {
    // The whole point of the state: this repository is in the org and AGP has no record of it.
    // A label reading "registered" / "in sync" / "template" here would be the boolean's lie
    // restated in prose.
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

  it('renders the branch alone for a repo that exists with no commit yet', () => {
    // `head_sha` is null for a present-but-empty repo (`_head_or_none` maps "" → null). The row
    // must still read as present; the missing sha must not turn into "unknown" or "—@—".
    const m = reconcileRowModel(row({ state: 'registered_present', default_branch: 'main', head_sha: null }));
    expect(m.provenance).toBe('main');
  });

  it('renders NO provenance for an org-origin adopt row', () => {
    // By the ruled cost model: org rows come out of the listing and are deliberately not
    // re-probed, so both fields are null PRE-POST. Rendering "unknown" would read as a fault.
    const m = reconcileRowModel(row({ origin: 'org', state: 'unregistered_present' }));
    expect(m.provenance).toBeNull();
  });

  it('renders NO provenance for a row whose repo is gone', () => {
    const m = reconcileRowModel(row({ state: 'registered_missing' }));
    expect(m.provenance).toBeNull();
  });
});

describe('isAdoptableName — the backend name rule, mirrored', () => {
  // AUTHORITY: `_NAME_RE = ^[a-z][a-z0-9-]{0,63}$` in github_template_service.py. `adopt`
  // applies it via `_require_valid_name` BEFORE anything else, so a listed org repo whose name
  // fails it 422s. These cases are the rule's edges, not a restatement of the regex.
  it('accepts lowercase alphanumerics and hyphens after a leading letter', () => {
    expect(isAdoptableName('strands-agentcore')).toBe(true);
    expect(isAdoptableName('a')).toBe(true);
    expect(isAdoptableName('a1-2-3')).toBe(true);
  });

  it('refuses the shapes GitHub allows and AGP does not', () => {
    // Every one of these is a legal GitHub repo name, which is exactly why the annotation
    // exists: such a repo is LISTED by reconcile and will 422 on adopt.
    expect(isAdoptableName('MyRepo')).toBe(false);       // uppercase
    expect(isAdoptableName('my_repo')).toBe(false);      // underscore
    expect(isAdoptableName('my.repo')).toBe(false);      // dot
    expect(isAdoptableName('1repo')).toBe(false);        // leading digit
    expect(isAdoptableName('-repo')).toBe(false);        // leading hyphen
    expect(isAdoptableName('')).toBe(false);
    expect(isAdoptableName('a'.repeat(65))).toBe(false); // 1 + 64 > the 64-char cap
  });

  it('accepts exactly 64 characters and refuses 65', () => {
    // `{0,63}` after the leading letter — an off-by-one here would either annotate a legal
    // repo as un-adoptable or let an illegal one through to a 422.
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

  it('does not annotate a NON-adopt row even when its name would fail the rule', () => {
    // A registered template with a legacy name is not being adopted, so the adopt rule has no
    // bearing on it — annotating it would be a warning about an action the row does not offer.
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

describe('queueAction — which rows the rollout BATCH can carry', () => {
  // The rollout route is a BATCH endpoint taking `template_names` + one `overwrite`, so the three
  // actions that call it are selections, not per-row executions. Adopt and deregister are
  // single-repo verbs on other routes, so they are not queueable and keep their own confirms.
  it('marks create, repush and recreate as the queueing actions', () => {
    expect(reconcileRowModel(row({ state: 'seed_absent' })).queueAction).toBe('create');
    expect(reconcileRowModel(row({ state: 'registered_present' })).queueAction).toBe('repush');
    expect(reconcileRowModel(row({ state: 'registered_missing' })).queueAction).toBe('recreate');
  });

  it('leaves adopt-only and deregister-only rows unqueueable', () => {
    expect(
      reconcileRowModel(row({ origin: 'org', state: 'unregistered_present' })).queueAction,
    ).toBeNull();
    // A registry-origin missing row offers deregister ONLY — a different route entirely.
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
    // An illegally-named org repo is refused for ADOPT; that must not accidentally make it look
    // like something the rollout batch could take instead.
    const m = reconcileRowModel(row({ name: 'Bad_Name', origin: 'org', state: 'unregistered_present' }));
    expect(m.queueAction).toBeNull();
    expect(m.actionDisabled).toBe(true);
  });
});

describe('batchPlan — what the wire carries, and the two overwrite flags', () => {
  // THE F1 REGRESSION TEST. The first cut of this surface kept a `confirmedRepushes` set whose only
  // writer was a stale-prune filter, so no interaction could ever add to it: the batch was always
  // just the creates, the footer always sent `overwrite=false`, and the "Queued" pill was
  // unreachable dead markup. The review proved it by deleting that branch with every test still
  // green — the E28B I-1 shape. The decision below is testable BECAUSE it lives here.
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

  it('SETS overwrite when a re-push is queued — the flag F1 could never set', () => {
    const plan = batchPlan(ROWS, q(['insync', 'repush']), 'none');
    expect(plan.names).toEqual(['insync']);
    expect(plan.overwrite).toBe(true);
    expect(plan.repushes).toEqual(['insync']);
  });

  it('carries creates and re-pushes together, with overwrite set by the re-push', () => {
    // The design intent the half-built state never reached: one batch, both kinds, and the single
    // wire flag decided by whether any member needs it.
    const plan = batchPlan(ROWS, q(['fresh', 'create'], ['insync', 'repush']), 'none');
    expect(plan.names).toEqual(['fresh', 'insync']);
    expect(plan.overwrite).toBe(true);
    expect(plan.creates).toEqual(['fresh']);
    expect(plan.repushes).toEqual(['insync']);
  });

  it('does NOT set overwrite for a re-create — the repo is gone, so nothing is written over', () => {
    // `_rollout_template` reaches `recreated` on `registered_missing` regardless of the flag, and
    // sending `overwrite=true` would additionally authorise re-pushing every OTHER registered
    // present template in the same batch. The flag is not a mood; it is scoped consent.
    const plan = batchPlan(ROWS, q(['gone', 'recreate']), 'none');
    expect(plan.names).toEqual(['gone']);
    expect(plan.overwrite).toBe(false);
    expect(plan.recreates).toEqual(['gone']);
  });

  it('emits names in ROW order, not in the map\'s insertion order', () => {
    // A `Map` iterates by insertion, so a body built from it would vary with click order. The wire
    // body should be a function of the org's state and the selection, nothing else.
    const plan = batchPlan(ROWS, q(['gone', 'recreate'], ['fresh', 'create'], ['insync', 'repush']), 'none');
    expect(plan.names).toEqual(['fresh', 'insync', 'gone']);
  });

  it('SELF-PRUNES a queued name whose row no longer offers that action', () => {
    // This replaces the stale-prune effect the old code ran on every reconcile. A name queued
    // before a Refresh may come back in a different state — queued as a create, now present and
    // unregistered — and submitting it then would be acting on a comparison the operator never saw.
    const plan = batchPlan(
      ROWS,
      q(['fresh', 'create'], ['vanished', 'create'], ['theirs', 'create'], ['uploaded', 'repush']),
      'none',
    );
    expect(plan.names).toEqual(['fresh']);
  });

  it('can never carry an unregistered_present name, even when it is queued', () => {
    // The client half of the server's refusal, now enforced where it is testable. Rollout answers
    // this state with action="skipped" and a reason naming adopt, whatever `overwrite` says.
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

  it('is a pure function of (rows, queued, overwriteInfra) — no hidden ordering state', () => {
    const a = batchPlan(ROWS, q(['insync', 'repush'], ['fresh', 'create']), 'none');
    const b = batchPlan(ROWS, q(['fresh', 'create'], ['insync', 'repush']), 'none');
    expect(a).toEqual(b);
  });
});

describe('batchPlan — PROPERTY 4: a tick carries the VERB it was placed against (E28D)', () => {
  // THE T6-L3 REGRESSION TEST. `queued` used to be a `Set<string>` of NAMES with no verb, so the
  // prune at `batchPlan` could only notice a row that had lost its action ENTIRELY — never one whose
  // action CHANGED. The live-test scenario, from E28C's progress notes:
  //
  //   1. `seed_absent` row → the tick is placed on a CREATE ("nothing can be written over").
  //   2. Someone creates the repo (or an earlier partial run did). The operator hits Refresh.
  //   3. The row returns as `registered_present` → its action is now REPUSH, and the tick survived.
  //   4. `batchPlan` classified it as a re-push ⇒ `overwrite: true` ON THE WIRE, from a tick the
  //      operator placed on a create.
  //
  // So the consent for the epic's most dangerous flag was manufactured by a state change the
  // operator never saw. `reconcileView.ts` CLAIMED the plan self-prunes so that submitting could
  // never "act on a comparison the operator never saw" — property 3 — and this was the case that
  // falsified the claim. The tick now carries its verb, and a mismatch drops it.
  const CREATE_ROWS = reconcileRows([row({ name: 'strands-agentcore', state: 'seed_absent' })]);
  const NOW_PRESENT = reconcileRows([
    row({
      name: 'strands-agentcore',
      state: 'registered_present',
      default_branch: 'main',
      head_sha: 'abc1234',
    }),
  ]);
  /** The tick as the operator placed it in step 1: against a CREATE. */
  const TICKED_AS_CREATE = q(['strands-agentcore', 'create']);

  it('carries the tick while the verb still matches', () => {
    const plan = batchPlan(CREATE_ROWS, TICKED_AS_CREATE, 'none');
    expect(plan.names).toEqual(['strands-agentcore']);
    expect(plan.creates).toEqual(['strands-agentcore']);
  });

  it('DROPS the tick when the row comes back offering a different verb', () => {
    const plan = batchPlan(NOW_PRESENT, TICKED_AS_CREATE, 'none');
    expect(plan.names).toEqual([]);
    expect(plan.empty).toBe(true);
  });

  it('and therefore NEVER escalates a create tick into an overwrite', () => {
    // The consequence that makes this a safety property rather than a tidiness one. Before the fix
    // this exact call returned `overwrite: true`.
    const plan = batchPlan(NOW_PRESENT, TICKED_AS_CREATE, 'none');
    expect(plan.overwrite).toBe(false);
    expect(plan.repushes).toEqual([]);
  });

  it('carries a re-push that was ticked AS a re-push — the fix is not a blanket refusal', () => {
    // The other direction, pinned so "drop everything on any change" cannot pass as this fix: a
    // deliberate re-push tick must still reach the wire and must still set the flag.
    const plan = batchPlan(NOW_PRESENT, q(['strands-agentcore', 'repush']), 'none');
    expect(plan.names).toEqual(['strands-agentcore']);
    expect(plan.overwrite).toBe(true);
  });

  it('reports every dropped tick, so the surface can SAY the selection changed', () => {
    // A silent drop is the failure mode of the stale-prune effect T6 deleted: the operator's tick
    // vanishes and the run does less than they authorised, with nothing said. Reported as a
    // DECISION here rather than derived in the `.tsx`, where no test could reach it.
    const ROWS = reconcileRows([
      row({ name: 'fresh', state: 'seed_absent' }),
      row({ name: 'insync', state: 'registered_present', default_branch: 'main', head_sha: 'abc1234' }),
      row({ name: 'theirs', origin: 'org', state: 'unregistered_present' }),
    ]);
    const plan = batchPlan(
      ROWS,
      // carried · verb changed · row lost its action · gone from the org entirely
      q(['fresh', 'create'], ['insync', 'create'], ['theirs', 'create'], ['vanished', 'create']),
      'none',
    );
    expect(plan.names).toEqual(['fresh']);
    expect(plan.dropped).toEqual(['insync', 'theirs', 'vanished']);
  });

  it('reports NO drops when every tick still matches', () => {
    // The negative half: a notice that fires on a normal Refresh would be trained away in a week.
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

describe('batchPlan — the infra consent is the THIRD argument, not a derivation (E28D)', () => {
  const ROWS = reconcileRows([
    row({ name: 'fresh', state: 'seed_absent' }),
    row({ name: 'insync', state: 'registered_present', default_branch: 'main', head_sha: 'abc1234' }),
  ]);

  it('leaves overwriteInfra false when the operator has not ticked it', () => {
    expect(batchPlan(ROWS, q(['fresh', 'create']), 'none').overwriteInfra).toBe(false);
  });

  it('does NOT let a template re-push authorise the infra push — the T8 split, client-side', () => {
    // THE WHOLE POINT OF THE SPLIT. `overwrite` had ONE value and TWO consumers: the backend handed
    // the templates' flag to `_ensure_infra`, so ticking a re-push on `strands-agentcore` pushed
    // AGP's Terraform module over the org's existing `agp-runtime-infra` and reported
    // "overwritten" — a write nobody asked for. The two flags are now independent in both
    // directions, and this is the direction that was the defect.
    const plan = batchPlan(ROWS, q(['insync', 'repush']), 'none');
    expect(plan.overwrite).toBe(true);
    expect(plan.overwriteInfra).toBe(false);
  });

  it('carries the infra consent alone, with no template queued at all', () => {
    // The submittable run the footer's third disjunct exists for: re-push the infra module and
    // nothing else.
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

  it('NEVER sends overwrite_infra for a repo that is not there — the review\'s Critical 1', () => {
    // THE WIRE-LEVEL ASSERTION, and the review named why it was missing: the old test proved the
    // INTENT was 'create' for an absent repo and stopped one function short of the BODY, so the
    // suite was green while `batchPlan` carried the raw checkbox straight through.
    //
    // The reachable path needs no code editing. Tick the box while the repo is PRESENT → hit
    // Refresh → the repo has since been deleted, so the row returns `seed_absent` and the checkbox
    // UNMOUNTS (it renders in the present branch only). Nothing was clearing the state, so the
    // surface held a consent the operator could no longer see or untick — and `_ensure_infra` probes
    // at SUBMIT time, so a repo recreated between that read and the click would be pushed over under
    // it. That is "a write nobody named", which is the defect T8's split exists to delete.
    const intent = infraIntentFor(row({ name: 'agp-runtime-infra', state: 'seed_absent' }), true);
    expect(intent).toBe('create');
    const plan = batchPlan(ROWS, q(), intent);
    expect(plan.overwriteInfra).toBe(false);
  });

  it('and the RE-READ clears the infra tick, so no consent outlives its checkbox', () => {
    // A SOURCE assertion, and it earned its place the hard way: removing this reset broke NO test in
    // the first fix round, because it is an effect in the `.tsx` and no unit call can reach it. The
    // pure functions above make a stale tick HARMLESS on the wire (Critical 1's first half); this is
    // the half that stops the surface holding an invisible consent at all.
    //
    // Scoped to the reconcile effect specifically — anchored between the `rolloutApi.reconcile(` call
    // and the `refetch` declaration that follows it — because the same call appears in `runRollout`'s
    // post-run reset, which is a DIFFERENT rule (a consent authorises one write). A bare
    // `toContain` would be satisfied by that one and would pass with this reset deleted.
    const src = stripComments(rolloutModalSrc);
    const effect = src.slice(
      src.indexOf('.reconcile(connection.id)'),
      src.indexOf('const refetch'),
    );
    expect(effect.length).toBeGreaterThan(200);
    expect(effect).toMatch(/setOverwriteInfra\(false\)/);
  });

  it('and the confirm body agrees with the title in that case', () => {
    // The same defect's copy half, which a wire-only fix would have left behind: with the raw tick
    // the title read "Create the runtime infra repo" while the body directly beneath it read "you
    // have also authorised re-pushing the module". A consent gate contradicting itself is this
    // epic's whole defect class, so the title, the body and the wire now key off ONE value.
    const intent = infraIntentFor(row({ name: 'agp-runtime-infra', state: 'seed_absent' }), true);
    const infraLine = batchConfirmLines(ROWS, q(), intent).filter((l) => /runtime-infra/i.test(l));
    expect(infraLine).toHaveLength(1);
    expect(infraLine[0]).not.toMatch(/authorised re-pushing/i);
    expect(batchConfirmTitle(batchPlan(ROWS, q(), intent), intent, 'myorg')).toMatch(/creat/i);
  });
});

describe('toggleQueuedAction — one click does what the box shows (review Critical 2)', () => {
  // THE DEAD-CLICK REGRESSION TEST. This reducer was inline in the `.tsx` and keyed on bare presence
  // (`next.has(name)`), which is why nothing caught it: a decision in that file is a decision no test
  // can reach — the founding argument of `reconcileView.ts`, applied to the last piece of logic that
  // had not moved.
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

  it('ticks a DROPPED tick in ONE call, against the row\'s new verb', () => {
    // The defect, stated as the operator's experience. The amber notice says "Re-select what you
    // still want, against the action each row offers now" — so they click the box the notice points
    // at. Its `checked` is false (the plan refuses the stale entry), but the map still holds
    // `['a','create']`, so a presence-keyed toggle DELETED it: click 1 did nothing visible and click 2
    // finally ticked. On a consent surface a control that ignores a click teaches double-clicking.
    const after = toggleQueuedAction(q(['a', 'create']), REPUSH_ROW);
    expect(after.get('a')).toBe('repush');
    // And the plan now carries it, which is what "the click worked" means end to end.
    expect(batchPlan([REPUSH_ROW], after, 'none').names).toEqual(['a']);
  });

  it('still round-trips: a second click on the re-selected row clears it', () => {
    // Toggle must remain a toggle — a fix that only ever SET would strand the operator ticked.
    const on = toggleQueuedAction(q(['a', 'create']), REPUSH_ROW);
    expect([...toggleQueuedAction(on, REPUSH_ROW)]).toEqual([]);
  });

  it('refuses to queue a row with no queueable action, unchanged', () => {
    // Adopt and deregister are single-repo verbs on other routes. Returned by REFERENCE so a caller
    // cannot mistake "nothing happened" for a new selection.
    const adoptRow = reconcileRows([row({ name: 'theirs', origin: 'org', state: 'unregistered_present' })])[0];
    const before = q(['a', 'create']);
    expect(toggleQueuedAction(before, adoptRow)).toBe(before);
  });

  it('leaves the OTHER rows\' ticks alone', () => {
    const rows = reconcileRows([
      row({ name: 'a', state: 'seed_absent' }),
      row({ name: 'b', state: 'seed_absent' }),
    ]);
    const after = toggleQueuedAction(q(['b', 'create']), rows[0]);
    expect(after.get('b')).toBe('create');
    expect(after.get('a')).toBe('create');
  });

  it('is the MODAL\'s writer, so the dead click cannot come back inline', () => {
    // A SOURCE assertion in this file's idiom: the `.tsx` must DELEGATE rather than keep its own
    // reducer, or the fix regresses in the one place tests cannot see.
    const src = stripComments(rolloutModalSrc);
    expect(src).toMatch(/toggleQueuedAction\(prev, row\)/);
    // The presence-keyed shape, specifically, must not return.
    expect(src).not.toMatch(/next\.has\(name\)/);
  });
});

describe('infraIntentFor — what the click will actually do to the forced repo (E28D)', () => {
  const infra = (over: Partial<ReconcileItemLike> = {}) =>
    row({ name: 'agp-runtime-infra', origin: 'seed', ...over });

  it('is CREATE when the repo is absent, whatever the tick says', () => {
    // Creating it is unconditional — a tenant runtime cannot deploy without it — which is exactly
    // why the checkbox does not render in this branch. Making the intent ignore the tick here means
    // a tick left over from a previous view cannot be represented as an absent-repo "re-push".
    expect(infraIntentFor(infra({ state: 'seed_absent' }), false)).toBe('create');
    expect(infraIntentFor(infra({ state: 'seed_absent' }), true)).toBe('create');
  });

  it('is NONE for a present repo the operator has not ticked', () => {
    // `_ensure_infra` returns action="skipped" and pushes nothing. "None" is the honest word.
    expect(infraIntentFor(infra({ state: 'unregistered_present', default_branch: 'main' }), false)).toBe(
      'none',
    );
  });

  it('is REPUSH for a present repo the operator DID tick', () => {
    expect(infraIntentFor(infra({ state: 'unregistered_present', default_branch: 'main' }), true)).toBe(
      'repush',
    );
  });

  it('is NONE when there is no infra row at all, even with the tick set', () => {
    // `infraNeedsCreate`'s fail-closed reading, extended: a view that could not report the repo is
    // not evidence about it, and the permissive reading would enable a submit on an unknown.
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

  /** The per-row lines, i.e. everything except the unconditional infra-repo line. */
  const rowLines = (lines: string[]) => lines.filter((l) => !/runtime-infra/i.test(l));

  /** One tick of each queueable verb, each against the verb its row actually offers. */
  const ALL_THREE = q(['fresh', 'create'], ['insync', 'repush'], ['gone', 'recreate']);

  it('states what a re-push writes, in the row\'s own line', () => {
    // The destructive-ADJACENT action. Moving execution to the footer means this sentence has to
    // travel with it — a batch that silently re-pushed would be exactly the consent gap the
    // per-row confirm used to close.
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
    // Row order, so the confirm reads down the same table the operator just looked at.
    expect(lines[0]).toContain('fresh');
    expect(lines[1]).toContain('insync');
    expect(lines[2]).toContain('gone');
  });

  it('never promises a deletion or a replacement', () => {
    // E28C/T3 deleted delete+recreate. Every line here describes an additive commit.
    // Across every infra intent, since the infra line's wording branches on it.
    for (const infraIntent of ['none', 'create', 'repush'] as const) {
      for (const line of batchConfirmLines(ROWS, ALL_THREE, infraIntent)) {
        expect(line.toLowerCase()).not.toContain('delet');
        expect(line.toLowerCase()).not.toContain('destroy');
        expect(line).not.toMatch(/replaces the .*contents/i);
      }
    }
  });

  it('names the forced infra repo, because every rollout ensures it', () => {
    // It is ensured server-side on EVERY rollout, independent of the selection. A confirm that
    // listed only the ticked rows would understate what the click does.
    expect(batchConfirmLines(ROWS, q(['fresh', 'create']), 'none').join(' ')).toMatch(
      /runtime-infra|infra repo/i,
    );
  });

  it('says the infra module is LEFT ALONE when its own box is unticked', () => {
    // E28D, and it is a correction of a correction. E28C's final fix stopped the line promising an
    // unconditional re-push, but what it promised instead was that the module is "re-pushed here
    // only when a re-push ABOVE authorises it" — a true description of the coupling that T8 then
    // DELETED. The infra push now has its own consent, so a sentence pointing at the template ticks
    // describes a wire that no longer exists.
    const infra = batchConfirmLines(ROWS, q(['insync', 'repush']), 'none').filter((l) =>
      /runtime-infra/i.test(l),
    );
    expect(infra).toHaveLength(1);
    // Still true, and still the reason the line is unconditional: the repo is ensured either way.
    expect(infra[0]).toMatch(/if this org does not have it yet|created/i);
    expect(infra[0]).toMatch(/left exactly as it is|otherwise left/i);
    expect(infra[0]).not.toMatch(/every roll-out .{0,40}re-pushes/i);
    // THE RETIRED SENTENCE. A queued template re-push (above) must no longer be described as
    // authorising anything about the infra repo.
    expect(infra[0]).not.toMatch(/only when a re-push/i);
    expect(infra[0]).not.toMatch(/authoris/i);
  });

  it('says the module IS re-pushed when the infra box is ticked, and only then', () => {
    // The other half: the tick is a real consent, so the confirm has to state the write it buys.
    const ticked = batchConfirmLines(ROWS, q(), 'repush').filter((l) => /runtime-infra/i.test(l));
    expect(ticked).toHaveLength(1);
    expect(ticked[0]).toMatch(/re-push/i);
    // Idempotence is the calming fact, and it is true: `_push` writes no commit for an unchanged
    // module. It belongs in the sentence that authorises the push, not in the one that declines it.
    expect(ticked[0]).toMatch(/idempotent|writes no commit/i);
    // And the two sentences must actually DIFFER — a line that reads the same either way would make
    // the checkbox above it decoration.
    const unticked = batchConfirmLines(ROWS, q(), 'none').filter((l) => /runtime-infra/i.test(l));
    expect(ticked[0]).not.toBe(unticked[0]);
  });

  it('lists nothing for an empty selection', () => {
    // Only the infra line, which is unconditional — there is no per-row write to describe.
    const lines = batchConfirmLines(ROWS, q(), 'none');
    expect(lines.filter((l) => /fresh|insync|gone/.test(l))).toEqual([]);
  });

  it('drops a line for a tick whose verb changed, exactly as batchPlan drops the name', () => {
    // The confirm is derived from the same plan, so property 4 has to hold here too: a line for a
    // name the request will not carry is a consent gate describing a write that will not happen.
    const lines = batchConfirmLines(ROWS, q(['insync', 'create']), 'none');
    expect(lines.filter((l) => /insync/.test(l))).toEqual([]);
  });

  it('does not let the MODAL\'s infra hint restate the overstatement this line dropped', () => {
    // A SOURCE assertion, in this file's own idiom (see the deregister-classifier test): the infra
    // row's hint is rendered inside `RolloutTemplatesModal.tsx` with no jsdom to mount, and it was
    // the SECOND copy of the same wrong sentence — the consent line and the hint both promised a
    // Terraform re-push on every roll-out, while `_ensure_infra` skips an existing repo entirely at
    // overwrite=false. Two strings for one behaviour is how the first one survived being wrong, so
    // both are pinned together, here, where the behaviour is documented.
    // Anchored on the section label and searched FORWARD from it — `actionError &&` appears three
    // times in this file, and slicing to its first occurrence yields an empty string that would
    // make every assertion below vacuous. The length check is what caught exactly that.
    // STRIPPED of comments first: the `.tsx` documents each re-wording by quoting the sentence it
    // retired, so the absences below would fail on correct code against the raw source. Asserting
    // against prose would make this test a rule about how the fix may be explained.
    const src = stripComments(rolloutModalSrc);
    const start = src.indexOf('Runtime infra repo');
    const hint = src.slice(start, src.indexOf('actionError &&', start));
    expect(hint.length).toBeGreaterThan(200);
    // The PRESENT branch: left in place unless its OWN box is ticked.
    expect(hint).toMatch(/leaves it in place/i);
    expect(hint).not.toMatch(/Every roll-out re-pushes/i);
    // E28D: the hint MOVED WITH THE WIRE. It used to say the module is re-pushed "only when a
    // re-push above authorises it", which described `overwrite` serving two consumers — the exact
    // coupling T8 deleted. Pinned as an ABSENCE because the sentence was true when it was written
    // and is now false, which is the way copy usually goes wrong in this codebase.
    expect(hint).not.toMatch(/only when a re-push/i);
    expect(hint).not.toMatch(/a re-push above/i);
    // The ABSENT branch still promises the create+seed, because that is what `_ensure_infra` does
    // when the repo is missing — the fix must not flatten a true statement into a hedge.
    expect(hint).toMatch(/Not in the org yet\..{0,60}creates it/is);
  });

  it('gives the modal\'s infra CHECKBOX the present branch only, defaulted off', () => {
    // Same idiom, same window, for the control T8's wire made possible. Two properties that no unit
    // call can reach, both of them the reason the checkbox is honest:
    //
    //  • it renders only where it can be UNTICKED. When the repo is absent, creating it is
    //    unconditional (a tenant runtime cannot deploy without it), so a box there would be a
    //    control whose off position is a lie.
    //  • its state comes from `overwriteInfra`, so the tick and the wire field are one value.
    const src = stripComments(rolloutModalSrc);
    const start = src.indexOf('Runtime infra repo');
    const section = src.slice(start, src.indexOf('actionError &&', start));
    expect(section).toContain('type="checkbox"');
    expect(section).toMatch(/checked=\{overwriteInfra\}/);
    // Gated on the SAME predicate the hint's present branch uses, so the control and the copy
    // cannot disagree about which branch they are in.
    expect(section).toMatch(/infraRow\.provenance &&/);
  });
});

describe('batchConfirmTitle — the confirm names what it is about to do (T6-L2)', () => {
  // "Roll out 0 templates — myorg" was the live-test finding: `canSubmit` deliberately allows a
  // submit with NOTHING ticked when the infra repo is absent, and the title then contradicted its
  // own body, whose single bullet was the infra line. The count was right in the FOOTER (which
  // renders a bare "Review roll-out") and wrong in the title for one structural reason — the footer
  // read a value and the title was a template literal in a file no test can read. So the title moves
  // here, where these cases are pinnable.
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
    // The templates are what the operator selected, so they stay the headline. The infra act is in
    // the body's own bullet, which is where it was always described.
    expect(batchConfirmTitle(plan({ names: ['a'], empty: false }), 'create', 'myorg')).toBe(
      'Roll out 1 template — myorg',
    );
  });

  it('NEVER says "0 templates" — the T6-L2 regression', () => {
    for (const intent of ['none', 'create', 'repush'] as const) {
      expect(batchConfirmTitle(plan(), intent, 'myorg')).not.toMatch(/\b0 templates\b/);
    }
  });

  it('names the CREATE when that is the only write the click performs', () => {
    const title = batchConfirmTitle(plan(), 'create', 'myorg');
    expect(title).toContain('myorg');
    expect(title).toMatch(/creat/i);
    expect(title).toMatch(/infra/i);
  });

  it('names the RE-PUSH when the infra tick is the only write', () => {
    // The run the infra checkbox alone makes submittable. It must not read as a create — the repo
    // is already there, and the write is a commit on top of someone's existing module.
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
    // n=0 with no infra intent is gated off by `canSubmit`, so it should not be reachable — but a
    // title function that returned '' or threw would white-screen the confirm if it ever were.
    // `rolloutActionLabel`'s F2 reasoning, applied to a title: degrade, never fail.
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

  it('is the MODAL\'s title, not a second copy of it', () => {
    // A SOURCE assertion in this file's idiom, and the reason T6-L2 existed at all: the count was
    // correct in the footer and wrong in the title because the title was a template literal in a
    // file no test can read. Every case above can pass while the surface stays wrong, so what is
    // pinned is that the `.tsx` gets its title FROM HERE.
    //
    // Two properties, and deliberately not "the call is spelled `title={batchConfirmTitle(...)}`":
    // the modal binds the result to a const so the aria label can reuse the same string, which is
    // the better shape and which a spelling assertion would have forbidden. Comments stripped, since
    // the retired literal is quoted in the prose that explains its removal.
    const src = stripComments(rolloutModalSrc);
    // 1. The function is actually CALLED (not merely imported — an unused import is a `tsc` error
    //    here, but an import used only in a type position would not be).
    expect(src).toMatch(/batchConfirmTitle\(\s*plan/);
    // 2. And no template literal builds a title from the count again. This is the exact shape that
    //    produced "Roll out 0 templates — myorg".
    expect(src).not.toMatch(/title=\{`Roll out \$\{/);
    expect(src).not.toMatch(/`Roll out \$\{plan\.names\.length/);
  });
});

describe('infraNeedsCreate — the forced repo\'s one question, in the selector (F4)', () => {
  // Moved out of the `.tsx`, where a `state ===` comparison was untestable by construction — the
  // same class as F1, just smaller.
  it('is true only when the infra repo is absent', () => {
    expect(infraNeedsCreate(row({ name: 'agp-runtime-infra', state: 'seed_absent' }))).toBe(true);
    expect(
      infraNeedsCreate(row({ name: 'agp-runtime-infra', state: 'unregistered_present', default_branch: 'main' })),
    ).toBe(false);
  });

  it('is false when there is no infra row at all', () => {
    // A view that could not report it is not evidence that it is missing — the fail-closed reading,
    // since the alternative enables a submit button on an unknown.
    expect(infraNeedsCreate(null)).toBe(false);
  });

  it('only ever sees the two states the infra repo can reach', () => {
    // `reconcile` builds this row with `registered=False` (it is NEVER registered), so
    // `seed_absent` means precisely "not in the org yet" with no other state to confuse it with.
    // Pinned so a future writer who registers the infra repo has to revisit this predicate.
    expect(infraNeedsCreate(row({ name: 'agp-runtime-infra', state: 'registered_present' }))).toBe(false);
    expect(infraNeedsCreate(row({ name: 'agp-runtime-infra', state: 'registered_missing' }))).toBe(false);
  });
});

describe('rolloutActionLabel — the result vocabulary, TOTAL over a wire string (F2)', () => {
  it('labels every action the backend can report', () => {
    // `RolloutResultItemView.action` is pinned to five values (connections.py:511). A missing
    // member would render a result row with no verdict — the outcome the operator came for.
    for (const action of ['created', 'overwritten', 'recreated', 'skipped', 'adopted'] as const) {
      expect(rolloutActionLabel(action).label.length).toBeGreaterThan(0);
      expect(rolloutActionLabel(action).cls.length).toBeGreaterThan(0);
    }
  });

  it('degrades an UNKNOWN word to the word itself instead of throwing', () => {
    // THE F2 REGRESSION TEST. The result view indexed a `Record` with a value the server types as
    // a bare `str`, so a sixth backend word yielded `undefined` and the next `.cls` threw a
    // TypeError — white-screening the whole result list rather than degrading one row. The result
    // view is the operator's RECEIPT for writes that already happened, so it is the last surface
    // that may fail to render.
    const view = rolloutActionLabel('quarantined');
    expect(view.label).toBe('quarantined');
    expect(view.cls.length).toBeGreaterThan(0);
    expect(view.known).toBe(false);
  });

  it('reports the five pinned words as known', () => {
    // `known` is the discriminator: without it "matched a pinned word" and "fell through" are
    // indistinguishable for any word whose label happens to equal itself, which is exactly how a
    // backend rename would silently start rendering raw wire values with every test green.
    expect(rolloutActionLabel('created').known).toBe(true);
    expect(rolloutActionLabel('skipped').known).toBe(true);
  });

  it('renders an unknown word MUTED — it must not claim success or failure', () => {
    // A word this build has not been taught about is not an achievement and not an error. Slate is
    // the palette's only neutral (`OPS_BADGE.unknown`'s reasoning, unchanged): emerald would
    // report a success nobody verified, rose a failure nobody observed.
    const view = rolloutActionLabel('something-new');
    expect(view.cls).toContain('slate');
    expect(view.cls).not.toContain('emerald');
    expect(view.cls).not.toContain('rose');
  });

  it('survives the empty string and prototype member names', () => {
    // The lookup key is SERVER-SUPPLIED, so the `Map` reasoning applies here too: an object
    // literal would resolve 'toString' to `Object.prototype`'s member and hand a FUNCTION to a
    // `.cls` read. And an empty action must still render a row rather than an empty pill.
    for (const key of ['toString', 'constructor', '__proto__', 'hasOwnProperty']) {
      expect(rolloutActionLabel(key).known, key).toBe(false);
      expect(typeof rolloutActionLabel(key).cls).toBe('string');
    }
    expect(rolloutActionLabel('').label.length).toBeGreaterThan(0);
  });

  it('distinguishes recreated from created and from overwritten', () => {
    // Three different facts: genuinely new, a record existed but the repo was gone, and a
    // re-push on top of a live repo. E28C/T3's review found "created" reported for a
    // repository that already existed — the word IS the audit trail.
    const words = new Set([
      rolloutActionLabel('created').label,
      rolloutActionLabel('recreated').label,
      rolloutActionLabel('overwritten').label,
    ]);
    expect(words.size).toBe(3);
  });

  it('never says an overwritten row was destroyed or replaced', () => {
    // E28C/T3 DELETED delete+recreate: "overwritten" is now an idempotent commit on top,
    // history preserved. The old copy ("Overwriting replaces the existing repository's
    // contents") described a verb that no longer exists.
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
    // The truth after T3, and the reason this sentence can be calm. The OLD copy promised a
    // replacement, which is what the code used to do.
    expect(text).toMatch(/history/i);
    expect(text).not.toMatch(/\breplaces the .*contents\b/i);
    expect(text).not.toMatch(/permanently|destroy/i);
  });

  it('does not claim the repository is deleted or recreated', () => {
    expect(text.toLowerCase()).not.toContain('deleted');
  });
});

describe('adoptConfirm — register-as-is, with and without provenance', () => {
  it('shows what read_repo found when the row carries it', () => {
    const text = adoptConfirm(row({ name: 'x', default_branch: 'develop', head_sha: 'deadbeefcafe' }));
    expect(text).toContain('develop');
    expect(text).toContain('deadbee');
  });

  it('says what WILL happen, without provenance, for an org-origin row', () => {
    // The cost ruling's consequence: no per-row probe, so both fields are null pre-POST. The
    // confirm must not show "unknown" (reads as a fault) nor invent a branch — it states the
    // act and that the details are verified on adopt.
    const text = adoptConfirm(row({ name: 'theirs', origin: 'org', state: 'unregistered_present' }));
    expect(text).toContain('theirs');
    expect(text).toContain(ADOPT_CONFIRM_NO_PROVENANCE);
    expect(text.toLowerCase()).not.toContain('unknown');
  });

  it('promises no push and no content change, in both shapes', () => {
    // Adopt is a governance statement. An operator adopting the repo they wrote must not be
    // led to think AGP is about to write to it.
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
    // The preview IS the consent screen (tenet 6). If arriving here created anything, this
    // sentence would be the lie the epic exists to remove.
    expect(SEED_CONSENT_PROMPT('org')).toContain('Nothing is created until you confirm');
  });
});

describe('classifyRolloutError — the route\'s fixed detail literals → a sentence and a verdict', () => {
  // Mirrors `classifyTemplateError`'s shape (templatesView.ts) because the same interceptor
  // problem applies: `api/client.ts` replaces the AxiosError with `new Error(detail)`, so by the
  // time this surface holds a rejection THE STATUS IS GONE. The detail literal is fixed per
  // `.kind` server-side (connections.py:209-215) precisely so it can be classified.
  const ROUTE_DETAILS: Record<RolloutErrorKind, string> = {
    not_found: 'Unknown base template',
    repo_not_found: 'Repository not found in the org',
    conflict: 'Template already registered',
    rollout_error: 'Template rollout failed',
    validation: 'Invalid template name or connection id',
  };

  it('recognizes every literal the route pins', () => {
    for (const [kind, detail] of Object.entries(ROUTE_DETAILS)) {
      expect(classifyRolloutError(new Error(detail), 'fallback').kind, detail).toBe(kind);
    }
  });

  it('treats ONLY rollout_error as retryable', () => {
    // The service docstring is explicit: "the only retryable kind". 404/409/422 are permanent —
    // re-POSTing the same name returns the same answer, and offering a retry would be advice
    // the product cannot honour.
    expect(classifyRolloutError(new Error(ROUTE_DETAILS.rollout_error), 'f').retryable).toBe(true);
    for (const kind of ['not_found', 'repo_not_found', 'conflict', 'validation'] as const) {
      expect(classifyRolloutError(new Error(ROUTE_DETAILS[kind]), 'f').retryable, kind).toBe(false);
    }
  });

  it('appends the wait hint to the retryable kind only', () => {
    const retryable = classifyRolloutError(new Error(ROUTE_DETAILS.rollout_error), 'f').message;
    const terminal = classifyRolloutError(new Error(ROUTE_DETAILS.conflict), 'f').message;
    expect(retryable).toMatch(/try again/i);
    expect(terminal).not.toMatch(/try again/i);
  });

  it('tells 404-repo apart from 404-seed in the sentence, not only in the kind', () => {
    // The route split these two literals deliberately: telling an operator "Unknown base
    // template" when their REPO name was mistyped points them at AGP's seed list, which has
    // nothing to do with their mistake.
    const seed = classifyRolloutError(new Error(ROUTE_DETAILS.not_found), 'f').message;
    const repo = classifyRolloutError(new Error(ROUTE_DETAILS.repo_not_found), 'f').message;
    expect(seed).not.toBe(repo);
    expect(repo).toMatch(/repositor/i);
  });

  it('points a conflict at the rule that refused, not just at "already registered"', () => {
    // The server enforces THREE rules under one 409 (already registered / the infra repo / a
    // materialized agent repo) and the message names which. The client's sentence must leave
    // room for all three rather than asserting the first.
    const msg = classifyRolloutError(new Error(ROUTE_DETAILS.conflict), 'f').message;
    expect(msg).toMatch(/already|platform|agent/i);
  });

  it('names the name rule for a validation failure', () => {
    // 422 is what a listed-but-illegally-named org repo returns on adopt. The sentence has to
    // be the same rule `ADOPT_NAME_RULE_HINT` states, or the two channels contradict.
    const msg = classifyRolloutError(new Error(ROUTE_DETAILS.validation), 'f').message;
    expect(msg).toMatch(/lowercase/i);
  });

  it('falls back to the raw message, TERMINAL, for anything unrecognized', () => {
    // Same rule as `classifyTemplateError`: an unknown failure is more likely a bug than
    // contention, so inviting a retry would be a guess presented as advice.
    const view = classifyRolloutError(new Error('HTTP 500'), 'fallback');
    expect(view.kind).toBeNull();
    expect(view.retryable).toBe(false);
    expect(view.message).toBe('HTTP 500');
  });

  it('uses the fallback for an empty or non-Error rejection', () => {
    expect(classifyRolloutError(new Error(''), 'fallback').message).toBe('fallback');
    expect(classifyRolloutError(new Error('   '), 'fallback').message).toBe('fallback');
    expect(classifyRolloutError('boom', 'fallback').message).toBe('fallback');
    expect(classifyRolloutError(null, 'fallback').message).toBe('fallback');
    expect(classifyRolloutError(undefined, 'fallback').message).toBe('fallback');
  });

  it('never classifies a prototype member name as a recognized kind', () => {
    // A `Map`, not an object literal — the lookup key is a SERVER-SUPPLIED string, and an
    // object would resolve 'toString' / 'constructor' to `Object.prototype`'s member.
    // (`githubLink.ts`'s reasoning, unchanged.)
    for (const key of ['toString', 'constructor', 'hasOwnProperty', '__proto__']) {
      expect(classifyRolloutError(new Error(key), 'f').kind, key).toBeNull();
    }
  });
});

describe('deregister failures belong to the CATALOG route\'s classifier (F3)', () => {
  // THE F3 REGRESSION TEST. Deregister on this surface calls `githubTemplatesApi.remove` — a
  // different router with a different `_ERROR_DETAIL` table — but its failures were routed through
  // `classifyRolloutError`, which recognizes none of those literals. The consequence was specific:
  // a 503 "Template catalog is temporarily unavailable" is RETRYABLE and rendered as a raw,
  // terminal, rose-tinted failure with no Retry, telling an admin the catalog was broken when the
  // remedy was to wait.
  const CATALOG_DETAILS = [
    'Template not found',
    'Template catalog is temporarily unavailable',
    'GitHub template operation failed',
    'Invalid template metadata',
    'Invalid template zip',
  ];

  it('the rollout classifier recognizes NONE of the catalog route\'s literals', () => {
    // The premise of the bug, asserted so the fix cannot be quietly reverted by teaching the wrong
    // classifier these strings — which would then have TWO owners for one route's errors.
    for (const detail of CATALOG_DETAILS) {
      expect(classifyRolloutError(new Error(detail), 'f').kind, detail).toBeNull();
    }
  });

  it('the catalog classifier does recognize them, and calls the store fault retryable', () => {
    for (const detail of CATALOG_DETAILS) {
      expect(classifyTemplateError(new Error(detail), 'f').kind, detail).not.toBeNull();
    }
    const store = classifyTemplateError(new Error('Template catalog is temporarily unavailable'), 'f');
    expect(store.retryable).toBe(true);
    expect(store.message).toMatch(/try again/i);
  });

  it('and the two tables are DISJOINT, so neither route can claim the other\'s errors', () => {
    // Each classifier owns exactly one route. Overlap would make the surface's tint and Retry
    // depend on which handler happened to catch the rejection.
    const ROLLOUT_DETAILS = [
      'Unknown base template',
      'Repository not found in the org',
      'Template already registered',
      'Template rollout failed',
      'Invalid template name or connection id',
    ];
    for (const detail of ROLLOUT_DETAILS) {
      expect(classifyTemplateError(new Error(detail), 'f').kind, detail).toBeNull();
    }
  });

  it('the modal hands its deregister handler the catalog classifier', () => {
    // A SOURCE assertion, because there is no jsdom to observe which classifier a rejected promise
    // reaches. Narrow and specific: the deregister handler is the one place on this surface that
    // calls a catalog route, so it is the one place `classifyTemplateError` may appear — and it must.
    expect(rolloutModalSrc).toContain('classifyTemplateError');
    const handler = rolloutModalSrc.slice(
      rolloutModalSrc.indexOf('const runDeregister'),
      rolloutModalSrc.indexOf('const askConfirm'),
    );
    expect(handler.length).toBeGreaterThan(200);
    expect(handler).toContain('githubTemplatesApi.remove');
    // The CALL, not a mention: the handler's comment names the wrong classifier deliberately (to
    // explain why it is wrong), so a bare `not.toContain` would fail on correct code. Matching
    // `classifier(` is what distinguishes an invocation from prose about one.
    expect(handler).toMatch(/classifyTemplateError\(/);
    expect(handler).not.toMatch(/classifyRolloutError\(/);
  });
});

describe('the cost model — which surfaces pay for a provider call', () => {
  it('names the provider-backed calls as reconcile-only', () => {
    expect(PAGE_LOAD_PROVIDER_CALLS).toEqual([]);
    expect(RECONCILE_EFFECT_DEPS).toContain('reloadNonce');
  });

  it('leaves the Templates page free of any reconcile call', () => {
    // THE RULED COST MODEL (D-C3): provider calls happen on this surface's OPEN and explicit
    // Refresh only, so the Templates page stays registry-only and instant. Asserted as an
    // ABSENCE across the page source, because there is no jsdom to observe a fetch that never
    // happens — and a page-load reconcile is a one-line import away.
    const pages = import.meta.glob<string>('./Templates.tsx', {
      query: '?raw',
      import: 'default',
      eager: true,
    });
    const src = pages['./Templates.tsx'];
    // The sweep really read the page — a glob that silently stopped matching would make the
    // assertion below vacuous while staying green.
    expect(typeof src).toBe('string');
    expect(src.length).toBeGreaterThan(1000);
    // `rolloutApi.reconcile(` / `.rollout(` / `.adopt(` must appear NOWHERE in the page: the
    // modal owns all three. A page that called reconcile itself would spend a paginated
    // `list_repos` plus a probe per row on every org switch.
    expect(src).not.toMatch(/rolloutApi\s*\.\s*(reconcile|rollout|adopt)\s*\(/);
  });

  it('keeps the page-load fetch set closed at the two registry reads', () => {
    // NAMED AND CLOSED, so a third read cannot be added without this test failing. The two are
    // `connectionsApi.list()` (which orgs exist) and `githubTemplatesApi.list()` (the registry
    // catalog for the selected one) — both registry-only.
    const src = import.meta.glob<string>('./Templates.tsx', {
      query: '?raw',
      import: 'default',
      eager: true,
    })['./Templates.tsx'];
    const apiCalls = [...src.matchAll(/\b(\w+Api)\s*\n?\s*\.\s*(\w+)\s*\(/g)].map(
      (m) => `${m[1]}.${m[2]}`,
    );
    // Deduplicated: the same call may legitimately appear in an effect and in a refetch.
    const distinct = [...new Set(apiCalls)].sort();
    expect(distinct).toEqual([
      'connectionsApi.list',
      'githubTemplatesApi.list',
      'githubTemplatesApi.patch',
      'githubTemplatesApi.remove',
      'githubTemplatesApi.upload',
    ]);
    // The three write calls are explicit operator clicks (delete / edit / upload), not page
    // loads, and all three are registry writes — no provider call among them.
  });
});

describe('the deleted field names must not survive in TS', () => {
  it('mentions exists_in_org / base_templates nowhere in src/', () => {
    // GREP-PINNED, like E28B's key sweep. Both are DELETED from the wire, not renamed
    // (connections.py:463-490 states why): a client still reading a boolean would keep
    // rendering the same two wrong answers — a registered template whose repo is gone shown as
    // in-sync, and an existing repo shown as creatable.
    //
    // TWO OF THE FOUR DELETED NAMES ARE DELIBERATELY EXCLUDED, on E28B's own
    // `finalize` precedent (repositoryDetailTabs.test.ts:2343): a guard that cries wolf gets
    // deleted, so a sweep may only carry names that are distinctive.
    //
    //   `selectable` — gone from the wire, but `repositoryDetailTabs.ts` OWNS this word for an
    //     unrelated concept (`selectableTabKeys`, `TabDef.ready`), and `DeleteRepositoryModal` /
    //     `TabStrip` / `RepositoryDetail` all use it correctly. Sweeping it would fail on 14
    //     lines that have nothing to do with rollout.
    //   `kind` — same, worse: `EmptyStateCta.kind`, `TemplateErrorKind`, `RolloutErrorKind` and
    //     every classifier in this codebase use it.
    //
    // What DOES pin those two is `tsc`: they are absent from `ReconcileItem` in `api/client.ts`,
    // so a component reading `item.selectable` or `item.kind` fails to compile. The grep is only
    // needed for the two names a stale STRING or comment could keep alive — which is exactly how
    // `client.ts:2052-2057`'s comment survived being wrong.
    const DELETED = ['exists_in_org', 'base_templates'];
    const sources = Object.entries(
      import.meta.glob<string>('../../**/*.{ts,tsx}', { query: '?raw', import: 'default', eager: true }),
    ).filter(([path]) => !path.endsWith('.test.ts') && !path.endsWith('.test.tsx'));
    // The sweep really reached `src/`, and specifically the client — where all three names
    // lived. An empty or narrowed glob would make every assertion below vacuous.
    expect(sources.length).toBeGreaterThan(20);
    expect(sources.map(([p]) => p).filter((p) => p.endsWith('api/client.ts'))).toHaveLength(1);
    for (const [path, src] of sources) {
      for (const name of DELETED) {
        expect(src, `${path} still mentions the deleted field ${name}`).not.toContain(name);
      }
    }
  });
});
