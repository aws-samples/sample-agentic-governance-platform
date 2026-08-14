// repositoryDetailTabs.test.ts — the pure companion behind `/ops/repositories/:id`
// (E28/T11, contracts C5 + D11).
//
// Everything the repository detail page DECIDES lives in `repositoryDetailTabs.ts` because
// vitest collects only `src/**/*.test.ts`: a judgement made inside a `.tsx` is a judgement no
// test can reach, which is how the forked status tables shipped a production repo in
// provisioning's amber. Same split T10 used for `repoRowModel.ts`.
//
// The four judgements pinned here, and why each is load-bearing:
//
//   • TAB VISIBILITY — six tabs are REGISTERED, and T11 builds two of them. A tab whose body
//     lands in T12/T14 must not be SELECTABLE yet, or it opens onto an empty panel. That is
//     mechanical (`ready`), not a comment.
//   • THE ENVIRONMENT STRIP — one row per stage the API returns, and NOTHING for a stage it
//     does not. Zero stages is an honest empty state, not a fabricated dev row.
//   • RUNTIME ATTRIBUTION — the strip has a per-stage Runtime column and the platform has
//     exactly ONE agent-wide runtime answer. Rendering that one answer in every stage's row
//     would claim each stage was probed. `runtimeScope` refuses to.
//   • UNGOVERNED-IN-PROD — `deployed` AND `proposed`, both, never one.
//
// Plus source-as-text guards over the `.tsx` files (the `settingsSections.test.ts:352` idiom),
// for the two rules that would otherwise be comment-only: no stage literal, and no
// stage-captioned runtime pill.

import { describe, expect, it } from 'vitest';

import {
  ACTOR_KIND_TITLE,
  DELIVERY_BUILDING_LABEL,
  ENVIRONMENT_EMPTY_COPY,
  RECORD_STATUS_LABEL,
  REPOSITORY_DETAIL_TABS,
  RUNTIME_SCOPE_NOTE,
  collapseByBuild,
  deliveryHeaderState,
  environmentRows,
  environmentStripState,
  headerActions,
  isTabSelectable,
  materializeSummary,
  prodGovernanceState,
  prodServingState,
  promotionArtifact,
  recordStatusLabel,
  repoBackLink,
  runtimeScope,
  runtimeStatusTitle,
  selectableTabKeys,
  shouldPollRepo,
  sortedStageNames,
  stageRuntimeCell,
  tabId,
  tabPanelId,
  ungovernedInProd,
  type EnvironmentRowSource,
  type ProdServingState,
} from './repositoryDetailTabs';
// The delivery label table, read as a VALUE: the derived header must fall back to EXACTLY what the
// pill shows today, and asserting that against the table rather than against re-typed strings is
// what keeps the two from drifting while both look correct in isolation.
import { CICD_LABEL } from './opsStatus';
import { nextTabKey } from './projectDetailTabs';
// The tag-only caution's copy, asserted to be what the MARKER carries — so the derivation and the
// pinned constants cannot drift apart while both look correct in isolation.
import { PROMOTION_TAG_ONLY_LABEL, PROMOTION_TAG_ONLY_NOTE } from './repoRowModel';
// The promote dialog's tone table (E28C/T7). Imported as a VALUE rather than read as source text —
// which is the reason it was moved into a `.ts` companion: the tint is a judgement about whether an
// approval names exact bytes, and indexing it here checks the string that actually reaches the DOM
// instead of regexing a window of a `.tsx` that may have drifted out from under the slice.
import { ARTIFACT_TONE_CLS } from './promoteConfirm';
// The three-way step verdict the page now reads. Imported so the summary is tested end-to-end from
// the SAME function the materialize modal and both repository lists read — a copy of its walk over
// the steps here would prove only that the copy agrees with itself.
import { nextBadgeFromSteps } from './ProjectRepositoriesTab';
import pureSrc from './repositoryDetailTabs.ts?raw';
import projectDetailSrc from './ProjectDetail.tsx?raw';
import clientSrc from '../../api/client.ts?raw';
// This file's own source. The stage-literal guard is applied to it as well as to the four
// modules it covers — a rule that exempts the file enforcing it is a rule with a hole in it.
import ownSrc from './repositoryDetailTabs.test.ts?raw';
import repositoryDetailSrc from './RepositoryDetail.tsx?raw';
import environmentStripSrc from './EnvironmentStrip.tsx?raw';
// The fleet row — its runtime pill is the third consumer OB-15 wired (E29/T11).
import repoRowTsxSrc from './RepoRow.tsx?raw';
// `OpsPage`'s frame + the router. The frame owns the DEFAULT back label (one literal, so its 18
// other call sites keep their wording); the router is read so the back link's route is pinned against
// the pattern `App.tsx` actually mounts rather than against a second copy of the string (D-C4b).
import opsPageSrc from './OpsPage.tsx?raw';
import appSrc from '../../App.tsx?raw';
import { OPS_BACK_LABEL } from './OpsPage';
// The product's ONE promote dialog (E28C/T7, D-C4d). The reachability guard and the tone table's
// tint assertions were re-pointed here when 4d extracted the confirm out of the project tab's row:
// the dead-caution-arm mutation lives wherever the marker is rendered, and this is now the surface
// where that render decides whether a production approval names bytes or a mutable pointer.
import promoteConfirmSrc from './PromoteConfirm.tsx?raw';
import tabStripSrc from './TabStrip.tsx?raw';
// Vite's `?raw` rather than `node:fs`: `tsconfig.app.json` sets `types: ["vite/client"]` (no
// `node`), so `node:fs` has no declarations here while `vite/client` declares `*?raw`. Same
// idiom as `settingsSections.test.ts` / `operationsNav.test.ts`. The explicit `.tsx` is
// load-bearing on a case-insensitive filesystem, where a name differing from a sibling module
// only in casing resolves to the sibling.

// ---------------------------------------------------------------------------
// The registry
// ---------------------------------------------------------------------------

describe('REPOSITORY_DETAIL_TABS — the registry', () => {
  it('is non-empty (guards every assertion below against a vacuous pass)', () => {
    expect(REPOSITORY_DETAIL_TABS.length).toBeGreaterThan(0);
  });

  it('registers EXACTLY the six pinned tabs, in the pinned order', () => {
    expect(REPOSITORY_DETAIL_TABS.map((t) => t.key)).toEqual([
      'overview',
      'deployments',
      'pull-requests',
      'access',
      'observability',
      'resources',
    ]);
  });

  it('carries the pinned labels', () => {
    expect(REPOSITORY_DETAIL_TABS.map((t) => t.label)).toEqual([
      'Overview',
      'Deployments',
      'Pull requests',
      'Access',
      'Observability',
      'Resources',
    ]);
  });

  it('every key is a unique slug', () => {
    const keys = REPOSITORY_DETAIL_TABS.map((t) => t.key);
    for (const k of keys) expect(k, k).toMatch(/^[a-z][a-z0-9-]*$/);
    expect(new Set(keys).size).toBe(keys.length);
  });
});

// ---------------------------------------------------------------------------
// Tab visibility — the plan's first required test
// ---------------------------------------------------------------------------

describe('tab visibility — a registered tab is not automatically a selectable one', () => {
  it('only the tabs whose body EXISTS are selectable', () => {
    // ALL SIX bodies now exist: T11 built Overview + Access, T12 added Deployments, Observability
    // and Resources, T14 added Pull requests. Registering all six up front fixed the strip's shape
    // and order, so each task added a body and flipped one flag rather than re-deciding the page's
    // information architecture. In REGISTRY order, not flip order.
    expect(selectableTabKeys()).toEqual([
      'overview',
      'deployments',
      'pull-requests',
      'access',
      'observability',
      'resources',
    ]);
  });

  it('`ready` is a BUILD-TIME flag and every tab now has a body', () => {
    // The registry is fully `ready` as of T14 — which is exactly why this flag can no longer be
    // the mechanism for a RUNTIME capability. `ready` answers "does this body exist in the shipped
    // app?"; whether a given ORG can serve pull requests is a per-org fact with a different
    // lifetime (the App's `pull_requests` permission is a manual grant GitHub does not
    // retro-apply), and overloading `ready` with it would make this registry lie about what the
    // epic built — per org, on a value that is global to the build.
    //
    // That second mechanism is `prTabVisibility` in `repo-tabs/pullRequestsTab.ts`, pinned by its
    // own test, and `RepositoryDetail` applies it as a SECOND filter beside `ready`.
    expect(REPOSITORY_DETAIL_TABS.filter((t) => !t.ready)).toEqual([]);
    // The flag's mechanism is still intact even with nothing to suppress: an unregistered key is
    // still unselectable, so a future tab that forgets its body cannot ship as an empty panel.
    expect(isTabSelectable('no-such-tab')).toBe(false);
  });

  it('every tab carries an explicit `ready` flag — never an implicit default', () => {
    // A missing flag defaulting to selectable is how an empty panel ships. The type requires
    // it; this asserts nobody satisfied the type with `undefined`.
    for (const t of REPOSITORY_DETAIL_TABS) expect(typeof t.ready, t.key).toBe('boolean');
  });

  it('an unknown key is not selectable', () => {
    expect(isTabSelectable('no-such-tab')).toBe(false);
  });

  it('the default tab is a selectable one', () => {
    expect(selectableTabKeys()[0]).toBe('overview');
    expect(isTabSelectable('overview')).toBe(true);
  });

  it('the keyboard model walks the RENDERED tabs, in registry order', () => {
    // The roving-tabindex arithmetic is `projectDetailTabs.nextTabKey`, REUSED rather than
    // re-derived (a fourth copy of index-with-wraparound is a fourth chance to get it wrong).
    const keys = selectableTabKeys();
    expect(nextTabKey(keys, 'overview', 'ArrowRight')).toBe('deployments');
    expect(nextTabKey(keys, 'deployments', 'ArrowRight')).toBe('pull-requests');
    expect(nextTabKey(keys, 'resources', 'ArrowRight')).toBe('overview'); // wraparound
    expect(nextTabKey(keys, 'overview', 'ArrowLeft')).toBe('resources');
    expect(nextTabKey(keys, 'overview', 'End')).toBe('resources');
    expect(nextTabKey(keys, 'resources', 'Home')).toBe('overview');
    expect(nextTabKey(keys, 'overview', 'Enter')).toBeNull();
    // And it never offers a tab that cannot be opened, from ANY starting position.
    for (const from of keys) {
      for (const pressed of ['ArrowRight', 'ArrowLeft', 'Home', 'End']) {
        const next = nextTabKey(keys, from, pressed);
        expect(next && isTabSelectable(next), `${from}/${pressed}`).toBe(true);
      }
    }
  });

  it('the keyboard model steps over a tab HIDDEN AT RUNTIME, not merely a bodyless one', () => {
    // The property this suite originally pinned — stepping over a not-yet-ready tab — went vacuous
    // when T14 made every `ready` true. But the property still MATTERS, because there is now a
    // second and permanent reason a registered tab may be absent: an org whose App lacks the
    // `pull_requests` grant never renders Pull requests (E28/T14, A3), and it sits BETWEEN two
    // rendered tabs in the registry.
    //
    // So the guard moves to the set `RepositoryDetail` actually feeds `TabStrip`: the registry
    // filtered by `ready` AND by runtime visibility. Fed that, ArrowRight from Deployments must
    // reach Access without stopping on a tab this org cannot serve.
    const rendered = selectableTabKeys().filter((k) => k !== 'pull-requests');
    expect(rendered).toEqual(['overview', 'deployments', 'access', 'observability', 'resources']);
    expect(nextTabKey(rendered, 'deployments', 'ArrowRight')).toBe('access');
    expect(nextTabKey(rendered, 'access', 'ArrowLeft')).toBe('deployments');
    // …and focus can never land on the hidden tab from anywhere.
    for (const from of rendered) {
      for (const pressed of ['ArrowRight', 'ArrowLeft', 'Home', 'End']) {
        expect(nextTabKey(rendered, from, pressed), `${from}/${pressed}`).not.toBe(
          'pull-requests',
        );
      }
    }
  });
});

describe('DOM id derivations', () => {
  it('are one derivation each, so a tab and its panel can never disagree', () => {
    expect(tabId('overview')).toBe('repo-tab-overview');
    expect(tabPanelId('overview')).toBe('repo-tabpanel-overview');
  });

  it('a tab id can never collide with a panel id', () => {
    const ids = REPOSITORY_DETAIL_TABS.flatMap((t) => [tabId(t.key), tabPanelId(t.key)]);
    expect(new Set(ids).size).toBe(ids.length);
  });

  it('does not collide with ProjectDetail or Settings ids', () => {
    // Three surfaces now derive tab ids; a shared prefix would collide if two ever rendered
    // on one page.
    expect(tabId('access')).not.toBe('project-tab-access');
    expect(tabId('access')).not.toBe('settings-tab-access');
  });
});

// ---------------------------------------------------------------------------
// repoBackLink — the repository page returns to its PARENT PROJECT (E28C/T7, D-C4b)
//
// THIS BLOCK EXISTS BECAUSE 4b SHIPPED WITHOUT A GUARD. Review re-pointed the loaded render's
// `backTo` at the fleet list and all 1099 tests stayed green: the assertions covering 4b were about
// the three early returns and the 18 untouched `OpsPage` call sites, and NOTHING pinned the one line
// the task was for. The destination is now a selector, so a mutation of it fails on behaviour rather
// than on the shape of a source line.
//
// The `.tsx` wiring guards live further down with the other source-as-text checks (the loaded render
// consumes this selector, the early returns keep the fleet fallback, and `OpsPage`'s default label is
// one literal) — those are COUNTED, because occurrence-counting is what this project has learned
// survives a mutation that a substring check does not.
// ---------------------------------------------------------------------------

describe('repoBackLink — a repository returns to the project that owns it', () => {
  it('points at the parent PROJECT, never at the fleet list', () => {
    // The regression review actually performed. `/ops/repositories` is correct only for a reader who
    // arrived from the fleet list, and is a two-level jump from the far more common arrival — the
    // project's own repositories tab. `project_id` is on the record, so this survives a refresh, a
    // deep link and a bookmark, which is what the reported defect was about.
    const link = repoBackLink({ projectId: 'proj-7', projectName: 'Claims' });
    expect(link.to).toBe('/ops/projects/proj-7');
    expect(link.to).not.toContain('/ops/repositories');
  });

  it('labels the link with the project NAME, so it reads as a place', () => {
    expect(repoBackLink({ projectId: 'proj-7', projectName: 'Claims' }).label).toBe('← Claims');
  });

  it('falls back to the project id — a true reference beats a vague word', () => {
    // An opaque id is still an honest reference to WHICH project, and it is the thing an operator can
    // search for. "← Project" would be a label that names no destination, and an empty arrow would
    // read as a rendering fault. Blank and whitespace-only names are absence, not data.
    for (const missing of [null, undefined, '', '   ']) {
      const link = repoBackLink({ projectId: 'proj-7', projectName: missing });
      expect(link.label, String(missing)).toBe('← proj-7');
    }
  });

  it('always leads with the arrow, and never renders a bare arrow', () => {
    // The arrow is what makes it read as a back link rather than as a breadcrumb or a title.
    for (const name of ['Claims', null]) {
      const { label } = repoBackLink({ projectId: 'proj-7', projectName: name });
      expect(label.startsWith('← ')).toBe(true);
      expect(label.trim().length).toBeGreaterThan(2);
    }
  });

  it('names a route the router actually serves', () => {
    // A back link to a route that does not exist is a dead end that no type checks. `App.tsx` mounts
    // `/ops/projects/:id`, so the shape is pinned against the router's own pattern rather than
    // against a second copy of the string.
    expect(appSrc).toContain('path="/ops/projects/:id"');
    const link = repoBackLink({ projectId: 'proj-7' });
    // The receiver is the ROUTER'S PATTERN, so the `.replace` genuinely derives the expected value
    // from it. It used to be the already-substituted path, which made the call a no-op and left the
    // assertion comparing a hand-written literal to itself — passing whatever the pattern said.
    expect(link.to).toBe('/ops/projects/:id'.replace(':id', 'proj-7'));
  });
});

// ---------------------------------------------------------------------------
// The environment strip (C5) — the plan's second required test
// ---------------------------------------------------------------------------

/** A tenant carrying exactly the named stages. No dev/prod assumption anywhere. */
function tenant(...names: string[]): EnvironmentRowSource['stages'] {
  return Object.fromEntries(
    names.map((n) => [
      n,
      {
        account_id: '000000000000',
        region: 'eu-central-1',
        ecr_repo_uri: `example.invalid/${n}`,
        push_role_arn: `arn:aws:iam::000000000000:role/push-${n}`,
        deploy_role_arn: `arn:aws:iam::000000000000:role/deploy-${n}`,
      },
    ]),
  );
}

describe('environmentRows — one row per stage THE API RETURNS (C5)', () => {
  it('renders N rows for N stages, whatever they are called', () => {
    expect(environmentRows({ stages: tenant('a'), deployments: [] })).toHaveLength(1);
    expect(environmentRows({ stages: tenant('a', 'b'), deployments: [] })).toHaveLength(2);
    expect(
      environmentRows({ stages: tenant('a', 'b', 'c', 'd'), deployments: [] }),
    ).toHaveLength(4);
  });

  it('renders the stage names the tenant actually carries — nothing invented', () => {
    const rows = environmentRows({ stages: tenant('uat', 'sandbox'), deployments: [] });
    expect(rows.map((r) => r.stage).sort()).toEqual(['sandbox', 'uat']);
  });

  it('a tenant with NO stages renders NO rows — an honest empty state', () => {
    // The fabricated-dev-row failure: a page that shows a dev row for a tenant that has no
    // dev stage is stating something the API never said.
    expect(environmentRows({ stages: {}, deployments: [] })).toEqual([]);
  });

  it('an unresolved tenant (null stages) renders no rows, not a default pair', () => {
    expect(environmentRows({ stages: null, deployments: [] })).toEqual([]);
  });

  it('a stage the API does not return does not render, even with deployments for it', () => {
    // The tenant record is the authority on which stages EXIST. Deployment history for a
    // retired stage must not resurrect a row.
    const rows = environmentRows({
      stages: tenant('uat'),
      deployments: [
        deployment({ stage: 'retired', image_tag: 'a-1', started_at: '2026-07-31T10:00:00Z' }),
      ],
    });
    expect(rows.map((r) => r.stage)).toEqual(['uat']);
  });

  it('sorts stages alphabetically — never by a hardcoded stage order', () => {
    // A fixed order array naming the conventional stages is the same hardcode wearing a hat, so
    // the order is a data property (the name) and stays stable for any stage set.
    //
    // The fixture must DISCRIMINATE the two implementations, and the obvious one does not: the
    // conventional pair happens to be in alphabetical order relative to `uat`, so
    // `[conventional…, uat]` is what BOTH a privileged-order array and `localeCompare` return.
    // (An earlier version of this test claimed a mutation it could not detect.) So the stage set
    // below includes a name that sorts BEFORE the conventional pair: alphabetically it leads,
    // and under any implementation that ranks the two conventional stages first it does not.
    //
    // The names are BUILT rather than written, so this file — like the production files — carries
    // no quoted stage literal for the C5 guard below to find. That guard reads raw source and
    // skips neither comments nor test data, deliberately: a rule with an exemption for "the file
    // that tests the rule" is a rule with a hole in it.
    const [devish, prodish] = ['d' + 'ev', 'p' + 'rod'];
    expect(sortedStageNames(tenant(prodish, devish, 'alpha'))).toEqual([
      'alpha',
      devish,
      prodish,
    ]);
    // …and one where a stage sorts BETWEEN the pair, which a privileged order would also break.
    expect(sortedStageNames(tenant(prodish, 'edge', devish))).toEqual([devish, 'edge', prodish]);
    expect(sortedStageNames(tenant('b', 'a'))).toEqual(['a', 'b']);
    expect(sortedStageNames({})).toEqual([]);
  });
});

function deployment(over: Partial<EnvironmentRowSource['deployments'][number]> = {}) {
  return {
    id: 'dep-11111111',
    repo_id: 'repo-1',
    agent_id: 'a-1',
    stage: 'uat',
    seq_key: 'repo-1#uat#2026-07-31T10:00:00Z#1111',
    image_tag: 'a-1-tree000',
    outcome: 'succeeded' as const,
    started_at: '2026-07-31T10:00:00Z',
    ...over,
  };
}

describe('environmentRows — what each row says about its stage', () => {
  it('names the SUCCEEDED deployment that is current for that stage', () => {
    const rows = environmentRows({
      stages: tenant('uat'),
      deployments: [
        deployment({ image_tag: 'a-1-newest', started_at: '2026-07-31T10:00:00Z' }),
        deployment({
          id: 'dep-22222222', image_tag: 'a-1-older', started_at: '2026-07-30T10:00:00Z',
        }),
      ],
    });
    expect(rows[0].imageTag).toBe('a-1-newest');
    expect(rows[0].deployedAt).toBe('2026-07-31T10:00:00Z');
  });

  it('a FAILED attempt is not what the stage is running', () => {
    // The newest row for a stage may be a failure. Reporting its tag as the deployed version
    // would name an image that never served traffic.
    const rows = environmentRows({
      stages: tenant('uat'),
      deployments: [
        deployment({
          id: 'dep-ffffffff', image_tag: 'a-1-broken', started_at: '2026-07-31T10:00:00Z',
          outcome: 'failed',
        }),
        deployment({
          id: 'dep-22222222', image_tag: 'a-1-good', started_at: '2026-07-30T10:00:00Z',
        }),
      ],
    });
    expect(rows[0].imageTag).toBe('a-1-good');
  });

  it('a STARTED (in-flight) attempt is not what the stage is running either', () => {
    const rows = environmentRows({
      stages: tenant('uat'),
      deployments: [
        deployment({
          id: 'dep-99999999', image_tag: 'a-1-inflight', started_at: '2026-07-31T10:00:00Z',
          outcome: 'started', build_id: 'build:live',
        }),
        deployment({
          id: 'dep-22222222', image_tag: 'a-1-good', started_at: '2026-07-30T10:00:00Z',
        }),
      ],
    });
    expect(rows[0].imageTag).toBe('a-1-good');
    // …but the in-flight attempt is still REPORTED, because "a deploy is running" is exactly
    // what an operator on this page needs to know. Its build has NO terminal row, so it is
    // genuinely still running — see the collapse suite for the finished case.
    expect(rows[0].inFlight).toBe(true);
  });

  it('a stage with NO deployment reads as never deployed — not as an error', () => {
    const rows = environmentRows({ stages: tenant('uat'), deployments: [] });
    expect(rows[0].imageTag).toBeNull();
    expect(rows[0].deployedAt).toBeNull();
    expect(rows[0].actor).toBeNull();
    expect(rows[0].inFlight).toBe(false);
  });

  it('shows the short sha beside the tag, and null when the row carries none', () => {
    const withSha = environmentRows({
      stages: tenant('uat'),
      deployments: [deployment({ source_sha: '3f9a1c2b4d5e6f' })],
    });
    expect(withSha[0].shortSha).toBe('3f9a1c2');
    // A build-written row carries NO source_sha by design (C1) — absent, not blank.
    const without = environmentRows({
      stages: tenant('uat'),
      deployments: [deployment({ source_sha: null })],
    });
    expect(without[0].shortSha).toBeNull();
  });

  it('branches the actor on `actor_kind` — two currencies, never merged', () => {
    const gh = environmentRows({
      stages: tenant('uat'),
      deployments: [deployment({ actor: 'jorge', actor_kind: 'github' })],
    });
    expect(gh[0].actor).toEqual({ kind: 'github', display: '@jorge' });

    const entra = environmentRows({
      stages: tenant('uat'),
      deployments: [
        deployment({ actor: '00000000-0000-0000-0000-000000000009', actor_kind: 'entra' }),
      ],
    });
    expect(entra[0].actor).toEqual({
      kind: 'entra',
      display: '00000000-0000-0000-0000-000000000009',
    });
    // Not `@`-prefixed: the `@` marks the PROVIDER's currency, and an Entra oid is not one.
    expect(entra[0].actor?.display.startsWith('@')).toBe(false);
  });

  it('a build-written row (no actor at all) reads as ABSENT, not "unknown user"', () => {
    const rows = environmentRows({
      stages: tenant('uat'),
      deployments: [deployment({ actor: null, actor_kind: null })],
    });
    expect(rows[0].actor).toBeNull();
  });

  it('an actor with no `actor_kind` is not guessed into one', () => {
    // Attributing an unlabelled actor to GitHub would render an Entra oid as `@oid`.
    const rows = environmentRows({
      stages: tenant('uat'),
      deployments: [deployment({ actor: 'someone', actor_kind: null })],
    });
    expect(rows[0].actor).toEqual({ kind: 'unknown', display: 'someone' });
  });

  it('drift is per-stage: this stage vs the newest succeeded image anywhere', () => {
    const rows = environmentRows({
      stages: tenant('sandbox', 'uat'),
      deployments: [
        deployment({ stage: 'sandbox', image_tag: 'a-1-new', started_at: '2026-07-31T10:00:00Z' }),
        deployment({
          id: 'dep-22222222', stage: 'uat', image_tag: 'a-1-old',
          started_at: '2026-07-20T10:00:00Z',
        }),
      ],
    });
    const bySt = Object.fromEntries(rows.map((r) => [r.stage, r]));
    expect(bySt['uat'].drift).toEqual({ behindTag: 'a-1-new' });
    // The stage that HOLDS the newest image is not behind anything.
    expect(bySt['sandbox'].drift).toBeNull();
  });

  it('a stage running the same image as everything else has no drift', () => {
    const rows = environmentRows({
      stages: tenant('a', 'b'),
      deployments: [
        deployment({ stage: 'a', image_tag: 'same', started_at: '2026-07-31T10:00:00Z' }),
        deployment({
          id: 'dep-22222222', stage: 'b', image_tag: 'same',
          started_at: '2026-07-30T10:00:00Z',
        }),
      ],
    });
    for (const r of rows) expect(r.drift, r.stage).toBeNull();
  });

  it('a never-deployed stage is not reported as DRIFTED', () => {
    // "Behind" needs something to be behind FROM. A stage with no deployment has never
    // shipped, which is a different and more accurate statement.
    const rows = environmentRows({
      stages: tenant('a', 'b'),
      deployments: [deployment({ stage: 'a', image_tag: 'only' })],
    });
    const b = rows.find((r) => r.stage === 'b')!;
    expect(b.imageTag).toBeNull();
    expect(b.drift).toBeNull();
  });
});

// ---------------------------------------------------------------------------
// collapseByBuild — ONE ATTEMPT, TWO APPEND-ONLY ROWS
//
// This is the rule T12 IMPORTS rather than re-derives, so it is pinned here in full. The
// partition is append-only: AGP writes a `started` row when a build is requested (the only row
// carrying actor/actor_kind/source_sha) and the buildspec writes a SEPARATE terminal row (which
// carries none of those, by design). Nothing ever closes a started row.
// ---------------------------------------------------------------------------

describe('collapseByBuild — a started row whose build has a terminal row is FINISHED', () => {
  it('collapses a started+succeeded pair sharing one build_id into ONE finished attempt', () => {
    const attempts = collapseByBuild([
      deployment({
        id: 'dep-aaaaaaaa', outcome: 'started', build_id: 'b:1', actor: 'jorge',
        actor_kind: 'github', source_sha: '3f9a1c2b4d', started_at: '2026-07-31T10:00:00Z',
      }),
      deployment({
        id: 'dep-bbbbbbbb', outcome: 'succeeded', build_id: 'b:1', actor: null,
        actor_kind: null, source_sha: null, started_at: '2026-07-31T10:06:00Z',
      }),
    ]);
    expect(attempts).toHaveLength(1);
    expect(attempts[0].outcome).toBe('succeeded');
    expect(attempts[0].inFlight).toBe(false);
    // The terminal row owns the OUTCOME; the started row owns the ACTOR and the SHA.
    expect(attempts[0].row.id).toBe('dep-bbbbbbbb');
    expect(attempts[0].actor).toEqual({ kind: 'github', display: '@jorge' });
    expect(attempts[0].shortSha).toBe('3f9a1c2');
  });

  it('a started row with NO terminal row for its build is still in flight', () => {
    const attempts = collapseByBuild([
      deployment({ outcome: 'started', build_id: 'b:live' }),
    ]);
    expect(attempts).toHaveLength(1);
    expect(attempts[0].inFlight).toBe(true);
    expect(attempts[0].outcome).toBe('started');
  });

  it('a NULL build_id is UN-COLLAPSIBLE — the started row stays in flight', () => {
    // Nothing joins it to a partner, and retiring it on a guess would hide a running deploy.
    const attempts = collapseByBuild([
      deployment({ id: 'dep-11111111', outcome: 'started', build_id: null }),
      deployment({ id: 'dep-22222222', outcome: 'succeeded', build_id: null }),
    ]);
    expect(attempts).toHaveLength(2);
    expect(attempts.some((a) => a.inFlight)).toBe(true);
  });

  it('a terminal row with NO started partner is KEPT, never dropped', () => {
    // The build ran while AGP was unreachable, so nobody wrote a started row (C1's note). A
    // reader that assumed a pair would silently lose the only record of a real deployment.
    const attempts = collapseByBuild([
      deployment({ id: 'dep-cccccccc', outcome: 'succeeded', build_id: 'b:orphan' }),
    ]);
    expect(attempts).toHaveLength(1);
    expect(attempts[0].outcome).toBe('succeeded');
    expect(attempts[0].inFlight).toBe(false);
  });

  it('collapses a started+FAILED pair too — a failure closes the attempt as much as a success', () => {
    const attempts = collapseByBuild([
      deployment({ id: 'dep-dddddddd', outcome: 'started', build_id: 'b:2', actor: 'jorge', actor_kind: 'github' }),
      deployment({ id: 'dep-eeeeeeee', outcome: 'failed', build_id: 'b:2' }),
    ]);
    expect(attempts).toHaveLength(1);
    expect(attempts[0].outcome).toBe('failed');
    expect(attempts[0].inFlight).toBe(false);
    expect(attempts[0].actor).toEqual({ kind: 'github', display: '@jorge' });
  });

  it('does not collapse ACROSS builds — two independent attempts stay two', () => {
    const attempts = collapseByBuild([
      deployment({ id: 'dep-11111111', outcome: 'succeeded', build_id: 'b:1' }),
      deployment({ id: 'dep-22222222', outcome: 'started', build_id: 'b:2' }),
    ]);
    expect(attempts).toHaveLength(2);
    expect(attempts.filter((a) => a.inFlight)).toHaveLength(1);
  });
});

describe('environmentRows — the collapse, seen through the strip', () => {
  it('a completed deploy does NOT leave the stage permanently "in progress"', () => {
    // THE BUG THIS PINS: nothing closes a `started` row, so `some(outcome === 'started')` was
    // true forever — one successful deploy and every stage wore a permanent amber "Deployment
    // in progress" on the page an operator opens to find out whether something is running.
    const rows = environmentRows({
      stages: tenant('uat'),
      deployments: [
        deployment({
          id: 'dep-aaaaaaaa', outcome: 'started', build_id: 'b:1', image_tag: 'a-1-shipped',
          actor: '00000000-0000-0000-0000-000000000009', actor_kind: 'entra',
          source_sha: 'abcdef1234', started_at: '2026-07-31T10:00:00Z',
        }),
        deployment({
          id: 'dep-bbbbbbbb', outcome: 'succeeded', build_id: 'b:1', image_tag: 'a-1-shipped',
          actor: null, actor_kind: null, source_sha: null,
          started_at: '2026-07-31T10:06:00Z',
        }),
      ],
    });
    expect(rows[0].inFlight).toBe(false);
    expect(rows[0].imageTag).toBe('a-1-shipped');
  });

  it('"Deployed by" and the short sha come from the STARTED row of the same build', () => {
    // The mirror defect: the ONLY writer of a `succeeded` row is the buildspec, which carries no
    // actor, no actor_kind and no source_sha BY DESIGN — so reading them off the succeeded row
    // made both columns structurally always em-dash, while the platform DID know who promoted.
    const rows = environmentRows({
      stages: tenant('uat'),
      deployments: [
        deployment({
          id: 'dep-aaaaaaaa', outcome: 'started', build_id: 'b:9',
          actor: '00000000-0000-0000-0000-000000000009', actor_kind: 'entra',
          source_sha: '3f9a1c2b4d5e', started_at: '2026-07-31T10:00:00Z',
        }),
        deployment({
          id: 'dep-bbbbbbbb', outcome: 'succeeded', build_id: 'b:9', actor: null,
          actor_kind: null, source_sha: null, started_at: '2026-07-31T10:06:00Z',
        }),
      ],
    });
    expect(rows[0].actor).toEqual({
      kind: 'entra',
      display: '00000000-0000-0000-0000-000000000009',
    });
    expect(rows[0].shortSha).toBe('3f9a1c2');
  });

  it('a still-running deploy IS reported, alongside what the stage is currently serving', () => {
    const rows = environmentRows({
      stages: tenant('uat'),
      deployments: [
        deployment({ id: 'dep-11111111', outcome: 'started', build_id: 'b:old', image_tag: 'a-1-v1' }),
        deployment({
          id: 'dep-22222222', outcome: 'succeeded', build_id: 'b:old', image_tag: 'a-1-v1',
          started_at: '2026-07-30T10:00:00Z',
        }),
        deployment({
          id: 'dep-33333333', outcome: 'started', build_id: 'b:new', image_tag: 'a-1-v2',
          started_at: '2026-07-31T12:00:00Z',
        }),
      ],
    });
    expect(rows[0].inFlight).toBe(true);
    expect(rows[0].imageTag).toBe('a-1-v1');
  });
});

// ---------------------------------------------------------------------------
// A FAILED HISTORY READ IS NOT "NEVER DEPLOYED"
// ---------------------------------------------------------------------------

describe('environmentRows — an unreadable history is unknown, not empty', () => {
  it('reports historyUnknown rather than the positive claim "never deployed"', () => {
    // A rejected history read arrives as an empty array, which is indistinguishable from "this
    // agent has never deployed" — and the strip rendered the second. "We could not ask" is not
    // a statement about production. Same rule as absent runtime ⇒ unknown-never-ready and a
    // failed count ⇒ em-dash-never-zero.
    const rows = environmentRows({ stages: tenant('uat'), deployments: [], historyError: true });
    expect(rows).toHaveLength(1);
    expect(rows[0].historyUnknown).toBe(true);
  });

  it('derives NOTHING from the history when it could not be read', () => {
    const rows = environmentRows({
      stages: tenant('uat'),
      // Even if rows were somehow present, an errored read is not evidence.
      deployments: [deployment({ outcome: 'started', build_id: 'b:1' })],
      historyError: true,
    });
    expect(rows[0].imageTag).toBeNull();
    expect(rows[0].actor).toBeNull();
    expect(rows[0].shortSha).toBeNull();
    expect(rows[0].drift).toBeNull();
    // Critically NOT "a deploy is in progress" — that would be a claim too.
    expect(rows[0].inFlight).toBe(false);
  });

  it('a genuinely EMPTY history is still reported as empty, not as unknown', () => {
    // The two states must stay distinguishable in both directions, or the fix has only moved
    // the dishonesty: a repo that really has never deployed should say so.
    const rows = environmentRows({ stages: tenant('uat'), deployments: [], historyError: false });
    expect(rows[0].historyUnknown).toBe(false);
    expect(rows[0].imageTag).toBeNull();
  });

  it('defaults to KNOWN when the caller omits the flag', () => {
    const rows = environmentRows({ stages: tenant('uat'), deployments: [] });
    expect(rows[0].historyUnknown).toBe(false);
  });
});

// ---------------------------------------------------------------------------
// The actor's CURRENCY — three kinds, three labels
// ---------------------------------------------------------------------------

describe('ACTOR_KIND_TITLE — every kind has its own label, and none guesses', () => {
  it('covers all three kinds', () => {
    expect(Object.keys(ACTOR_KIND_TITLE).sort()).toEqual(['entra', 'github', 'unknown']);
  });

  it('does NOT describe an unlabelled actor as a provider login', () => {
    // `unknown` exists so an actor with no `actor_kind` is not guessed into a currency. A
    // two-way ternary put it under "GitHub login", asserting a provider identity nobody
    // established — the display string was right and the hover text undid it.
    expect(ACTOR_KIND_TITLE.unknown).not.toBe(ACTOR_KIND_TITLE.github);
    expect(ACTOR_KIND_TITLE.unknown).not.toMatch(/^GitHub login$/);
    expect(ACTOR_KIND_TITLE.github).toMatch(/github/i);
    expect(ACTOR_KIND_TITLE.entra).toMatch(/entra/i);
  });

  it('every kind `actorOf` can produce has a label', () => {
    // Belt and braces over the type: the three kinds are reachable from real rows.
    for (const kind of ['github', 'entra', 'unknown'] as const) {
      expect(ACTOR_KIND_TITLE[kind], kind).toBeTruthy();
    }
  });

  it('the strip RENDERS through the table, not a re-derived ternary', () => {
    // Having the table is not the property; USING it is. A three-way `Record` in the `.ts` and a
    // two-way ternary in the `.tsx` is exactly the shape that shipped `unknown` under "GitHub
    // login" — so the guard is that the currency labels appear ONLY via the table.
    expect(environmentStripSrc).toContain('ACTOR_KIND_TITLE[row.actor.kind]');
    for (const literal of ['GitHub login', 'Entra object id']) {
      expect(environmentStripSrc, literal).not.toContain(literal);
    }
  });
});

// ---------------------------------------------------------------------------
// headerActions — the three header affordances, each on its OWN existing gate
// ---------------------------------------------------------------------------

const OWNER_INPUT = {
  held: 'owner' as const,
  roleLevel: 0,
  ungoverned: false,
  cicdStatus: 'deployed',
  prodCandidateStatus: 'pending',
  steps: [] as { status: 'pending' | 'running' | 'done' | 'failed' }[],
};

describe('headerActions — promote / retry / delete, on the gates their ROUTES use', () => {
  it('offers all three to an owner with a pending candidate and a failed step', () => {
    const may = headerActions({ ...OWNER_INPUT, steps: [{ status: 'failed' }] });
    expect(may).toEqual({ promote: true, retry: true, destroy: true });
  });

  it('offers NOTHING to a viewer', () => {
    const may = headerActions({
      ...OWNER_INPUT, held: 'viewer', steps: [{ status: 'failed' }],
    });
    expect(may).toEqual({ promote: false, retry: false, destroy: false });
  });

  it('RETRY gets the design-§3 ungoverned fallback; PROMOTE does not', () => {
    // The retry route gates through `_require_project_role_or_ungoverned`, so a role-less caller
    // on an ungoverned project must still be able to recover it. Promote is on the STRICT gate,
    // so the same caller must NOT be offered it — the route would 403 every click.
    const may = headerActions({
      ...OWNER_INPUT, held: null, ungoverned: true, steps: [{ status: 'failed' }],
    });
    expect(may.retry).toBe(true);
    expect(may.promote).toBe(false);
    // Delete is OWNER-gated and the fallback stops at maintainer, so it is not offered either.
    expect(may.destroy).toBe(false);
  });

  it('does not offer RETRY when no step has failed — the route would 409', () => {
    for (const steps of [
      [] as { status: 'pending' | 'running' | 'done' | 'failed' }[],
      [{ status: 'done' as const }, { status: 'done' as const }],
      [{ status: 'running' as const }, { status: 'pending' as const }],
    ]) {
      expect(headerActions({ ...OWNER_INPUT, steps }).retry, JSON.stringify(steps)).toBe(false);
    }
  });

  it('suppresses PROMOTE while a delivery is in flight', () => {
    for (const status of ['provisioning', 'promoting']) {
      expect(headerActions({ ...OWNER_INPUT, cicdStatus: status }).promote, status).toBe(false);
    }
  });

  it('suppresses PROMOTE with no pending candidate', () => {
    for (const candidate of [null, undefined, 'promoted', '']) {
      expect(
        headerActions({ ...OWNER_INPUT, prodCandidateStatus: candidate }).promote,
        String(candidate),
      ).toBe(false);
    }
  });

  it('narrows the delivery status through the shared boundary', () => {
    // A raw `===` would miss ` DEPLOYED ` — and here that would OFFER a promote during a
    // promotion, which the route answers with a 409.
    expect(headerActions({ ...OWNER_INPUT, cicdStatus: ' PROMOTING ' }).promote).toBe(false);
  });

  it('a platform admin holds all three without a role row', () => {
    const may = headerActions({
      ...OWNER_INPUT, held: null, roleLevel: 2, steps: [{ status: 'failed' }],
    });
    expect(may).toEqual({ promote: true, retry: true, destroy: true });
  });

  it('DELETE is offered whatever the repo state — a broken repo is the one to tear down', () => {
    for (const status of ['provisioning', 'failed', 'deployed', 'unknown']) {
      expect(headerActions({ ...OWNER_INPUT, cicdStatus: status }).destroy, status).toBe(true);
    }
  });
});

// ---------------------------------------------------------------------------
// Runtime attribution — the trap in this strip
// ---------------------------------------------------------------------------

describe('runtimeScope — the runtime answer is PER-AGENT and cannot be attributed to a stage', () => {
  it('is agent-scoped whenever the probe could not name a stage', () => {
    // T5's route returns `stage: "unknown"` in practice: the agent envelope holds ONE
    // `agent_arn`, overwritten by whichever stage deployed last, so the probe cannot say
    // which stage it read.
    expect(runtimeScope({ stage: 'unknown', status: 'ready' }).kind).toBe('agent');
    expect(runtimeScope({ stage: '', status: 'ready' }).kind).toBe('agent');
    expect(runtimeScope(undefined).kind).toBe('agent');
  });

  it('NEVER attributes an agent-scoped reading to a stage, even one the tenant has', () => {
    const scope = runtimeScope({ stage: 'unknown', status: 'ready' });
    expect(scope.stage).toBeNull();
    // The whole point: no caller can obtain a stage name to caption the pill with.
    expect(Object.values(scope).includes('uat')).toBe(false);
  });

  it('reports the note that MUST accompany the per-stage Runtime column', () => {
    // The strip has a per-stage Runtime column and the platform has ONE agent-wide answer.
    // Rendering that answer in each row would claim each stage was probed; the honest
    // treatment labels the column as not attributable, and the pill lives ONCE, agent-level.
    expect(RUNTIME_SCOPE_NOTE).toMatch(/not attribut/i);
    expect(runtimeScope(undefined).note).toBe(RUNTIME_SCOPE_NOTE);
  });

  it('would become stage-scoped if the envelope ever gained a real stage', () => {
    // Forward-compatible rather than hardcoded to "always unknown": the day the runtime
    // envelope is per-stage, a probe that names its stage is attributable and the strip can
    // say so — WITHOUT the UI having invented the attribution in the meantime.
    const scope = runtimeScope({ stage: 'uat', status: 'ready' });
    expect(scope.kind).toBe('stage');
    expect(scope.stage).toBe('uat');
    expect(scope.note).toBeNull();
  });

  it('does not treat a stage-shaped reading as attributable when the status is unknown', () => {
    // An unreachable control plane that somehow echoed a stage has still read nothing.
    expect(runtimeScope({ stage: 'uat', status: 'unknown' }).kind).toBe('agent');
  });
});

// ---------------------------------------------------------------------------
// stageRuntimeCell — WHAT DOES ONE STAGE'S RUNTIME CELL SHOW? (E28C/T7, D-C4c)
//
// The other half of `runtimeScope`, and the reason that function was written
// forward-compatible rather than hardcoded to "always unattributable": the backend has
// answered `?stage=` since E28A (`agents.py:695-731` — a stage the agent owns is probed for
// itself, a stage it does not own comes back `not_deployed` with NO AWS call), and until now
// the frontend never asked. Every stage row therefore carried the same not-attributable note,
// including the rows for which a real per-stage reading was one query parameter away.
//
// THREE OUTCOMES, and the distinction between the last two is the whole point:
//   • `pill`    — this reading is ATTRIBUTABLE to this stage, so the stage may make a runtime
//                 claim and the cell renders a real status pill.
//   • `note`    — a reading exists but is NOT attributable (a legacy scalar-only record answers
//                 `stage: "unknown"`). `RUNTIME_SCOPE_NOTE` is preserved verbatim for exactly
//                 this case — the column still documents the platform's limitation rather than
//                 filling itself in.
//   • `absent`  — no reading at all: the probe was never made or it failed. An em dash. NOT a
//                 note (nothing is being claimed about attributability) and emphatically not a
//                 pill, because absent data must never render as a status.
//
// ATTRIBUTION IS DELEGATED TO `runtimeScope`, never re-derived. Two answers to "is this
// reading attributable?" would drift exactly as the forked status tables did, and this one is
// the reading that would be WRONG in the harmful direction — a stage row claiming a runtime it
// never probed is the incident class the strip was built to prevent.
//
// AND THE STAGE MUST MATCH. A reading attributable to one stage does not describe a different
// one, so a per-stage map that somehow held a mismatched entry falls back to the note rather
// than captioning one stage's pill with another stage's name. (No conventional stage name is
// written here even in prose — the guard further down covers this file too.)
// ---------------------------------------------------------------------------

describe('stageRuntimeCell — a stage row may only make a runtime claim it can attribute', () => {
  it('renders a PILL for a reading attributable to this very stage', () => {
    const cell = stageRuntimeCell('uat', { stage: 'uat', status: 'ready' });
    expect(cell.kind).toBe('pill');
    // The narrowed key, so the caller indexes the shared tables and never re-narrows.
    expect(cell.status).toBe('ready');
    expect(cell.note).toBeNull();
  });

  it('narrows an UNRECOGNIZED wire status rather than passing it through', () => {
    // The `toRuntimeStatus` boundary, applied here so the `.tsx` cannot receive a raw string.
    // A status nobody produces is `unknown`, never rendered as itself and never as ready.
    const cell = stageRuntimeCell('uat', { stage: 'uat', status: 'ASCENDING' });
    // `unknown` status is not attributable evidence at all (`runtimeScope`'s own rule), so this
    // is the NOTE — it must not become a pill reading "Runtime unknown" per stage, which would
    // still be a per-stage claim.
    expect(cell.kind).toBe('note');
  });

  it('keeps RUNTIME_SCOPE_NOTE for a reading that exists but is not attributable', () => {
    // The legacy scalar-only record. This is the case the column was designed around and it
    // does NOT regress: the note is preserved verbatim, from the same constant.
    const cell = stageRuntimeCell('uat', { stage: 'unknown', status: 'ready' });
    expect(cell.kind).toBe('note');
    expect(cell.note).toBe(RUNTIME_SCOPE_NOTE);
  });

  it('renders ABSENT — not a note, and never a pill — when there is no reading', () => {
    // The probe failed or was never made. Distinct from the note: nothing is being said about
    // attributability, so saying something would be its own small fabrication.
    const cell = stageRuntimeCell('uat', undefined);
    expect(cell.kind).toBe('absent');
    expect(cell.note).toBeNull();
  });

  it('refuses to caption THIS stage with ANOTHER stage’s reading', () => {
    // The mismatch guard. `runtimeScope` would call this reading attributable — and it is, to
    // `uat`. It establishes nothing about the stage being rendered.
    const cell = stageRuntimeCell('prod-like', { stage: 'uat', status: 'ready' });
    expect(cell.kind).toBe('note');
    expect(cell.note).toBe(RUNTIME_SCOPE_NOTE);
  });

  it('delegates attribution to runtimeScope rather than re-deriving it', () => {
    // Pinned as a PROPERTY over the two functions, not as a source grep: for every reading,
    // a pill is offered exactly when `runtimeScope` calls it stage-attributable AND names this
    // stage. A second, drifting notion of attributability breaks this.
    const readings = [
      { stage: 'uat', status: 'ready' },
      { stage: 'uat', status: 'unknown' },
      { stage: 'unknown', status: 'ready' },
      { stage: '', status: 'ready' },
      { stage: 'uat', status: 'not_deployed' },
      { stage: 'uat', status: 'failed' },
    ];
    for (const reading of readings) {
      const scope = runtimeScope(reading);
      const expectPill = scope.kind === 'stage' && scope.stage === 'uat';
      expect(stageRuntimeCell('uat', reading).kind).toBe(expectPill ? 'pill' : 'note');
    }
  });

  it('reports a genuinely-not-deployed stage as a pill, because that IS an attributed answer', () => {
    // The E28A contract's most useful case: asked about a stage it owns no runtime for, the
    // backend answers `not_deployed` WITHOUT an AWS call. That is a real per-stage fact — a
    // definite "nothing is running here" — so it earns a pill rather than the note that used
    // to stand in for every stage's answer.
    const cell = stageRuntimeCell('uat', { stage: 'uat', status: 'not_deployed' });
    expect(cell.kind).toBe('pill');
    expect(cell.status).toBe('not_deployed');
  });
});

// ---------------------------------------------------------------------------
// prodServingState — WHAT ESTABLISHES THAT PRODUCTION IS SERVING? (E28A/T4, finding #5)
//
// THE BUG THIS REPLACES. `ungovernedInProd` asked `toCicdStatus(cicd_status) === 'deployed'`,
// and `buildspec.yml:391` runs its terminal delivery write for EVERY stage with no branch. So
// `cicd_status` is a DELIVERY fact with no stage in it, and the banner read it as a PRODUCTION
// claim. Observed live: a repository whose only successful deploy was to a non-production stage
// displayed "Serving production without governance approval" with nothing in production at all.
// The predicate was true when only promote wrote that value and stopped being true the day the
// non-production path did too — so it now cries wolf on every new repository, which is the
// "teach the operator to ignore it" failure its own comment warned against.
//
// WHY THE BUILD_ID JOIN, and not any of the three scalars on their own:
//   • `last_promoted_at` is `_promotion_in_flight`'s CLOCK — stamped on promote's success AND
//     failure paths, and by an any-stage rollback. It proves a promote was attempted.
//   • `last_promoted_image_tag` is stamped OPTIMISTICALLY before the apply succeeds (finding
//     #10), so it proves an attempt too, not an outcome.
//   • `last_promotion_build_id` is the only one JOINABLE TO AN OUTCOME. Verified byte-identical
//     on live data against the production row's `build_id`, whose outcome was `failed` — which
//     is precisely the state the old predicate answered "deployed" for.
//
// THE ROW IS IDENTIFIED BY THAT JOIN AND NEVER BY `row.stage` (C5 — no stage literal may appear
// in `frontend/`, and the guard further down covers this file too). The join is what makes the
// derivation stage-literal-free, and it covers production ROLLBACKS for free: they stamp the
// same trio. The last test in this block asserts the verdict is unchanged when every row's
// `stage` is nonsense, so the join can never silently regress into a stage comparison.
// ---------------------------------------------------------------------------

/** The full input, so each case overrides only the field it is about. Nothing stamped by default. */
function serving(over: Partial<Parameters<typeof prodServingState>[0]> = {}): ProdServingState {
  return prodServingState({
    lastPromotionBuildId: null,
    lastPromotedAt: null,
    lastPromotedImageTag: null,
    deployments: [],
    ...over,
  });
}

describe('prodServingState — production is established by an OUTCOME, never by a stamp', () => {
  it('a successful build on a non-production stage is not production (#5)', () => {
    // THE REGRESSION TEST for the finding, and the most important one in this file. A repository
    // whose only successful delivery went to a non-production stage has promoted NOTHING: all
    // three promotion scalars are blank, so no promote was ever even attempted.
    expect(
      serving({
        deployments: [
          deployment({ build_id: 'build-1', outcome: 'succeeded' }),
          deployment({ build_id: 'build-1', outcome: 'started' }),
        ],
      }),
    ).toBe('none');
    // …and a blank-but-present stamp is absence too, not a value: the record round-trips empty
    // strings and a whitespace-only build id joins to nothing.
    expect(
      serving({
        lastPromotionBuildId: '   ',
        lastPromotedAt: '',
        lastPromotedImageTag: '  ',
        deployments: [deployment({ build_id: 'build-1', outcome: 'succeeded' })],
      }),
    ).toBe('none');
  });

  it('production is established by a SUCCEEDED row joined to the promotion build, not by a stamp', () => {
    expect(
      serving({
        lastPromotionBuildId: 'build-9',
        lastPromotedAt: '2026-08-01T10:00:00Z',
        lastPromotedImageTag: 'a-1-tree999',
        deployments: [deployment({ build_id: 'build-9', outcome: 'succeeded' })],
      }),
    ).toBe('serving');
    // The other half of the sentence: the SAME three stamps, and a succeeded row belonging to a
    // DIFFERENT build, establish nothing. An implementation that trusted the stamps would answer
    // `serving` here.
    expect(
      serving({
        lastPromotionBuildId: 'build-9',
        lastPromotedAt: '2026-08-01T10:00:00Z',
        lastPromotedImageTag: 'a-1-tree999',
        deployments: [deployment({ build_id: 'build-other', outcome: 'succeeded' })],
      }),
    ).toBe('unknown');
  });

  it('a promotion whose build FAILED is not serving production', () => {
    // The live case, verbatim: the record carried the optimistic image-tag stamp (#10) while the
    // build that was supposed to deliver it failed. Nothing is serving.
    expect(
      serving({
        lastPromotionBuildId: 'build-9',
        lastPromotedAt: '2026-08-01T10:00:00Z',
        lastPromotedImageTag: 'a-1-tree999',
        deployments: [deployment({ build_id: 'build-9', outcome: 'failed' })],
      }),
    ).toBe('none');
    // Both append-only rows for that build are present — the terminal one is the one that knows
    // (C1), so a `started` partner does not make it in-flight again.
    expect(
      serving({
        lastPromotionBuildId: 'build-9',
        lastPromotedImageTag: 'a-1-tree999',
        deployments: [
          deployment({ build_id: 'build-9', outcome: 'started' }),
          deployment({ build_id: 'build-9', outcome: 'failed' }),
        ],
      }),
    ).toBe('none');
  });

  it('a FAILED promotion in front of an older succeeded image is not SILENCE (gap 2)', () => {
    // THE GAP-2 CASE, and the one that makes branch 5 two questions rather than one. A newer
    // promote FAILED and overwrote `last_promotion_build_id`, but an earlier promote succeeded on
    // that same stage — so the stage is still serving an image, and if the agent is `proposed` it is
    // serving it unapproved. Answering `none` off the failed join alone was false silence on this
    // page's highest-consequence statement, and it also contradicted the Deployments tab, which
    // names that stage's current image on the same screen.
    //
    // The stage is read off the FAILED ROW ITSELF, so this fixture never writes a stage name: both
    // rows carry the fixture's default, whatever it is.
    expect(
      serving({
        lastPromotionBuildId: 'build-new',
        lastPromotedAt: '2026-08-01T10:00:00Z',
        lastPromotedImageTag: 'a-1-tree999',
        deployments: [
          deployment({ build_id: 'build-new', outcome: 'failed' }),
          deployment({ build_id: 'build-old', outcome: 'succeeded' }),
        ],
      }),
    ).toBe('serving');
    // THE DISCRIMINATING DIRECTION, which is what keeps #5 fixed: the older succeeded row sits on a
    // DIFFERENT stage, so it is no evidence about the stage the failed promote targeted. Nothing is
    // serving there and the honest answer is still `none` — a fix that answered `serving` on any
    // succeeded row anywhere would re-break the finding.
    const elsewhere = 'some-other-stage';
    expect(
      serving({
        lastPromotionBuildId: 'build-new',
        deployments: [
          deployment({ build_id: 'build-new', outcome: 'failed' }),
          deployment({ stage: elsewhere, build_id: 'build-old', outcome: 'succeeded' }),
        ],
      }),
    ).toBe('none');
    // A blank stage on the failed row is TWO unknowns, not a match: grouping absent stages together
    // would join unrelated rows, the same both-sides-real rule the build-id join follows.
    expect(
      serving({
        lastPromotionBuildId: 'build-new',
        deployments: [
          deployment({ stage: '  ', build_id: 'build-new', outcome: 'failed' }),
          deployment({ stage: '', build_id: 'build-old', outcome: 'succeeded' }),
        ],
      }),
    ).toBe('none');
  });

  it('an unread deployment history over a stamped promotion is UNKNOWN, not silence', () => {
    // A failed history read arrives as an empty array, indistinguishable from "nothing ever
    // shipped" — and here a promote demonstrably WAS attempted. Absent data is not good news, so
    // this must not read as the silence a genuinely un-promoted repository earns.
    expect(serving({ lastPromotionBuildId: 'build-9', historyError: true })).toBe('unknown');
    // The stamp need not be the build id: any of the three proves an attempt was made.
    expect(serving({ lastPromotedAt: '2026-08-01T10:00:00Z', historyError: true })).toBe('unknown');
    expect(serving({ lastPromotedImageTag: 'a-1-tree999', historyError: true })).toBe('unknown');
    // With NOTHING stamped there is no promotion to be uncertain about, so an unreadable history
    // is not an alarm — warning on every unread history would train the operator to ignore it.
    expect(serving({ historyError: true })).toBe('none');
  });

  it('a stamp with no delivery row that confirms or denies it is UNKNOWN', () => {
    // The unresolved gap, landing here rather than in false silence: the best-effort row write can
    // be LOST, so a build that really did deliver may have no row to join to.
    expect(serving({ lastPromotionBuildId: 'build-9' })).toBe('unknown');
    expect(
      serving({
        lastPromotionBuildId: 'build-9',
        deployments: [deployment({ build_id: 'build-other', outcome: 'failed' })],
      }),
    ).toBe('unknown');
    // A promotion still RUNNING has not established an outcome either way.
    expect(
      serving({
        lastPromotionBuildId: 'build-9',
        deployments: [deployment({ build_id: 'build-9', outcome: 'started' })],
      }),
    ).toBe('unknown');
    // And a promote stamped WITHOUT a build id — the pre-E27 shape — is unjoinable, so it stays
    // unknown however many rows exist.
    expect(
      serving({
        lastPromotedAt: '2026-08-01T10:00:00Z',
        lastPromotedImageTag: 'a-1-tree999',
        deployments: [deployment({ build_id: 'build-9', outcome: 'succeeded' })],
      }),
    ).toBe('unknown');
  });

  it('makes no claim while the history is still loading', () => {
    // A loading page states nothing. Checked BEFORE the unreadable-history branch, because a read
    // still in flight has not failed — flashing an "approval unknown" banner for one paint and
    // then withdrawing it is the alarm that teaches the operator to disbelieve the banner.
    expect(serving({ lastPromotionBuildId: 'build-9', historyLoading: true })).toBe('none');
    expect(
      serving({ lastPromotionBuildId: 'build-9', historyLoading: true, historyError: true }),
    ).toBe('none');
    // …and it does not suppress a verdict the rows already support once they have arrived.
    expect(
      serving({
        lastPromotionBuildId: 'build-9',
        historyLoading: false,
        deployments: [deployment({ build_id: 'build-9', outcome: 'succeeded' })],
      }),
    ).toBe('serving');
  });

  it('identifies the production row WITHOUT naming a stage', () => {
    // C5 forbids a stage literal in `frontend/` and the join is what keeps this derivation free of
    // one. This test is the fence: the verdict must be IDENTICAL when every row's stage is a
    // nonsense value, and must NOT be reachable by a stage comparison.
    const nonsense = ['zzz-not-a-stage', '', 'Ω', 'uat'];
    for (const stage of nonsense) {
      expect(
        serving({
          lastPromotionBuildId: 'build-9',
          deployments: [deployment({ stage, build_id: 'build-9', outcome: 'succeeded' })],
        }),
        stage,
      ).toBe('serving');
      expect(
        serving({
          lastPromotionBuildId: 'build-9',
          deployments: [deployment({ stage, build_id: 'build-9', outcome: 'failed' })],
        }),
        stage,
      ).toBe('none');
    }
    // The discriminating direction: a succeeded row for an UNRELATED build, sitting on the
    // conventional production stage name. A stage comparison answers `serving` here; the join
    // answers `unknown`. The name is BUILT rather than written so the C5 guard finds no literal.
    const conventional = 'p' + 'rod';
    expect(
      serving({
        lastPromotionBuildId: 'build-9',
        deployments: [
          deployment({ stage: conventional, build_id: 'build-other', outcome: 'succeeded' }),
        ],
      }),
    ).toBe('unknown');
  });
});

// ---------------------------------------------------------------------------
// Ungoverned-in-prod — the plan's third required test (D11)
// ---------------------------------------------------------------------------

describe('ungovernedInProd — SERVING production AND proposed, both, never one', () => {
  it('flags an agent SERVING production that governance never approved', () => {
    expect(ungovernedInProd('serving', 'proposed')).toBe(true);
  });

  it('does NOT flag on a serving production alone', () => {
    for (const lifecycle of ['approved', 'pending_approval', 'rejected', 'deprecated']) {
      expect(ungovernedInProd('serving', lifecycle), lifecycle).toBe(false);
    }
  });

  it('does NOT flag on `proposed` alone', () => {
    // `none` is the state a repository that only ever delivered to a non-production stage lands
    // in (#5), and `unknown` is a production question we have not answered — neither is the
    // established finding this warning states.
    for (const state of ['unknown', 'none'] as ProdServingState[]) {
      expect(ungovernedInProd(state, 'proposed'), state).toBe(false);
    }
  });

  it('does not flag when the lifecycle is missing', () => {
    expect(ungovernedInProd('serving', undefined)).toBe(false);
    expect(ungovernedInProd('serving', null)).toBe(false);
    expect(ungovernedInProd('serving', '  ')).toBe(false);
  });

  it('takes a PRODUCTION verdict, not a delivery status (#5)', () => {
    // The signature change IS the fix: there is no `cicd_status` to be handed here any more, so
    // a delivery fact with no stage in it cannot be mistaken for a production claim. The union
    // has exactly three members and none of them is a delivery status.
    const states: ProdServingState[] = ['serving', 'unknown', 'none'];
    for (const s of states) {
      expect(s, s).not.toMatch(/deployed|provisioning|promoting|failed|ready/);
    }
  });
});

// ---------------------------------------------------------------------------
// Source-as-text guards over the `.tsx` wiring
// ---------------------------------------------------------------------------

// ---------------------------------------------------------------------------
// environmentStripState — WHY the strip is empty (E28 final review, FR-1)
//
// The defect: `stages: null` was the ONLY signal for both "the tenant carries no stage" and "the
// tenant did not resolve", so both rendered "No environments are configured for this tenant" — a
// DEFINITE claim about the tenant, derived from a read that never happened, while silently deleting
// the entire what-is-running-where answer this page exists to give. Three live paths reach it: the
// project read threw, the admin tenant directory read failed (fail-silent by design), or the
// repository's tenant is not among a non-admin's own memberships.
// ---------------------------------------------------------------------------

describe('environmentStripState — an unresolved tenant is not a tenant with no stages', () => {
  it('reports stages-unknown when the tenant could not be resolved', () => {
    // The live shape: the tenant id was known, the record was not found, so the page sets the flag.
    expect(
      environmentStripState({ stages: null, stagesUnknown: true, deployments: [] }),
    ).toBe('stages-unknown');
    // `undefined` stages reach it the same way — `tenantAccount?.stages` on an absent record.
    expect(
      environmentStripState({ stages: undefined, stagesUnknown: true, deployments: [] }),
    ).toBe('stages-unknown');
  });

  it('still reports no-stages when the tenant WAS read and carries none', () => {
    // Both directions, or the fix has only moved the dishonesty. An empty stage map is
    // representable and reachable on the backend, and a tenant that really has no environment
    // should say so — an operator told "could not be read" about a correctly-configured tenant goes
    // hunting for an outage that is not there.
    expect(environmentStripState({ stages: {}, stagesUnknown: false, deployments: [] })).toBe(
      'no-stages',
    );
  });

  it('defaults to no-stages when the caller omits the flag', () => {
    // Same default as `historyError`: a caller that says nothing is not asserting a failure.
    expect(environmentStripState({ stages: {}, deployments: [] })).toBe('no-stages');
    expect(environmentStripState({ stages: null, deployments: [] })).toBe('no-stages');
  });

  it('reports rows whenever there are rows, whatever the flag says', () => {
    // Real data outranks a flag about its absence — an unresolved tenant cannot produce rows, but
    // if a caller ever passes both, the rows are the evidence.
    expect(environmentStripState({ stages: tenant('uat'), deployments: [] })).toBe('rows');
    expect(
      environmentStripState({ stages: tenant('uat'), stagesUnknown: true, deployments: [] }),
    ).toBe('rows');
  });
});

describe('ENVIRONMENT_EMPTY_COPY — only one of the two may describe the tenant', () => {
  it('covers both empty states and nothing else', () => {
    expect(Object.keys(ENVIRONMENT_EMPTY_COPY).sort()).toEqual(['no-stages', 'stages-unknown']);
  });

  it('never lets the unresolved case state how the tenant is CONFIGURED', () => {
    // The claim the strip used to make over a failed read, asserted absent. "Configured" is the
    // word that turns a limit of our read into a fact about the tenant.
    const unknown = ENVIRONMENT_EMPTY_COPY['stages-unknown'].toLowerCase();
    expect(unknown).not.toMatch(/are configured/);
    expect(unknown).not.toMatch(/no environments are/);
    // …and it says the absence is NOT established, so a reader is not left to infer it.
    expect(unknown).toMatch(/could not be read/);
    expect(unknown).toMatch(/not a report that the tenant has none/);
  });

  it('keeps the genuine zero-stage case reading as a configuration', () => {
    expect(ENVIRONMENT_EMPTY_COPY['no-stages']).toMatch(/configured/i);
  });

  it('the two sentences are DISTINCT', () => {
    // A copy edit that merged them would put the confident version back over a failed read.
    expect(ENVIRONMENT_EMPTY_COPY['no-stages']).not.toBe(
      ENVIRONMENT_EMPTY_COPY['stages-unknown'],
    );
  });
});

// ---------------------------------------------------------------------------
// materializeSummary — did it SUCCEED, not did it STOP (E28 final review, FR-2)
//
// The defect: the Overview's timeline header read `isMaterializeTerminal(steps) ? 'Complete' :
// 'In progress'`, and that predicate answers "has the run STOPPED?" — a failure HALTS the run, so a
// failed step IS terminal. A repository whose materialize FAILED therefore rendered the word
// "Complete" directly above its own rose failed step: good news printed over a failure, the
// inversion D13 forbids. The second branch was the same error inverted — an empty `steps[]` read
// "In progress", a positive claim that work is running, derived from an absent record.
// ---------------------------------------------------------------------------

describe('materializeSummary — a FAILED materialize never reads as complete', () => {
  it('reports failure for a failed run, and never claims success', () => {
    const s = materializeSummary('failed', 8);
    expect(s.succeeded).toBe(false);
    expect(s.label).not.toMatch(/complete|done|success/i);
    expect(s.label).toMatch(/fail/i);
  });

  it('reports success ONLY when every recorded step is done', () => {
    const s = materializeSummary('ready', 8);
    expect(s.succeeded).toBe(true);
    expect(s.label).toBe('Complete');
  });

  it('reports in-progress while the run is still going', () => {
    const s = materializeSummary('provisioning', 8);
    expect(s.succeeded).toBe(false);
    expect(s.label).toBe('In progress');
  });

  it('does NOT claim work is running when there are no steps at all', () => {
    // The other half of the finding. An absent record is not a running one — a pre-E25C repository
    // has no timeline, and "In progress" over it asserted activity nobody observed.
    for (const badge of ['failed', 'ready', 'provisioning'] as const) {
      const s = materializeSummary(badge, 0);
      expect(s.succeeded, badge).toBe(false);
      expect(s.label, badge).not.toMatch(/in progress|complete/i);
    }
    expect(materializeSummary('provisioning', 0).label).toBe('Not recorded');
  });

  it('agrees with `nextBadgeFromSteps` for every real step shape', () => {
    // Read end-to-end from the SAME function the modal and both lists read, which is the point:
    // the same repository must not read failed in the modal and complete here.
    const done = { status: 'done' as const };
    const failed = { status: 'failed' as const };
    const pending = { status: 'pending' as const };
    const running = { status: 'running' as const };
    const summaryOf = (steps: { status: 'done' | 'failed' | 'pending' | 'running' }[]) =>
      materializeSummary(nextBadgeFromSteps(steps as never), steps.length);

    // THE BUG'S OWN FIXTURE: a failure halts the run, so trailing steps stay pending. This is the
    // shape that rendered "Complete".
    expect(summaryOf([done, failed, pending]).succeeded).toBe(false);
    expect(summaryOf([done, failed, pending]).label).toMatch(/fail/i);
    // …and the three healthy shapes still answer as before.
    expect(summaryOf([done, done, done]).label).toBe('Complete');
    expect(summaryOf([done, running, pending]).label).toBe('In progress');
    expect(summaryOf([]).label).toBe('Not recorded');
  });
});

// ---------------------------------------------------------------------------
// promotionArtifact — WHAT WOULD AN APPROVAL APPROVE? (E28B/T6, item 2)
//
// Promotion's contract is an APPROVED IMAGE DIGEST: the digest names the exact bytes, which is what
// an approval can honestly attest to, and `promote_repo` passes it to the deploy verbatim rather than
// re-resolving the tag — because the tenant registry is mutable and the build is not reproducible, so
// between approval and deployment a tag can point at different bytes.
//
// BUT `prod_candidate_digest` IS OPTIONAL BY DESIGN: a repo whose committed `build.yml` predates this
// epic registers candidates tag-only, and the backend accepts them rather than 422-ing every pre-epic
// repo out of the deploy path. So the guarantee is CONDITIONAL ON THE DEPLOYED TEMPLATE — and before
// this derivation, tag-only and digest-pinned candidates were indistinguishable on screen. The gap
// was assumed closed because nothing rendered it.
// ---------------------------------------------------------------------------

/** A canonical digest: `sha256:` + exactly 64 lowercase hex. */
const CANDIDATE_DIGEST = `sha256:abc1234${'d'.repeat(57)}`;

/**
 * Strip comments before a source assertion — the `templates.test.ts` helper, re-declared here
 * because that one is not exported and this file previously had no stripper at all.
 *
 * Load-bearing rather than tidiness: this project has shipped NINE source-as-text guards that their
 * own explanatory comment satisfied. Every reachability assertion below reads stripped source, and
 * `the reachability guards cannot be satisfied by prose` asserts that property directly.
 *
 * Removes `/* … *\/` blocks (which also empties a `{/* … *\/}` JSX comment) and whole lines that are
 * line comments. Deliberately does NOT touch `//` mid-line, so a URL or an SVG path inside a string
 * literal survives intact.
 */
function stripDetailComments(src: string): string {
  return src
    .replace(/\/\*[\s\S]*?\*\//g, '')
    .split('\n')
    .filter((line) => !line.trim().startsWith('//'))
    .join('\n');
}

/** A repo with a pending, digest-pinned candidate — the state the epic's guarantee describes. */
const PINNED = {
  prod_candidate_status: 'pending',
  prod_candidate_image_tag: 'agent-7-3f9a1c2',
  prod_candidate_digest: CANDIDATE_DIGEST,
};

describe('promotionArtifact — a tag-only candidate is VISIBLE, not assumed away', () => {
  it('reports a digest-pinned candidate as `digest`, and puts the abbreviated digest IN the marker', () => {
    const a = promotionArtifact(PINNED);
    expect(a.kind).toBe('digest');
    // THE MARKER IS THE RENDERED VALUE. The component prints `marker.text` unconditionally, so
    // asserting it here is asserting what reaches the screen — see the reachability suite below for
    // why that replaced a pair of render branches.
    expect(a.marker).toEqual({ text: 'sha256:abc1234…', note: null, tone: 'pinned' });
    // The TAG SURVIVES alongside it: it is the human-readable label every other surface renders, and
    // a rollback still validates against it. The digest replaces neither.
    expect(a.imageTag).toBe('agent-7-3f9a1c2');
  });

  it('reports a candidate with NO digest as `tag-only`, carrying the caution as its marker', () => {
    // THE ITEM. A pre-epic `build.yml` posts no digest, so this is the ordinary shape of a repo the
    // template has not caught up with — not a fault, and not nothing.
    for (const missing of [null, undefined, '', '   ']) {
      const a = promotionArtifact({ ...PINNED, prod_candidate_digest: missing });
      expect(a.kind, String(missing)).toBe('tag-only');
      // The caution's text AND its explanation AND the tone that makes it amber — all three, because
      // the component reads exactly these and has no copy or class of its own to fall back on.
      expect(a.marker, String(missing)).toEqual({
        text: PROMOTION_TAG_ONLY_LABEL,
        note: PROMOTION_TAG_ONLY_NOTE,
        tone: 'caution',
      });
      // The tag is still named — it is the only thing there is to describe what would ship.
      expect(a.imageTag, String(missing)).toBe('agent-7-3f9a1c2');
    }
    // A record that omits the field ENTIRELY, which is every repo predating T4.
    const legacy = promotionArtifact({
      prod_candidate_status: 'pending',
      prod_candidate_image_tag: 'agent-7-3f9a1c2',
    });
    expect(legacy.kind).toBe('tag-only');
    expect(legacy.marker?.tone).toBe('caution');
  });

  it('reports `none` with NO MARKER when nothing is pending — no caution between merges', () => {
    // `none` is NOT a third caution. With no candidate there is nothing to approve at all
    // (`canPromote` already withholds the button), so warning here would fire on every repository
    // between merges — the cry-wolf failure `repoAction` refuses for the same reason. A null marker
    // is what makes that structural: the component renders the marker, so no marker is no row.
    for (const status of [null, undefined, '', 'promoted', 'Pending', 'PENDING', 'nonsense']) {
      const a = promotionArtifact({ ...PINNED, prod_candidate_status: status });
      expect(a.kind, String(status)).toBe('none');
      expect(a.marker, String(status)).toBeNull();
    }
    expect(promotionArtifact(null)).toEqual({ kind: 'none', marker: null, imageTag: null });
    expect(promotionArtifact(undefined)).toEqual({ kind: 'none', marker: null, imageTag: null });
  });

  it('keeps the record’s image tag readable even when nothing is approvable', () => {
    // A review finding (Minor 2). The tag USED to be nulled out for `kind: 'none'`, on the argument
    // that a consumed candidate must not be describable — but `kind` already carries approvability,
    // so blanking the tag as well made a present field unreachable through this selector and left
    // the next consumer to read the record directly, forking the derivation. That trap would have
    // been sprung by the promote-confirm wiring immediately below.
    const consumed = promotionArtifact({
      prod_candidate_status: 'promoted',
      prod_candidate_image_tag: 'agent-7-3f9a1c2',
      prod_candidate_digest: CANDIDATE_DIGEST,
    });
    expect(consumed.kind).toBe('none');
    expect(consumed.imageTag).toBe('agent-7-3f9a1c2');
    // …and the judgement is still unambiguous, so no consumer can mistake it for approvable.
    expect(consumed.marker).toBeNull();
    // Blank stays absence, not data — the `text()` rule this module applies everywhere.
    expect(promotionArtifact({ prod_candidate_image_tag: '   ' }).imageTag).toBeNull();
  });

  it('marker is null EXACTLY when kind is `none` — the two can never disagree', () => {
    // The component narrows on `marker !== null`; this is what makes that equivalent to the
    // approvability judgement. If they could diverge, the field would render for a state that has
    // nothing to approve, or hide for one that does.
    for (const status of ['pending', 'promoted', null, '', 'nonsense']) {
      for (const digest of [CANDIDATE_DIGEST, null, '', 'garbage']) {
        const a = promotionArtifact({
          prod_candidate_status: status,
          prod_candidate_image_tag: 'agent-7-abc',
          prod_candidate_digest: digest,
        });
        expect(a.marker === null, `${status}/${digest}`).toBe(a.kind === 'none');
      }
    }
  });

  it('gates on the LITERAL "pending", the same comparison the button makes', () => {
    // `canPromote` / `prodCandidateView` / `repoAction` all compare to the literal. If this one
    // drifted, the description and the button would disagree about whether anything is waiting — and
    // a leftover digest on a consumed candidate would describe an artifact nobody may approve.
    expect(promotionArtifact({ ...PINNED, prod_candidate_status: 'promoted' }).kind).toBe('none');
    expect(promotionArtifact({ ...PINNED, prod_candidate_status: 'pending' }).kind).toBe('digest');
  });

  it('never claims `digest` on a value the backend would have refused', () => {
    // A malformed digest must not be presented as a pin — that would report the guarantee as held
    // when it is not, which is worse than the tag-only caution it displaces. `shortDigest` echoes an
    // unrecognized value verbatim, so the kind stays `digest` ONLY for a canonical one.
    const bad = promotionArtifact({ ...PINNED, prod_candidate_digest: `sha256:${'A'.repeat(64)}` });
    // Uppercase would not resolve at a registry. It is echoed, not truncated — so a reader sees the
    // real stored value rather than a confident-looking abbreviation of something unparsed.
    expect(bad.marker?.text).toBe(`sha256:${'A'.repeat(64)}`);
    expect(bad.marker?.text).not.toContain('…');
  });

  it('is TOTAL — every combination yields one of the three kinds and a coherent marker', () => {
    for (const status of ['pending', 'promoted', null, '']) {
      for (const tag of ['agent-7-abc', null, '  ']) {
        for (const digest of [CANDIDATE_DIGEST, null, '', 'garbage']) {
          const a = promotionArtifact({
            prod_candidate_status: status,
            prod_candidate_image_tag: tag,
            prod_candidate_digest: digest,
          });
          expect(['digest', 'tag-only', 'none']).toContain(a.kind);
          // A marker, when present, is always renderable: non-empty text and a known tone. An empty
          // string would render a blank row that looks like a fault in the page.
          if (a.marker !== null) {
            expect(a.marker.text.length, `${status}/${digest}`).toBeGreaterThan(0);
            expect(['pinned', 'caution']).toContain(a.marker.tone);
          }
        }
      }
    }
  });
});

// ---------------------------------------------------------------------------
// REACHABILITY — the review's MUT-1, and why this suite is shaped the way it is.
//
// The reviewer made the caution branch unreachable (`? ( … ) : (false && ( … ))`) and ALL 969 TESTS
// STAYED GREEN. Both original guards were satisfied by dead code: `toContain('PROMOTION_TAG_ONLY_LABEL')`
// matches an identifier inside an arm nothing evaluates, and the branch regex matched the branch that
// was still present. Ninth vacuous guard in this project.
//
// IT IS NOT FIXABLE WITH A SHARPER SOURCE ASSERTION. `vitest.config.ts` collects only
// `src/**\/*.test.ts`, there is no jsdom and no testing-library, so no rendered-output assertion is
// possible — every guard over a `.tsx` reads TEXT, and text cannot distinguish a live branch from a
// dead one. Adding jsdom was explicitly not the ask.
//
// SO THE BRANCH WAS DELETED INSTEAD OF GUARDED. `promotionArtifact` returns the marker already
// resolved (text, tooltip, tone) and the component renders that ONE value with no condition of its
// own. The guards below therefore pin two things a text assertion CAN establish: that the pure
// decision is correct for every input (above), and that the component has no second condition to
// kill (here). Together those make a dead render branch unrepresentable rather than undetectable.
// ---------------------------------------------------------------------------

describe('the promote surface renders the artifact through ONE branchless value', () => {
  it('reads the resolved marker and holds no branch of its own over it', () => {
    // THE REACHABILITY GUARD, and it is COUNTING rather than pattern-absence — because a
    // pattern-absence version of it already failed once. My first attempt matched a ternary as
    // `? … :` on ONE LINE, and the reviewer's mutation writes `? (` with the arms on the lines below,
    // so it slipped straight through: the dead-branch variant survived again.
    //
    // What actually establishes reachability in a jsdom-less suite is ARITY. The marker is rendered
    // in exactly ONE place, and the field contains exactly the two conditionals it needs. Any dead
    // arm has to either duplicate the render (count goes up) or add a conditional (count goes up), so
    // both mutation shapes break a count instead of dodging a regex.
    const s = stripDetailComments(repositoryDetailSrc);

    // 1. The marker's text reaches the screen, from EXACTLY ONE site. Two sites is a ternary's pair
    //    of arms, which is the mutation that survived twice.
    expect([...s.matchAll(/\{artifact\.marker\.text\}/g)]).toHaveLength(1);

    // 2. The field's conditional budget, spent precisely. Slice the block so a conditional elsewhere
    //    on this 1100-line page cannot pay for one added here.
    const start = s.indexOf('artifact.marker !== null');
    expect(start).toBeGreaterThan(-1);
    const block = s.slice(start, s.indexOf('</Field>', start));
    // The slice really holds the render, so every count below reads the markup it claims to.
    expect(block).toContain('{artifact.marker.text}');
    // TWO `&&`, no more: the marker narrowing, and the image tag's own presence check. A third is a
    // wrapper — `false && (…)` around the render is the other way to kill it.
    expect([...block.matchAll(/&&/g)]).toHaveLength(2);
    // ZERO ternaries. `(?<!\?)\?(?![?.])` excludes the tooltip's `?? undefined` and any `?.`; unlike
    // the version this replaces it does NOT require a `:` on the same line, so `? (` at end of line
    // is caught. Proven able to fail below.
    const ternary = /(?<!\?)\?(?![?.])/g;
    expect([...block.matchAll(ternary)]).toHaveLength(0);

    // 3. THE COUNTS AND THE PATTERN ARE PROVEN ABLE TO FAIL, or "no branch" is unfalsifiable. This is
    //    the reviewer's exact mutation shape, and the multi-line form that defeated the first guard.
    const mutated = [
      "{artifact.marker.tone === 'pinned' ? (",
      '  <span>{artifact.marker.text}</span>',
      ') : (false && (',
      '  <span title={artifact.marker.note ?? undefined}>{artifact.marker.text}</span>',
      '))}',
    ].join('\n');
    expect([...mutated.matchAll(/\{artifact\.marker\.text\}/g)]).toHaveLength(2); // count 1 breaks
    expect([...mutated.matchAll(ternary)].length).toBeGreaterThan(0);             // count 3 breaks
    expect([...mutated.matchAll(/&&/g)].length).toBeGreaterThan(0);
    // …and the tooltip's coalesce alone is still not read as a ternary.
    expect([...'title={artifact.marker.note ?? undefined}'.matchAll(ternary)]).toHaveLength(0);

    // 4. The page must not re-derive the distinction the `.ts` already made.
    expect(s).not.toMatch(/artifact\.digest/);
    expect(s).not.toMatch(/artifact\.kind\s*===/);
  });

  it('holds no branch in the CONSENT DIALOG either — the same count, on the new file', () => {
    // RE-POINTED, and this is the copy that matters most (E28C/T7, D-C4d). The mutation this guard
    // defeated twice is a dead caution arm in the markup that renders the marker, and since 4d the
    // primary such markup is `PromoteConfirm.tsx`: the product's ONE promote dialog, at the moment
    // production is authorised. The mechanism is carried over verbatim — COUNTING, not
    // pattern-absence, because arity is what establishes reachability in a jsdom-less suite.
    const s = stripDetailComments(promoteConfirmSrc);

    // 1. Exactly ONE render site. Two is a ternary's pair of arms.
    expect([...s.matchAll(/\{artifact\.marker\.text\}/g)]).toHaveLength(1);

    // 2. The conditional budget of the block, spent precisely. Sliced so a conditional elsewhere in
    //    the dialog (the dev-tag contrast has its own) cannot pay for one added here.
    const start = s.indexOf('artifact.marker !== null');
    expect(start).toBeGreaterThan(-1);
    const block = s.slice(start, s.indexOf('</dl>', start));
    expect(block).toContain('{artifact.marker.text}');
    // ONE `&&` — the marker narrowing, and nothing else. A second is a wrapper: `false && (…)`
    // around the render is the other way to kill it.
    expect([...block.matchAll(/&&/g)]).toHaveLength(1);
    // ZERO ternaries. `(?<!\?)\?(?![?.])` excludes the tooltip's `?? undefined` and any `?.`, and
    // does NOT require a `:` on the same line — so `? (` at end of line is caught.
    const ternary = /(?<!\?)\?(?![?.])/g;
    expect([...block.matchAll(ternary)]).toHaveLength(0);

    // 3. PROVEN ABLE TO FAIL, on the reviewer's exact multi-line mutation shape.
    const mutated = [
      "{artifact.marker.tone === 'pinned' ? (",
      '  <span>{artifact.marker.text}</span>',
      ') : (false && (',
      '  <span title={artifact.marker.note ?? undefined}>{artifact.marker.text}</span>',
      '))}',
    ].join('\n');
    expect([...mutated.matchAll(/\{artifact\.marker\.text\}/g)]).toHaveLength(2);
    expect([...mutated.matchAll(ternary)].length).toBeGreaterThan(0);
    expect([...mutated.matchAll(/&&/g)].length).toBeGreaterThan(0);

    // 4. And the dialog must not re-derive what the `.ts` decided.
    expect(s).not.toMatch(/artifact\.digest/);
    expect(s).not.toMatch(/artifact\.kind\s*===/);
  });

  it('takes its TONE from an indexed table, never from a conditional', () => {
    // The tint was the other half of the dead branch: the amber lived inside the caution arm. An
    // indexed lookup has no arm — a wrong tone is then a wrong TABLE ENTRY, which the entry
    // assertions below catch, rather than an unreachable branch nothing catches.
    //
    // THE INDEXED READ is still asserted HERE (this page renders the marker in its Overview field);
    // the TABLE DECLARATION moved to `PromoteConfirm.tsx` in E28C/T7, because it existed twice —
    // once here and once in the project tab's dialog — and two tables for one judgement is the fork
    // that shipped a live production repo in provisioning's amber.
    const s = stripDetailComments(repositoryDetailSrc);
    expect(s).toMatch(/ARTIFACT_TONE_CLS\[artifact\.marker\.tone\]/);
    // …and this page must NOT declare a copy of it. The one thing worse than two tables is two
    // tables that agree today.
    expect(s).not.toMatch(/ARTIFACT_TONE_CLS:\s*Record/);
    // Exhaustive BY TYPE, so a third tone is a `tsc` error naming the table rather than a marker
    // inheriting whichever side of a ternary it falls on. Asserted as a real value now that the
    // table lives in a `.ts`: every tone the union admits has an entry, and the keys are exactly
    // the union's — which a source regex could only approximate.
    expect(Object.keys(ARTIFACT_TONE_CLS).sort()).toEqual(['caution', 'pinned']);
    for (const cls of Object.values(ARTIFACT_TONE_CLS)) expect(cls.length).toBeGreaterThan(0);
  });

  it('tints the caution amber and the pinned digest as a neutral mono chip', () => {
    // NO LONGER A SOURCE SLICE (E28C/T7). This used to `indexOf` the table in a `.tsx` and regex a
    // 500-char window, which could be satisfied by amber appearing anywhere nearby and silently
    // stopped checking anything if the declaration moved. The table now lives in `promoteConfirm.ts`,
    // so the entries are INDEXED and each assertion reads the exact string that reaches the DOM.
    //
    // `pinned` may carry a `bg-` because the surviving single table is the dialog's CHIP idiom — a
    // bordered neutral chip, which reads correctly both beside a commit sha and in a field grid.
    const { caution, pinned } = ARTIFACT_TONE_CLS;
    expect(caution).toContain('bg-amber-50');
    expect(caution).toContain('text-amber-800');
    // Never a fault tint: a known, accepted, self-healing state must not read as an error.
    expect(caution).not.toMatch(/rose|text-red-/);
    // The pinned digest is the NORMAL case and must not look like a STATUS — no warning or
    // success colour, just mono on a neutral chip.
    expect(pinned).toContain('font-mono');
    expect(pinned).not.toMatch(/amber|rose|emerald|text-red-/);
    // The two must be DISTINGUISHABLE, or the marker's whole distinction is invisible on screen.
    expect(caution).not.toBe(pinned);
  });

  it('narrows on the marker, so the whole field disappears when nothing is approvable', () => {
    // Otherwise it sits as an em dash on every repository between merges, and a permanently-present
    // row about production approval is noise the operator learns to skip. `marker !== null` is pinned
    // equivalent to `kind === 'none'` by the derivation suite above.
    expect(stripDetailComments(repositoryDetailSrc)).toMatch(/artifact\.marker !== null &&/);
  });

  it('shows the caution’s explanation, from the marker rather than a local constant', () => {
    // The label alone says "mutable" without saying what follows from it. The note is the only place
    // the consequence and the remedy exist, so a badge rendered without it is a warning with no
    // content — and reading it off the marker is what keeps it impossible to render one without the
    // other.
    expect(stripDetailComments(repositoryDetailSrc)).toMatch(/artifact\.marker\.note/);
  });

  it('writes neither the copy nor the decision inline', () => {
    // ONE constant each with a test — the idiom `CANDIDATE_ACTOR_VERB` and `LAST_DEPLOYED_LABEL`
    // established, because this epic's earlier wording defects lived in four and two copies. The page
    // must not even IMPORT them now: the marker carries the copy, so an import here would mean the
    // component is choosing copy again.
    const s = stripDetailComments(repositoryDetailSrc);
    expect(s).not.toMatch(/Tag-only —/);
    expect(s).not.toContain('PROMOTION_TAG_ONLY_LABEL');
    expect(s).not.toContain('PROMOTION_TAG_ONLY_NOTE');
  });

  it('calls the derivation rather than reading the raw field', () => {
    // A derivation nothing calls is inert, and a page reading `repo.prod_candidate_digest` directly
    // would have to re-decide the three states inline — where no test reaches it.
    expect(repositoryDetailSrc).toMatch(/promotionArtifact\s*\(/);
    expect(repositoryDetailSrc).not.toMatch(/repo\.prod_candidate_digest/);
  });

  it('does not turn the caution into a BLOCK on promoting', () => {
    // A tag-only candidate is still promotable — the backend accepts it and the legacy tag path
    // works. The marker informs the owner; it must not withhold the verb. `headerActions.promote`
    // is the only gate, and it does not consult the artifact.
    expect(headerActions({
      held: 'owner',
      roleLevel: 0,
      ungoverned: false,
      cicdStatus: 'deployed',
      prodCandidateStatus: 'pending',
      steps: [],
    }).promote).toBe(true);
    // Asserted structurally too: the digest must not appear in the gate's inputs.
    expect(repositoryDetailSrc).not.toMatch(/headerActions\s*\(\s*\{[^)]*digest/);
  });

  it('the reachability guards cannot be satisfied by prose', () => {
    // The project's eight-times failure, asserted directly. Every guard above reads comment-free
    // source; a comment quoting the render form or the tone lookup must not pass them.
    const prose = [
      '// The marker renders as {artifact.marker.text} with no branch.',
      "{/* Tone comes from ARTIFACT_TONE_CLS[artifact.marker.tone] — amber for caution. */}",
      '// bg-amber-50 text-amber-800 is the caution tint.',
      '<span>{somethingElse}</span>',
    ].join('\n');
    const stripped = stripDetailComments(prose);
    expect(stripped).not.toContain('artifact.marker.text');
    expect(stripped).not.toContain('ARTIFACT_TONE_CLS');
    expect(stripped).not.toContain('bg-amber-50');
  });
});

// ---------------------------------------------------------------------------
// prodGovernanceState — an ABSENT alarm is a claim too (E28 final review)
//
// `ungovernedInProd` needs the agent's `lifecycle_state`, and the agent read on this page is
// best-effort: its catch left the record null, the lifecycle argument arrived `undefined`, and the
// predicate answered `false` — no banner. Correct for the question it was asked, wrong as the
// page's behaviour: an absent alarm reads as "checked, nothing wrong", which is
// absent-data-as-no-bad-news on the page's highest-consequence statement.
// ---------------------------------------------------------------------------

describe('prodGovernanceState — a failed governance read does not read as approval', () => {
  it('still raises the full warning on an established serving + proposed', () => {
    expect(
      prodGovernanceState({ serving: 'serving', lifecycleState: 'proposed', agentRead: true }),
    ).toBe('ungoverned');
  });

  it('says UNKNOWN when something is in production and the record could not be read', () => {
    // The finding. Before this the same inputs produced silence.
    expect(
      prodGovernanceState({ serving: 'serving', lifecycleState: undefined, agentRead: false }),
    ).toBe('unknown');
  });

  it('says UNKNOWN when production itself is unestablished and there is a question to ask', () => {
    // The new middle row of the composition (E28A/T4). `serving === 'unknown'` means we could not
    // determine whether production is serving — so a `proposed` agent, or a governance record we
    // could not read, is a question and not a finding. Slate, never amber, and never silence.
    expect(
      prodGovernanceState({ serving: 'unknown', lifecycleState: 'proposed', agentRead: true }),
    ).toBe('unknown');
    expect(
      prodGovernanceState({ serving: 'unknown', lifecycleState: undefined, agentRead: false }),
    ).toBe('unknown');
    // …but an unestablished production with an APPROVED agent asks nothing: whatever is or is not
    // running, governance signed it off.
    expect(
      prodGovernanceState({ serving: 'unknown', lifecycleState: 'approved', agentRead: true }),
    ).toBe('none');
  });

  it('stays SILENT when the record WAS read and does not say proposed', () => {
    // Both directions: a warning on every read agent would be noise, and an operator who learns to
    // ignore this banner is worse off than one who never saw it.
    for (const lifecycle of ['approved', 'active', 'deprecated', '', null, undefined]) {
      expect(
        prodGovernanceState({ serving: 'serving', lifecycleState: lifecycle, agentRead: true }),
        String(lifecycle),
      ).toBe('none');
    }
  });

  it('stays SILENT on an unread record when NOTHING is in production', () => {
    // A repository that has shipped nothing to production raises no governance question, so an
    // unread record there is not worth an alarm — warning on every unread record would train the
    // operator to ignore the banner that matters. This is also #5's end state: a repository whose
    // only successful delivery went to a non-production stage reaches `serving: 'none'`.
    for (const lifecycle of ['proposed', undefined, null]) {
      for (const agentRead of [true, false]) {
        expect(
          prodGovernanceState({ serving: 'none', lifecycleState: lifecycle, agentRead }),
          `${String(lifecycle)}/${agentRead}`,
        ).toBe('none');
      }
    }
  });

  it('a repository that only delivered to a non-production stage raises NO banner (#5)', () => {
    // END-TO-END, through the two functions the `.tsx` actually calls in the order it calls them.
    // The whole finding in one assertion: a successful non-production build, a freshly registered
    // (therefore `proposed`) agent, and NOTHING promoted. The old predicate answered `ungoverned`
    // on exactly this input and printed "Serving production without governance approval".
    const serving = prodServingState({
      lastPromotionBuildId: null,
      lastPromotedAt: null,
      lastPromotedImageTag: null,
      deployments: [
        deployment({ build_id: 'build-1', outcome: 'started' }),
        deployment({ build_id: 'build-1', outcome: 'succeeded' }),
      ],
    });
    expect(serving).toBe('none');
    expect(prodGovernanceState({ serving, lifecycleState: 'proposed', agentRead: true })).toBe(
      'none',
    );
  });

  it('never offers a remedy — it is a state, not an action', () => {
    // D11: an Ops surface states that an approval is outstanding and never offers one. The union
    // carries no verb, which is what makes that mechanical rather than a comment.
    const states: string[] = ['ungoverned', 'unknown', 'none'];
    for (const s of states) {
      expect(s).not.toMatch(/approve|grant|classify|deprecate/i);
    }
  });
});

// ---------------------------------------------------------------------------
// recordStatusLabel — the THIRD machine's word, and it must not collide with the other two
// (E28A, finding #12's frontend half)
// ---------------------------------------------------------------------------

describe('recordStatusLabel — never renders a bare wire value, never invents one', () => {
  it('names what the record status is actually about', () => {
    // `repo.status` tracks the MATERIALIZE run and nothing else — its only writers are creation,
    // the failure path, and finalize. So the words name that run rather than the repository's
    // health, which the two pills in the header already answer for their own machines.
    expect(recordStatusLabel('provisioning')).toBe('Materializing');
    expect(recordStatusLabel('ready')).toBe('Materialized');
    expect(recordStatusLabel('failed')).toBe('Materialize failed');
  });

  it('does not render the word the OTHER two machines already use for something else', () => {
    // `opsStatus.ts` deliberately labels delivery's `ready` "Not built yet" because the runtime
    // machine's `ready` means "the agent is up and serving". A third bare `ready` on the same page
    // — in a field beside those two pills — is that same ambiguity a third time, and the weakest
    // of the three claims wearing the strongest-sounding word.
    for (const raw of ['provisioning', 'ready', 'failed']) {
      expect(recordStatusLabel(raw), raw).not.toBe(raw);
    }
    expect(Object.values(RECORD_STATUS_LABEL)).not.toContain('Ready');
  });

  it('absence is absence, not a status', () => {
    // A record with no status has not established one; the caller renders its em dash.
    expect(recordStatusLabel(null)).toBeNull();
    expect(recordStatusLabel(undefined)).toBeNull();
    expect(recordStatusLabel('')).toBeNull();
    expect(recordStatusLabel('   ')).toBeNull();
  });

  it('shows an UNRECOGNIZED value verbatim rather than mapping it to a plausible neighbour', () => {
    // The `toCicdStatus` rule, applied one level down: a value nobody established must not be
    // rendered as the nearest member. Passing it through says "the record holds this" — which is
    // true — where "Materializing" over an unknown value would be a confident sentence about a
    // repository we know nothing about.
    expect(recordStatusLabel('archived')).toBe('archived');
    expect(recordStatusLabel('  archived  ')).toBe('archived');
    // Case is NOT normalized into a match, because `repo.status` has exactly two writers, both
    // backend enums — an off-case value did not come from them and guessing which member it meant
    // is the invention this refuses.
    expect(recordStatusLabel('READY')).toBe('READY');
  });
});

// ---------------------------------------------------------------------------
// shouldPollRepo — WHEN MUST THIS PAGE ASK AGAIN? (E28D, small-fixes map §4)
//
// The detail page had NO poller: it took one snapshot per load and `handleRetry` took a second one
// at the instant the retry was accepted — the moment the record is guaranteed to be at its LEAST
// final (steps reset to pending, `cicd_status` back to provisioning). Nothing ever asked again, so
// the surface whose whole selling point is that the timeline is permanently viewable here showed a
// frozen one. Two pollers already existed for this exact record and endpoint (the create modal's
// and the project tab's promoting rows), and the page that exists to watch a run had neither.
//
// The predicate is HERE rather than in the `.tsx` for two reasons: there is no jsdom in this
// vitest setup, so a mounted effect is untestable; and a source guard further down forbids the
// page from calling the has-it-stopped predicate at all (a past defect read it as "did it
// succeed"). So the page asks THIS question and the stopped-predicate stays reached from one place.
// ---------------------------------------------------------------------------

/** A repo record carrying only what `shouldPollRepo` reads. */
type Pollable = Parameters<typeof shouldPollRepo>[0];
function pollable(over: Partial<Pollable> = {}): Pollable {
  return { cicd_status: 'ready', steps: [], ...over };
}
/** Steps in each of the four statuses. Full records: the predicate takes the record's own array. */
function step(status: Pollable['steps'][number]['status']): Pollable['steps'][number] {
  return { key: `s-${status}`, label: status, status };
}
const RUNNING = step('running');
const PENDING = step('pending');
const DONE = step('done');
const FAILED = step('failed');

describe('shouldPollRepo — a settled repository does ZERO polling', () => {
  it('polls while the materialize run is still moving', () => {
    // The case the page was built for and never served: a run with work left in it.
    expect(shouldPollRepo(pollable({ steps: [DONE, RUNNING, PENDING] }))).toBe(true);
    expect(shouldPollRepo(pollable({ steps: [PENDING, PENDING] }))).toBe(true);
  });

  it('stops the moment the run reaches a terminal state', () => {
    // Every step done, and a failed run — a failure HALTS the run, so there is nothing further to
    // learn from asking. This is the rule the project tab already enforces for its rows: a table
    // with nothing in flight makes no request at all.
    expect(shouldPollRepo(pollable({ steps: [DONE, DONE] }))).toBe(false);
    expect(shouldPollRepo(pollable({ steps: [DONE, FAILED] }))).toBe(false);
    expect(shouldPollRepo(pollable({ steps: [FAILED, PENDING] }))).toBe(false);
  });

  it('a record with NO steps is not a record with a finished run', () => {
    // The stopped-predicate answers `true` for an empty array (nothing is running or pending), and
    // reading that as "terminal" is correct here but only because the delivery half is asked
    // separately below — an empty timeline is not evidence of a completed one, so it must not by
    // itself arm a poller either. Both directions matter: no steps + resting delivery ⇒ silence.
    expect(shouldPollRepo(pollable({ steps: [] }))).toBe(false);
  });

  it('ALSO polls while DELIVERY is in flight, so a promoting repo settles on this page too', () => {
    // The second condition, and it is not redundant: a promote runs long after materialize is
    // terminal, so the steps-side answer is `false` for the entire duration. Without this the page
    // would sit on "Promoting to prod…" forever while the project tab's row settled.
    expect(shouldPollRepo(pollable({ cicd_status: 'promoting', steps: [DONE, DONE] }))).toBe(true);
    expect(shouldPollRepo(pollable({ cicd_status: 'provisioning', steps: [] }))).toBe(true);
  });

  it('does not poll for a delivery status that is RESTING rather than running', () => {
    // The distinction the in-flight predicate draws and this must inherit rather than re-derive:
    // materialize-finished-no-build-yet and a terminal build are both states where nothing is
    // happening. A poller on either is a request every 3s for a record that will not change.
    for (const status of ['ready', 'deployed', 'failed', 'unknown']) {
      expect(shouldPollRepo(pollable({ cicd_status: status, steps: [DONE] })), status).toBe(false);
    }
  });

  it('narrows the wire value rather than comparing it raw', () => {
    // An unrecognized or absent status must not arm a poller. It reaches the in-flight predicate
    // through the same narrowing boundary every other reader uses, so it lands on `unknown`.
    expect(shouldPollRepo(pollable({ cicd_status: 'BUILDING', steps: [DONE] }))).toBe(false);
    expect(shouldPollRepo(pollable({ cicd_status: '', steps: [DONE] }))).toBe(false);
    // …and the narrowing is case-insensitive in the direction that MATTERS: a writer's value in a
    // different case still arms it, because the boundary lowercases before matching.
    expect(shouldPollRepo(pollable({ cicd_status: 'PROMOTING', steps: [DONE] }))).toBe(true);
  });
});

// ---------------------------------------------------------------------------
// deliveryHeaderState — the header may not say "Not built yet" over a running build (§5)
//
// Observed live: the delivery pill read "Not built yet" directly above the environment strip's
// live "Deployment in progress". Both were TRUE of their own source. The pill reads
// `repo.cicd_status`, a scalar with no stage in it whose `ready` means "materialize finished, no
// build has landed yet"; the strip reads the deployment HISTORY, which DOES know a build is
// running because the launch row exists with no terminal partner. The buildspec writes only
// terminal outcomes — there is no intermediate `building` write anywhere — so the header was stale
// for the whole duration of every build.
//
// DERIVED, NOT WRITTEN (the decided option). The delivery union stays exactly the set of values a
// real writer produces; "Building…" is a VIEW state assembled from two facts the page already
// holds, so the no-member-without-a-writer rule is honored rather than violated. The honest
// long-term answer is a writer in the buildspec, which the fleet list would also benefit from
// (it loads no history and so can derive nothing) — that is a separate change with its own live
// test, and this one fixes the contradiction on the one page where both facts are on screen.
// ---------------------------------------------------------------------------

/** The rows the header reads, carrying only the field it asks about. */
const idle = { inFlight: false };
const busy = { inFlight: true };

describe('deliveryHeaderState — "Building…" when the history says a build is running', () => {
  it('says Building… over a running build, and treats it as in flight', () => {
    // THE FINDING. `ready` is the state the contradiction was observed in: materialize finished,
    // the first build is running, and the header claimed nothing had ever been built.
    const state = deliveryHeaderState({
      cicdStatus: 'ready',
      environmentRows: [idle, busy],
      historyError: false,
    });
    expect(state.label).toBe(DELIVERY_BUILDING_LABEL);
    expect(state.inFlight).toBe(true);
  });

  it('says it over a REDEPLOY too, not only a first build', () => {
    // `deployed` is the same contradiction one build later: the record's newest terminal write says
    // deployed, and a new build is running right now. "Deployed" beside "Deployment in progress" is
    // the same stale sentence with a friendlier word.
    const state = deliveryHeaderState({
      cicdStatus: 'deployed',
      environmentRows: [busy],
      historyError: false,
    });
    expect(state.label).toBe(DELIVERY_BUILDING_LABEL);
    expect(state.inFlight).toBe(true);
  });

  it('leaves every resting record reading EXACTLY what it reads today', () => {
    // The derivation is additive: with no stage in flight the pill is unchanged, word for word,
    // from the shared table — asserted against the table itself so a re-typed string cannot drift.
    for (const raw of ['ready', 'deployed', 'provisioning', 'promoting', 'failed', 'unknown']) {
      const state = deliveryHeaderState({
        cicdStatus: raw,
        environmentRows: [idle, idle],
        historyError: false,
      });
      expect(state.label, raw).toBe(CICD_LABEL[raw as keyof typeof CICD_LABEL]);
    }
    // …including the two states the RECORD already reports as in flight, which keep their spinner
    // from the same predicate the pill used before this function existed.
    expect(deliveryHeaderState({ cicdStatus: 'provisioning', environmentRows: [] }).inFlight).toBe(
      true,
    );
    expect(deliveryHeaderState({ cicdStatus: 'promoting', environmentRows: [] }).inFlight).toBe(true);
    expect(deliveryHeaderState({ cicdStatus: 'ready', environmentRows: [] }).inFlight).toBe(false);
    expect(deliveryHeaderState({ cicdStatus: 'deployed', environmentRows: [] }).inFlight).toBe(false);
  });

  it('a FAILED history read never produces a Building… claim (fails safe)', () => {
    // The rule this whole page is built on: absent data is not good news, and it is not bad news
    // either — it is not evidence. A failed history read arrives as rows that report unknown, and
    // "Building…" derived from a read that never happened is a confident sentence about a build
    // nobody observed. The record's own word is what survives, because the record WAS read.
    const state = deliveryHeaderState({
      cicdStatus: 'ready',
      environmentRows: [busy],
      historyError: true,
    });
    expect(state.label).toBe(CICD_LABEL.ready);
    expect(state.inFlight).toBe(false);
    // And the discriminating direction, or the guard above would pass on a function that simply
    // never derived anything: the SAME rows with a readable history do produce the claim.
    expect(
      deliveryHeaderState({ cicdStatus: 'ready', environmentRows: [busy], historyError: false })
        .label,
    ).toBe(DELIVERY_BUILDING_LABEL);
  });

  it('never overrides a record state that is already RUNNING something else', () => {
    // The vocabulary collision this must not add to. `provisioning` means materialize is running
    // and `promoting` means a PROD build is running — both are more specific than "a build is
    // running", and both already spin. Replacing either with "Building…" would lose the stage of
    // the lifecycle the operator is actually watching. `failed` is louder still: a live build does
    // not un-fail the delivery the record recorded.
    for (const raw of ['provisioning', 'promoting', 'failed']) {
      const state = deliveryHeaderState({
        cicdStatus: raw,
        environmentRows: [busy],
        historyError: false,
      });
      expect(state.label, raw).toBe(CICD_LABEL[raw as keyof typeof CICD_LABEL]);
    }
  });

  it('says nothing about a build when NO status was reported at all', () => {
    // `unknown` is this module's fallback for a value no writer produced. An in-flight row beside
    // it is not enough to name the delivery state — the record is the thing that would have to
    // carry it, and it does not.
    const state = deliveryHeaderState({
      cicdStatus: null,
      environmentRows: [busy],
      historyError: false,
    });
    expect(state.label).toBe(CICD_LABEL.unknown);
    expect(state.inFlight).toBe(false);
  });

  it('is its own word, distinct from all three machines already on the page', () => {
    // Three machines contend for "ready" here and the page's labels exist to keep them apart. A
    // fourth ambiguity is the failure mode: "Building…" means a DELIVERY BUILD is running, which
    // is neither materialize running (`Provisioning`) nor the record's own `Materializing`.
    expect(Object.values(CICD_LABEL)).not.toContain(DELIVERY_BUILDING_LABEL);
    expect(Object.values(RECORD_STATUS_LABEL)).not.toContain(DELIVERY_BUILDING_LABEL);
    expect(DELIVERY_BUILDING_LABEL).toBe('Building…');
  });

  it('an absent historyError is the same as a readable history', () => {
    // The field is optional so the strip's own source shape can be passed straight through. An
    // omitted flag must mean "the read did not fail", matching how the row builder reads it.
    expect(deliveryHeaderState({ cicdStatus: 'ready', environmentRows: [busy] }).label).toBe(
      DELIVERY_BUILDING_LABEL,
    );
  });
});

describe('the .tsx surfaces obey the rules a .ts cannot enforce for them', () => {
  it('parses all three sources (guards every assertion below against an empty read)', () => {
    expect(repositoryDetailSrc.length).toBeGreaterThan(500);
    expect(environmentStripSrc.length).toBeGreaterThan(500);
    expect(tabStripSrc.length).toBeGreaterThan(300);
    expect(repositoryDetailSrc).toContain('OpsPage');
    expect(environmentStripSrc).toContain('stage');
  });

  it('contains NO stage literal anywhere (C5) — quoted OR as a property access', () => {
    // Deliberately matched on the RAW source with no lowercasing, trimming or comment
    // stripping: a normalization step is how a guard silently stops seeing the thing it
    // guards.
    //
    // TWO alternatives, because the first version of this guard COULD NOT SEE THE BUG IT WAS
    // WRITTEN FOR. It matched quoted forms only, on the reasoning that the bare word appears
    // legitimately in prose — true, but the live crash A2 fixed was
    // `tenantAccount.stages.<stage>.account_id`, a PROPERTY ACCESS: neither prose nor quoted,
    // and invisible to a quoted-only pattern. Since `noUncheckedIndexedAccess` is off
    // project-wide, re-introducing that read passes `tsc` AND every test and crashes a
    // single-stage tenant at runtime — so this test is the only possible guard, and it has to
    // cover the form that actually shipped.
    //
    //   1. a quoted literal            `'<stage>'`
    //   2. a stage-keyed member access  `.<stage>`  /  `['<stage>']`  /  `[<stage>]`
    //   3. a stage as a BINDING or an object KEY  `{ <stage>: … }` / `{ <stage>, … }`
    //
    // Alternative 2 requires a dot-or-bracket BEFORE the name and no identifier character
    // AFTER it, so it matches a stage used as an object KEY without matching the bare word in
    // prose or a longer identifier that merely starts with it (`a.development.x` and
    // `obj.prodigy.x` do not match; `stages.<stage>.account_id`, `stages["<stage>"]` and
    // `{...t.stages.<stage>}` do — asserted in the next test).
    //
    // Alternative 3 exists because 1 and 2 BOTH missed three live forms, one of which is
    // literally the declaration A2 deleted from `client.ts` — the file this guard covers
    // specifically to hold that contract:
    //   `stages: { <stage>: TenantStageConfig; … }`  (a hardcoded pair in a TYPE)
    //   `stages: { <stage>: cfg, … }`                (a hardcoded pair in a PAYLOAD)
    //   `const { <stage>, … } = t.stages;`           (THE A2 CRASH in destructuring syntax:
    //                                                 with `noUncheckedIndexedAccess` off the
    //                                                 binding types non-optional and a
    //                                                 single-stage tenant throws as before)
    // It requires an opening brace/comma/semicolon/paren BEFORE the name and a colon, comma or
    // closing brace AFTER, which is what an object key or a destructured binding looks like and
    // what `cond ? <stage> : x`, `f(<stage>)` and prose do not (asserted in the next test).
    //
    // The pattern is applied to THIS file too, further down, which is why nothing here writes a
    // quoted stage name even in a comment.
    const stageLiteral =
      /(['"`])(dev|prod)\1|[.[]\s*['"`]?(dev|prod)['"`]?(?![\w$])|[{,;(]\s*(dev|prod)\s*[:,}]/;
    for (const [name, src] of [
      ['RepositoryDetail.tsx', repositoryDetailSrc],
      ['EnvironmentStrip.tsx', environmentStripSrc],
      ['TabStrip.tsx', tabStripSrc],
      // ProjectDetail.tsx HELD the live crash, and was not covered at all. client.ts declares
      // the widened type whose whole point is that no reader may index a stage literal.
      ['ProjectDetail.tsx', projectDetailSrc],
      ['client.ts', clientSrc],
    ] as const) {
      expect(stageLiteral.test(src), `${name} contains a stage literal`).toBe(false);
    }
    // And the pure module too — the decisions are here, so a hardcode here would be worse.
    expect(stageLiteral.test(pureSrc)).toBe(false);
    // …and THIS FILE, so the guard covers the file that defines it. A test fixture that names a
    // conventional stage is how "no stage literal in frontend/" quietly becomes "except where we
    // test it", and the sort test above shows the two names can be exercised as INPUT without
    // being written as a literal.
    expect(stageLiteral.test(ownSrc)).toBe(false);
  });

  it('the stage guard can SEE a property-access stage read (the form A2 fixed)', () => {
    // A guard that cannot fail is worse than no guard, and the previous one could not fail on
    // the expression that caused the crash. This asserts the pattern's discriminating power
    // directly, so the guard above cannot quietly regress to quoted-only.
    const stageLiteral =
      /(['"`])(dev|prod)\1|[.[]\s*['"`]?(dev|prod)['"`]?(?![\w$])|[{,;(]\s*(dev|prod)\s*[:,}]/;
    const stage = 'd' + 'ev';
    const other = 'pro' + 'd';
    // The exact shapes that crashed a single-stage tenant, and their bracket forms.
    expect(stageLiteral.test(`tenantAccount.stages.${stage}.account_id`)).toBe(true);
    expect(stageLiteral.test(`t.stages["${stage}"]`)).toBe(true);
    expect(stageLiteral.test(`{...t.stages.${stage}}`)).toBe(true);
    // The three forms alternatives 1+2 missed. The first is LITERALLY the declaration A2 removed
    // from client.ts, so a quoted-or-member-access-only guard could not see the original
    // violation of the contract it exists to enforce.
    expect(
      stageLiteral.test(`stages: { ${stage}: TenantStageConfig; ${other}: TenantStageConfig };`),
    ).toBe(true);
    expect(stageLiteral.test(`stages: { ${stage}: cfg, ${other}: cfg }`)).toBe(true);
    expect(stageLiteral.test(`const { ${stage}, ${other} } = t.stages;`)).toBe(true);
    // …and prose or an unrelated identifier that merely CONTAINS the word must not match, or the
    // guard would be unusable and get deleted. The last three are the shapes alternative 3 must
    // NOT claim: a ternary branch, a call argument, and a longer identifier.
    expect(stageLiteral.test(`the ${stage} buildspec writes it`)).toBe(false);
    expect(stageLiteral.test(`a.${stage}elopment.x`)).toBe(false);
    expect(stageLiteral.test(`cond ? ${stage} : x`)).toBe(false);
    expect(stageLiteral.test(`f(${stage})`)).toBe(false);
  });

  it('does not caption a runtime pill by an UNATTRIBUTED stage (M-d, made mechanical)', () => {
    // TWO ASSERTIONS WERE FLIPPED HERE, DELIBERATELY (E28C/T7, D-C4c). This guard used to read
    // `expect(environmentStripSrc).not.toContain('RUNTIME_LABEL')` and the same for
    // `RUNTIME_BADGE_KEY`: while the platform had ONE runtime answer per agent, the ABSENCE of
    // pill markup in the strip was the mechanical guarantee that no stage row could make a
    // runtime claim. Since E28A the backend probes a NAMED stage, T7 asks, and some rows now
    // have real per-stage evidence — so pill markup must exist in that file, and pinning its
    // absence would now pin the defect (every stage showing the note, including the ones with a
    // genuine reading) rather than the rule.
    //
    // THE RULE ITSELF DID NOT CHANGE, so what replaces those two assertions is a guard on the
    // same rule one level in: the strip may not obtain a status EXCEPT through the model's
    // authorisation. `stageRuntimeCell` hands back a status only on its `pill` answer and `null`
    // on the other two, and it is the only party that compares a reading's stage to the row's.
    expect(environmentStripSrc).toContain('stageRuntimeCell');
    // The pill markup now EXISTS in the strip (the flip), and is reached only via `cell`.
    expect(environmentStripSrc).toContain('RUNTIME_LABEL');
    expect(environmentStripSrc).toContain('RUNTIME_BADGE_KEY');
    // …and every one of its reads is off the model's answer, never off a raw reading. A
    // `RUNTIME_LABEL[...]` indexed by anything else is the re-derivation this rule forbids.
    for (const table of ['RUNTIME_LABEL', 'RUNTIME_BADGE_KEY']) {
      const reads = [...environmentStripSrc.matchAll(new RegExp(`${table}\\[([^\\]]*)\\]`, 'g'))];
      expect(reads.length).toBeGreaterThan(0);
      for (const [, index] of reads) expect(index).toContain('cell.status');
    }
    // The strip must not reach for the AGENT-level reading at all: its prop is per-stage, and
    // `runtimeScope` (the agent-scope resolver) is no longer this file's to call — placing that
    // answer on a stage row is the original fabrication.
    expect(environmentStripSrc).not.toContain('runtimeScope(');
    // The agent-level pill is still rendered by the page ONCE, outside the per-stage rows.
    expect(repositoryDetailSrc).toContain('RUNTIME_LABEL');
  });

  it('wires the back link through the selector, and keeps the fleet fallback on the early returns', () => {
    // The `.tsx` half of D-C4b, and the half review broke. COUNTED rather than substring-matched,
    // because the counts are what a mutation cannot dodge: re-pointing the loaded render at the fleet
    // list moves BOTH numbers (four fallbacks, no selector call), and adding a fourth fleet fallback
    // to the loaded render moves the first.
    const s = stripDetailComments(repositoryDetailSrc);

    // 1. The loaded render asks the selector — the one place a destination is chosen. Exactly once:
    //    a second call site would be a second answer to "where does back go?".
    expect([...s.matchAll(/repoBackLink\(/g)]).toHaveLength(1);
    // …and its result is what reaches the frame, both halves. A call whose value is dropped and a
    //    hardcoded route beside it is precisely the shape this counts against.
    expect(s).toMatch(/backTo=\{backLink\.to\}/);
    expect(s).toMatch(/backLabel=\{backLink\.label\}/);

    // 2. EXACTLY THREE fleet-list fallbacks, one per early return. Those three render BEFORE `repo`
    //    exists, so they have no `project_id` to link to and must keep pointing at the fleet list —
    //    but a fourth means the loaded render regressed onto it, which is the review mutation.
    expect([...s.matchAll(/backTo="\/ops\/repositories"/g)]).toHaveLength(3);

    // 3. The page writes no arrow of its own. The label's leader belongs to the selector (and the
    //    default to `OpsPage`), so an inline arrow here would be a third source for one glyph.
    expect(s).not.toMatch(/backLabel="/);
    expect(s).not.toMatch(/←/);
  });

  it('keeps OpsPage’s default back label as ONE literal, for its 18 other call sites', () => {
    // `backLabel` is OPTIONAL precisely so every pre-existing caller keeps its exact wording. The
    // default is a named constant rather than inline JSX, so the 18 call sites that never pass the
    // prop cannot be changed by editing markup — and the literal must appear exactly once.
    expect([...opsPageSrc.matchAll(/'← Operations'/g)]).toHaveLength(1);
    expect(opsPageSrc).toMatch(/backLabel\s*=\s*OPS_BACK_LABEL/);
    expect(OPS_BACK_LABEL).toBe('← Operations');
    // The prop is genuinely optional (`backLabel?:`), which is what makes those call sites untouched.
    expect(opsPageSrc).toMatch(/backLabel\?:\s*string/);
    // And the frame renders the resolved value, not the constant directly — otherwise an override
    // would be accepted and ignored.
    expect(opsPageSrc).toMatch(/\{backLabel\}/);
  });

  it('routes every wire status through the narrowing boundary, never an assertion', () => {
    expect(repositoryDetailSrc).toContain('toCicdStatus');
    expect(repositoryDetailSrc).toContain('toRuntimeStatus');
    // `as CicdStatus` / `as RuntimeStatusKey` would ASSERT the narrowing instead of doing it.
    expect(repositoryDetailSrc).not.toContain('as CicdStatus');
    expect(repositoryDetailSrc).not.toContain('as RuntimeStatusKey');
  });

  it('POLLS while something is running, and asks the predicate rather than deciding itself', () => {
    // The page took ONE snapshot per load and `handleRetry` took a second at the instant the retry
    // was accepted — the moment the record is least final. Both halves are asserted because either
    // alone leaves the finding live: a predicate nobody calls is inert, and an interval that asks
    // its own question is the fork this split exists to prevent.
    expect(repositoryDetailSrc).toContain('shouldPollRepo');
    expect(repositoryDetailSrc).toMatch(/setInterval/);
    // The same endpoint the two existing pollers use — not a second one, and not the whole
    // best-effort page load (which would re-run the fleet list, the project, the agent and every
    // per-stage probe to learn one record's steps).
    expect(repositoryDetailSrc).toContain('projectsApi.getRepoStatus');
    // A settled repo must make NO request, so the interval is cleared rather than left ticking.
    expect(repositoryDetailSrc).toMatch(/clearInterval/);
    // The tick swallows its own failure and keeps polling — a transient error is not a terminal
    // state, and `refetch()`'s error handling does not apply to a background refresh.
    expect(repositoryDetailSrc).toMatch(/getRepoStatus\([^)]*\)\s*\.catch\(\(\) => null\)/);
  });

  it('reloads the HISTORY once on the settle edge, not only the record (fix round 1, I-A)', () => {
    // The gap the record-only poller left: the deployment history is read in ONE place (the load
    // effect), so it is a page-load snapshot — and the derived header's word is computed FROM that
    // snapshot. Two live holes followed. After a Retry, materialize completing stops the poller
    // while the delivery build is only just starting, and the buildspec writes no intermediate
    // status — so the header would read "Not built yet" over a running build, this task's own
    // finding surviving on that path. After a promote settles, the stale launch row would make the
    // header claim "Building…" off a record read seconds earlier as deployed.
    //
    // Asserted on the TICK, which is what makes it a settle edge rather than a mount-time reload:
    // the tick asks the same pinned predicate about the record it just fetched, and on a `false`
    // answer stops itself and reloads once. Anchored on the call form so the comments explaining
    // this cannot satisfy it.
    // BOUNDED AT THE INTERVAL'S OWN CLOSING `}, 3000);`. Slicing to end-of-file instead made every
    // assertion below vacuous: the window was the whole rest of the page (~46k chars) and contained
    // four `refetch()` calls that have nothing to do with this poller — `handlePromote`'s among them
    // — so deleting the settle-edge reload entirely still left `toMatch(/refetch\(\)/)` green, and a
    // per-tick reload satisfied the ordering assertion too. A guard for a call site has to be scoped
    // to that call site.
    const tickStart = repositoryDetailSrc.search(/setInterval/);
    const tick = repositoryDetailSrc.slice(
      tickStart,
      repositoryDetailSrc.indexOf('}, 3000);', tickStart),
    );
    // The window really closed before the rest of the page — or the bound silently stopped matching
    // and the hole above is back while everything still looks anchored.
    expect(tick.length).toBeGreaterThan(200);
    expect(tick.length).toBeLessThan(1500);
    expect(tick).toMatch(/if\s*\(\s*!shouldPollRepo\(fresh\)\s*\)/);
    expect(tick).toMatch(/refetch\(\)/);
    // ONCE PER SETTLE, NEVER PER TICK. The interval is stopped before the reload is requested, so no
    // further tick can fire in the window before the dependency flip tears the effect down — a
    // `refetch` on every tick would re-run the fleet list, the project, the agent, every per-stage
    // runtime probe and the history every 3 seconds, which is the cost the record-only tick avoids.
    expect(tick.search(/stop\(\)/)).toBeLessThan(tick.search(/refetch\(\)/));
    // And the in-flight tick that resumes after teardown touches nothing — `clearInterval` does not
    // cancel a request already awaiting, so the cancelled flag is what makes the cleanup complete.
    expect(tick).toMatch(/if\s*\(cancelled/);
  });

  it('renders the DERIVED delivery header, so the pill cannot contradict the strip below it', () => {
    // The finding: "Not built yet" in the header directly above a live "Deployment in progress" in
    // the strip, for the whole duration of every build. The pill now renders one derived value, and
    // the LABEL and the SPINNER both come from it — deriving the word while leaving the spinner on
    // the raw record's predicate would have shipped "Building…" wearing a resting dot.
    expect(repositoryDetailSrc).toContain('deliveryHeaderState');
    expect(repositoryDetailSrc).toMatch(/delivery\.label/);
    expect(repositoryDetailSrc).toMatch(/delivery\.inFlight/);
    // The raw reads must be GONE from the pill, or the derivation sits beside what it replaced.
    // `CICD_LABEL[` and the bare predicate call are the two forms that would re-break it.
    expect(repositoryDetailSrc).not.toMatch(/\{CICD_LABEL\[/);
    expect(repositoryDetailSrc).not.toMatch(/isCicdInFlight\s*\(/);
    // It reads the SAME rows the strip does, from the same source object — two independently
    // derived row sets is how the header and the strip would disagree again while both look right.
    expect(repositoryDetailSrc).toMatch(/environmentRows\s*\(/);
  });

  it('uses the shared status LABELS, never its own strings', () => {
    // The runtime pill still indexes the shared table itself. The delivery pill reaches it ONE
    // level down since E28D — `deliveryHeaderState` reads `CICD_LABEL` and adds the single derived
    // word the wire cannot carry — so what is asserted here is that the page names no delivery
    // label of its own, which is checked directly below and by the derived-header test above.
    // (Anchored on the ACCESSOR, so the import comment explaining this cannot satisfy it.)
    expect(repositoryDetailSrc).toContain('RUNTIME_LABEL[');
    expect(pureSrc).toContain('CICD_LABEL[');
    // The two pills render UNCAPTIONED, which is why T10 gave them distinct `failed` labels.
    // A local "Failed" string here would undo that.
    expect(repositoryDetailSrc).not.toContain("'Delivery failed'");
    expect(repositoryDetailSrc).not.toContain("'Runtime failed'");
    // …and the derived word is not written in the page either: it is a pinned constant in the `.ts`,
    // so the pill and the tests that reason about the vocabulary collision read ONE string.
    expect(repositoryDetailSrc).not.toContain(DELIVERY_BUILDING_LABEL);
    expect(pureSrc).toContain(DELIVERY_BUILDING_LABEL);
  });

  it('offers NO governance verb — the lifecycle pill is read-only (D11)', () => {
    // The shadow-governance failure mode: an Ops surface must never grow an
    // approve/grant/classify/deprecate affordance. Promote is the ONE permitted action and it
    // is the governed route, not a second path.
    for (const forbidden of [
      'agentsApi.transition',
      'agentsApi.submit',
      'grantsApi.add',
      'grantsApi.remove',
      'projectRolesApi.grant',
      'projectRolesApi.revoke',
    ]) {
      expect(repositoryDetailSrc, forbidden).not.toContain(forbidden);
    }
  });

  it('reuses the EXISTING retry route and the EXISTING delete modal — no second path', () => {
    // Both capabilities already exist with the correct gates. A second confirm dialog or a
    // hand-rolled cascade here would be a second thing to keep in step with the E23 teardown.
    expect(repositoryDetailSrc).toContain('projectsApi.retryRepo');
    // Both the IMPORT and the mount, so removing either fails — and never a second delete path
    // (its own confirm, or a direct `deleteRepo` call bypassing the checklist).
    expect(repositoryDetailSrc).toContain("from './DeleteRepositoryModal'");
    expect(repositoryDetailSrc).toContain('<DeleteRepositoryModal');
    expect(repositoryDetailSrc).not.toContain('projectsApi.deleteRepo');
    expect(repositoryDetailSrc).not.toContain('projectsApi.deletePreview');
    // The gates are decided in the `.ts`, where the tests above reach them — not re-derived
    // here (and `retry` must not be gated on the plain role, which would hide the only
    // recovery path on an ungoverned project).
    expect(repositoryDetailSrc).toContain('headerActions');
    expect(repositoryDetailSrc).not.toContain('meetsRole');
    // "Nothing to retry" is an already-complete signal, not a failure — matched on the raw
    // message as control flow before any error copy.
    expect(repositoryDetailSrc).toMatch(/nothing to retry/i);
  });

  it('renders the step WORDING through the shared mapping, not the raw wire status', () => {
    // One repo's step must not read "pending" here and "Waiting" on the project page.
    expect(repositoryDetailSrc).toContain('stepStatusText');
    // The positive assertion above is the real guard; this negative one is tolerant of inner
    // whitespace and the usual stringify/cast wrappers, because a bare substring check is
    // evaded by `{ s.status }`, `{String(s.status)}` and `{s.status as string}`.
    expect(repositoryDetailSrc).not.toMatch(
      /\{\s*(?:String\(\s*)?s\.status(?:\s*\))?(?:\s+as\s+\w+)?\s*\}/,
    );
  });

  it('does not render the raw project UUID as the project link text', () => {
    // The design dropped opaque ids from Ops surfaces, and the project read that supplies
    // `effective_role` already carries the name — only it was being thrown away.
    expect(repositoryDetailSrc).toContain('projectName ?? repo.project_id');
    // The id is still used as a ROUTE segment and a prop, which is correct — so the guard is on
    // the id standing ALONE as an element's child, which is the rendering case.
    expect(repositoryDetailSrc).not.toMatch(/>\s*\{repo\.project_id\}/);
  });

  it('labels the framework field FRAMEWORK, never Model', () => {
    // `framework` is the scaffold name, fixed at creation; the governance agent page labels the
    // same field "Framework". The real model id is not on the TS `Agent` interface.
    expect(repositoryDetailSrc).toContain('label="Framework"');
    expect(repositoryDetailSrc).not.toContain('label="Model"');
  });

  it('honours TabStrip\'s own contract about the whole tabpanel attribute set', () => {
    // TabStrip renders NOTHING below two tabs, and its comment says a caller must then not
    // point a panel at a `role="tab"` that does not exist. Dormant at two ready tabs — but the
    // commit that EXTRACTS the contract must not be the one that violates it.
    expect(tabStripSrc).toContain('tabs.length < 2');
    // The FULL set is conditional, spread as one object — an orphan `role="tabpanel"` with no
    // owning tablist and no accessible name is what the Settings fix round rejected, so a
    // conditional LABEL beside an unconditional `role` is not enough.
    expect(repositoryDetailSrc).toContain('panelProps');
    // EVERY selectable tab's panel goes through it — a new tab body that spelled the attributes out
    // itself would reintroduce the orphan-ARIA case for its own panel only, which is the shape a
    // guard listing two fixed keys would have missed.
    for (const key of selectableTabKeys()) {
      expect(repositoryDetailSrc, key).toContain(`{...panelProps('${key}')}`);
    }
    // …and never the unconditional forms, in any of the four attributes.
    expect(repositoryDetailSrc).not.toContain('aria-labelledby={tabId(');
    expect(repositoryDetailSrc).not.toMatch(/role="tabpanel"/);
    expect(repositoryDetailSrc).not.toMatch(/tabIndex=\{0\}/);
  });

  it('clears the success notice when an action starts, so two claims cannot co-exist', () => {
    // A second promote that FAILS rendered the rose error beside the stale emerald notice.
    expect(repositoryDetailSrc).toContain('setPromoteNotice(null)');
    // …and the notice must not promise a reload the code does not wait for.
    expect(repositoryDetailSrc).not.toMatch(/updates when you reload/i);
  });

  it('reports an unreadable deployment history as unknown, never as "never deployed"', () => {
    expect(repositoryDetailSrc).toContain('setHistoryError(true)');
    expect(repositoryDetailSrc).toContain('historyError');
    // The strip needs the flag to say so.
    expect(environmentStripSrc).toContain('historyUnknown');
  });

  it('tells the strip when the TENANT could not be resolved (FR-1)', () => {
    // The `.ts` half was already self-aware — it documented `stages: null` as "the tenant could not
    // be resolved" — but the page never told it WHICH, so a failed read rendered as a definite
    // claim about the tenant. Both halves are asserted, because either alone leaves the finding
    // live: a derived flag nobody passes is inert, and a passed flag nothing renders is invisible.
    expect(repositoryDetailSrc).toContain('stagesUnknown');
    // Guarded on the tenant ID, not only the record: the project read's catch leaves the id null,
    // and with no id there is nothing to have failed to resolve.
    expect(repositoryDetailSrc).toMatch(/tenantId !== null && tenantAccount === null/);
    // …and it reaches the strip through the source prop rather than being computed and dropped.
    expect(repositoryDetailSrc).toMatch(/source=\{\{[^}]*stagesUnknown/);
  });

  it('renders BOTH empty states from the copy table, not one hardcoded sentence', () => {
    // The strip previously carried the confident sentence inline. Reading it from the table is what
    // makes the two states distinguishable AND keeps the wording pinned by the tests above.
    expect(environmentStripSrc).toContain('ENVIRONMENT_EMPTY_COPY');
    expect(environmentStripSrc).toContain('environmentStripState');
    // The old inline claim must be gone, or the honest branch would sit beside the dishonest one.
    expect(environmentStripSrc).not.toContain('No environments are configured');
    // And the state — not `rows.length === 0` — is what selects the empty row, so the two cases
    // cannot collapse back into one branch.
    expect(environmentStripSrc).toMatch(/state !== 'rows'/);
  });

  it('asks whether the materialize SUCCEEDED, not whether it stopped (FR-2)', () => {
    // The bug: the header read the has-it-STOPPED predicate, and a failure HALTS the run — so a
    // FAILED materialize rendered "Complete" above its own rose failed step.
    expect(repositoryDetailSrc).toContain('materializeSummary');
    expect(repositoryDetailSrc).toContain('nextBadgeFromSteps');
    // The stopped-predicate must no longer be consulted here. Its semantics are correct and other
    // callers depend on them — the defect was using it for this question — so the guard is that
    // THIS file does not call it. Anchored to the call form, and the file's own comment describes
    // that function rather than naming it, which is what leaves this able to fail.
    expect(repositoryDetailSrc).not.toMatch(/isMaterializeTerminal\s*\(/);
    // …and neither header word may be written inline: both come from the pinned table.
    expect(repositoryDetailSrc).not.toContain("'Complete'");
    expect(repositoryDetailSrc).not.toContain("'In progress'");
  });

  it('scopes the materialize card’s heading to the RUN it describes (#13)', () => {
    // Observed live: the card's verdict read "Complete" on a page whose delivery pill read
    // "Delivery failed". Both were TRUE — materialize genuinely completed and the production
    // deploy is a different machine — but a bare noun beside a bare verdict reads as a claim about
    // the whole repository. COPY ONLY: the badge logic and the three-way state are untouched, and
    // the heading now names its own scope so the verdict cannot be read past it.
    expect(repositoryDetailSrc).toMatch(/Materialization run/);
    // The bare heading must be gone, or the scoped one would sit beside it. Anchored on the JSX
    // element rather than the word, so the comments explaining this (here and in the page) cannot
    // satisfy it — asserted directly below.
    const bareHeading = />\s*Materialization\s*</;
    expect(bareHeading.test(repositoryDetailSrc)).toBe(false);
    expect(bareHeading.test('// the Materialization card used to read "Complete"')).toBe(false);
    expect(bareHeading.test('<h3>Materialization</h3>')).toBe(true);
  });

  it('warns when a PRODUCTION deployment’s governance record could not be read', () => {
    // An absent alarm reads as "checked, nothing wrong". The page must distinguish "read it, it is
    // fine" from "could not read it" — so it records the agent read's failure and asks the
    // three-way question rather than the two-way one.
    expect(repositoryDetailSrc).toContain('setAgentError(true)');
    expect(repositoryDetailSrc).toContain('prodGovernanceState');
    expect(repositoryDetailSrc).toMatch(/prodGovernance === 'unknown'/);
    // The predicate that could only answer yes-or-no must not be the page's gate any more.
    expect(repositoryDetailSrc).not.toMatch(/ungovernedInProd\s*\(/);
    // Still READ-ONLY: the new banner states the gap and links out, exactly like the old one. The
    // no-governance-verb guard above covers the whole file, so this asserts only that it links.
    expect(repositoryDetailSrc).toContain('agent’s governance record');
  });

  it('derives the banner from a PRODUCTION verdict, never from the delivery status (#5)', () => {
    // THE FINDING'S FENCE. The delivery status is written for EVERY stage with no branch, so a
    // successful non-production build satisfied the old gate and the page claimed "Serving
    // production". The page must therefore obtain a production verdict from the join and hand THAT
    // to the composition.
    //
    // Anchored STRUCTURALLY — on the call form and on the property name inside the composition's
    // object literal — rather than on a bare substring, because five guards in this epic were
    // defeated by their own explanatory comment containing the string they searched for. The two
    // assertions below are asserted UNABLE to match prose at the end of this test.
    const servingCall = /prodServingState\s*\(\s*\{/;
    const governanceTakesServing = /prodGovernanceState\s*\(\s*\{[^)]*\bserving\s*[,:]/;
    expect(servingCall.test(repositoryDetailSrc)).toBe(true);
    expect(governanceTakesServing.test(repositoryDetailSrc)).toBe(true);
    // The join's inputs really do reach it — a verdict derived from nothing would be inert. All
    // three scalars and both history flags, because dropping any one silently collapses a state:
    // without the history the join has nothing to join TO, and without `historyLoading` the page
    // flashes an alarm for one paint.
    const servingStart = repositoryDetailSrc.search(servingCall);
    const servingBlock = repositoryDetailSrc.slice(servingStart, servingStart + 600);
    for (const field of [
      'last_promotion_build_id',
      'last_promoted_at',
      'last_promoted_image_tag',
      'deployments',
      'historyError',
      'historyLoading',
    ]) {
      expect(servingBlock, field).toContain(field);
    }
    // …and the delivery status must NOT be what the composition is handed. `cicdStatus:` as an
    // argument key is the exact shape of the defect; the page still reads `cicd_status` for the
    // delivery PILL, which is correct and untouched, so the guard names the argument and not the
    // field.
    expect(repositoryDetailSrc).not.toMatch(/prodGovernanceState\s*\(\s*\{[^)]*cicdStatus/);
    // PROOF THE ANCHORS CANNOT MATCH PROSE. A comment mentioning either name — including this
    // test's own explanation above, and the page's — must not satisfy them, or the guard is
    // decorative. This is the mechanical version of "anchor it structurally".
    for (const prose of [
      '// prodServingState answers whether production is serving',
      '// see prodGovernanceState, which takes the serving verdict',
      '* `prodServingState` and `prodGovernanceState` compose: serving, then governance.',
    ]) {
      expect(servingCall.test(prose), prose).toBe(false);
      expect(governanceTakesServing.test(prose), prose).toBe(false);
    }
  });

  it('states the slate banner’s two uncertainties as a DISJUNCTION, never as both at once', () => {
    // The copy is inline JSX, so this is the `ENVIRONMENT_EMPTY_COPY` discipline applied through the
    // raw source: that table is pinned in both directions precisely so a copy edit cannot put a
    // confident sentence back over an unreadable read, and it is the guard that would have caught
    // this. TWO INDEPENDENT causes reach `unknown` — the governance record was unreadable, OR what
    // production is serving is unestablished — and the banner must claim only the disjunction. The
    // shipped copy said "and": false for the case the banner was built for, where production IS
    // established as serving and only the governance record could not be read.
    //
    // Anchored on the JSX conditional, not on a phrase, so the page's own comment explaining this
    // cannot satisfy it — asserted at the end of this test.
    const slateBanner = /\{prodGovernance === 'unknown' && \(/;
    expect(slateBanner.test(repositoryDetailSrc)).toBe(true);
    const start = repositoryDetailSrc.search(slateBanner);
    const body = repositoryDetailSrc.slice(start, start + 900);
    // The window really holds the whole body, so every assertion below reads the copy rather than
    // an empty slice. Bounded from the anchor FORWARD, which is also what keeps the comment above
    // the anchor — the one that quotes the old false sentence — out of the reads below.
    expect(body).toContain('role="status"');
    expect(body).toMatch(/could not be confirmed/);
    expect(body).toContain('Deployments tab');
    // THE DISJUNCTION, explicitly worded. A reader must not have to infer that only one of the two
    // may hold.
    expect(body).toMatch(/Either[\s\S]{0,240}\bor\b/);
    // …and the conjunction must be gone, or the honest clause would sit beside the false one.
    expect(body).not.toMatch(/and what production is serving/);
    // Neither direction of definite claim survives: not the old "deployed to production" (untrue for
    // the unestablished-serving case) and not the amber banner's accusation, which this one exists
    // NOT to make.
    expect(body).not.toMatch(/deployed to production/);
    expect(body).not.toMatch(/serving production/i);
    // PROOF THE ANCHOR CANNOT MATCH PROSE. A comment naming the state — including the page's own —
    // must not satisfy it, or this guard is decorative.
    for (const prose of [
      "// when prodGovernance === 'unknown' the slate banner renders instead of the amber one",
      '* prodGovernance === \'unknown\' && the copy must state a disjunction',
    ]) {
      expect(slateBanner.test(prose), prose).toBe(false);
    }
    // …and it DOES match the real conditional, so the anchor is not merely unfalsifiable.
    expect(slateBanner.test("{prodGovernance === 'unknown' && (")).toBe(true);
  });

  it('renders the record status through the pinned label, not as a raw wire value', () => {
    // A third machine's `ready` beside delivery's and the runtime's, in its bare lowercase wire
    // form, is the same ambiguity `opsStatus.ts` already refused twice. The `ready` row of that
    // table is not written by any producer yet — the record's writers put `provisioning` and
    // `failed` in this field — so it is pre-placed for a later task's writer, and the label path is
    // what must already be in place when that writer lands.
    expect(repositoryDetailSrc).toMatch(/recordStatusLabel\s*\(\s*repo\.status\s*\)/);
    // The old raw render must be gone, or the honest branch would sit beside the raw one. Tolerant
    // of whitespace and of the usual stringify wrapper, because a bare substring check is evaded by
    // `{ repo.status }` and `{String(repo.status)}`.
    expect(repositoryDetailSrc).not.toMatch(
      /\{\s*(?:String\(\s*)?repo\.status(?:\s*\))?\s*(?:\|\|[^}]*)?\}/,
    );
  });

  it('keeps `orgLabel`’s documented id fallback instead of discarding it', () => {
    // `opsLabels.orgLabel` falls back to the raw connection id when the connection does not
    // RESOLVE — "the thing an operator can search for". This page's connections read is
    // best-effort (a deleted connection, or a 403 for a caller who may not list them), so passing
    // `null` there blanked the cell to an em dash over a project that does have an org.
    expect(repositoryDetailSrc).toContain('orgLabel(connection, connectionId)');
    expect(repositoryDetailSrc).not.toContain('orgLabel(connection, null)');
    // The id has to be HELD for the fallback to have anything to fall back to, and held from the
    // project record — which is where it lives — rather than from the connection that failed.
    expect(repositoryDetailSrc).toContain('setConnectionId(detail.project.connection_id');
  });

  it('gates Promote on the role helper and calls the ONE promote route', () => {
    expect(repositoryDetailSrc).toContain('projectsApi.promoteRepo');
    expect(repositoryDetailSrc).toContain('promotionActionMessage');
    // Gating PROMOTE on the project's `ungoverned` bit (D11's trap) would offer the button on
    // exactly the pre-migration projects where the STRICT owner gate 403s.
    //
    // The page now READS that bit — the retry route legitimately needs it, because its gate
    // carries the design-§3 fallback — so the guard moved from "the field is never read" to the
    // property that actually matters: only `headerActions` may combine it with a verb, and it
    // is pinned there (`RETRY gets the design-§3 ungoverned fallback; PROMOTE does not`) with
    // both directions asserted. So the assertion here is that the page does not re-derive a
    // promote gate of its own from it.
    expect(repositoryDetailSrc).not.toContain('canPromote');
    expect(repositoryDetailSrc).toContain('headerActions({');
  });

  it('carries the accessible tablist contract in ONE extracted component', () => {
    expect(tabStripSrc).toContain('role="tablist"');
    expect(tabStripSrc).toContain('role="tab"');
    expect(tabStripSrc).toContain('aria-selected');
    expect(tabStripSrc).toContain('aria-controls');
    expect(tabStripSrc).toContain('nextTabKey');
    // `aria-current="page"` is for navigation between PAGES — on a client-side tab it
    // announces a current page the URL contradicts (WCAG 4.1.2).
    expect(tabStripSrc).not.toContain('aria-current');
    // The page uses the extracted strip rather than open-coding a fourth copy.
    expect(repositoryDetailSrc).toContain('TabStrip');
    expect(repositoryDetailSrc).not.toContain('role="tablist"');
    // …and the panels are still labelled by their tab.
    expect(repositoryDetailSrc).toContain('aria-labelledby');
    expect(repositoryDetailSrc).toContain('tabPanelId(');
  });

  it('keeps the tab button class string byte-identical to ProjectDetail.tsx:344', () => {
    // Three surfaces, one visual system. The class string is the contract.
    const shared =
      'px-3.5 py-1.5 rounded-lg text-sm font-medium transition-colors whitespace-nowrap';
    expect(tabStripSrc).toContain(shared);
    expect(tabStripSrc).toContain('bg-white text-slate-900 shadow-sm');
    expect(tabStripSrc).toContain('text-slate-500 hover:text-slate-700');
    expect(tabStripSrc).toContain(
      'flex items-center gap-1 p-1 mb-4 bg-emerald-50/60 rounded-xl w-fit overflow-x-auto',
    );
  });

  it('mounts each ready tab body and stubs none of them', () => {
    // T12's three are now MOUNTED (their bodies live in `repo-tabs/`, and the panel wiring is
    // asserted below). The property this still guards is that the page never grows a STUB: a
    // placeholder body here would satisfy the registry's `ready` flag while opening onto nothing,
    // which is the exact failure the flag exists to prevent. So each name must appear as an IMPORT
    // from its own module, not as a local function declared in this file.
    for (const owned of ['DeploymentsTab', 'ResourcesTab', 'ObservabilityTab']) {
      // The EXPLICIT `.tsx` is required, not cosmetic: each component has a `.ts` companion whose
      // name differs only in casing, and on a case-insensitive filesystem an extensionless
      // specifier resolves to the companion — binding the import to a module with no default
      // export. `tsc` catches that today (TS1149), but pinning it here says why it must stay.
      expect(repositoryDetailSrc, owned).toContain(`from './repo-tabs/${owned}.tsx'`);
      expect(repositoryDetailSrc, owned).not.toContain(`function ${owned}(`);
    }
    // Pull requests (T14) obeys the SAME two rules: imported from its own module with the explicit
    // extension, never declared here. It is not in the loop above only because it also has a named
    // export the page imports from the companion (`prTabVisibility`), so the negative assertion has
    // to name the COMPONENT rather than the module path.
    expect(repositoryDetailSrc).toContain("from './repo-tabs/PullRequestsTab.tsx'");
    expect(repositoryDetailSrc).not.toContain('function PullRequestsTab(');
    // …and the companion likewise, whose name differs from the component only in casing — the exact
    // pair an extensionless specifier resolves the wrong way round on this filesystem.
    expect(repositoryDetailSrc).toContain("from './repo-tabs/pullRequestsTab.ts'");
  });

  it('reads the pull requests HERE, because that read is also the capability probe (T14/A3)', () => {
    // Inverted from T11's "does not call the PR routes yet": T14 mounted the four routes those
    // client calls target, so calling them is now correct. But the LIST call specifically belongs
    // to the page rather than to the tab, and that is a real ordering constraint, not a style
    // choice: its answer decides whether the Pull requests tab appears in the strip AT ALL, and a
    // tab body only mounts once its tab is selectable — a tab that fetched its own visibility could
    // never become visible, because nothing would mount to ask.
    expect(repositoryDetailSrc).toContain('pullRequestsApi.list');
    // The visibility DECISION stays in the companion `.ts` where a test reaches it, and is applied
    // as a SECOND filter beside `ready` — never by flipping `ready`, which is a build-time flag and
    // would make the registry lie per-org.
    expect(repositoryDetailSrc).toContain('prTabVisibility');
    expect(repositoryDetailSrc).toMatch(/prVisibility === 'visible'/);
    // The three WRITE verbs are the tab's, not the page's — a second copy of them here would be a
    // second path with its own error mapping.
    for (const verb of ['pullRequestsApi.create', 'pullRequestsApi.approve', 'pullRequestsApi.merge']) {
      expect(repositoryDetailSrc, verb).not.toContain(verb);
    }
  });

  it('never lets the capability refusal reach the page as an ERROR state', () => {
    // The requirement is a HIDDEN tab, not a broken one — so a failed PR read must not set the
    // PAGE's error/notFound state, which would replace the whole repository page with an error card
    // over a tab the operator may not even have wanted. It sets only the tab's visibility.
    //
    // THE SPAN MATTERS, and the first version of this guard got it wrong: a lazy match ending at
    // the first `setPrVisibility(` stopped inside the SUCCESS branch and never reached the catch, so
    // adding `setError(...)` to the failure branch left it green (verified by mutation). The span is
    // therefore anchored from the list read to the OUTER `.catch` that follows it — i.e. the whole
    // PR try/catch and nothing else. `setError` is legitimate in that outer handler (a repository
    // that genuinely failed to load IS a page error), which is exactly why the boundary has to be
    // exact rather than generous.
    const start = repositoryDetailSrc.search(/pullRequestsApi\s*\.\s*list\s*\(/);
    expect(start, 'the PR list read should be findable').toBeGreaterThan(-1);
    const rest = repositoryDetailSrc.slice(start);
    const end = rest.search(/\.catch\(/);
    expect(end, 'the outer .catch should follow the PR read').toBeGreaterThan(-1);
    const prBlock = rest.slice(0, end);
    // Guards against a vacuous span: the block really does contain BOTH branches.
    expect(prBlock).toContain("status: 'error'");
    expect(prBlock).toContain("status: 'ok'");
    expect(prBlock).not.toContain('setError(');
    expect(prBlock).not.toContain('setNotFound(');
  });

  it('renders the freed step timeline from `s.label`, not from re-derived stage names', () => {
    // Step LABELS are stage-neutral as of T6b and are rendered VERBATIM. Re-deriving them
    // would re-introduce the "Create dev environment" that lied for a uat-only tenant.
    expect(repositoryDetailSrc).toContain('{s.label}');
    // The timeline is really MOUNTED, so the assertion above is about something on screen. This
    // used to name the has-it-STOPPED predicate as the proxy for that; the page no longer calls it
    // (see the FR-2 guard above — a halted run is stopped, so it rendered a FAILED materialize as
    // "Complete"), so the proxy is now the loop that emits the steps.
    expect(repositoryDetailSrc).toContain('repo.steps.map(');
  });

  it('every clickable element it adds is keyboard-reachable (M-e, not inherited)', () => {
    // T10's row shipped a clickable `<tr>` with no role/tabIndex/key handler; T13 owns that
    // fix. Nothing here may repeat it, so this page uses real `<button>`/`<Link>` elements —
    // there is no `onClick` on a non-interactive tag.
    const clickableNonInteractive = /<(tr|div|span|td|li)\b[^>]*\sonClick/;
    expect(clickableNonInteractive.test(repositoryDetailSrc)).toBe(false);
    expect(clickableNonInteractive.test(environmentStripSrc)).toBe(false);
  });

  // -------------------------------------------------------------------------
  // The timeline's step COUNT moved, and its RENDERING must not have (E28B/T6, item 3).
  // -------------------------------------------------------------------------

  it('renders the timeline from `label` and uses `key` only as a list key', () => {
    // THE VERIFIED CONSTRAINT THIS TASK MAY NOT BREAK. `MATERIALIZE_STEPS` went from eight keys to
    // five, and five of the old ones were DELETED rather than renamed — but historical and in-flight
    // records still carry them. Both timelines render `steps[].label` VERBATIM, so an old record
    // keeps rendering its own stored labels; an unknown key must never crash or blank a row.
    expect(repositoryDetailSrc).toContain('{s.label}');
    expect(repositoryDetailSrc).toMatch(/key=\{s\.key\}/);
  });

  it('introduces NO key→label map — an unknown step key must degrade to its stored label', () => {
    // Explicitly forbidden by the backend's own migration note. A map is the one change that would
    // turn a historical record's row into a blank or a crash, and it is the tempting change the
    // moment the keys are renamed. Swept over EVERY non-test source file rather than this page only,
    // because the map could be added anywhere and imported here.
    //
    // Both key sets are searched: a map keyed by the NEW names is just as forbidden, and it is the
    // one someone would actually write.
    //
    // THE SETS ARE CORRECTED FROM A REVIEW FINDING. The first version listed `create_branch` — which
    // was never one of the eight (it was folded into `commit_config`'s body by E28A/T3, precisely to
    // avoid adding a ninth key) — and OMITTED `create_repo`, so a map keyed solely on the new
    // `create_repo` would have passed. Verified against `models/repository.py`: the current five are
    // `mint_identity, create_repo, push_template, set_repo_vars, finalize`, and the superseded ones
    // are `generate_repo, commit_config, create_env_dev, create_env_prod, set_env_vars`.
    //
    // `finalize` is in NEITHER list, deliberately: it survived the rename AND is a common English
    // word this codebase uses for unrelated things (`connectionsApi.finalize`, `_finalize_repo` in six
    // files). Including it would make this sweep fail on code that has nothing to do with step labels
    // — a guard that cries wolf gets deleted, so it is left to the four distinctive new keys.
    const SUPERSEDED_KEYS = [
      'generate_repo',
      'commit_config',
      'create_env_dev',
      'create_env_prod',
      'set_env_vars',
    ];
    const CURRENT_KEYS = ['mint_identity', 'create_repo', 'push_template', 'set_repo_vars'];
    // `../../**` reaches all of `src/`, not just `src/components/` — the map could be added in
    // `api/client.ts` beside the `StepState` type it would key, and `../**` does not see that file.
    // The same narrow glob left the step-count guard blind to two of its three real cases.
    const sources = Object.entries(
      import.meta.glob<string>('../../**/*.{ts,tsx}', { query: '?raw', import: 'default', eager: true }),
    ).filter(([path]) => !path.endsWith('.test.ts') && !path.endsWith('.test.tsx'));
    // The sweep really read something, and specifically reached the client — an empty or narrowed
    // glob would make every assertion below vacuous while staying green.
    expect(sources.length).toBeGreaterThan(20);
    expect(sources.map(([p]) => p).filter((p) => p.endsWith('api/client.ts'))).toHaveLength(1);
    for (const [path, src] of sources) {
      for (const key of [...SUPERSEDED_KEYS, ...CURRENT_KEYS]) {
        expect(src, `${path} mentions the step key ${key}`).not.toContain(key);
      }
    }
    // THE KEY SET MATCHES THE BACKEND. Asserted as a count so a key silently dropped from either list
    // — the review finding, where `create_repo` was missing and a map keyed on it would have passed —
    // fails here rather than opening a hole.
    expect(SUPERSEDED_KEYS).toHaveLength(5);
    expect(CURRENT_KEYS).toHaveLength(4);
    // …and the four current ones really are the distinctive members of `MATERIALIZE_STEPS`, minus
    // `finalize` for the collision reason above.
    expect(CURRENT_KEYS).toContain('create_repo');
    expect([...SUPERSEDED_KEYS, ...CURRENT_KEYS]).not.toContain('create_branch');
  });

  it('claims no stale step COUNT anywhere in the shipped frontend', () => {
    // Materialize has FIVE steps now. Three strings said eight — the modal's prose, which an
    // operator reads while watching the timeline, and two client comments. A promised total the rows
    // will never reach invites the operator to wait for steps that do not exist.
    //
    // Swept file-wide rather than asserted at three line numbers, because a fourth copy is exactly
    // how this went stale: the count is a fact about the backend, and any restatement of it here can
    // rot. Comments are INCLUDED in the sweep on purpose — two of the three stale strings WERE
    // comments, so a stripper here would exempt the majority of the defect.
    //
    // THE GLOB REACHES `src/`, NOT `src/components/`. `../**` from this directory covers only
    // `components/**`, which EXCLUDES `api/client.ts` — where two of the three stale strings actually
    // lived. A mutation proved it: restoring "The 8 background steps advance" in `client.ts` left this
    // suite fully green, i.e. the guard did not cover the majority of the defect it was written for.
    const sources = Object.entries(
      import.meta.glob<string>('../../**/*.{ts,tsx}', { query: '?raw', import: 'default', eager: true }),
    ).filter(([path]) => !path.endsWith('.test.ts') && !path.endsWith('.test.tsx'));
    // The sweep really reached the client — asserted by NAME, because a glob that silently stopped
    // matching it would restore the hole above without failing anything.
    expect(sources.map(([p]) => p).filter((p) => p.endsWith('api/client.ts'))).toHaveLength(1);
    // Every way the old count was written, plus the digit forms a fix would reach for. `8 steps`
    // and `8-step` covered two of the three real cases.
    const stale = /\b(eight|8)[\s-]+(background\s+|side-effecting\s+|sequential\s+|materialize\s+)?steps?\b/i;
    for (const [path, src] of sources) {
      expect(stale.test(src), `${path} still claims eight steps`).toBe(false);
    }
    // AND the guard is proven able to fail — the exact three strings that shipped, so this cannot
    // pass because the regex stopped matching.
    expect(stale.test('this runs eight steps in the background')).toBe(true);
    expect(stale.test('The 8 background steps advance')).toBe(true);
    expect(stale.test('the 8 side-effecting steps then advance')).toBe(true);
    expect(stale.test('The 8-step timeline was visible')).toBe(true);
  });

  it('leaves the summary counting the record’s OWN steps, never a literal', () => {
    // `materializeSummary` takes a `stepCount` and special-cases only 0, so a record with eight
    // stored steps and one with five both summarize correctly. A total hardcoded anywhere would make
    // the count a contract again — the thing that just broke.
    expect(repositoryDetailSrc).toContain('repo.steps.length');
    expect(materializeSummary('ready', 8)).toEqual({ label: 'Complete', succeeded: true });
    expect(materializeSummary('ready', 5)).toEqual({ label: 'Complete', succeeded: true });
    // An unrecognized count is not a special case at all — only 0 is.
    expect(materializeSummary('provisioning', 13).label).toBe('In progress');
  });

  it('contains no AWS account id (a hard project rule), including in the strip', () => {
    // A bare 12-digit run. `000000000000` in a TEST fixture is obviously-fake and permitted;
    // production source must carry none at all.
    const twelveDigits = /(?<!\d)\d{12}(?!\d)/;
    for (const [name, src] of [
      ['RepositoryDetail.tsx', repositoryDetailSrc],
      ['EnvironmentStrip.tsx', environmentStripSrc],
      ['TabStrip.tsx', tabStripSrc],
      ['repositoryDetailTabs.ts', pureSrc],
    ] as const) {
      expect(twelveDigits.test(src), `${name} contains a 12-digit literal`).toBe(false);
    }
  });
});

// ---------------------------------------------------------------------------
// runtimeStatusTitle — the probe hint nothing rendered (E29/T11, OB-15)
// ---------------------------------------------------------------------------

describe('runtimeStatusTitle', () => {
  it('returns the detail alone when there is no scope note', () => {
    // The fleet row's case: no per-stage claim to qualify, so the hint is the whole tooltip.
    expect(runtimeStatusTitle({ detail: 'runtime is deleting' })).toBe('runtime is deleting');
  });

  it('returns the scope note alone when the reading carries no detail', () => {
    // The pre-T11 behaviour, preserved exactly: a reading with nothing to add must not gain a
    // dangling separator or lose the sentence it already had.
    expect(runtimeStatusTitle({ detail: null }, RUNTIME_SCOPE_NOTE)).toBe(RUNTIME_SCOPE_NOTE);
    expect(runtimeStatusTitle(undefined, RUNTIME_SCOPE_NOTE)).toBe(RUNTIME_SCOPE_NOTE);
  });

  it('keeps the two claims SEPARATED when both are present', () => {
    // They are different claims — what the reading can be attributed to, and what the probe
    // found. Run together they would read as one sentence asserting more than either does.
    const t = runtimeStatusTitle({ detail: 'runtime is deleting' }, RUNTIME_SCOPE_NOTE);
    expect(t).toContain(RUNTIME_SCOPE_NOTE);
    expect(t).toContain('runtime is deleting');
    expect(t).toBe(`${RUNTIME_SCOPE_NOTE} — runtime is deleting`);
  });

  it('returns null — NOT an empty string — when there is nothing to say', () => {
    // `title=""` is a hover target that says nothing, which is a worse affordance than no
    // attribute. Every caller spells this `?? undefined`, so null is what makes the attribute
    // disappear rather than render empty.
    expect(runtimeStatusTitle(undefined)).toBeNull();
    expect(runtimeStatusTitle(null)).toBeNull();
    expect(runtimeStatusTitle({ detail: null })).toBeNull();
    expect(runtimeStatusTitle({ detail: undefined }, null)).toBeNull();
  });

  it('treats a blank detail as absence, not as data', () => {
    // Same `text()` rule the rest of this module applies: a whitespace-only hint is a record
    // artifact, and rendering it would produce a tooltip containing a space.
    expect(runtimeStatusTitle({ detail: '   ' })).toBeNull();
    expect(runtimeStatusTitle({ detail: '' }, RUNTIME_SCOPE_NOTE)).toBe(RUNTIME_SCOPE_NOTE);
    expect(runtimeStatusTitle({ detail: '\n' })).toBeNull();
  });

  it('surfaces the T10 distinction the status field CANNOT express', () => {
    // The reason OB-15 is a defect rather than a cosmetic gap. Both of these produce the
    // identical "Runtime unknown" pill, and they instruct an operator to look in opposite
    // places: one says the platform answered about a real runtime state, the other says nothing
    // was established at all. Only `detail` separates them, so only rendering it closes T10's
    // honesty work.
    const recognizedState = runtimeStatusTitle({ detail: 'runtime is deleting' });
    const probeNeverRan = runtimeStatusTitle({
      detail: 'runtime status could not be read (no Databricks reader is configured)',
    });
    expect(recognizedState).not.toBe(probeNeverRan);
    expect(recognizedState).not.toBeNull();
    expect(probeNeverRan).not.toBeNull();
  });

  it('never invents a hint the backend did not send', () => {
    // A default sentence here would be a claim about a probe nobody made — the same fabrication
    // `runtimeScope` and `stageRuntimeCell` exist to prevent, one field over. Absence stays
    // absent.
    expect(runtimeStatusTitle({})).toBeNull();
  });
});

// The wiring guard for OB-15. `runtimeStatusTitle` is only worth anything if it is actually CALLED
// — the defect it fixes was precisely a correct backend field that no render site read, so a pure
// function nobody invokes would reproduce the bug with more code. Comment-free source, per the
// `stripDetailComments` rule above (this project has shipped nine guards its own prose satisfied),
// which is why the assertions below name the call form rather than quoting it in a comment.
describe('OB-15 — the probe hint reaches every surface that renders a runtime pill', () => {
  it('parses all three sources (guards the assertions below against an empty read)', () => {
    for (const [name, src] of [
      ['RepositoryDetail', repositoryDetailSrc],
      ['EnvironmentStrip', environmentStripSrc],
      ['RepoRow', repoRowTsxSrc],
    ] as const) {
      expect(src.length, name).toBeGreaterThan(1000);
    }
  });

  it('is called by all three runtime-pill render sites', () => {
    for (const [name, src] of [
      ['RepositoryDetail', repositoryDetailSrc],
      ['EnvironmentStrip', environmentStripSrc],
      ['RepoRow', repoRowTsxSrc],
    ] as const) {
      expect(stripDetailComments(src), name).toContain('runtimeStatusTitle(');
    }
  });

  it('no site renders the raw detail field as a status label', () => {
    // The hint is a tooltip and may be absent; a site that read it as the pill's WORD would print
    // an empty pill, or a hint where a status belongs. The labels stay table-driven.
    for (const [name, src] of [
      ['RepositoryDetail', repositoryDetailSrc],
      ['EnvironmentStrip', environmentStripSrc],
      ['RepoRow', repoRowTsxSrc],
    ] as const) {
      const stripped = stripDetailComments(src);
      expect(stripped, name).not.toContain('{runtime?.detail}');
      expect(stripped, name).not.toContain('{runtime.detail}');
    }
  });
});
