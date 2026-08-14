// reconcileView.ts — the pure decisions behind THE RECONCILE SURFACE (E28C/T6, design D-C3).
//
// `RolloutTemplatesModal.tsx` renders; this file DECIDES. The split is the same one
// `templatesView.ts` / `repoRowModel.ts` / `projectRoles.ts` established, and it is not
// stylistic: vitest collects only `src/**/*.test.ts` in a node environment with NO jsdom, so any
// judgement made inside the `.tsx` is a judgement no test can reach.
//
// WHAT THIS SURFACE IS. One place where AGP's template registry and the connected org's actual
// repositories are compared. Every row carries the STATE of that comparison and every state has
// exactly ONE honest action behind it — the table below is the client half of the table in
// `template_rollout_service.py`'s module docstring, and the two must agree because an operator
// reads this one and the server executes that one.
//
//   ======================  ==================================  ==========================
//   state                   meaning                             the action offered here
//   ======================  ==================================  ==========================
//   registered_present      in sync                             re-push (SEED rows only)
//   registered_missing      the repo behind the record is gone   re-create from seed /
//                                                                deregister
//   unregistered_present    a repo AGP does not know about       ADOPT
//   seed_absent             a seed with nothing in the org       create
//   ======================  ==================================  ==========================
//
// WHY THE BOOLEAN IS GONE (and why the old modal could not be patched). The previous version read a
// single "is it already in the org?" flag, answered from AGP's own DDB catalog — evidence about
// AGP's store, never about the org — so it was wrong in both directions: a registered template whose
// repo had been deleted rendered as in-sync with nothing offered, and a repo that already carried a
// seed's name rendered as creatable, which is how the 2026-08-04 live test ended in a hand repo
// delete. The template-vs-infra kind flag and the "is this a choice?" flag are gone too: the infra
// repo's forcedness is now STRUCTURAL (its own field on the view), so it cannot be turned into a
// choice by flipping a boolean. (The deleted field names are not written anywhere in `src/` — the
// grep in `reconcileView.test.ts` is what keeps them from creeping back in as prose.)
//
// ORIGIN GATES TWO ACTIONS, AND THAT IS THE ONE PLACE THIS FILE GOES BEYOND THE STATE TABLE. Both
// "re-push" and "re-create from seed" push SEED BYTES FROM DISK, and `_rollout_template` raises
// `not_found` ("Unknown base template") for a name with no scaffold dir. An uploaded or adopted
// template (origin "registry") has no seed, so offering it either action would be an affordance
// whose every click 404s — the over-promise this codebase refuses elsewhere by rendering nothing
// (`promoteBlockedReason`, the gated Upload button, the withheld Retry on a terminal failure).
//
// Deliberately client-free (structural `ReconcileItemLike` rather than importing `ReconcileItem`
// from `../../api/client`), for the same reason `repoRowModel.ts` declares `RepoRowSource`: it
// keeps this module out of axios's import graph so its tests stay plain units, and it states the
// contract as "these five fields, nothing else".

// ---------------------------------------------------------------------------
// The wire row
// ---------------------------------------------------------------------------

/** The four values `ReconcileItemView.state` can carry (connections.py:479). */
export type ReconcileState =
  | 'registered_present'
  | 'registered_missing'
  | 'unregistered_present'
  | 'seed_absent';

/** Why a row exists: AGP ships a scaffold / AGP has a record with no scaffold / found in the org. */
export type ReconcileOrigin = 'seed' | 'registry' | 'org';

/** The five fields this surface reads off a reconcile row. */
export interface ReconcileItemLike {
  name: string;
  origin: ReconcileOrigin;
  state: ReconcileState;
  /** What `read_repo` found. `null` = nothing to read (absent repo, or an un-probed org row). */
  default_branch: string | null;
  /** `null` for an absent repo, an un-probed org row, OR a repo that exists with no commit yet. */
  head_sha: string | null;
}

// ---------------------------------------------------------------------------
// Verbal labels — an audit screen, not a wizard
// ---------------------------------------------------------------------------

/**
 * The state's label, in words rather than in colour.
 *
 * All four are DISTINCT sentences about the comparison, because this is the row's whole verdict
 * and the colour beside it is a second channel, not the primary one. `unregistered_present`
 * carefully never says "registered" or "in sync": that row is a repository AGP has NO record of,
 * and a reassuring word there is the deleted boolean's lie restated in prose.
 */
export const RECONCILE_STATE_LABEL: Record<ReconcileState, string> = {
  registered_present: 'In sync',
  registered_missing: 'Repository missing',
  unregistered_present: 'Found in org, not ours',
  seed_absent: 'Not in org yet',
};

/**
 * The tint, from `OPS_BADGE`'s semantic palette — no new visual language.
 *
 * emerald = in sync (a claim AGP can back with a probe), amber = an action is waiting (the repo
 * behind a record is gone; a seed has never been pushed), slate = NOT OURS. Slate for
 * `unregistered_present` is deliberate: rose would report a fault where none occurred (someone
 * else's repository existing is not a failure) and amber would group it with AGP's own pending
 * work. Nothing here is rose, because no state on this surface is an error — errors arrive
 * through `classifyRolloutError`.
 */
export const RECONCILE_STATE_TONE: Record<ReconcileState, string> = {
  registered_present: 'bg-emerald-50 text-emerald-700',
  registered_missing: 'bg-amber-50 text-amber-700',
  unregistered_present: 'bg-slate-100 text-slate-500',
  seed_absent: 'bg-amber-50 text-amber-700',
};

// ---------------------------------------------------------------------------
// The one honest action per row
// ---------------------------------------------------------------------------

/** The five verbs this surface can offer. `deregister` is `githubTemplatesApi.remove`. */
export type ReconcileAction = 'create' | 'repush' | 'recreate' | 'adopt' | 'deregister';

/**
 * The two SINGLE-REPO verbs — the ones that execute immediately, from their own per-row confirm.
 *
 * Named because the split is a real domain distinction and three separate places have to agree on
 * it: `adopt` (`POST /templates/adopt`) and `deregister` (`DELETE /github-templates/{name}`) are
 * one-repo verbs on their own routes, while `create`/`repush`/`recreate` are the ROLLOUT BATCH
 * (`QUEUEABLE` below) and are consented to once in the footer.
 *
 * Extracted while fixing a real type error: the modal's confirm handler was typed to accept the
 * full `ReconcileAction`, so it claimed to handle three verbs it does not — and `tsc -b` caught the
 * contradiction against the row prop that correctly named only these two. A union of string
 * literals repeated inline in three places is exactly the kind of thing that drifts.
 */
export type SingleRepoAction = Extract<ReconcileAction, 'adopt' | 'deregister'>;

/** Button copy per verb, plus whether it is the row's primary. */
export const RECONCILE_ACTION_LABEL: Record<ReconcileAction, string> = {
  create: 'Create',
  repush: 'Re-push seed',
  recreate: 'Re-create from seed',
  adopt: 'Adopt',
  deregister: 'Deregister',
};

/**
 * The adopt name rule, stated for a human.
 *
 * WHY A CLIENT-SIDE ANNOTATION IS NOT A DUPLICATED AUTHORITY. `reconcile` LISTS every org repo
 * AGP does not account for, including ones whose names GitHub allows and AGP's catalog does not
 * (`MyRepo`, `my_repo`, `my.repo`, `1repo`). Adopt applies `_require_valid_name` first, so those
 * rows 422. The server stays the authority; this is the surface refusing to offer a click it
 * knows is refused — the same reason a terminal failure gets no Retry button.
 *
 * The rule and not the regex, because the regex is not actionable: an operator who reads
 * `^[a-z][a-z0-9-]{0,63}$` still has to work out what to rename the repo to.
 */
export const ADOPT_NAME_RULE_HINT =
  'This repository’s name can’t be a template name: AGP requires lowercase letters, digits and hyphens, starting with a letter. Rename it in the provider to adopt it.';

/**
 * `_NAME_RE` from `github_template_service.py:58`, mirrored EXACTLY.
 *
 * THE AUTHORITY IS THE BACKEND. This pattern is a copy for one purpose — to decide whether to
 * offer the adopt button — and it must stay byte-identical to
 * `^[a-z][a-z0-9-]{0,63}$` or the two disagree in one of two bad ways: a legal repo annotated
 * un-adoptable (an action wrongly withheld) or an illegal one offered and 422'd.
 *
 * The `{0,63}` after the leading letter caps the whole name at 64 characters, and the class holds
 * no `/` and no `.`, so a name that passes cannot express a path segment.
 */
const ADOPT_NAME_RE = /^[a-z][a-z0-9-]{0,63}$/;

/** Would `adopt` accept this repository name, or 422 on it? */
export function isAdoptableName(name: string): boolean {
  return ADOPT_NAME_RE.test(name);
}

/**
 * The three actions that are the ROLLOUT BATCH verb, so a row offering one is a row the footer can
 * carry. Adopt and deregister are single-repo verbs on OTHER routes
 * (`POST /templates/adopt`, `DELETE /github-templates/{name}`), so they are not queueable and keep
 * their own immediate confirms.
 */
const QUEUEABLE: readonly ReconcileAction[] = ['create', 'repush', 'recreate'];

/** One rendered row: the wire item plus every decision about it. */
export interface ReconcileRowModel {
  item: ReconcileItemLike;
  /** The state's verbal label. */
  label: string;
  /** The state's `OPS_BADGE` tint. */
  tone: string;
  /** The actions offered, in recommendation order. Empty = in sync, nothing to do. */
  actions: ReconcileAction[];
  /**
   * The action this row contributes to the rollout BATCH, or null when it has none.
   *
   * THIS FIELD IS THE F1 FIX. The first cut kept the batch's membership in the component as two
   * `Set`s, and the re-push set's only writer was a stale-prune filter — so no interaction could
   * ever add to it, the wire flag was permanently `false`, and the "Queued" pill was unreachable
   * markup. The review proved it by deleting that branch with all 1040 tests still green: the E28B
   * I-1 shape, in the file whose header cites I-1. A row's batch eligibility is a DECISION, so it
   * belongs to the model, where `batchPlan` below can be tested against it.
   */
  queueAction: ReconcileAction | null;
  /** `branch @ sha7`, `branch`, or null when there was nothing to read. */
  provenance: string | null;
  /** Is the row's action refused before it is clicked (an un-adoptable name)? */
  actionDisabled: boolean;
  /** Why it is refused, or null. */
  note: string | null;
}

/**
 * ONE ROW'S WHOLE MODEL — the function this task's tests are about.
 *
 * The `switch` is exhaustive over a closed union with no `default`, so a fifth state added to the
 * wire is a `tsc` error naming this function rather than a row that silently renders with no
 * action.
 */
export function reconcileRowModel(item: ReconcileItemLike): ReconcileRowModel {
  const seeded = item.origin === 'seed';
  let actions: ReconcileAction[];
  switch (item.state) {
    case 'seed_absent':
      actions = ['create'];
      break;
    case 'registered_present':
      // Re-push is the ONLY thing an in-sync row can offer, and only when AGP ships the bytes.
      // A registry-origin row in sync is a legitimate row with nothing to do.
      actions = seeded ? ['repush'] : [];
      break;
    case 'registered_missing':
      // Re-create needs a seed on disk; deregister is always available, because a record
      // pointing at nothing is a record the operator must be able to drop.
      actions = seeded ? ['recreate', 'deregister'] : ['deregister'];
      break;
    case 'unregistered_present':
      // The state that cost a repository in the live test. Adopt, and NOTHING else: rollout
      // refuses this row server-side no matter what `overwrite` says.
      actions = ['adopt'];
      break;
  }

  const adoptBlocked = actions[0] === 'adopt' && !isAdoptableName(item.name);
  return {
    item,
    label: RECONCILE_STATE_LABEL[item.state],
    tone: RECONCILE_STATE_TONE[item.state],
    actions,
    // Derived from `actions`, never re-decided from `state` — one authority, so a change to the
    // table above cannot leave the batch offering something the row does not.
    queueAction: actions.find((a) => QUEUEABLE.includes(a)) ?? null,
    provenance: provenanceFor(item),
    actionDisabled: adoptBlocked,
    // Annotated only where the rule BEARS on the offered action. A registered row with a legacy
    // name is not being adopted, so a name warning on it would be a warning about nothing.
    note: adoptBlocked ? ADOPT_NAME_RULE_HINT : null,
  };
}

/**
 * What `read_repo` found, as one string — or null.
 *
 * THREE null cases, all legitimate and none of them a fault: the repo is absent; the row came out
 * of the org listing and was deliberately NOT re-probed (the ruled cost model — no per-row probe);
 * or the repo exists with no commit yet, which is `head_sha: null` with a real branch. That last
 * case is why the branch renders alone rather than as `main @ unknown`: "unknown" reads as a
 * failed read, and a repo with no commit is a normal thing to find.
 */
function provenanceFor(item: ReconcileItemLike): string | null {
  if (!item.default_branch) return null;
  if (!item.head_sha) return item.default_branch;
  return `${item.default_branch} @ ${item.head_sha.slice(0, 7)}`;
}

/** Every row's model, in the wire's own order (the server sorts; the client must not resort). */
export function reconcileRows(items: readonly ReconcileItemLike[]): ReconcileRowModel[] {
  return items.map(reconcileRowModel);
}

/** The counts the consent screen states before anything executes. */
export interface ReconcileSummary {
  createCount: number;
  adoptCount: number;
  inSyncCount: number;
  missingCount: number;
}

/**
 * The preview's numbers — attached to `reconcileRows` because it is the same concern (what this
 * surface is about to do), and derived from the MODELS rather than re-read off the states so the
 * counts cannot disagree with the buttons beneath them.
 */
reconcileRows.summarize = function summarize(rows: readonly ReconcileRowModel[]): ReconcileSummary {
  return {
    createCount: rows.filter((r) => r.item.state === 'seed_absent').length,
    adoptCount: rows.filter((r) => r.item.state === 'unregistered_present').length,
    inSyncCount: rows.filter((r) => r.item.state === 'registered_present').length,
    missingCount: rows.filter((r) => r.item.state === 'registered_missing').length,
  };
};

// ---------------------------------------------------------------------------
// The rollout request
// ---------------------------------------------------------------------------

export interface RolloutRequestBody {
  template_names: string[];
  /** The TEMPLATES' consent. Since E28D it authorises nothing about the infra repo. */
  overwrite: boolean;
  /**
   * The forced infra repo's OWN consent (E28D, `connections.py:502`).
   *
   * It exists because `overwrite` had one value and TWO consumers server-side: `rollout()` handed
   * the templates' flag to `_ensure_infra`, so ticking a re-push on a template pushed AGP's
   * Terraform module over the org's existing `agp-runtime-infra` and reported "overwritten" — a
   * write to a repository the operator never named. Both flags default `false`, which makes the
   * split a NARROWING: an old payload sending only `overwrite` no longer authorises the infra push.
   */
  overwrite_infra: boolean;
}

/** The whole batch, resolved: the wire body plus the breakdown the confirm and footer describe. */
export interface BatchPlan {
  /** `template_names`, in ROW order. */
  names: string[];
  /** The TEMPLATES' wire flag. */
  overwrite: boolean;
  /** The INFRA repo's own wire flag — the operator's tick, carried through unchanged. */
  overwriteInfra: boolean;
  /** The queued names by kind, for the confirm's per-row lines and the footer's count. */
  creates: string[];
  repushes: string[];
  recreates: string[];
  /**
   * The ticks this plan REFUSED to carry, sorted.
   *
   * Reported rather than discarded because a silent drop is what the stale-prune effect T6 deleted
   * used to do: the tick disappeared and the run did less than the operator authorised, with nothing
   * said. Sorted so the notice reads the same whatever order the boxes were clicked in.
   */
  dropped: string[];
  /** Nothing queued — the footer's primary is dead unless the infra repo still needs an act. */
  empty: boolean;
}

/**
 * THE BATCH, decided here rather than in the component (the F1 fix).
 *
 * It takes the CURRENT rows, the selection and the infra consent, and answers with a body — which
 * makes four properties testable that were previously untestable by construction:
 *
 *  1. **`overwrite` is actually set.** The old code's re-push set had no writer, so this flag was
 *     permanently `false` and a queued re-push silently did nothing. Now it is `true` exactly when a
 *     row whose `queueAction` is `repush` is in the batch.
 *  2. **`overwrite` stays SCOPED.** A re-create does NOT set it. `_rollout_template` reaches
 *     `recreated` on `registered_missing` regardless of the flag, so setting it for a re-create
 *     would additionally authorise re-pushing every OTHER registered-present template in the same
 *     batch — a checkbox on one row becoming consent for another's write. Since E28D it does not
 *     reach the INFRA repo either: `overwriteInfra` is its own argument, never derived from these
 *     rows, because the infra repo is its own FIELD on the view and not one of them.
 *  3. **It SELF-PRUNES.** A name is carried only if its CURRENT row still offers that action, which
 *     replaces the stale-prune effect the component used to run after every reconcile. A name queued
 *     before a Refresh may come back in a different state, and submitting it then would act on a
 *     comparison the operator never saw. `unregistered_present` therefore can never appear, whatever
 *     is queued — the client half of the server's refusal.
 *  4. **A tick CARRIES ITS VERB, so a verb change drops it** (E28D, live-test finding T6-L3). The
 *     selection was a `Set` of bare NAMES, which made property 3 weaker than it claimed: the prune
 *     could see a row that had lost its action entirely, but not one whose action had CHANGED. A
 *     tick placed on a `create` ("nothing can be written over") survived a Refresh that returned the
 *     row as `registered_present`, and was then classified as a re-push — manufacturing
 *     `overwrite: true` out of a create. A `Map` name → the verb visible when it was ticked closes
 *     it: the verb must still match, or the tick is dropped and reported.
 *
 * Row order, not insertion order: the body is a function of the org's state and the selection, so it
 * must not vary with the order the operator happened to click.
 */
export function batchPlan(
  rows: readonly ReconcileRowModel[],
  queued: ReadonlyMap<string, ReconcileAction>,
  // THE INTENT, NOT THE RAW TICK — and this is a fix, not a preference (E28D review, Critical 1).
  // It took the checkbox boolean and carried it to the wire verbatim, so `infraIntentFor` made an
  // absent-repo re-push unrepresentable in the INTENT while the request could still carry
  // `overwrite_infra: true` for a repo that is not there. Reachable without touching code: tick the
  // box on a present repo, Refresh onto one that has since been deleted — the checkbox UNMOUNTS
  // (present-branch only) and the consent stays, invisible and un-untickable. `_ensure_infra` probes
  // at SUBMIT time, so a repo recreated between the read and the click would then be pushed over
  // under a consent that was never on screen: "a write nobody named", the exact thing T8's split
  // deleted, reproduced inside the fix for it.
  infraIntent: InfraIntent,
): BatchPlan {
  // Property 4: the row must still offer the verb the tick was PLACED against, not merely some verb.
  const live = rows.filter(
    (r) => r.queueAction !== null && queued.get(r.item.name) === r.queueAction,
  );
  const carried = new Set(live.map((r) => r.item.name));
  const by = (action: ReconcileAction) =>
    live.filter((r) => r.queueAction === action).map((r) => r.item.name);
  const repushes = by('repush');
  return {
    names: live.map((r) => r.item.name),
    // ONLY a template re-push earns it. See property 2 above.
    overwrite: repushes.length > 0,
    // THE WIRE FIELD IS THE INTENT, so "an absent repo being re-pushed" is unrepresentable in the
    // BODY and not merely in a derived value the body could bypass. Still never derived from `rows`
    // — the infra repo is its own field on the view — but no longer a raw checkbox either.
    overwriteInfra: infraIntent === 'repush',
    creates: by('create'),
    repushes,
    recreates: by('recreate'),
    // Every ticked name the plan refused, whether its verb changed, its row lost its action, or the
    // name left the org between reads. All three are the same fact to an operator: it is not in this
    // run any more.
    dropped: [...queued.keys()].filter((name) => !carried.has(name)).sort(),
    empty: live.length === 0,
  };
}

/**
 * TICK OR UN-TICK ONE ROW — the selection's only writer, as a pure reducer (E28D review, Critical 2).
 *
 * Extracted from the component rather than tested through a re-implementation, for this file's
 * founding reason: a reducer inside the `.tsx` is a decision no test can reach, and a test that
 * re-states its logic pins the copy instead of the code. E28B's nine vacuous guards are what that
 * looks like when it goes wrong.
 *
 * IT TOGGLES ON THE VERB, NOT ON PRESENCE. Keyed on "is this name in the map" it had a dead first
 * click on the exact path the dropped-tick notice sends operators down: a tick whose row now offers a
 * different verb renders UNCHECKED (`checked` reads the plan, which refuses it) while the map still
 * holds the stale entry — so the first click deleted the invisible entry and looked like nothing, and
 * only the second ticked. Comparing the verb makes one click do what the box shows.
 *
 * A row with no queueable action returns the map UNCHANGED (same reference), so a caller cannot
 * accidentally queue an adopt or a deregister — those are single-repo verbs on other routes.
 */
export function toggleQueuedAction(
  queued: ReadonlyMap<string, ReconcileAction>,
  row: ReconcileRowModel,
): ReadonlyMap<string, ReconcileAction> {
  const action = row.queueAction;
  if (action === null) return queued;
  const next = new Map(queued);
  if (next.get(row.item.name) === action) next.delete(row.item.name);
  else next.set(row.item.name, action);
  return next;
}

/**
 * THE ONE CONSENT GATE'S LINES — what the batch is about to write, per row, plus the forced infra
 * repo.
 *
 * Execution moved from per-row buttons to the footer, so the sentences that used to sit in the
 * per-row confirms have to travel with it. A batch that re-pushed onto live repositories while
 * saying only "Roll out (3)" would reopen exactly the consent gap the per-row confirm closed.
 *
 * The infra line is UNCONDITIONAL because the server ensures that repo on every rollout,
 * independent of the selection — a confirm listing only the ticked rows would understate the click.
 *
 * What it says is worded to `_ensure_infra`, not to the shape of the other lines (E28C final
 * review): that method CREATES the repo when it is absent, but when it already exists and its
 * consent is false it returns `skipped` and pushes NOTHING. The line used to promise a Terraform
 * re-push on every roll-out, which overstated the write in the safe direction — and overstatement is
 * this epic's own defect class, so the direction does not excuse it.
 *
 * E28D CORRECTED THAT CORRECTION. The replacement said the module is re-pushed "only when a re-push
 * above authorises it" — an accurate description of a coupling that should never have existed, and
 * one T8 then deleted by giving the infra push its own wire field. So the sentence now keys off
 * `overwriteInfra`, the operator's own tick, and the template lines above it authorise nothing here.
 */
export function batchConfirmLines(
  rows: readonly ReconcileRowModel[],
  queued: ReadonlyMap<string, ReconcileAction>,
  // THE INTENT, for the same reason `batchPlan` takes it — and the review's probe is why this
  // argument changed too rather than only the wire field. With the raw tick, an absent repo produced
  // a title reading "Create the runtime infra repo" over a body reading "you have also authorised
  // re-pushing the module": the consent gate contradicting itself, which is this epic's whole defect
  // class. One value now decides the title, the body and the wire together.
  infraIntent: InfraIntent,
): string[] {
  const plan = batchPlan(rows, queued, infraIntent);
  const lines = rows
    .filter((r) => plan.names.includes(r.item.name))
    .map((r) => {
      if (r.queueAction === 'repush') return repushConfirm(r.item.name);
      if (r.queueAction === 'recreate') return recreateConfirm(r.item.name);
      return (
        `Create “${r.item.name}” in this org and push AGP’s starter files. The repository does not ` +
        'exist yet, so nothing can be written over.'
      );
    });
  // The lead clause is UNCONDITIONAL because the ensure is: the server creates this repo when it is
  // absent on every rollout, whatever is ticked. Only the TAIL — what happens to one that already
  // exists — is the operator's choice now, so only the tail branches.
  const infraLead =
    'Every roll-out also ensures the forced runtime-infra repo: it is created and seeded with ' +
    'AGP’s Terraform module if this org does not have it yet, and otherwise left exactly as it is. ';
  lines.push(
    infraLead +
      (plan.overwriteInfra
        ? 'You have also authorised re-pushing the module into an existing one: AGP adds one commit ' +
          'with the module’s files, and that push is idempotent, so an unchanged module writes no ' +
          'commit at all.'
        : 'An existing one is not written to — re-pushing the module needs its own box ticked above.'),
  );
  return lines;
}

/**
 * Does the FORCED infra repo still need creating? (F4 — moved out of the `.tsx`.)
 *
 * It is the reason a rollout with nothing ticked can still be meaningful, so it gates the footer's
 * primary. `seed_absent` is the exact test rather than an approximation: `reconcile` builds this row
 * with `registered=False` — the infra repo is NEVER registered — so only the two unregistered states
 * are reachable and `seed_absent` means precisely "not in the org yet".
 *
 * A missing row reads as `false`. "AGP could not report it" is not evidence of absence, and the
 * permissive reading here enables a submit button on an unknown.
 */
export function infraNeedsCreate(infra: ReconcileItemLike | null | undefined): boolean {
  return infra?.state === 'seed_absent';
}

/** What a roll-out will do to the forced infra repo: nothing, create it, or re-push the module. */
export type InfraIntent = 'none' | 'create' | 'repush';

/**
 * THE INFRA REPO'S WHOLE STORY, as one value (E28D).
 *
 * Two questions used to be answered separately and could therefore disagree: "does it need
 * creating?" (`infraNeedsCreate`) and "has the operator ticked the re-push?" (a `.tsx` boolean). The
 * confirm title needs BOTH to name the act, and the footer needs to know whether either amounts to a
 * write worth submitting — so they collapse into one three-valued answer.
 *
 * **`create` WINS OVER THE TICK, deliberately.** When the repo is absent the create is unconditional
 * (a tenant runtime cannot deploy without it), which is why no checkbox renders in that branch — and
 * making the intent ignore the tick there is what makes "an absent repo being re-pushed" not merely
 * unlikely but UNREPRESENTABLE. A tick left over from a previous view therefore cannot change what
 * this surface says or sends.
 *
 * A missing row is `none`, on `infraNeedsCreate`'s fail-closed reading: "AGP could not report it" is
 * not evidence about it, and the permissive answer would enable a submit on an unknown.
 */
export function infraIntentFor(
  infra: ReconcileItemLike | null | undefined,
  overwriteInfra: boolean,
): InfraIntent {
  if (!infra) return 'none';
  if (infraNeedsCreate(infra)) return 'create';
  return overwriteInfra ? 'repush' : 'none';
}

/**
 * THE BATCH CONFIRM'S TITLE — a pure function, because the literal it replaces was the bug (T6-L2).
 *
 * The live test found "Roll out **0 templates** — myorg" over a body whose one bullet was the infra
 * line. `canSubmit` allows a submit with nothing ticked when the infra repo needs an act, so zero is
 * a legitimate count — but the title then contradicted its own content AND undersold the one write
 * the click performs. The footer got the same count right at the same moment, and the asymmetry has a
 * structural cause: the footer read a value while the title was a template literal in a `.tsx` no
 * test can read. Moving it here is the fix; the wording is secondary.
 *
 * Takes the INTENT rather than a bare "needs infra" boolean, because once the infra repo has its own
 * consent the zero-template case splits in two — creating the repo and re-pushing into an existing
 * one are different acts, and a title that called the second one "create" would misname the only
 * write in the run.
 *
 * TOTAL over its inputs, including the `n === 0 && 'none'` case the footer's `canSubmit` gates off:
 * an unreachable input should still return a sentence rather than `''` or a throw. Same reasoning as
 * `rolloutActionLabel`'s — this is the last surface allowed to fail to render, because the operator
 * is reading it to decide whether to authorise a write.
 */
export function batchConfirmTitle(plan: BatchPlan, infraIntent: InfraIntent, org: string): string {
  const n = plan.names.length;
  if (n > 0) return `Roll out ${n === 1 ? '1 template' : `${n} templates`} — ${org}`;
  if (infraIntent === 'create') return `Create the runtime infra repo — ${org}`;
  if (infraIntent === 'repush') return `Re-push the runtime infra module — ${org}`;
  // Footer-unreachable. A calm, honest sentence beats an empty title bar.
  return `Nothing selected — ${org}`;
}

// ---------------------------------------------------------------------------
// The result vocabulary
// ---------------------------------------------------------------------------

// The five words `RolloutResultItemView.action` can carry (connections.py:511) are NOT a union type
// here, and that is the F2 shape rather than an omission: the field is `str` server-side, so
// `rolloutActionLabel` takes a wide `string` and is total over it. A union would only have been
// assignable by a cast — a compile-time promise about a runtime value the wire does not enforce.
// (E28D deleted the `RolloutAction` alias that used to sit here: it had no reference left in `src/`
// once the result view moved to the total function, and an exported type nothing consumes is the
// next reader's invitation to re-narrow the lookup this file exists to keep wide.)

/**
 * The receipt, one label per word. ALL FIVE, because a missing member renders a result row with
 * no verdict — the one thing the operator opened the result view for.
 *
 * "Re-pushed" for `overwritten` is the E28C/T3 correction. The word on the wire keeps its name,
 * but delete+recreate is DELETED: the push is now an idempotent repo-ensure followed by
 * `commit_files`, i.e. one commit on top with history preserved. So the label must not say
 * "replaced" — the old modal's amber "Overwritten" described a verb that no longer exists.
 *
 * "Re-created" is its own word for its own fact: a record existed, the repo behind it was gone,
 * and it was rebuilt from seed. Distinct from "Created" (genuinely new) because the E28C/T3
 * review found "created" reported for a repository that already existed — the word IS the audit
 * trail, so three facts get three words.
 */
// A `Map` for the same reason every other lookup here is one: the key is SERVER-SUPPLIED, and an
// object literal would resolve `'toString'` / `'constructor'` to `Object.prototype`'s member and hand
// a FUNCTION to the `.cls` read below.
const ROLLOUT_ACTION_LABELS: ReadonlyMap<string, { label: string; cls: string }> = new Map([
  ['created', { label: 'Created', cls: 'bg-emerald-50 text-emerald-700' }],
  ['recreated', { label: 'Re-created from seed', cls: 'bg-emerald-50 text-emerald-700' }],
  ['overwritten', { label: 'Re-pushed', cls: 'bg-emerald-50 text-emerald-700' }],
  ['adopted', { label: 'Adopted', cls: 'bg-emerald-50 text-emerald-700' }],
  // The only NEUTRAL one of the five: nothing happened. Slate rather than amber, because a skip is
  // usually "already in sync" — reporting that in amber would invite a second click.
  ['skipped', { label: 'Skipped', cls: 'bg-slate-100 text-slate-500' }],
] as const);

/** What a result row renders: a label, a tint, and whether the word was one we know. */
export interface RolloutActionView {
  label: string;
  cls: string;
  /**
   * Did the wire word match a pinned one?
   *
   * The discriminator, and it is load-bearing for the same reason `classifyRolloutError` returns a
   * `kind`: an unknown word's label IS the word, so `label` alone cannot distinguish "recognized"
   * from "fell through" — which is precisely how a backend rename would start rendering raw wire
   * values with every test still green.
   */
  known: boolean;
}

/**
 * The result vocabulary, as a TOTAL function over the wire string (the F2 fix).
 *
 * WHY A FUNCTION AND NOT A `Record` INDEX. `RolloutResultItemView.action` is typed `str`
 * server-side, so the five values are a CONVENTION the backend maintains, not a guarantee the wire
 * enforces. The result view indexed a `Record` with it, so a sixth word yielded `undefined` and the
 * next `.cls` read threw a TypeError — white-screening the entire result list rather than degrading
 * one row. That is the worst possible surface to lose: the result view is the operator's RECEIPT for
 * writes that have ALREADY HAPPENED, so failing to render it means the platform did work and then
 * refused to say what.
 *
 * An unknown word therefore renders as ITSELF, muted. Slate is the only honest tint (`OPS_BADGE`'s
 * `unknown` reasoning, unchanged): emerald would report a success nobody verified, rose a failure
 * nobody observed. Showing the raw word also tells whoever debugs it exactly what arrived.
 */
export function rolloutActionLabel(action: string): RolloutActionView {
  const known = ROLLOUT_ACTION_LABELS.get(action);
  if (known) return { ...known, known: true };
  return {
    // An empty action still has to render a row, so it falls back to a word rather than to ''.
    label: action || 'Unreported',
    cls: 'bg-slate-100 text-slate-500',
    known: false,
  };
}

// ---------------------------------------------------------------------------
// The confirmations — what each write actually writes
// ---------------------------------------------------------------------------

/**
 * RE-PUSH: the destructive-ADJACENT action, and the sentence that has to state exactly what it
 * writes.
 *
 * The predecessor of this copy is the reason it is pinned by a test. The old modal said
 * "Overwriting replaces the existing repository’s contents" under an amber caution — true of the
 * verb at the time (a repo DELETE followed by a re-create from a zip, which destroyed the
 * repository, its history and its issues) and FALSE the moment E28C/T3 replaced that path. A push
 * is now one idempotent commit on top; a re-run that changes nothing writes no commit, moves no
 * ref and fires no build.
 *
 * So the sentence names the repository, names the write (one commit of the seed's files), states
 * what is NOT touched (history), and promises nothing about deletion. Written here rather than in
 * the `.tsx` for the reason at the top of this file.
 */
export function repushConfirm(name: string): string {
  return (
    `Re-push the seed into “${name}”? AGP adds one commit with the starter files on the ` +
    'repository’s default branch. Its history stays — nothing is removed, and a re-push that ' +
    'changes no file writes no commit at all.'
  );
}

/**
 * RE-CREATE: the record exists, the repository behind it does not.
 *
 * Calm and short, because there is nothing at risk: the repo is gone, so this create can neither
 * collide with nor destroy anything.
 */
export function recreateConfirm(name: string): string {
  return (
    `Re-create “${name}” from AGP’s seed? The registered repository no longer exists in the org, ` +
    'so this creates it again and pushes the starter files. The catalog record is kept, ' +
    'including who registered it.'
  );
}

/**
 * The clause an adopt confirm uses when the row carries NO provenance.
 *
 * Exported so the test pins the exact wording and the modal cannot paraphrase it. This is the
 * ruled cost model surfacing in the UI: org-origin rows come out of one paginated listing and are
 * deliberately not re-probed, so `default_branch`/`head_sha` are null PRE-POST. The clause says
 * what WILL happen rather than showing "unknown" — which would read as a failed read of a
 * repository that is demonstrably there.
 */
export const ADOPT_CONFIRM_NO_PROVENANCE =
  'AGP reads the repository when you adopt it, and records the branch and commit it finds then.';

/**
 * ADOPT: register-as-is. A governance statement, never a content check and never a push.
 *
 * Two shapes, because a row may or may not carry provenance, and the difference is not cosmetic:
 * with a branch and a SHA the confirm can show WHAT is being registered; without them it must
 * state the act and say when the details are read. Neither shape may suggest AGP writes to the
 * repository — an operator adopting the repo they wrote must not be led to expect a push.
 */
export function adoptConfirm(item: ReconcileItemLike): string {
  const head = `Adopt “${item.name}” as a template? AGP registers it as-is — no files are read, changed or pushed.`;
  const prov = provenanceFor(item);
  return prov ? `${head} Currently at ${prov}.` : `${head} ${ADOPT_CONFIRM_NO_PROVENANCE}`;
}

/**
 * DEREGISTER, from this surface. Deliberately the same claims as
 * `templatesView.ts`'s `deleteTemplateConfirm`, in this surface's own words: the route DELETES THE
 * RECORD and leaves the repository completely alone.
 *
 * Here the repository is already gone, which makes the second sentence about the general rule
 * rather than about this repo — a template is a POINTER, and its target may be a mirror in another
 * org that AGP has no business deleting.
 */
export function deregisterConfirm(name: string): string {
  return (
    `Deregister “${name}”? This removes AGP’s record so the name can no longer be materialized. ` +
    'AGP never deletes the repository behind a template — and this one is already missing from ' +
    'the org, which is why the record has nothing to point at.'
  );
}

/**
 * THE POST-FINALIZE CONSENT PROMPT (design D-C3, pinned verbatim).
 *
 * Connection registration stays WRITE-FREE: finalize creates no repository, and this surface's
 * preview is the consent screen (tenet 6). The second sentence is the promise that makes the
 * first one safe to ask, and it is only true because nothing on this surface executes on
 * navigation — which is what `PAGE_LOAD_PROVIDER_CALLS` and the Templates-page sweep pin.
 */
export function SEED_CONSENT_PROMPT(org: string): string {
  return `Seed starter templates into ${org}? Nothing is created until you confirm below.`;
}

// ---------------------------------------------------------------------------
// classifyRolloutError — the routes' FIXED `detail` literals → a sentence + a retry verdict
//
// `rolloutApi` had NO classification, which is what this adds. The problem is the one
// `classifyTemplateError` already solves for the catalog routes: the axios interceptor replaces
// the AxiosError with `new Error(response.data.detail)` (`api/client.ts:66-67`), so by the time a
// component holds the rejection THE STATUS CODE IS GONE. The `detail` literal is fixed per `.kind`
// server-side (never `str(err)`) precisely so it can be classified.
//
// WHY IT MATTERS HERE SPECIFICALLY. Before E28C/T3 this surface's failures all arrived as 502
// "Template rollout failed" — including the 422 a pre-existing repo produced, which is how a
// name collision was reported as "connecting failed" and sent an operator to delete a repository
// by hand. Now that the route splits five kinds, the console must tell them apart: exactly ONE is
// retryable, and inviting a retry on a 409 or a 422 is advice the product cannot honour.
//
// A `Map`, not an object literal — `githubLink.ts`'s reasoning unchanged: the lookup key is a
// SERVER-SUPPLIED string, and an object would resolve `'toString'` / `'constructor'` to
// `Object.prototype`'s member, classifying a `detail` of "toString" as a recognized kind whose
// value is a function.
// ---------------------------------------------------------------------------

/** The five `.kind` values `_ROLLOUT_ERROR_DETAIL` is keyed by (connections.py:209-215). */
export type RolloutErrorKind =
  | 'not_found'
  | 'repo_not_found'
  | 'conflict'
  | 'rollout_error'
  | 'validation';

/** Appended to the RETRYABLE kind, so the human is told to wait rather than to give up. */
export const ROLLOUT_RETRY_HINT = 'Try again in a moment.';

// Detail sentence → kind. Keep these BYTE-IDENTICAL to the route's `_ROLLOUT_ERROR_DETAIL`.
const ROLLOUT_DETAIL_KINDS: ReadonlyMap<string, RolloutErrorKind> = new Map([
  ['Unknown base template', 'not_found'],
  ['Repository not found in the org', 'repo_not_found'],
  ['Template already registered', 'conflict'],
  ['Template rollout failed', 'rollout_error'],
  ['Invalid template name or connection id', 'validation'],
] as const);

/**
 * The ONE retryable kind, and the service docstring says so in as many words: `rollout_error`
 * (502) is "the only retryable kind".
 *
 * The other four are permanent. Re-POSTing the same missing seed, the same missing repo, the same
 * already-registered name or the same illegal name returns the same answer — so a Retry button on
 * any of them is an affordance whose every click is refused, which this codebase already treats as
 * worse than an absent one.
 */
const ROLLOUT_RETRYABLE_KINDS: readonly RolloutErrorKind[] = ['rollout_error'];

/** What each recognized kind SAYS. Fixed sentences, so a `Record` with no default branch. */
const ROLLOUT_MESSAGES: Record<RolloutErrorKind, string> = {
  // Ours, transient, and NOTHING was half-written that the operator must clean up: every write
  // verb on this surface is idempotent by content, so the remedy really is to re-run it.
  rollout_error:
    'The rollout failed at the provider or the catalog rather than because of anything you entered. Re-running it is safe — AGP’s writes here are idempotent, so a repeat push changes nothing it has already done.',
  // The SEED is unknown — a name AGP does not ship. Distinct from the repo 404 below, which the
  // route split off deliberately.
  not_found:
    'AGP doesn’t ship a starter template under that name, so there is nothing to push. Refresh this surface — the seed list may have changed.',
  // The REPO is unknown. Naming the repository (not the seed list) is the whole point of the split.
  repo_not_found:
    'That repository isn’t in this org any more, so it can’t be adopted. Refresh this surface to see what is actually there.',
  // THREE server rules answer 409 and the server message names which; this sentence must leave
  // room for all three rather than asserting the first.
  conflict:
    'AGP already accounts for that repository. It is either registered as a template, the platform’s own runtime-infra repo, or a materialized agent’s repository — none of which can be adopted as a template.',
  // The name rule — the same one `ADOPT_NAME_RULE_HINT` states, deliberately, so the pre-click
  // annotation and the post-click failure cannot contradict each other.
  validation:
    'That name isn’t a legal template name. AGP requires lowercase letters, digits and hyphens, starting with a letter.',
};

/**
 * WHAT A CLASSIFIED FAILURE RENDERS AS — the two fields every error surface on this modal reads.
 *
 * This exists because the reconcile surface talks to TWO routers and therefore has TWO classifiers:
 * `classifyRolloutError` for the rollout/adopt routes, and `classifyTemplateError` (from
 * `templatesView.ts`) for the deregister route, whose `detail` literals are a different set
 * entirely (F3). Their `kind` unions are disjoint by design — `"invalid_zip"` is not a rollout kind
 * — so no single `kind`-carrying type can hold both, and `tsc -b` caught exactly that: the error
 * STATE was typed `RolloutErrorView` while the deregister handler assigned a `TemplateErrorView`.
 *
 * The honest fix is to widen the STATE to what it actually consumes rather than to cast one view
 * into the other. A cast would have been a lie in the direction that matters: it would claim a
 * catalog `kind` is a rollout `kind`, and the next reader to branch on `.kind` would silently get a
 * value the union says is impossible. This type carries no `kind` at all — because no render site
 * reads one. All three read `.message` and `.retryable`, and `retryable` is the only field that
 * drives behaviour (tint, headline, whether a Retry is offered).
 *
 * Each classifier keeps returning its OWN full view, `kind` included, so its tests still pin the
 * classification. This is only the shape the component stores.
 */
export interface ClassifiedErrorView {
  /** The sentence to show. Never a raw store/provider body — both routes pin fixed literals. */
  message: string;
  /**
   * Is the remedy to WAIT?
   *
   * The SINGLE authority on that, and every channel reads it: the card's tint, its headline, and
   * whether a Retry button renders at all.
   */
  retryable: boolean;
}

export interface RolloutErrorView extends ClassifiedErrorView {
  /** The matched route `.kind`, or null when nothing matched. */
  kind: RolloutErrorKind | null;
}

/**
 * Turn a rejected `rolloutApi` call into a sentence, a retry verdict and its kind.
 *
 * An UNRECOGNIZED error falls back to its own message (or `fallback` when it carries none) and is
 * TERMINAL — an unknown failure is more likely a bug than contention, so inviting a retry on it
 * would be a guess presented as advice. Same rule, same reasoning, as `classifyTemplateError` and
 * `classifyLinkError`.
 */
export function classifyRolloutError(err: unknown, fallback: string): RolloutErrorView {
  const raw = err instanceof Error && err.message ? err.message.trim() : '';
  const kind = ROLLOUT_DETAIL_KINDS.get(raw) ?? null;
  if (kind === null) return { message: raw || fallback, retryable: false, kind: null };
  const retryable = ROLLOUT_RETRYABLE_KINDS.includes(kind);
  return {
    message: retryable ? `${ROLLOUT_MESSAGES[kind]} ${ROLLOUT_RETRY_HINT}` : ROLLOUT_MESSAGES[kind],
    retryable,
    kind,
  };
}

// ---------------------------------------------------------------------------
// The cost model, as a contract
// ---------------------------------------------------------------------------

/**
 * THE PROVIDER-BACKED CALLS A PAGE LOAD MAY MAKE: none.
 *
 * An empty array is the point. D-C3 ruled that provider calls happen ONLY on this surface's open
 * and its explicit Refresh — one paginated `list_repos` plus a bounded set of `read_repo` probes —
 * so no list or page route gains one, and Bitbucket's 1,000 req/h (the binding budget) is never
 * approached by browsing. `reconcileView.test.ts` pins the Templates page's whole fetch set
 * against this rule by name, because there is no jsdom here to observe a call that must never
 * happen.
 */
export const PAGE_LOAD_PROVIDER_CALLS: readonly string[] = [];

/**
 * The reactive inputs of the reconcile fetch in `RolloutTemplatesModal.tsx`.
 *
 * `reloadNonce` in the deps IS the explicit Refresh — the only way to re-pay for a provider read
 * after open. Without it the surface would have no way to re-look, and an operator who fixed
 * something in the provider would be reading a stale comparison while acting on it. Same pinning
 * idiom, and the same reason, as `CATALOG_EFFECT_DEPS` in `templatesView.ts`.
 */
export const RECONCILE_EFFECT_DEPS = ['connectionId', 'reloadNonce'] as const;
