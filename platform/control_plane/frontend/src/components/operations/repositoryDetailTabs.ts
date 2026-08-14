// repositoryDetailTabs.ts — the pure companion behind `/ops/repositories/:id` (E28/T11,
// contracts C5 + D11). The tab registry, the environment-strip derivation, the runtime-scope
// resolution, and the ungoverned-in-prod flag: everything on that page that is a DECISION
// rather than markup.
//
// It lives in a `.ts` because vitest collects only `src/**/*.test.ts` — a judgement made inside
// a `.tsx` is a judgement no test can reach, which is exactly how two forked copies of the
// status→tint table shipped a repo live in production wearing provisioning's amber.
// `repoRowModel.ts` (T10) and `projectDetailTabs.ts` (E27) established the idiom; the `.tsx`
// files here are wiring only — props in, markup out, no branching a test cannot see.
//
// NO STAGE LITERAL APPEARS IN THIS FILE, and none may (C5). A tenant's stage set is OPEN: the
// backend requires at least ONE stage, not a dev+prod pair, so a tenant may legitimately be
// `uat`-only. Every stage here comes from the tenant record and is compared to nothing.

import type { Deployment, RuntimeStatus, StepState, TenantStageConfig } from '../../api/client';
import {
  CICD_LABEL,
  isCicdInFlight,
  toCicdStatus,
  toRuntimeStatus,
  type RuntimeStatusKey,
} from './opsStatus';
// The HAS-THE-MATERIALIZE-RUN-STOPPED predicate, from the module that owns the timeline it drives.
// Imported here rather than reimplemented: a second copy of "which step states mean the run is
// over" is the drift that already shipped once on this page (a halted run reading as a completed
// one), and it is imported into the `.ts` rather than the page because a source guard below forbids
// the page from calling it at all — the page asks `shouldPollRepo`, which is a different question.
import { isMaterializeTerminal } from './ProjectRepositoriesTab';
// TYPE ONLY, for the `keyof` below. `opsUi.ts` is the class-string palette and holds no logic, so
// this does not pull anything into the module's runtime graph — the same import shape
// `ProjectRepositoriesTab.tsx` uses for the same reason.
import type { OPS_BADGE } from './opsUi';
import {
  canDestroy,
  canPromote,
  mayMaintainProject,
  type ProjectRoleName,
} from './projectRoles';
// The digest abbreviation and the tag-only caution's copy, from the module that owns the row's shared
// display derivations — so the promote surface and any list rendering an artifact cannot disagree
// about how a digest is shortened or what the caution says. The two constants are imported HERE
// rather than in each `.tsx` because the marker is now resolved in this module (see
// `promotionArtifact`): a component that picked the copy itself would be a render branch again.
import {
  PROMOTION_TAG_ONLY_LABEL,
  PROMOTION_TAG_ONLY_NOTE,
  shortDigest,
} from './repoRowModel';

// ---------------------------------------------------------------------------
// The tab registry.
//
// SIX tabs are registered and ALL SIX now have bodies. That split was the whole design of this
// module: registering all six up front fixed the strip's shape and order, so each later task added
// a body and flipped one flag rather than re-deciding the page's information architecture. The
// alternative — register a tab when its body lands — would have had four tasks editing the same
// registry in four different orders. T11 shipped Overview and Access, T12 flipped three
// (Deployments, Observability, Resources), and T14 flipped Pull requests once its body existed.
//
// The mechanism is retained rather than retired: the next tab this page grows registers with
// `ready: false` and becomes selectable only when its body lands.
//
// `ready` is MECHANICAL, not a comment. A registered tab whose body does not exist yet must
// not be SELECTABLE, or it opens onto an empty panel — and an empty panel behind a real
// `role="tab"` is worse than an absent tab, because the tablist announces "3 of 6" and hands
// the keyboard user somewhere with nothing in it. The flag is required by the type (not
// optional-defaulting-to-true) precisely so that adding a tab and forgetting the flag is a
// compile error rather than a shipped empty panel.
// ---------------------------------------------------------------------------

export interface RepositoryDetailTab {
  key: string;
  label: string;
  /**
   * Does this tab have a BODY in the shipped app?
   *
   * `false` ⇒ registered but not selectable: the tab is not rendered in the strip and the
   * keyboard model never lands on it. Each owning task flips its own flag to `true` in the same
   * commit that adds the body — that is the only edit this registry needs from them.
   */
  ready: boolean;
}

export const REPOSITORY_DETAIL_TABS: readonly RepositoryDetailTab[] = [
  { key: 'overview', label: 'Overview', ready: true },
  // The per-stage deployment history over `agentOpsApi.deployments`, with the OWNER-gated rollback
  // confirm (E28/T12). It CONSUMES `collapseByBuild` below rather than carrying a second copy of
  // the started-row rule — the whole reason that function is exported from here.
  { key: 'deployments', label: 'Deployments', ready: true },
  // The repository's pull requests, opened/approved/merged as the caller's own linked GitHub
  // account (E28/T14). `ready: true` says only that a BODY now exists — whether a given ORG can
  // serve pull requests is a separate RUNTIME fact (the App's `pull_requests` grant is manual per
  // org and GitHub does not retro-apply a manifest change), decided by `prTabVisibility` in
  // `repo-tabs/pullRequestsTab.ts` and reported upward by the tab. Overloading `ready` with that
  // would make this registry lie about what the epic built, and lie PER ORG on a value that is
  // global to the build.
  { key: 'pull-requests', label: 'Pull requests', ready: true },
  { key: 'access', label: 'Access', ready: true },
  // Project-scoped Langfuse metrics with this repository's agent broken out (E28/T12). D13 holds:
  // "not instrumented" is a DISTINCT third state, so an unconfigured environment and a failed read
  // each say so rather than rendering as 0.
  { key: 'observability', label: 'Observability', ready: true },
  // The five-artifact inventory, read through the READ-ONLY delete-preview probe (E28/T12).
  { key: 'resources', label: 'Resources', ready: true },
] as const;

/** The tabs a user may actually open, in registry order. */
export function selectableTabKeys(): string[] {
  return REPOSITORY_DETAIL_TABS.filter((t) => t.ready).map((t) => t.key);
}

/**
 * May this tab be opened? An unknown key is `false` — a stale deep link or a typo must not
 * select something, and the page falls back to the first selectable tab.
 */
export function isTabSelectable(key: string): boolean {
  return REPOSITORY_DETAIL_TABS.some((t) => t.key === key && t.ready);
}

/**
 * DOM ids tying a tab to its panel — one derivation each, so the two can never disagree.
 * The `repo-` prefix keeps them distinct from `projectDetailTabs`' `project-` and
 * `settingsSections`' `settings-`: three surfaces derive tab ids now, and a shared prefix
 * would collide the day two of them render on one page.
 */
export function tabId(key: string): string {
  return `repo-tab-${key}`;
}
export function tabPanelId(key: string): string {
  return `repo-tabpanel-${key}`;
}

// ---------------------------------------------------------------------------
// The environment strip (C5).
//
// ONE ROW PER STAGE THE API RETURNS. Never a hardcoded pair, and a stage the API does not
// return does not render — zero stages is an honest empty state, not a fabricated dev row.
// The tenant record is the authority on which stages EXIST; deployment history for a retired
// stage must not resurrect a row for it.
//
// Stages are sorted ALPHABETICALLY. A fixed order array naming the two conventional stages
// would be the same hardcode wearing a hat, so the sort key is a data property (the stage's own
// name) and stays stable and meaningful for any stage set a tenant carries.
//
// Note this file contains no quoted stage name AT ALL, not even inside a comment. The guard
// that enforces C5 reads the raw source and does not skip comments — deliberately, because a
// guard that has to decide what is "only a comment" is a guard a comment-shaped hit can defeat.
//
// A GLOBAL ENVIRONMENT SWITCHER IS EXPLICITLY REJECTED by the design: every stage shows at
// once, because hiding the comparison is the incident class ("I thought I was in dev").
// ---------------------------------------------------------------------------

export interface EnvironmentRowSource {
  /**
   * The tenant's stage map, as the API returns it (`Record<string, TenantStageConfig>` since
   * E28/T11 — it was wrongly typed as a dev+prod pair). `null` when the tenant could not be
   * resolved, which renders NO rows rather than a guessed default pair.
   */
  stages: Record<string, TenantStageConfig> | null | undefined;
  /**
   * The tenant could not be RESOLVED — so which stages exist is unknown.
   *
   * The counterpart of `historyError` one level up, and it exists for the same reason (E28 final
   * review, FR-1). `stages: null` collapsed two situations that must not read alike: a tenant that
   * genuinely carries no stages, and a tenant nobody could look up. Both produced zero rows, and
   * the strip printed "no environments are configured for this tenant" — a DEFINITE claim about the
   * tenant, derived from a read that never happened, while silently deleting the whole
   * what-is-running-where answer this page exists to give.
   *
   * All THREE paths that reach it are live and none is exotic: the project read threw (its catch
   * swallows everything, so the tenant id is never learned); the admin tenant directory read failed
   * (that hook is fail-silent BY DESIGN); or the repository's tenant is simply not among a
   * non-admin caller's own memberships. On the live-test surface a hiccup in any of them reads as
   * "the strip is broken" or, worse, as the tenant genuinely having no stages.
   *
   * Set it when a tenant id IS known and no record for it could be found. When the tenant id
   * itself is absent there is nothing to have failed to resolve, and this stays false.
   */
  stagesUnknown?: boolean;
  /** The agent's deployment history, any order. */
  deployments: readonly Deployment[];
  /**
   * The history read FAILED — we do not know what any stage is running.
   *
   * Distinct from an empty history, and the distinction is the whole reason this field exists.
   * "This stage has never shipped" is a definite statement about production; "we could not ask"
   * is not, and rendering the second as the first is the reassuring direction on the one row
   * whose question is "what is live?". Same rule as `undefined` runtime ⇒ unknown-never-ready
   * and a failed count ⇒ em-dash-never-zero: absent data is not good news.
   *
   * When set, every row reports `historyUnknown` and NOTHING is derived from the (empty)
   * history — no version, no actor, no drift, no in-flight claim.
   */
  historyError?: boolean;
}

/**
 * How an actor is DISPLAYED, with the currency it came from attached.
 *
 * A GitHub login and an Entra oid are two different currencies and must never be rendered as
 * one (E27A §6): one is proven by the provider's OIDC token, the other by an Entra token, and
 * AGP joins neither to the other. So the kind travels with the string and the `@` prefix marks
 * the provider's currency specifically. `unknown` exists because an actor with no `actor_kind`
 * must not be GUESSED into one — prefixing an unlabelled oid with `@` would present a platform
 * id as a GitHub handle.
 */
export interface DeploymentActor {
  kind: 'github' | 'entra' | 'unknown';
  display: string;
}

/**
 * The hover text naming the CURRENCY an actor string is in — one entry per `kind`, no
 * `default` branch, so a fourth kind is a `tsc` error naming this table (the C3 idiom).
 *
 * It lives here rather than in the `.tsx` because it is the same three-way decision `actorOf`
 * makes, and a two-way re-derivation of it in a ternary is what shipped `unknown` under
 * "GitHub login" — asserting a provider identity that was never established, in the one place
 * whose job is to keep the two currencies apart.
 */
export const ACTOR_KIND_TITLE: Record<DeploymentActor['kind'], string> = {
  github: 'GitHub login',
  entra: 'Entra object id',
  // Not "unknown user": the actor is known, its CURRENCY is not. The row carried no
  // `actor_kind`, so nothing establishes whether this is a provider login or a platform id.
  unknown: 'Actor recorded without an identity provider — not known to be a GitHub login',
};

export interface EnvironmentRow {
  /** The stage's name, verbatim from the tenant record. Free-form (D8). */
  stage: string;
  /** The image tag this stage is RUNNING, or null when it has never deployed. */
  imageTag: string | null;
  /** `source_sha` git-short (7), or null — a build-written row carries none by design (C1). */
  shortSha: string | null;
  /** When that image landed (ISO-8601, as delivered), or null. */
  deployedAt: string | null;
  /** Who deployed it, in its own currency — or null for a build-written row (no actor). */
  actor: DeploymentActor | null;
  /** True when an attempt for this stage is currently running. */
  inFlight: boolean;
  /** Set only when this stage is behind a newer image that succeeded somewhere. */
  drift: { behindTag: string } | null;
  /** The stage's AWS region, from the tenant config. Display-only. */
  region: string;
  /**
   * The history read FAILED, so nothing about what this stage is running is known.
   *
   * Every other field is null/false when this is true — NOT because the stage is empty, but
   * because there is no evidence either way. The renderer must say so rather than fall back to
   * "Never deployed", which is a claim.
   */
  historyUnknown: boolean;
}

/** Trim to a real value, or null. Blank strings are absence, not data. */
function text(value: string | null | undefined): string | null {
  const trimmed = (value ?? '').trim();
  return trimmed.length > 0 ? trimmed : null;
}

/** The stage names a tenant carries, alphabetically. Never a hardcoded order. */
export function sortedStageNames(
  stages: Record<string, TenantStageConfig> | null | undefined,
): string[] {
  return Object.keys(stages ?? {}).sort((a, b) => a.localeCompare(b));
}

/** Newest first by `started_at`, with `seq_key` breaking a same-millisecond tie. */
function newestFirst(rows: readonly Deployment[]): Deployment[] {
  return [...rows].sort((a, b) =>
    a.started_at === b.started_at
      ? b.seq_key.localeCompare(a.seq_key)
      : b.started_at.localeCompare(a.started_at),
  );
}

// ---------------------------------------------------------------------------
// collapseByBuild — ONE ATTEMPT, TWO ROWS. The rule every reader of this partition needs.
//
// **T12 MUST IMPORT THIS, NOT RE-DERIVE IT.** T12 owns the deployment-history list, which
// answers the same two questions off the same partition. Two copies of this rule is the drift
// this epic exists to remove, so it is exported and tested here and T12 consumes it.
//
// THE SHAPE OF THE DATA. The `deployment` partition is APPEND-ONLY (C1): nothing ever updates
// a row, so ONE delivery attempt can be TWO rows sharing a `build_id`.
//
//   • the `started` row — written by AGP when the build is REQUESTED. It is the ONLY row that
//     carries `actor` + `actor_kind` (the Entra oid of the promoter, or the OIDC-proven GitHub
//     login) and the ONLY row that carries `source_sha`.
//   • the TERMINAL row (`succeeded` / `failed`) — written by the buildspec, which is the only
//     party that knows the outcome. It carries NO actor, NO actor_kind and NO source_sha BY
//     DESIGN: a build has no human actor, and the buildspec has no commit sha in scope.
//     `buildspec.yml:104-106` states the reader contract this implements verbatim: "any reader
//     collapsing the two rows for one build_id must take source_sha from the `started` row,
//     never from this one."
//
// WHAT GOES WRONG WITHOUT THE COLLAPSE — both directions, and both were shipped:
//   • `some(outcome === 'started')` reports a deploy in progress FOREVER, because nothing ever
//     closes a `started` row. One successful deploy and every stage wears a permanent amber
//     "Deployment in progress" — the worst kind of stale, on the page an operator opens to find
//     out whether something is still running.
//   • taking the actor/sha from the newest SUCCEEDED row yields null every time, for every
//     stage, forever — the succeeded row structurally has neither. "Deployed by" and the short
//     sha would be permanently em-dash while the platform DOES know who promoted: it is on the
//     `started` row that got discarded.
//
// THE RULE.
//   • Group by `build_id`. A `started` row whose build_id also has a terminal row is FINISHED.
//   • A NULL/absent build_id is UN-COLLAPSIBLE — nothing joins it to a partner, so a `started`
//     row without one stays in flight. Refusing to guess is the point: a build_id-less started
//     row might be a live deploy, and silently retiring it would hide a running one.
//   • A terminal row with NO `started` partner is KEPT AS IS (the build ran while AGP was
//     unreachable, so nobody wrote a started row). A reader that assumed a pair would silently
//     drop the only record of a real deployment.
//   • The collapsed attempt takes its OUTCOME and TIMING from the terminal row (which is the
//     one that knows), and its ACTOR + SOURCE_SHA from the started row (the only one that has
//     them).
//   • CAVEAT for T12: the grouping key is `build_id` ALONE, so a started row would be closed
//     against a terminal row of a DIFFERENT stage if a build_id were ever shared across stages.
//     Unreachable today — one build deploys one stage — but if that ever changes, the key must
//     become `build_id` + `stage`.
// ---------------------------------------------------------------------------

/** One delivery ATTEMPT — the two append-only rows for one build, read as one fact. */
export interface DeploymentAttempt {
  /** The row the outcome and timing come from: the terminal row, or the started row alone. */
  row: Deployment;
  /** Terminal outcome, or `started` while still running. */
  outcome: Deployment['outcome'];
  /** True only for a `started` row with no terminal partner. */
  inFlight: boolean;
  /** From the STARTED row when one exists — the terminal row carries none (C1). */
  actor: DeploymentActor | null;
  /** From the STARTED row when one exists, git-short (7). */
  shortSha: string | null;
}

export function collapseByBuild(rows: readonly Deployment[]): DeploymentAttempt[] {
  // Which build_ids have a terminal row? Only a non-blank build_id can join two rows.
  const closed = new Set<string>();
  for (const r of rows) {
    const build = text(r.build_id);
    if (build !== null && r.outcome !== 'started') closed.add(build);
  }
  // The started row per build_id, for the actor/sha the terminal row cannot carry. Newest
  // first, so a build_id somehow reused takes the most recent request.
  const startedByBuild = new Map<string, Deployment>();
  for (const r of newestFirst(rows)) {
    const build = text(r.build_id);
    if (build !== null && r.outcome === 'started' && !startedByBuild.has(build)) {
      startedByBuild.set(build, r);
    }
  }

  const attempts: DeploymentAttempt[] = [];
  for (const row of newestFirst(rows)) {
    const build = text(row.build_id);
    if (row.outcome === 'started') {
      // Superseded by its own terminal row — that row represents this attempt instead.
      if (build !== null && closed.has(build)) continue;
      attempts.push({
        row,
        outcome: 'started',
        inFlight: true,
        actor: actorOf(row),
        shortSha: shortShaOf(row),
      });
      continue;
    }
    // A terminal row. Its partner supplies what it structurally cannot carry; with no partner
    // (build ran while AGP was unreachable) it stands alone and reports its own fields.
    const started = build === null ? undefined : startedByBuild.get(build);
    attempts.push({
      row,
      outcome: row.outcome,
      inFlight: false,
      actor: actorOf(started ?? row),
      shortSha: shortShaOf(started ?? row),
    });
  }
  return attempts;
}

/**
 * What a stage is actually RUNNING is its newest SUCCEEDED attempt — not its newest row.
 *
 * The distinction is the point. The newest row for a stage may be a `failed` attempt or one
 * still `started`, and naming either as the deployed version would report an image that never
 * served traffic as what production is serving. That is the same class of confident-but-wrong
 * claim `opsStatus.ts` exists to prevent, on the one row whose question is "what is live?".
 *
 * Reads COLLAPSED attempts, so the returned attempt carries the actor and sha off the
 * `started` row of the same build — the succeeded row has neither (see `collapseByBuild`).
 */
function currentFor(attempts: readonly DeploymentAttempt[], stage: string): DeploymentAttempt | null {
  return attempts.find((a) => a.row.stage === stage && a.outcome === 'succeeded') ?? null;
}

function shortShaOf(row: Deployment): string | null {
  return text(row.source_sha)?.slice(0, 7) ?? null;
}

function actorOf(row: Deployment): DeploymentActor | null {
  const actor = text(row.actor);
  // A build-written row deliberately carries NO actor (C1). Absent is the honest rendering —
  // "unknown user" would assert that a human did it and we lost their name.
  if (actor === null) return null;
  const kind = text(row.actor_kind);
  if (kind === 'github') return { kind: 'github', display: `@${actor.replace(/^@/, '')}` };
  if (kind === 'entra') return { kind: 'entra', display: actor };
  return { kind: 'unknown', display: actor };
}

/**
 * One row per stage the tenant carries, alphabetically.
 *
 * DRIFT is per-stage and cross-stage: a stage is "behind" when the newest image that succeeded
 * ANYWHERE differs from the one this stage is running. That answers the operator's actual
 * question ("is what this environment serving still current?") for an open stage set, without
 * naming a privileged pair of stages — which a dev-vs-prod comparison would have to.
 *
 * A stage with NO deployment is never reported as drifted. "Behind" needs something to be
 * behind FROM; a stage that has never shipped has never shipped, which is a different and more
 * accurate statement, and calling it drift would put a warning on every new repo.
 *
 * DECIDED, not accidental: the yardstick scans ALL attempts, including ones for a stage the
 * tenant no longer carries, so a live stage can read "behind <tag>" for a tag that appears in no
 * rendered row. That is the intended reading of "succeeded anywhere" — the newest image the
 * platform ever shipped is still the newest image, and pretending a retired stage's release never
 * happened would understate how stale a stage is. The alternative (restrict the yardstick to
 * rendered stages) would make the same stage's drift answer change when a tenant's stage set is
 * edited, which is worse.
 */
export function environmentRows(source: EnvironmentRowSource): EnvironmentRow[] {
  const stages = source.stages ?? {};
  const names = sortedStageNames(stages);
  // The history read FAILED ⇒ derive NOTHING from it. A failed read arrives as an empty array,
  // and an empty array is indistinguishable from "this agent has never deployed" — so the
  // caller says which it was, and every row reports unknown rather than "Never deployed".
  const unknown = source.historyError === true;
  const attempts = unknown ? [] : collapseByBuild(source.deployments);
  // The newest image that succeeded anywhere — the yardstick drift is measured against.
  const newestSucceeded = attempts.find((a) => a.outcome === 'succeeded') ?? null;

  return names.map((stage) => {
    const current = currentFor(attempts, stage);
    const imageTag = current === null ? null : text(current.row.image_tag);
    const newestTag = newestSucceeded === null ? null : text(newestSucceeded.row.image_tag);
    const behind =
      newestTag !== null && imageTag !== null && newestTag !== imageTag
        ? { behindTag: newestTag }
        : null;
    return {
      stage,
      imageTag,
      shortSha: current?.shortSha ?? null,
      deployedAt: current === null ? null : text(current.row.started_at),
      actor: current?.actor ?? null,
      // COLLAPSED, so a `started` row whose build has finished is not still in flight. Before
      // the collapse this was `some(outcome === 'started')`, and nothing ever closes a started
      // row — so one successful deploy left every stage permanently "in progress".
      inFlight: attempts.some((a) => a.row.stage === stage && a.inFlight),
      drift: behind,
      region: stages[stage]?.region ?? '',
      historyUnknown: unknown,
    };
  });
}

// ---------------------------------------------------------------------------
// environmentStripState — WHY IS THE STRIP EMPTY? (E28 final review, FR-1)
//
// The strip had ONE empty state and needs two, because the two situations behind it are not the
// same fact and only one of them is a statement about the tenant:
//
//   • `no-stages` — the tenant record WAS read and carries no stage. That is a real, if unusual,
//     configuration and the strip may say so. `stages: Dict[str, TenantStageConfig]` has no
//     minimum on the backend, so an empty map is representable and reachable.
//   • `stages-unknown` — the tenant was never resolved. Nothing about its stages was established,
//     so a sentence about how it is configured is a claim with no evidence under it. Three live
//     paths reach this; they are enumerated on `EnvironmentRowSource.stagesUnknown`.
//
// The distinction runs in BOTH directions on purpose. A fix that reported every empty strip as
// unreadable would only have moved the dishonesty: a tenant that really has no stages should say
// so, and an operator who sees "could not be read" on a correctly-configured tenant would go
// hunting for an outage that is not there.
//
// It is a STATE with a copy table rather than a boolean the `.tsx` branches on, so the wording is
// pinned by a test and cannot drift back toward the confident version, and so the guard can assert
// that BOTH members actually reach the screen. That last part is not theoretical: the existing
// `historyError` flag is INERT in this path — with zero rows there is no row to report
// `historyUnknown` on — which is exactly how a correctly-derived flag ends up rendering nothing.
// ---------------------------------------------------------------------------

export type EnvironmentStripState = 'rows' | 'no-stages' | 'stages-unknown';

/**
 * The sentence each empty state shows, keyed by state. `Record` with no `default` branch, so a
 * third empty state is a `tsc` error naming this table (the C3 idiom).
 *
 * `no-stages` states a configuration; `stages-unknown` states the limits of our read and never
 * mentions how the tenant is set up, because that is the thing it does not know.
 */
export const ENVIRONMENT_EMPTY_COPY: Record<Exclude<EnvironmentStripState, 'rows'>, string> = {
  'no-stages': 'No environments are configured for this tenant.',
  'stages-unknown':
    'Environments could not be read — this repository’s tenant did not resolve, so which stages exist is unknown. This is not a report that the tenant has none.',
};

export function environmentStripState(source: EnvironmentRowSource): EnvironmentStripState {
  if (environmentRows(source).length > 0) return 'rows';
  // Checked only once there are no rows: an unresolved tenant cannot produce rows, but if a
  // caller ever passes both, the rows are real data and outrank a flag about their absence.
  return source.stagesUnknown === true ? 'stages-unknown' : 'no-stages';
}

// ---------------------------------------------------------------------------
// materializeSummary — the ONE-WORD verdict over the Overview's step timeline.
//
// THE BUG THIS REPLACES (E28 final review, FR-2). The header read
// `isMaterializeTerminal(steps) ? 'Complete' : 'In progress'`, and `isMaterializeTerminal`
// answers "HAS THE RUN STOPPED?", not "DID IT SUCCEED?" — its own comment says so: a `failed`
// step is terminal, because a failure HALTS the run and the trailing steps never execute. Those
// are the right semantics for the polling predicate it was written for (stop asking), and other
// callers depend on them, so it is untouched. Using it to answer a DIFFERENT question is the bug:
// a repository whose materialize FAILED rendered the word **Complete** directly above its own
// rose failed step — good news printed over a failure, which is the inversion D13 forbids and the
// single most misleading thing this card could say.
//
// The three-way answer already existed one function over. `nextBadgeFromSteps` distinguishes
// failed / all-done / still-running and is what the materialize modal and the repository lists
// already read, so this takes ITS output as input rather than re-deriving the walk over the steps.
// That is the whole point: two independent derivations of "how did materialize go?" is the drift
// this epic exists to remove, and the same repository must not read failed in the modal and
// complete here.
//
// ABSENT STEPS ARE NOT "IN PROGRESS". The second branch was the same error in the other
// direction: an empty `steps[]` rendered "In progress" — a positive claim that work is running
// right now, derived from the ABSENCE of any step record. A record that lists no steps has not
// established that anything is happening; a pre-E25C repository has no timeline at all. So it says
// the run was not recorded. (`nextBadgeFromSteps` alone cannot express this: it answers
// `provisioning` for an empty list, which is correct for a pill it only ever renders over a live
// materialize run, and wrong as a sentence about a record that carries no steps.)
// ---------------------------------------------------------------------------

/** The steps' own verdict, as `nextBadgeFromSteps` reports it. */
export type StepBadgeKey = keyof typeof OPS_BADGE;

export interface MaterializeSummary {
  /** The word the card's header shows. Never a positive claim the steps do not support. */
  label: string;
  /** True only when every recorded step is `done` — the ONE state that may read as success. */
  succeeded: boolean;
}

export function materializeSummary(
  badge: StepBadgeKey,
  stepCount: number,
): MaterializeSummary {
  // No steps ⇒ no evidence either way. Checked BEFORE the badge, because the badge's answer for an
  // empty list is about a run in flight and there is no run here to be in flight.
  if (stepCount === 0) return { label: 'Not recorded', succeeded: false };
  if (badge === 'failed') return { label: 'Failed', succeeded: false };
  if (badge === 'ready') return { label: 'Complete', succeeded: true };
  return { label: 'In progress', succeeded: false };
}

// ---------------------------------------------------------------------------
// runtimeScope — HOW HONESTLY MAY THE RUNTIME ANSWER BE PLACED ON THIS PAGE?
//
// This is the trap in the environment strip, and the reasoning matters more than the code.
//
// THE CONFLICT. C5 gives the strip a per-stage `Runtime status` column. The platform has
// exactly ONE runtime answer per agent: the agent envelope holds a single `agent_arn`, which
// `buildspec.yml` writes with no stage branch, so whichever stage deployed last overwrites it
// and the boto3 probe behind `GET /agents/{id}/runtime` cannot know which stage's runtime it
// just described. The route returns `stage: "unknown"` in practice, and `opsStatus.ts:111-130`
// states that contract on the field itself.
//
// THE TEMPTING WRONG ANSWERS, and why each is refused:
//   • Render the one answer in EVERY stage's row. This is the worst option, because it is
//     invisible: three rows each showing "Ready" reads as three probes, and an operator would
//     conclude prod is healthy on the strength of a reading that may have come from uat. It
//     manufactures per-stage evidence out of one agent-level fact.
//   • Caption the pill with a stage name (a "prod runtime" pill). Same error, stated out loud.
//   • Drop the Runtime column and show nothing. Also wrong: the runtime answer IS the second
//     independent state machine this whole epic exists to surface, and hiding it because it
//     cannot be attributed loses a real fact to a presentation problem.
//   • Fabricate a per-stage probe matrix. Not available; the data does not exist.
//
// THE RESOLUTION. Runtime is rendered ONCE, agent-level, in the page header beside the
// delivery pill (the two independent pills the design asks for) — where its scope is
// truthfully the agent. The strip's Runtime column is then explicitly labelled
// not-attributable-per-stage rather than filled in, so the column's presence documents the
// platform's actual limitation instead of papering over it. A reader cannot come away
// believing any particular stage was probed, because no stage row makes a runtime claim.
//
// WHY THIS IS A FUNCTION RATHER THAN A COMMENT (finding M-d). "Never caption a runtime pill by
// stage" was enforced only by comments across T5/T10. Here it is mechanical: `runtimeScope`
// is the ONLY way the strip obtains the runtime reading, and for an unattributable one it
// returns `stage: null`. There is no stage name available to caption with, so the mistake is
// unreachable rather than merely discouraged — and a test pins it.
//
// FORWARD-COMPATIBLE, NOT HARDCODED to "always unattributable": the day the envelope becomes
// per-stage, a probe that names a real stage IS attributable and this returns `kind: 'stage'`.
// A reading whose status is `unknown` stays agent-scoped whatever stage it claims — an
// unreachable control plane has established nothing to attribute.
// ---------------------------------------------------------------------------

/** The sentence the strip's Runtime column shows INSTEAD of a per-stage pill. */
export const RUNTIME_SCOPE_NOTE =
  'Not attributable per stage — the platform reports one runtime per agent';

export interface RuntimeScope {
  /** `agent` ⇒ the reading belongs to the agent, not to any stage. */
  kind: 'agent' | 'stage';
  /** The stage the reading is KNOWN to describe. `null` whenever `kind` is `agent`. */
  stage: string | null;
  /** Why the column is not filled in. `null` when the reading is genuinely per-stage. */
  note: string | null;
}

export function runtimeScope(
  runtime: Pick<RuntimeStatus, 'stage' | 'status'> | null | undefined,
): RuntimeScope {
  const stage = text(runtime?.stage);
  const status = text(runtime?.status);
  const attributable = stage !== null && stage !== 'unknown' && status !== null && status !== 'unknown';
  return attributable
    ? { kind: 'stage', stage, note: null }
    : { kind: 'agent', stage: null, note: RUNTIME_SCOPE_NOTE };
}

// ---------------------------------------------------------------------------
// repoBackLink — WHERE DOES THIS PAGE'S BACK LINK GO, AND WHAT DOES IT SAY? (E28C/T7, D-C4b)
//
// WHY THIS IS A FUNCTION AND NOT AN INLINE TEMPLATE STRING, which is the judgement I got wrong the
// first time. I descoped this selector on the argument that `projectName ?? project_id` is a
// coalesce rather than a decision — true as far as it goes, and beside the point: the thing that can
// regress is the DESTINATION. Review proved it by re-pointing the loaded render back at the fleet
// list, and the whole suite stayed green, because the only assertions covering 4b were about the
// three early returns and the 18 untouched call sites. The one spec item in the task with no seam
// was the one the task existed for.
//
// So both halves live here now: a test can state "a repository's back link goes to its parent
// PROJECT, labelled with the project's name" over real values, and a mutation of either half fails
// an assertion about behaviour rather than about the shape of a source line.
//
// THE DESTINATION IS THE PARENT PROJECT because a repository has exactly one, and `project_id` is on
// the record — so the link survives a refresh, a deep link and a bookmark, which is what the reported
// defect actually was. The fleet list is the correct destination only for a reader who arrived from
// it, and a two-level jump for the far more common arrival from the project's own repositories tab.
//
// THE LABEL NAMES THE PLACE, so the link reads as somewhere to return to rather than as a category.
// It falls back to the raw `project_id` — the same expression the header cell renders — because an
// opaque id is still a TRUE reference, whereas "← Project" would tell an operator nothing about which
// project they are going back to. `null`/blank name ⇒ the id, never an empty arrow.
// ---------------------------------------------------------------------------

/** The arrow prefix, one place, so the two label forms cannot disagree about their leader. */
const BACK_ARROW = '←';

export interface RepoBackLink {
  /** The route to return to. */
  to: string;
  /** The link's text, arrow included. */
  label: string;
}

export function repoBackLink(input: {
  projectId: string;
  projectName?: string | null;
}): RepoBackLink {
  const name = text(input.projectName);
  return {
    to: `/ops/projects/${input.projectId}`,
    label: `${BACK_ARROW} ${name ?? input.projectId}`,
  };
}

// ---------------------------------------------------------------------------
// stageRuntimeCell — WHAT DOES ONE STAGE'S RUNTIME CELL SHOW? (E28C/T7, D-C4c)
//
// The day `runtimeScope` was written forward-compatible for has arrived, and this is what
// arrived with it. The backend has accepted `?stage=` since E28A (`agents.py:695-731`): asked
// about a stage the agent owns, it probes THAT stage's runtime; asked about one it does not, it
// answers not-deployed with no AWS call rather than falling through to another stage's reading.
// The frontend simply never asked — so every stage row carried the not-attributable note,
// including the rows that were one query parameter away from a real answer.
//
// THE COLUMN'S OLD BEHAVIOUR IS NOT REMOVED, IT BECOMES THE FALLBACK. `runtimeScope` and
// `RUNTIME_SCOPE_NOTE` are untouched and still decide attributability — this function DELEGATES
// to them rather than forming a second opinion, because two notions of "is this reading
// attributable?" would drift exactly as the forked status tables did, and this is the reading
// that fails in the harmful direction. A legacy scalar-only record still answers with the
// unattributable sentinel and still gets the note, which is precisely why the note survives.
//
// THREE OUTCOMES, and the third is the one that is easy to get wrong:
//   • `pill`   — attributable TO THIS STAGE. The row may make a runtime claim.
//   • `note`   — a reading exists but cannot be attributed here. The column keeps documenting
//                the platform's limitation instead of filling itself in.
//   • `absent` — no reading AT ALL (the probe failed, or was never made). An em dash, NOT the
//                note: the note is a claim about attributability, and there is nothing here to
//                make a claim about. And never a pill — absent data must not render as status.
//
// THE STAGE MUST MATCH, and that check is this function's own contribution. A reading
// attributable to one stage establishes nothing about a different one, so a mismatched entry
// falls back to the note rather than captioning this stage's pill with another stage's answer —
// the same fabrication `runtimeScope` exists to make unreachable, one level down.
//
// `status` is the NARROWED key, so the `.tsx` indexes the shared `RUNTIME_LABEL` /
// `RUNTIME_BADGE_KEY` tables and never re-narrows a wire string.
//
// AND THE NARROWING HAPPENS BEFORE THE ATTRIBUTION CHECK, which closes a gap this task's own
// test found. `runtimeScope` compares the RAW `status` against the unattributable sentinel, so
// it answers "attributable" for any status string that is merely UNRECOGNIZED — and an
// unrecognized status narrows to that very sentinel at render time. Left alone, a stage row
// would have shown a "Runtime unknown" PILL, captioned to a stage, built out of a value this
// platform version cannot interpret: a per-stage claim derived from no evidence, which is the
// exact error class the column exists to prevent. So the rule `runtimeScope` states — an
// unreachable/unreadable control plane has established nothing to attribute — is applied to the
// NARROWED key here rather than to the raw string. `runtimeScope` is deliberately not modified:
// it is pinned by E28's tests, its raw comparison is correct for every value the route actually
// produces, and the fix belongs in the function that decides to place a pill on a stage row.
// ---------------------------------------------------------------------------

export interface StageRuntimeCell {
  kind: 'pill' | 'note' | 'absent';
  /** The narrowed status to render. Non-null ONLY when `kind` is `pill`. */
  status: RuntimeStatusKey | null;
  /** Why no pill. Non-null ONLY when `kind` is `note`. */
  note: string | null;
}

export function stageRuntimeCell(
  stage: string,
  reading: Pick<RuntimeStatus, 'stage' | 'status'> | null | undefined,
): StageRuntimeCell {
  // No reading at all — say nothing rather than something.
  if (reading === null || reading === undefined) {
    return { kind: 'absent', status: null, note: null };
  }
  // Narrow FIRST, then ask about attribution — see the comment above for the gap this closes.
  const status = toRuntimeStatus(reading.status);
  const scope = runtimeScope({ stage: reading.stage, status });
  // Attributable AND about the stage being rendered. Both halves are required.
  if (scope.kind === 'stage' && scope.stage === stage) {
    return { kind: 'pill', status, note: null };
  }
  return { kind: 'note', status: null, note: RUNTIME_SCOPE_NOTE };
}

// ---------------------------------------------------------------------------
// runtimeStatusTitle — WHY does the runtime say that? (E29/T11, OB-15)
//
// THE GAP THIS CLOSES. `RuntimeStatus.detail` is a safe short hint the backend has always
// returned and NO frontend consumer has ever rendered. Harmless while the hints were
// interchangeable — and no longer harmless as of E29/T10, which made `detail` carry the ONE
// distinction the status field itself cannot express.
//
// `unknown` is produced by two situations that mean opposite things to an operator:
//
//   • THE RUNTIME IS IN A STATE THIS BUILD DOES NOT RECOGNIZE. The probe SUCCEEDED. The platform
//     answered, and the answer was a status word (a deliberate DELETING, or a value newer than
//     this frontend). Something is genuinely happening to that runtime.
//   • THE PROBE COULD NOT BE MADE OR COULD NOT COMPLETE. Credentials, throttling, an unreachable
//     control plane, or — on Databricks — no reader configured at all. Nothing whatsoever has been
//     established about the runtime; it may be perfectly healthy.
//
// Both render the identical "Runtime unknown" pill, and T10 deliberately spent its effort keeping
// those two apart in the backend (`unknown` vs `not_deployed`, `_safe_probe_detail`, the
// never-raises degraded path that says "no Databricks reader is configured"). Dropping the hint on
// the floor threw that away at the last inch: an operator seeing "Runtime unknown" cannot tell
// whether to go look at the runtime or at AGP's own credentials.
//
// A TOOLTIP, NOT A REDESIGN. The hint qualifies a pill that already exists and says the right
// word; it is a second sentence about the same fact, not a new status. So it rides on `title`
// (and, where the surface already shows prose, one quiet line) rather than becoming another
// badge — a new visual element for "why" would compete with the status it is explaining.
//
// WHY A FUNCTION FOR WHAT LOOKS LIKE STRING CONCATENATION. Because the two halves are not
// symmetric and the composition is the decision: the SCOPE note (what the platform can attribute)
// and the DETAIL hint (what the probe found) can both be present, both absent, or either alone,
// and they must not be run together into one undifferentiated sentence. `null` here means "no
// `title` attribute at all" — an empty tooltip is a worse affordance than none, and a `title=""`
// is a hover target that says nothing. Every arm is reachable from a test.
//
// THE DETAIL IS NEVER RENDERED AS THE STATUS. It is a hint, so it can be absent, and a caller must
// not fall back to it as a label — the pill's word comes from `RUNTIME_LABEL` through the narrowed
// key, exactly as before. This function only ever produces a tooltip.
// ---------------------------------------------------------------------------

export function runtimeStatusTitle(
  reading: Pick<RuntimeStatus, 'detail'> | null | undefined,
  scopeNote?: string | null,
): string | null {
  const detail = text(reading?.detail);
  const scope = text(scopeNote);
  if (detail === null) return scope;
  if (scope === null) return detail;
  // Both. The scope note describes what the reading CAN be attributed to and the detail describes
  // what the probe FOUND — two different claims, so they are separated rather than concatenated
  // into a sentence that reads as one.
  return `${scope} — ${detail}`;
}

// ---------------------------------------------------------------------------
// prodServingState — WHAT ESTABLISHES THAT PRODUCTION IS SERVING? (E28A/T4, finding #5)
//
// THE BUG THIS FIXES, and it is a data-model error rather than a coding slip.
// `ungovernedInProd` asked `toCicdStatus(cicd_status) === 'deployed'`. But `buildspec.yml:391`
// runs its terminal delivery write for EVERY stage with no branch — so `cicd_status` is a
// DELIVERY fact with NO STAGE IN IT, and the banner read it as a PRODUCTION claim. Observed
// live: a repository whose only successful deploy went to a non-production stage displayed
// "Serving production without governance approval" with nothing in production at all and no
// promotion ever attempted. The predicate was true when only promote wrote that value and
// stopped being true the day the other path did too — so it now fires on every new repository,
// which is the "teach the operator to ignore it" failure its own comment warned against.
//
// THE FIX IS TO DROP `cicd_status` FROM THIS QUESTION ENTIRELY. The delivery PILL keeps it (it
// is an honest delivery fact and that is what the pill claims); only the banner stops using it.
//
// NO SCALAR ON THE REPOSITORY RECORD PROVES PRODUCTION IS SERVING:
//   • `last_promoted_at` is `_promotion_in_flight`'s CLOCK. It is stamped on promote's success
//     AND failure paths, and by an any-stage rollback. It proves an attempt.
//   • `last_promoted_image_tag` is stamped OPTIMISTICALLY, before the apply succeeds (finding
//     #10). It proves an attempt too — the live record claimed production served an image it
//     had never served.
//   • `last_promotion_build_id` is the ONLY one JOINABLE TO AN OUTCOME, and the join was
//     verified byte-identical on live data against the production row's `build_id` — whose
//     outcome was `failed`, i.e. exactly the state the old predicate answered "deployed" for.
//
// SO THE ANSWER IS A JOIN, NOT A READ: the promotion build id, matched against the append-only
// deployment history, whose terminal row is the only party that knows how the build ended.
//
// THE ROW IS IDENTIFIED BY THAT JOIN AND NEVER BY `row.stage` (C5 — no stage literal appears in
// this file and none may). The join is what makes this derivation stage-literal-free, and it
// covers production ROLLBACKS for free, because a rollback stamps the same trio. A test asserts
// the verdict is UNCHANGED when every row's stage is a nonsense value, so the join can never
// silently regress into a stage comparison.
//
// A FAILED PROMOTION IS NOT AN EMPTY PRODUCTION — the second hop, and why branch 5 is two
// questions rather than one. A newer failed promote OVERWRITES `last_promotion_build_id` while the
// stage carries on serving the image an OLDER promote put there. Answering `none` off the failed
// join alone was silence over a production that may genuinely be serving an unapproved image —
// the exact false-silence class this function exists to remove, and the on-screen contradiction
// of the Deployments tab, which would name that stage's current image on the same page.
//
// So branch 5 reads the STAGE OFF THE FAILED PROMOTION'S OWN ROW — a value read off data, never a
// literal (C5) — and asks whether that stage has any succeeded row. This is the same derivation
// `deploymentsTab.ts`'s `stageHistories` already makes: group on whatever stage strings the rows
// carry, and let a succeeded row be what establishes serving. `serving` when one exists, `none`
// only when the failed promote is all there ever was.
//
// WHAT REMAINS UNRESOLVED, STATED RATHER THAN PAPERED OVER: the best-effort deployment-row write
// can be LOST, so a build that really did deliver production may have no row to join to. Where
// the lost row is the PROMOTION's, that lands in branch 6 → `unknown`, the slate banner. Where
// the lost row is the OLDER SUCCEEDED one, branch 5's second hop finds nothing and answers
// `none` — so an incomplete history is still capable of false silence here, and this function
// cannot detect it: an unwritten row and a stage that never shipped are the same empty filter.
// Closing that needs a durable write on the backend, not a second frontend join.
// ---------------------------------------------------------------------------

export type ProdServingState = 'serving' | 'unknown' | 'none';

export function prodServingState(input: {
  /** `repo.last_promotion_build_id` — the only promotion scalar joinable to an outcome. */
  lastPromotionBuildId: string | null | undefined;
  /** `repo.last_promoted_at` — the in-flight clock. Evidence of an ATTEMPT only. */
  lastPromotedAt: string | null | undefined;
  /** `repo.last_promoted_image_tag` — stamped optimistically (#10). An ATTEMPT only. */
  lastPromotedImageTag: string | null | undefined;
  /** The agent's deployment history, any order. Empty when unread — hence the two flags. */
  deployments: readonly Deployment[];
  /** The history read FAILED, so the join has nothing to join to and cannot conclude. */
  historyError?: boolean;
  /** The history read is STILL IN FLIGHT. A loading page makes no claim. */
  historyLoading?: boolean;
}): ProdServingState {
  const buildId = text(input.lastPromotionBuildId);
  const attempted =
    buildId !== null ||
    text(input.lastPromotedAt) !== null ||
    text(input.lastPromotedImageTag) !== null;
  // 1. NOTHING WAS EVER STAMPED ⇒ no promotion was even attempted. THIS IS WHAT KILLS #5: a
  //    repository whose only successful delivery went to a non-production stage reaches here,
  //    and the answer is a definite "nothing is in production" rather than an alarm.
  if (!attempted) return 'none';
  // 2. STILL LOADING ⇒ no claim, in either direction. Checked before the error branch because a
  //    read in flight has not failed; flashing "approval unknown" for one paint and then
  //    withdrawing it is how an operator learns to disbelieve the banner.
  if (input.historyLoading === true) return 'none';
  // 3. THE HISTORY COULD NOT BE READ, and a promotion demonstrably WAS attempted. An unread
  //    history arrives as an empty array, indistinguishable from "nothing ever shipped" — so
  //    this must not borrow that silence. Absent data is not good news.
  if (input.historyError === true) return 'unknown';
  if (buildId === null) {
    // A promotion stamped without a build id (the pre-E27 shape) is UNJOINABLE. There is
    // evidence of an attempt and no way to reach its outcome, which is the definition of
    // unknown — guessing from the two optimistic scalars is the error this function exists to
    // stop making.
    return 'unknown';
  }
  // 4. THE JOIN. A row is this promotion's row when its `build_id` matches, whatever stage it
  //    names. The terminal row is the only one that knows the outcome (C1), and a succeeded one
  //    is the only evidence that production actually serves this image.
  const rows = input.deployments.filter((d) => text(d.build_id) === buildId);
  if (rows.some((d) => d.outcome === 'succeeded')) return 'serving';
  // 5. A TERMINAL FAILURE for that build ⇒ the promotion did not deliver, so nothing is serving
  //    FROM IT — regardless of the optimistic image-tag stamp beside it (#10). This is the live
  //    case, and it is exactly where the old predicate answered "deployed".
  //
  //    But "this promotion delivered nothing" is NOT "nothing is in production": a newer failed
  //    promote overwrites the stamp while the stage carries on serving the image an OLDER promote
  //    put there. So the second hop — read the stage off the failed promotion's OWN ROW as a
  //    VALUE and ask whether that stage has any succeeded row at all. One exists ⇒ something is
  //    serving there and the governance question is live. None ⇒ the failed promote is all there
  //    ever was, and `none` is the honest answer (this is the branch that keeps #5 fixed).
  const failed = rows.find((d) => d.outcome === 'failed') ?? null;
  if (failed !== null) {
    // Both sides real, as everywhere else in this join: a blank stage on each side is two
    // unknowns, and grouping those together would join unrelated rows — the error branch 3b
    // refuses. The succeeded row found here necessarily belongs to a DIFFERENT build, because
    // branch 4 has already returned for every row carrying this promotion's build id.
    const stage = text(failed.stage);
    const stillServing =
      stage !== null &&
      input.deployments.some((d) => text(d.stage) === stage && d.outcome === 'succeeded');
    return stillServing ? 'serving' : 'none';
  }
  // 6. Otherwise: no row for this build (a lost best-effort write), or only a `started` row (the
  //    promotion is still running). Either way the outcome is not established.
  return 'unknown';
}

// ---------------------------------------------------------------------------
// ungovernedInProd (D11) — an agent SERVING PRODUCTION that governance never approved.
//
// BOTH conditions, never one: production is ESTABLISHED as serving AND the agent's registry
// lifecycle is still `proposed`. Each alone is an ordinary state — a serving approved agent is
// the happy path, and a `proposed` agent that has shipped nothing is just a new registration —
// so flagging on either would make the warning meaningless and teach the operator to ignore it.
// Together they are the one combination that says the platform is running something in
// production that the governance surface never signed off.
//
// ITS FIRST ARGUMENT IS A PRODUCTION VERDICT, NOT A DELIVERY STATUS (E28A/T4). It used to take
// `cicd_status`, which carries no stage and is written for every one of them — see
// `prodServingState` for the whole reasoning. The signature change IS the fix: there is no
// delivery status to be handed here any more, so the category error is unreachable rather than
// merely discouraged. Only `serving` qualifies; `unknown` is a question and is answered one
// level up by `prodGovernanceState`, in slate, never in amber.
//
// The lifecycle value needs narrowing only for blankness: it is a backend enum with one writer.
//
// This STATES the condition and never offers a remedy. The lifecycle pill on this page is
// read-only (D11) — an Ops surface that grew an approve button would be the shadow-governance
// failure mode this epic forbids. The approval lives on the governance surface.
// ---------------------------------------------------------------------------
export function ungovernedInProd(
  serving: ProdServingState,
  lifecycleState: string | null | undefined,
): boolean {
  return serving === 'serving' && text(lifecycleState) === 'proposed';
}

// ---------------------------------------------------------------------------
// prodGovernanceState — the banner's THREE answers, because absence of an alarm is a claim too.
//
// `ungovernedInProd` needs the agent's `lifecycle_state`, and the agent read on this page is
// BEST-EFFORT: its catch leaves the record null, so the lifecycle argument arrives `undefined` and
// the predicate answers `false` — no banner. That is correct for the question it was asked ("is this
// repo known to be ungoverned in prod?") and wrong as the page's whole behaviour (E28 final review).
// An ABSENT ALARM reads as "checked, nothing wrong": it is absent-data-as-no-bad-news, the exact rule
// this epic applies everywhere else, on the page's highest-consequence statement.
//
// The three states, and why the middle one has to exist:
//   • `ungoverned` — production established as serving AND the agent proposed. The full warning.
//   • `unknown`    — a governance question we could not answer. TWO independent sources of
//     uncertainty now feed it (E28A/T4): the governance record could not be read, or PRODUCTION
//     ITSELF is unestablished. Either way something might be serving production unapproved and we
//     cannot say. That is worth saying and is NOT the same sentence as the warning: it accuses
//     nobody and asks the operator to check.
//   • `none`       — nothing is in production, or the record was read and does not say
//     `proposed`. Silence is earned here.
//
// `agentRead` is a separate argument rather than inferred from a null lifecycle because the two are
// genuinely different: a record that WAS read may legitimately carry a blank lifecycle (a pre-E4
// envelope), and that is a read we made — it must not raise an alarm about our own ability to read.
// Only the read itself failing does.
//
// WHAT `serving: 'unknown'` MUST NOT DO is raise the AMBER banner. The unresolved gap it carries
// (a lost deployment-row write — see `prodServingState`) makes the uncertainty genuine, and an
// amber "serving production without
// governance approval" over an unproven production is the false accusation that gets a banner
// disbelieved. Slate says the true thing: we do not know.
// ---------------------------------------------------------------------------

export type ProdGovernanceState = 'ungoverned' | 'unknown' | 'none';

export function prodGovernanceState(input: {
  /** The PRODUCTION verdict from `prodServingState` — never a delivery status (#5). */
  serving: ProdServingState;
  lifecycleState: string | null | undefined;
  /** False when the agent record could not be read at all. */
  agentRead: boolean;
}): ProdGovernanceState {
  if (ungovernedInProd(input.serving, input.lifecycleState)) return 'ungoverned';
  // Is there a governance question here at all? Either the record says `proposed` (so approval is
  // outstanding) or we could not read it (so we do not know whether it does).
  const question = text(input.lifecycleState) === 'proposed' || !input.agentRead;
  if (!question) return 'none';
  // A question, and production is established as serving ⇒ only the unread-record case can reach
  // here (the `proposed` one is already `ungoverned` above).
  if (input.serving === 'serving') return 'unknown';
  // A question, and whether production is serving is UNESTABLISHED. Not the warning — nothing is
  // proven — but not silence either, which would be absent-data-as-no-bad-news on the page's
  // highest-consequence statement.
  if (input.serving === 'unknown') return 'unknown';
  // `serving === 'none'`: nothing is in production, so there is no production governance question
  // to raise however the agent's record reads. This is #5's end state — a repository whose only
  // successful delivery went to a non-production stage now correctly says nothing.
  return 'none';
}

// ---------------------------------------------------------------------------
// shouldPollRepo — MUST THIS PAGE ASK AGAIN? (E28D, small-fixes map §4)
//
// The page had no poller. It read the record once per load, and `handleRetry` read it a second time
// at the instant the retry was accepted — which is the moment the record is at its LEAST final
// (steps reset to pending, delivery back to provisioning). Nothing asked again, so the surface
// whose selling point is that the materialize timeline is permanently viewable here showed a frozen
// one for the whole run. Two pollers already existed against this exact record and endpoint — the
// create modal's and the project tab's promoting rows — and the page built to watch a run had none.
//
// TWO conditions, and neither implies the other:
//
//   • THE MATERIALIZE RUN has steps left to move. Asked through the shared stopped-predicate, so
//     "which step states mean it is over" has one definition. `steps.length > 0` is part of the
//     question rather than a guard on it: that predicate answers `true` for an empty array (nothing
//     is running or pending, correctly), and an empty timeline is not evidence of a finished one —
//     a record with no steps has established nothing, so it must not arm a poller by itself.
//   • DELIVERY is in flight. A promote runs long after materialize is terminal, so the first
//     condition is false for its entire duration; without this the page would sit on
//     "Promoting to prod…" while the project tab's row for the same repo settled. This is the same
//     condition the project tab's poller uses, reached through the narrowing boundary so an
//     unrecognized wire value lands on `unknown` and arms nothing.
//
// A repo that satisfies neither does ZERO polling. That is the rule, not an optimization: a
// 3-second request for a record that will not change is the kind of background traffic nobody
// notices until it is a bill, and every existing poller here already obeys it.
// ---------------------------------------------------------------------------

export function shouldPollRepo(repo: {
  cicd_status: string | null | undefined;
  steps: readonly StepState[];
}): boolean {
  const materializeMoving = repo.steps.length > 0 && !isMaterializeTerminal([...repo.steps]);
  return materializeMoving || isCicdInFlight(toCicdStatus(repo.cicd_status));
}

// ---------------------------------------------------------------------------
// deliveryHeaderState — the header may not claim "Not built yet" over a running build (§5)
//
// Observed live: the delivery pill read "Not built yet" directly above the environment strip's
// "Deployment in progress", on the same screen, for the whole duration of every build. Both
// statements were TRUE of their own source, which is what made this structural rather than a bug in
// either half:
//
//   • the pill reads `cicd_status`, one scalar with no stage in it, whose `ready` means
//     "materialize finished, no build has landed yet" — hence the label, which is right;
//   • the strip reads the deployment HISTORY, which DOES know a build is running, because the
//     launch row exists with no terminal partner yet.
//
// The gap is that the buildspec writes only TERMINAL outcomes. There is no intermediate write
// anywhere, which is exactly why `building` was deleted from the delivery union — a member no
// writer produces is a branch no reality reaches. That reasoning stands. So the union is untouched
// and the header takes a DERIVED state instead: "Building…" is a VIEW word assembled from two facts
// this page already holds, and the wire's vocabulary keeps meaning only what a writer wrote.
//
// (A writer in the buildspec is the better long-term answer — the fleet list loads no history and
// so can derive nothing — but it is an infra change with its own live test. This fixes the
// contradiction on the one page where both facts are on screen.)
//
// FAILS SAFE. A failed history read arrives as rows that report unknown, and "Building…" derived
// from a read that never happened would be a confident sentence about a build nobody observed. The
// record's own word survives, because the record WAS read. Same rule as an absent runtime reading
// unknown rather than ready.
//
// AND IT ONLY SPEAKS WHERE THE RECORD IS SILENT. `provisioning` (materialize is running),
// `promoting` (a PROD build is running) and `failed` are all more specific than "a build is
// running", so they are never overridden — three machines already contend for the word "ready" on
// this page and "Building…" must not become a fourth ambiguity. It means precisely: a delivery
// build is running, and the record has not said anything more specific.
// ---------------------------------------------------------------------------

/** The derived word. Its own constant, so the pill and the tests read one string. */
export const DELIVERY_BUILDING_LABEL = 'Building…';

export interface DeliveryHeaderState {
  /** The pill's label — the derived word, or the record's own from the shared table. */
  label: string;
  /** Whether the pill wears the in-flight treatment (a spinner instead of a resting dot). */
  inFlight: boolean;
}

export function deliveryHeaderState(input: {
  /** The RAW wire value; narrowed here, never asserted. */
  cicdStatus: string | null | undefined;
  /** The strip's OWN rows, so the two surfaces cannot derive different answers (only `inFlight` is read). */
  environmentRows: readonly Pick<EnvironmentRow, 'inFlight'>[];
  /** The history read failed ⇒ derive nothing from it. Optional: absent means it did not fail. */
  historyError?: boolean;
}): DeliveryHeaderState {
  const cicd = toCicdStatus(input.cicdStatus);
  const recordSaysInFlight = isCicdInFlight(cicd);
  // Only the two states that mean "the record knows of no build running right now" may be
  // overridden. Everything else is either more specific or louder, and both must win.
  const overridable = cicd === 'ready' || cicd === 'deployed';
  const historyKnown = input.historyError !== true;
  const buildRunning = historyKnown && input.environmentRows.some((r) => r.inFlight);
  if (overridable && buildRunning) {
    return { label: DELIVERY_BUILDING_LABEL, inFlight: true };
  }
  return { label: CICD_LABEL[cicd], inFlight: recordSaysInFlight };
}

// ---------------------------------------------------------------------------
// recordStatusLabel — the THIRD state machine's word (E28A, finding #12's frontend half).
//
// `repo.status` had no surface at all until this page rendered it RAW, and raw is wrong for two
// reasons that compound:
//
//   1. IT IS A BARE LOWERCASE WIRE VALUE sitting in a field beside two sentence-case pills.
//   2. IT SHARES A WORD WITH BOTH OF THEM AND MEANS SOMETHING ELSE BY IT. `opsStatus.ts` already
//      refused this collision once: delivery's `ready` is labelled "Not built yet" precisely
//      because the runtime machine's `ready` means "the agent is up and serving". A third bare
//      `ready` on the same page is that same ambiguity a third time — and it is the WEAKEST of
//      the three claims wearing the strongest-sounding word. (NO PRODUCER WRITES `ready` HERE YET:
//      the field's writers put `provisioning` and `failed` in it, so that row of the table is
//      pre-placed for the writer a later task adds rather than currently reachable. The table has
//      to be in place before the writer, not after, or the raw word ships for one release.)
//
// So the labels name the ONE run this field tracks. `repo.status`' only writers are creation, the
// failure path, and finalize — i.e. the MATERIALIZE run, not the repository's health, which the
// two pills answer for their own machines.
//
// AN UNRECOGNIZED VALUE IS SHOWN VERBATIM, never mapped to a plausible neighbour — the
// `toCicdStatus` rule one level down. Passing it through says "the record holds this", which is
// true; "Materializing" over a value nobody established would be a confident sentence about a
// repository we know nothing about. Case is deliberately NOT normalized into a match: both writers
// are backend enums, so an off-case value did not come from them and guessing what it meant is the
// invention this refuses.
// ---------------------------------------------------------------------------

/**
 * The record status → its label. `Record<string, string>` rather than a closed union because the
 * backend enum is not mirrored in TS; the lookup miss is the honest verbatim path above.
 */
export const RECORD_STATUS_LABEL: Record<string, string> = {
  provisioning: 'Materializing',
  ready: 'Materialized',
  failed: 'Materialize failed',
};

/** The label, or `null` when the record carries no status at all (the caller renders absence). */
export function recordStatusLabel(raw: string | null | undefined): string | null {
  const value = text(raw);
  if (value === null) return null;
  return RECORD_STATUS_LABEL[value] ?? value;
}

// ---------------------------------------------------------------------------
// promotionArtifact — WHAT EXACTLY WOULD AN APPROVAL APPROVE? (E28B/T6, D-B3)
//
// This is the promote surface's answer to the one question the epic's headline guarantee rests on,
// and it has three answers rather than two.
//
// THE GUARANTEE, and why it is conditional. Promotion's contract is an APPROVED IMAGE DIGEST, not a
// branch: "promotion needs to guarantee *these exact bytes were approved*, and a digest is that
// guarantee by construction". `promote_repo` hands `prod_candidate_digest` to the deploy verbatim
// and the buildspec deploys `<repo>@<digest>` rather than re-resolving the tag — because between the
// approval and the deployment the tag may point at different bytes. That is not hypothetical: the
// tenant registry is mutable and the image build is not reproducible (a floating base image, ranged
// deps, no lockfile).
//
// BUT `prod_candidate_digest` IS OPTIONAL BY DESIGN. A materialized repo carries a COMMITTED copy of
// its build workflow, so a repo whose `build.yml` predates this epic keeps registering candidates
// with no digest, and `record_prod_candidate` takes them tag-only (`image_digest: str = ""`) rather
// than refusing them — making it required would have 422'd every pre-epic repo, turning a missing
// optimization into a total outage of the deploy path. That trade is right, AND it means the
// guarantee holds only where the deployed template is current.
//
// WHICH IS WHY THE THIRD ANSWER EXISTS. Before this, `tag-only` and `digest` were indistinguishable
// on screen: both rendered an image tag and nothing else, so a surface promising byte-exact approval
// showed the same thing whether or not it could deliver one. The gap was ASSUMED CLOSED because
// nothing rendered it — absent-data-as-no-bad-news, on the highest-consequence claim this surface
// makes. `tag-only` is a CAUTION, not a fault: it is a known, accepted, self-healing state (the
// repo's next template update ships a digest-posting workflow), so it must not read as an error and
// must never block the button. The owner needs to know they are approving a pointer; they do not need
// to be told their repository is broken.
//
// `none` is the third, and it is NOT a caution: with no candidate there is nothing to approve at all
// (`canPromote` already withholds the button), so a warning there would fire on every repository
// between merges — the cry-wolf failure `repoAction` refuses for the same reason.
//
// GATED ON THE CANDIDATE BEING REAL, via the same LITERAL `'pending'` comparison `canPromote`,
// `prodCandidateView` and `repoAction` make. Anything else is not a candidate, so a leftover digest
// beside a consumed or unrecognized status must not describe an approvable artifact — the button and
// this description would then disagree about whether anything is waiting.
// ---------------------------------------------------------------------------

export type PromotionArtifactKind = 'digest' | 'tag-only' | 'none';

/**
 * WHY THIS IS ONE VALUE AND NOT A PAIR OF RENDER BRANCHES — the review finding that reshaped it.
 *
 * The first cut of this derivation returned `{kind, imageTag, digest}` and left the component to
 * branch: `artifact.digest !== null ? <digest/> : <caution/>`. A reviewer made the caution arm
 * unreachable (`: (false && ( … ))`) and THE WHOLE SUITE STAYED GREEN — 969 passed with the marker
 * impossible to render. Both guards were satisfied by dead code: `toContain('PROMOTION_TAG_ONLY_LABEL')`
 * matches an identifier inside an arm nothing evaluates, and the branch regex matched the branch
 * that was still there. That is the ninth vacuous guard in this project.
 *
 * IT CANNOT BE FIXED BY A BETTER SOURCE ASSERTION, and that is the point. `vitest.config.ts`
 * collects only `src/**\/*.test.ts`, there is no jsdom and no testing-library, so NO rendered-output
 * assertion is possible here at all — any guard over a `.tsx` is a guard over text, and text cannot
 * distinguish a live branch from a dead one.
 *
 * SO THE BRANCH IS REMOVED RATHER THAN GUARDED. This returns the marker ALREADY RESOLVED — its text,
 * its tooltip and a semantic tone — and the component renders that single value with no condition of
 * its own. A dead render branch is then not merely detectable, it is unrepresentable: there is
 * nothing to make unreachable. The decision moved into the `.ts`, where a test reaches it directly,
 * which is the same move `runtimeScope` made for "never caption a runtime pill by stage" (finding
 * M-d) — mechanical rather than discouraged.
 *
 * TONE IS A SEMANTIC KEY, NEVER A CLASS STRING. `opsLabels.ts` states the rule this module follows:
 * a pure module returns strings and keys, and each surface applies its own classes. The `.tsx` maps
 * `tone` through one indexed table, so the caution's amber cannot be reached by a branch either.
 */
export interface ArtifactMarker {
  /**
   * The text on screen. The abbreviated DIGEST where one exists, and the tag-only caution's label
   * where none does — so the marker is what an approval is actually about, in one place.
   */
  text: string;
  /** The tooltip: the caution's full explanation and remedy, or null for a pinned digest. */
  note: string | null;
  /**
   * `pinned` ⇒ the approval names exact bytes. `caution` ⇒ it names a mutable pointer.
   *
   * Two keys and not a boolean, because the `.tsx`'s table is `Record<ArtifactTone, string>` with no
   * default branch — a third state later is a `tsc` error naming that table rather than a marker
   * that silently inherits whichever side of a ternary it lands on. That is exactly how
   * `SHOW_FILTER_STRIP` was broken once.
   */
  tone: 'pinned' | 'caution';
}

export interface PromotionArtifact {
  /** Which of the three states. `none` ⇒ nothing is approvable and there is no marker. */
  kind: PromotionArtifactKind;
  /** The resolved marker, or null EXACTLY when `kind` is `none`. The component renders this. */
  marker: ArtifactMarker | null;
  /**
   * The candidate's image tag as the record holds it, WHATEVER the kind.
   *
   * Deliberately not nulled out for `kind: 'none'` (a review finding). It was, on the argument that
   * a consumed candidate must not be describable — but `kind` already carries approvability, so
   * blanking the tag as well made a present field unreachable through this selector and left the
   * next consumer to read the record directly, forking the derivation. The tag is a fact about the
   * record; `kind` is the judgement about it. Consumers gate on `kind`.
   */
  imageTag: string | null;
}

export function promotionArtifact(
  repo:
    | {
        prod_candidate_status?: string | null;
        prod_candidate_image_tag?: string | null;
        prod_candidate_digest?: string | null;
      }
    | null
    | undefined,
): PromotionArtifact {
  const imageTag = text(repo?.prod_candidate_image_tag);
  // No candidate ⇒ nothing to approve, so NO MARKER. Checked first: a stale digest left on a
  // consumed candidate is not an approvable artifact, and `none` must not render a caution — that
  // would fire on every repository between merges, the cry-wolf failure `repoAction` refuses.
  if (repo?.prod_candidate_status !== 'pending') {
    return { kind: 'none', marker: null, imageTag };
  }
  const digest = shortDigest(repo.prod_candidate_digest);
  if (digest === null) {
    return {
      kind: 'tag-only',
      marker: { text: PROMOTION_TAG_ONLY_LABEL, note: PROMOTION_TAG_ONLY_NOTE, tone: 'caution' },
      imageTag,
    };
  }
  return { kind: 'digest', marker: { text: digest, note: null, tone: 'pinned' }, imageTag };
}

// ---------------------------------------------------------------------------
// headerActions — WHICH of the three actions may this caller be OFFERED?
//
// An operator lands on this page precisely WHEN a repo is broken, so a detail page that reports
// a failure it cannot act on is the wrong shape. All three are EXISTING capabilities on their
// EXISTING gates — nothing new is built here and there is no second path:
//
//   • promote — `canPromote`. The STRICT owner gate: the design-§3 ungoverned fallback does NOT
//     reach promote, so a pre-migration project still needs a real OWNER row. Suppressed while
//     a delivery is in flight, because the route answers a second promote with a 409.
//   • retry   — `mayMaintainProject`, NOT plain `meetsRole(…, 'maintainer')`. The retry route
//     gates through `_require_project_role_or_ungoverned`, so it DOES get the §3 fallback;
//     gating on the role alone would hide the only recovery path on every ungoverned project.
//     Offered only when a materialize step actually FAILED — the route 409s "Nothing to retry"
//     otherwise, and an affordance whose every click is refused is not an affordance.
//   • destroy — `canDestroy` (owner-or-admin), the same gate the project surface uses for the
//     irreversible E23 five-item cascade. Offered whatever the repo's state: a half-materialized
//     repo is exactly the one an operator needs to tear down.
//
// Each is CONDITIONALLY RENDERED rather than `disabled` (the epic's FE constraint): `disabled`
// is reserved for an in-flight request, so a caller without the standing is never shown a
// button whose every click 403s.
// ---------------------------------------------------------------------------
export interface HeaderActions {
  promote: boolean;
  retry: boolean;
  destroy: boolean;
}

export function headerActions(input: {
  held: ProjectRoleName | null;
  roleLevel: number;
  ungoverned: boolean | null | undefined;
  cicdStatus: string | null | undefined;
  prodCandidateStatus: string | null | undefined;
  steps: readonly Pick<StepState, 'status'>[];
}): HeaderActions {
  const cicd = toCicdStatus(input.cicdStatus);
  return {
    promote:
      canPromote(input.held, input.roleLevel, input.prodCandidateStatus ?? null) &&
      !isCicdInFlight(cicd),
    retry:
      mayMaintainProject(input.held, input.roleLevel, input.ungoverned) &&
      input.steps.some((s) => s.status === 'failed'),
    destroy: canDestroy(input.held, input.roleLevel),
  };
}
