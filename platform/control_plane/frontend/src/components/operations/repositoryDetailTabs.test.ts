import { describe, expect, expectTypeOf, it } from 'vitest';

import { ApiFailure, type ErrorCode } from '../../api/failure';
import {
  ACTOR_KIND_TITLE,
  UNKNOWN_ENTRA_USER,
  actorText,
  actorTitle,
  entraActorName,
  CONFIG_RUNTIME_NOTE,
  DELIVERY_BUILDING_LABEL,
  BUILD_NOW_STUCK_HINT,
  ENVIRONMENT_EMPTY_COPY,
  RECORD_STATUS_LABEL,
  REPOSITORY_DETAIL_TABS,
  RUNTIME_SCOPE_NOTE,
  BUILD_NOW_STARTED_NOTICE,
  BUILD_REFUSAL_CODES,
  buildNowStartedNotice,
  deliveryFailureReason,
  agentRuntimeLabel,
  buildNowActionMessage,
  canBuildNow,
  classifyBuildFailure,
  collapseByBuild,
  deliveryDeployingLabel,
  deliveryHeaderState,
  environmentRows,
  environmentStripState,
  headerActions,
  GATEWAY_DEPLOYED_LABEL,
  GATEWAY_NOT_BUILT_LABEL,
  gatewayStatus,
  repoArtifactLink,
  repoKindOf,
  MCP_KIND_CHIP_LABEL,
  offersRollback,
  repoKindChip,
  rowActionForKind,
  tabsForKind,
  initialTabKey,
  isTabSelectable,
  materializeSummary,
  prodGovernanceState,
  prodServingState,
  promotionArtifact,
  recordStatusLabel,
  representativeImageTag,
  headerName,
  probeStages,
  repoBackLink,
  BACK_LABEL_PENDING,
  runtimeScope,
  runtimeStatusTitle,
  selectableTabKeys,
  settledFrom,
  shouldPollRepo,
  sortedStageNames,
  stageRuntimeCell,
  stageRuntimeDisplay,
  tabId,
  tabPanelId,
  ungovernedInProd,
  type EnvironmentRowSource,
  type ProdServingState,
  type BuildRefusalKind,
} from './repositoryDetailTabs';
import { CICD_LABEL, type CicdStatus } from './opsStatus';
import { materializeDialogCopy } from './templateChain.ts';
import { nextTabKey } from './projectDetailTabs';
import { PROMOTION_TAG_ONLY_LABEL, PROMOTION_TAG_ONLY_NOTE } from './repoRowModel';
import { ARTIFACT_TONE_CLS } from './promoteConfirmModel';
import { nextBadgeFromSteps } from './materializeSteps';
import { stripComments } from '../../rawSource';
import { deployment, stepState, tenantStageConfig } from '../../test/wire';
import pureRaw from './repositoryDetailTabs.ts?raw';
import projectDetailRaw from './ProjectDetail.tsx?raw';
import clientRaw from '../../api/client.ts?raw';
import type { RuntimeStatus } from '../../api/openapi';
import ownRaw from './repositoryDetailTabs.test.ts?raw';
import repositoryDetailRaw from './RepositoryDetail.tsx?raw';
import overviewTabRaw from './repo-tabs/OverviewTab.tsx?raw';
import accessTabRaw from './repo-tabs/AccessTab.tsx?raw';
import repoSummaryCardRaw from './RepoSummaryCard.tsx?raw';
import repoFieldsRaw from './RepoFields.tsx?raw';
import useRepositoryDetailRaw from './useRepositoryDetail.ts?raw';
import environmentStripRaw from './EnvironmentStrip.tsx?raw';
import repoRowTsxRaw from './RepoRow.tsx?raw';
import addRepoModalRaw from './AddRepoModal.tsx?raw';
import opsPageRaw from './OpsPage.tsx?raw';
import opsLabelsRaw from './opsLabels.ts?raw';
import { PAGE_PATHS } from '../../routes';
import { OPS_BACK_LABEL } from './opsLabels';
import promoteConfirmRaw from './PromoteConfirm.tsx?raw';
import tabStripRaw from './TabStrip.tsx?raw';

const pureSrc = stripComments(pureRaw);
const projectDetailSrc = stripComments(projectDetailRaw);
const clientSrc = stripComments(clientRaw);
const ownSrc = stripComments(ownRaw);
const repositoryDetailSrc = [
  repositoryDetailRaw,
  overviewTabRaw,
  accessTabRaw,
  repoSummaryCardRaw,
  repoFieldsRaw,
  useRepositoryDetailRaw,
].map(stripComments).join('\n');
const environmentStripSrc = stripComments(environmentStripRaw);
const repoRowTsxSrc = stripComments(repoRowTsxRaw);
const addRepoModalSrc = stripComments(addRepoModalRaw);
const opsPageSrc = stripComments(opsPageRaw);
const opsLabelsSrc = stripComments(opsLabelsRaw);
const promoteConfirmSrc = stripComments(promoteConfirmRaw);
const tabStripSrc = stripComments(tabStripRaw);
const TREE_RAW = Object.entries(
  import.meta.glob<string>('../../**/*.{ts,tsx}', { query: '?raw', import: 'default', eager: true }),
).filter(([path]) => !path.endsWith('.test.ts') && !path.endsWith('.test.tsx'));
const TREE = TREE_RAW.map(([path, raw]) => [path, stripComments(raw)] as const);
export const NON_TEST_SOURCE_FLOOR = 20;

describe('REPOSITORY_DETAIL_TABS — the registry', () => {
  it('is non-empty (guards every assertion below against a vacuous pass)', () => {
    expect(REPOSITORY_DETAIL_TABS.length).toBeGreaterThan(0);
  });

  it('registers EXACTLY the seven pinned tabs, in the pinned order', () => {
    expect(REPOSITORY_DETAIL_TABS.map((t) => t.key)).toEqual([
      'overview',
      'deployments',
      'pull-requests',
      'access',
      'observability',
      'evaluations',
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
      'Evaluations',
      'Resources',
    ]);
  });

  it('every key is a unique slug', () => {
    const keys = REPOSITORY_DETAIL_TABS.map((t) => t.key);
    for (const k of keys) expect(k, k).toMatch(/^[a-z][a-z0-9-]*$/);
    expect(new Set(keys).size).toBe(keys.length);
  });
});

describe('tab visibility — a registered tab is not automatically a selectable one', () => {
  it('only the tabs whose body EXISTS are selectable', () => {
    expect(selectableTabKeys()).toEqual([
      'overview',
      'deployments',
      'pull-requests',
      'access',
      'observability',
      'evaluations',
      'resources',
    ]);
  });

  it('ready is a build-time flag answering whether a body exists; whether an org can serve a tab is a second filter, never ready', () => {
    expect(REPOSITORY_DETAIL_TABS.filter((t) => !t.ready)).toEqual([]);
    expect(isTabSelectable('no-such-tab')).toBe(false);
  });

  it('every tab carries an explicit `ready` flag — never an implicit default', () => {
    for (const t of REPOSITORY_DETAIL_TABS) expect(typeof t.ready, t.key).toBe('boolean');
  });

  it('an unknown key is not selectable', () => {
    expect(isTabSelectable('no-such-tab')).toBe(false);
  });

  it('the default tab is a selectable one', () => {
    expect(selectableTabKeys()[0]).toBe('overview');
    expect(isTabSelectable('overview')).toBe(true);
  });

  it('a `?tab=` deep link opens that tab when it is selectable, else the default', () => {
    expect(initialTabKey('?tab=observability')).toBe('observability');
    expect(initialTabKey('?x=1&tab=evaluations')).toBe('evaluations');
    expect(initialTabKey('?tab=no-such-tab')).toBe('overview');
    expect(initialTabKey('?tab=observability', 'mcp_server')).toBe('overview');
    expect(initialTabKey('?tab=')).toBe('overview');
    expect(initialTabKey('')).toBe('overview');
  });

  it('the keyboard model walks the RENDERED tabs, in registry order', () => {
    const keys = selectableTabKeys();
    expect(nextTabKey(keys, 'overview', 'ArrowRight')).toBe('deployments');
    expect(nextTabKey(keys, 'deployments', 'ArrowRight')).toBe('pull-requests');
    expect(nextTabKey(keys, 'resources', 'ArrowRight')).toBe('overview');
    expect(nextTabKey(keys, 'overview', 'ArrowLeft')).toBe('resources');
    expect(nextTabKey(keys, 'overview', 'End')).toBe('resources');
    expect(nextTabKey(keys, 'resources', 'Home')).toBe('overview');
    expect(nextTabKey(keys, 'overview', 'Enter')).toBeNull();
    for (const from of keys) {
      for (const pressed of ['ArrowRight', 'ArrowLeft', 'Home', 'End']) {
        const next = nextTabKey(keys, from, pressed);
        expect(next && isTabSelectable(next), `${from}/${pressed}`).toBe(true);
      }
    }
  });

  it('the keyboard model steps over a tab HIDDEN AT RUNTIME, not merely a bodyless one', () => {
    const rendered = selectableTabKeys().filter((k) => k !== 'pull-requests');
    expect(rendered).toEqual(['overview', 'deployments', 'access', 'observability', 'evaluations', 'resources']);
    expect(nextTabKey(rendered, 'deployments', 'ArrowRight')).toBe('access');
    expect(nextTabKey(rendered, 'access', 'ArrowLeft')).toBe('deployments');
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
    expect(tabId('access')).not.toBe('project-tab-access');
    expect(tabId('access')).not.toBe('settings-tab-access');
  });
});

describe('headerName — the Agent/Project header cells never flash a UUID', () => {
  it('shows the NAME once its read has returned one', () => {
    expect(headerName('Claims bot', true, 'a-uuid')).toBe('Claims bot');
    expect(headerName('Claims bot', false, 'a-uuid')).toBe('Claims bot');
  });

  it('returns null (render a placeholder) while the read is still pending — not the id', () => {
    for (const missing of [null, undefined, '', '  ']) {
      expect(headerName(missing, false, 'a-uuid'), String(missing)).toBeNull();
    }
  });

  it('falls back to the id only once the read settled without a name — a true reference', () => {
    expect(headerName(null, true, 'a-uuid')).toBe('a-uuid');
    expect(headerName('   ', true, 'a-uuid')).toBe('a-uuid');
  });

  it('the page renders both cells through it', () => {
    expect(repositoryDetailSrc).not.toContain('agent?.name ?? repo.agent_id');
    expect(repositoryDetailSrc).not.toContain('projectName ?? repo.project_id');
    expect(repositoryDetailSrc.match(/headerName\(/g)?.length).toBe(2);
  });
});

describe('repoBackLink — a repository returns to the project that owns it', () => {
  it('points at the parent PROJECT, never at the fleet list', () => {
    const link = repoBackLink({ projectId: 'proj-7', projectName: 'Claims' });
    expect(link.to).toBe('/ops/projects/proj-7');
    expect(link.to).not.toContain('/ops/repositories');
  });

  it('labels the link with the project NAME, so it reads as a place', () => {
    expect(repoBackLink({ projectId: 'proj-7', projectName: 'Claims' }).label).toBe('← Claims');
  });

  it('falls back to the project id — a true reference beats a vague word', () => {
    for (const missing of [null, undefined, '', '   ']) {
      const link = repoBackLink({ projectId: 'proj-7', projectName: missing });
      expect(link.label, String(missing)).toBe('← proj-7');
    }
  });

  it('never shows the raw project id while the project read is still pending', () => {
    for (const missing of [null, undefined, '']) {
      const { label } = repoBackLink({ projectId: 'proj-7', projectName: missing, settled: false });
      expect(label, String(missing)).toBe(`← ${BACK_LABEL_PENDING}`);
      expect(label).not.toContain('proj-7');
    }
    expect(repoBackLink({ projectId: 'proj-7', projectName: 'Claims', settled: false }).label).toBe('← Claims');
    expect(repoBackLink({ projectId: 'proj-7', projectName: null, settled: true }).label).toBe('← proj-7');
    expect(repositoryDetailSrc).toContain('settled: projectRead');
  });

  it('always leads with the arrow, and never renders a bare arrow', () => {
    for (const name of ['Claims', null]) {
      const { label } = repoBackLink({ projectId: 'proj-7', projectName: name });
      expect(label.startsWith('← ')).toBe(true);
      expect(label.trim().length).toBeGreaterThan(2);
    }
  });

  it('names a route the router actually serves', () => {
    expect(PAGE_PATHS).toContain('/ops/projects/:id');
    const link = repoBackLink({ projectId: 'proj-7' });
    expect(link.to).toBe('/ops/projects/:id'.replace(':id', 'proj-7'));
  });
});

function tenant(...names: string[]): EnvironmentRowSource['stages'] {
  return Object.fromEntries(
    names.map((n) => [
      n,
      tenantStageConfig({
        ecr_repo_uri: `example.invalid/${n}`,
        push_role_arn: `arn:aws:iam::000000000000:role/push-${n}`,
        deploy_role_arn: `arn:aws:iam::000000000000:role/deploy-${n}`,
      }),
    ]),
  );
}

function runtimeStatus(overrides: Partial<RuntimeStatus> = {}): RuntimeStatus {
  return {
    agent_id: 'agent-1',
    checked_at: '2026-07-31T10:00:00Z',
    detail: null,
    image_tag: null,
    runtime_arn: null,
    stage: 'uat',
    status: 'ready',
    ...overrides,
  };
}

describe('settledFrom — a dependent read is settled once it answered or carries data', () => {
  it('loading with no carried data is not settled, so the header shows its skeleton instead of the project id during the first read', () => {
    expect(settledFrom({ kind: 'loading', data: null })).toBe(false);
    expect(settledFrom({ kind: 'loading', data: { name: 'carried' } })).toBe(true);
    expect(settledFrom({ kind: 'ready', data: { name: 'Claims' } })).toBe(true);
    expect(settledFrom({ kind: 'error', message: 'boom', failure: null, data: null })).toBe(true);
    expect(headerName(null, settledFrom({ kind: 'loading', data: null }), 'a-uuid')).toBeNull();
  });

  it('the page resolves null, never a placeholder object, for a dependent read whose input is absent, so data === null means never fetched', () => {
    expect(repositoryDetailSrc).toMatch(/: Promise\.resolve<ProjectRead \| null>\(null\)/);
    expect(repositoryDetailSrc).toMatch(/: Promise\.resolve<PullRequestView\[\] \| null>\(null\)/);
    expect(repositoryDetailSrc).toContain('const projectRead = settledFrom(projectLoad.state);');
  });
});

describe('probeStages — one probe per stage, each failure its own', () => {
  it('one failed probe leaves only its stage undefined, so an unreachable stage cannot erase the strip', async () => {
    const byStage = await probeStages(['sandbox', 'uat', 'live'], (stage) =>
      stage === 'uat' ? Promise.reject(new Error('unreachable')) : Promise.resolve(runtimeStatus({ stage })),
    );
    expect(Object.keys(byStage)).toEqual(['sandbox', 'uat', 'live']);
    expect(byStage.uat).toBeUndefined();
    expect(byStage.sandbox?.stage).toBe('sandbox');
    expect(byStage.live?.stage).toBe('live');
  });

  it('a stage name containing a comma is probed as itself', async () => {
    const probed: string[] = [];
    const byStage = await probeStages(['qa,uat', 'uat'], (stage) => {
      probed.push(stage);
      return Promise.resolve(runtimeStatus({ stage }));
    });
    expect(probed).toEqual(['qa,uat', 'uat']);
    expect(Object.keys(byStage)).toEqual(['qa,uat', 'uat']);
    expect(byStage['qa,uat']?.stage).toBe('qa,uat');
  });
});

describe('environmentRows — one row per stage the API returns, never a hardcoded pair', () => {
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
    expect(environmentRows({ stages: {}, deployments: [] })).toEqual([]);
  });

  it('an unresolved tenant (null stages) renders no rows, not a default pair', () => {
    expect(environmentRows({ stages: null, deployments: [] })).toEqual([]);
  });

  it('a stage the API does not return does not render, even with deployments for it', () => {
    const rows = environmentRows({
      stages: tenant('uat'),
      deployments: [
        deployment({ stage: 'retired', image_tag: 'a-1', started_at: '2026-07-31T10:00:00Z' }),
      ],
    });
    expect(rows.map((r) => r.stage)).toEqual(['uat']);
  });

  it('sorts stages alphabetically — never by a hardcoded stage order', () => {
    const [devish, prodish] = ['d' + 'ev', 'p' + 'rod'];
    expect(sortedStageNames(tenant(prodish, devish, 'alpha'))).toEqual([
      'alpha',
      devish,
      prodish,
    ]);
    expect(sortedStageNames(tenant(prodish, 'edge', devish))).toEqual([devish, 'edge', prodish]);
    expect(sortedStageNames(tenant('b', 'a'))).toEqual(['a', 'b']);
    expect(sortedStageNames({})).toEqual([]);
  });
});

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
      now: Date.parse('2026-07-31T10:05:00Z'),
    });
    expect(rows[0].imageTag).toBe('a-1-good');
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
    const rows = environmentRows({
      stages: tenant('a', 'b'),
      deployments: [deployment({ stage: 'a', image_tag: 'only' })],
    });
    const b = rows.find((r) => r.stage === 'b')!;
    expect(b.imageTag).toBeNull();
    expect(b.drift).toBeNull();
  });
});

describe('collapseByBuild — a started row past the 60-min bound, or dated ahead, is not in flight', () => {
  const START = '2026-07-31T10:00:00Z';
  const at = (m: number) => Date.parse(START) + m * 60_000;
  const orphan = () => [deployment({ outcome: 'started', build_id: 'b:lost', started_at: START })];

  it('keeps a started row in flight inside the window', () => {
    expect(collapseByBuild(orphan(), at(59))[0].inFlight).toBe(true);
    expect(collapseByBuild(orphan(), at(0))[0].inFlight).toBe(true);
  });

  it('retires it at the backend bound (>= 60 min, backend uses `< window`)', () => {
    const [a] = collapseByBuild(orphan(), at(60));
    expect(a.inFlight).toBe(false);
    expect(a.outcome).toBe('started');
    expect(collapseByBuild(orphan(), at(240))[0].inFlight).toBe(false);
  });

  it('a start dated in the FUTURE is not in flight (the backend treats it as stale)', () => {
    expect(collapseByBuild(orphan(), at(-1))[0].inFlight).toBe(false);
  });

  it('an unparseable start stays in flight — retiring it on a guess could hide a live deploy', () => {
    const rows = [deployment({ outcome: 'started', build_id: 'b:x', started_at: 'nope' })];
    expect(collapseByBuild(rows, at(500))[0].inFlight).toBe(true);
  });

  it('applies no window when no clock is passed: the history list reads every started row without a terminal partner as in flight', () => {
    expect(collapseByBuild(orphan())[0].inFlight).toBe(true);
    expect(collapseByBuild(orphan(), undefined)[0].inFlight).toBe(true);
  });

  it('environmentRows threads `now`, so the strip and Build now unlock together', () => {
    const src = { stages: tenant('uat'), deployments: orphan() };
    expect(environmentRows({ ...src, now: at(30) })[0].inFlight).toBe(true);
    expect(environmentRows({ ...src, now: at(61) })[0].inFlight).toBe(false);
  });
});

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
    const attempts = collapseByBuild([
      deployment({ id: 'dep-11111111', outcome: 'started', build_id: null }),
      deployment({ id: 'dep-22222222', outcome: 'succeeded', build_id: null }),
    ]);
    expect(attempts).toHaveLength(2);
    expect(attempts.some((a) => a.inFlight)).toBe(true);
  });

  it('a terminal row with NO started partner is KEPT, never dropped', () => {
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
      now: Date.parse('2026-07-31T12:05:00Z'),
    });
    expect(rows[0].inFlight).toBe(true);
    expect(rows[0].imageTag).toBe('a-1-v1');
  });
});

describe('environmentRows — an unreadable history is unknown, not empty', () => {
  it('reports historyUnknown rather than the positive claim "never deployed"', () => {
    const rows = environmentRows({ stages: tenant('uat'), deployments: [], historyError: true });
    expect(rows).toHaveLength(1);
    expect(rows[0].historyUnknown).toBe(true);
  });

  it('derives NOTHING from the history when it could not be read', () => {
    const rows = environmentRows({
      stages: tenant('uat'),
      deployments: [deployment({ outcome: 'started', build_id: 'b:1' })],
      historyError: true,
    });
    expect(rows[0].imageTag).toBeNull();
    expect(rows[0].actor).toBeNull();
    expect(rows[0].shortSha).toBeNull();
    expect(rows[0].drift).toBeNull();
    expect(rows[0].inFlight).toBe(false);
  });

  it('a genuinely EMPTY history is still reported as empty, not as unknown', () => {
    const rows = environmentRows({ stages: tenant('uat'), deployments: [], historyError: false });
    expect(rows[0].historyUnknown).toBe(false);
    expect(rows[0].imageTag).toBeNull();
  });

  it('defaults to KNOWN when the caller omits the flag', () => {
    const rows = environmentRows({ stages: tenant('uat'), deployments: [] });
    expect(rows[0].historyUnknown).toBe(false);
  });
});

describe('ACTOR_KIND_TITLE — every kind has its own label, and none guesses', () => {
  it('covers all three kinds', () => {
    expect(Object.keys(ACTOR_KIND_TITLE).sort()).toEqual(['entra', 'github', 'unknown']);
  });

  it('does not describe an unlabelled actor as a provider login, nor as an unknown user: the actor is known, its currency is not', () => {
    expect(ACTOR_KIND_TITLE.unknown).not.toBe(ACTOR_KIND_TITLE.github);
    expect(ACTOR_KIND_TITLE.unknown).not.toMatch(/unknown user/i);
    expect(ACTOR_KIND_TITLE.unknown).not.toMatch(/^GitHub login$/);
    expect(ACTOR_KIND_TITLE.github).toMatch(/github/i);
    expect(ACTOR_KIND_TITLE.entra).toMatch(/entra/i);
  });

  it('every kind `actorOf` can produce has a label', () => {
    for (const kind of ['github', 'entra', 'unknown'] as const) {
      expect(ACTOR_KIND_TITLE[kind], kind).toBeTruthy();
    }
  });

  it('the strip RENDERS through the table, not a re-derived ternary', () => {
    expect(environmentStripSrc).toContain('actorTitle(row.actor)');
    for (const literal of ['GitHub login', 'Entra object id']) {
      expect(environmentStripSrc, literal).not.toContain(literal);
    }
  });
});

describe('entraActorName / actorText — a name, never a raw oid', () => {
  const OID = '11111111-2222-4333-8444-555555555555';
  const me = { oid: OID, name: 'Lars Svensson', email: 'lars@example.com' };

  it("names the viewer's own oid, case-insensitively", () => {
    expect(entraActorName(OID, me)).toBe('Lars Svensson');
    expect(entraActorName(OID.toUpperCase(), me)).toBe('Lars Svensson');
    expect(entraActorName(OID, { ...me, name: null })).toBe('lars@example.com');
  });

  it('reads "Another user" for any other oid — never the GUID, never "Unknown"', () => {
    expect(UNKNOWN_ENTRA_USER).toBe('Another user');
    expect(entraActorName('00000000-0000-0000-0000-000000000009', me)).toBe(UNKNOWN_ENTRA_USER);
    expect(entraActorName(OID, null)).toBe(UNKNOWN_ENTRA_USER);
  });

  it('passes a non-GUID actor (dev-auth email) through verbatim', () => {
    expect(entraActorName('dev@example.com', null)).toBe('dev@example.com');
  });

  it('actorText resolves entra only; actorTitle keeps the raw oid in the hover', () => {
    expect(actorText({ kind: 'entra', display: OID }, me)).toBe('Lars Svensson');
    expect(actorText({ kind: 'github', display: '@jorge' }, me)).toBe('@jorge');
    expect(actorTitle({ kind: 'entra', display: OID })).toContain(OID);
    expect(actorTitle({ kind: 'github', display: '@jorge' })).toBe(ACTOR_KIND_TITLE.github);
  });
});

const OWNER_INPUT = {
  held: 'owner' as const,
  roleLevel: 0,
  ungoverned: false,
  cicdStatus: 'deployed' as const,
  prodCandidateStatus: 'pending',
  steps: [] as { status: 'pending' | 'running' | 'done' | 'failed' }[],
};

describe('headerActions — Run evals follows the DEV build\'s eval state', () => {
  const AGENT = { ...OWNER_INPUT, hasAgent: true };
  it('leads (Promote secondary) when the dev build has no run, or its run was inconclusive', () => {
    for (const devEval of ['none', 'inconclusive'] as const) {
      const may = headerActions({ ...AGENT, devEval });
      expect([may.runEvals, may.evalsLead, may.promote], devEval).toEqual([true, true, true]);
    }
  });
  it('a FAILED run keeps Promote primary; Run evals stays, secondary (no re-run-until-green lead)', () => {
    const may = headerActions({ ...AGENT, devEval: 'failed' });
    expect([may.runEvals, may.evalsLead, may.promote]).toEqual([true, false, true]);
  });
  it('no Run evals once a run passed, while one runs, or before the runs are read', () => {
    for (const devEval of ['passed', 'running', null, undefined] as const) {
      const may = headerActions({ ...AGENT, devEval });
      expect([may.runEvals, may.evalsLead], String(devEval)).toEqual([false, false]);
    }
  });
  it('works without a prod candidate — the dev build is what gets evaluated', () => {
    const may = headerActions({ ...AGENT, prodCandidateStatus: null, devEval: 'none' });
    expect([may.runEvals, may.evalsLead, may.promote]).toEqual([true, true, false]);
  });
  it('no Run evals with nothing to evaluate or no standing to start a run', () => {
    expect(headerActions({ ...AGENT, hasAgent: false, devEval: 'none' }).runEvals).toBe(false);
    expect(headerActions({ ...AGENT, held: 'viewer', devEval: 'none' }).runEvals).toBe(false);
    expect(headerActions({ ...AGENT, artifactKind: 'mcp_server', devEval: 'none' }).runEvals).toBe(false);
  });
  it('a maintainer, who cannot promote, still gets Run evals', () => {
    const may = headerActions({ ...AGENT, held: 'maintainer', devEval: 'none' });
    expect([may.promote, may.runEvals]).toEqual([false, true]);
  });
});

describe('headerActions — promote / retry / delete, on the gates their ROUTES use', () => {
  it('offers all three to an owner with a pending candidate and a failed step', () => {
    const may = headerActions({ ...OWNER_INPUT, steps: [{ status: 'failed' }] });
    expect(may).toEqual({ promote: true, retry: true, destroy: true, runEvals: false, evalsLead: false });
  });

  it('offers NOTHING to a viewer', () => {
    const may = headerActions({
      ...OWNER_INPUT, held: 'viewer', steps: [{ status: 'failed' }],
    });
    expect(may).toEqual({ promote: false, retry: false, destroy: false, runEvals: false, evalsLead: false });
  });

  it('Retry gets the ungoverned fallback its route carries; Promote, on the strict owner gate, does not', () => {
    const may = headerActions({
      ...OWNER_INPUT, held: null, ungoverned: true, steps: [{ status: 'failed' }],
    });
    expect(may.retry).toBe(true);
    expect(may.promote).toBe(false);
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
    const inFlight: CicdStatus[] = ['provisioning', 'promoting'];
    for (const status of inFlight) {
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

  it('a platform admin holds all three without a role row', () => {
    const may = headerActions({
      ...OWNER_INPUT, held: null, roleLevel: 2, steps: [{ status: 'failed' }],
    });
    expect(may).toEqual({ promote: true, retry: true, destroy: true, runEvals: false, evalsLead: false });
  });

  it('DELETE is offered whatever the repo state — a broken repo is the one to tear down', () => {
    const states: CicdStatus[] = ['provisioning', 'failed', 'deployed', 'unknown'];
    for (const status of states) {
      expect(headerActions({ ...OWNER_INPUT, cicdStatus: status }).destroy, status).toBe(true);
    }
  });

  it('suppresses PROMOTE when the candidate itself failed its dev deploy', () => {
    expect(
      headerActions({ ...OWNER_INPUT, devOutcomeForCandidate: 'failed' }).promote,
    ).toBe(false);
  });

  it('still offers PROMOTE when the dev outcome is succeeded, started, or unknown', () => {
    for (const outcome of ['succeeded', 'started', null, undefined]) {
      expect(
        headerActions({ ...OWNER_INPUT, devOutcomeForCandidate: outcome }).promote,
        String(outcome),
      ).toBe(true);
    }
  });
});

describe('runtimeScope — the runtime answer is PER-AGENT and cannot be attributed to a stage', () => {
  it('is agent-scoped whenever the probe could not name a stage', () => {
    expect(runtimeScope({ stage: 'unknown', status: 'ready' }).kind).toBe('agent');
    expect(runtimeScope({ stage: '', status: 'ready' }).kind).toBe('agent');
    expect(runtimeScope(undefined).kind).toBe('agent');
  });

  it('NEVER attributes an agent-scoped reading to a stage, even one the tenant has', () => {
    const scope = runtimeScope({ stage: 'unknown', status: 'ready' });
    expect(scope.stage).toBeNull();
    expect(Object.values(scope).includes('uat')).toBe(false);
  });

  it('reports the note that MUST accompany the per-stage Runtime column', () => {
    expect(RUNTIME_SCOPE_NOTE).toMatch(/not attribut/i);
    expect(runtimeScope(undefined).note).toBe(RUNTIME_SCOPE_NOTE);
  });

  it('would become stage-scoped if the envelope ever gained a real stage', () => {
    const scope = runtimeScope({ stage: 'uat', status: 'ready' });
    expect(scope.kind).toBe('stage');
    expect(scope.stage).toBe('uat');
    expect(scope.note).toBeNull();
  });

  it('does not treat a stage-shaped reading as attributable when the status is unknown', () => {
    expect(runtimeScope({ stage: 'uat', status: 'unknown' }).kind).toBe('agent');
  });
});

describe('stageRuntimeCell — a stage row may only make a runtime claim it can attribute', () => {
  it('renders a PILL for a reading attributable to this very stage', () => {
    const cell = stageRuntimeCell('uat', { stage: 'uat', status: 'ready' });
    expect(cell.kind).toBe('pill');
    expect(cell.status).toBe('ready');
    expect(cell.note).toBeNull();
  });

  it('keeps RUNTIME_SCOPE_NOTE for a reading that exists but is not attributable', () => {
    const cell = stageRuntimeCell('uat', { stage: 'unknown', status: 'ready' });
    expect(cell.kind).toBe('note');
    expect(cell.note).toBe(RUNTIME_SCOPE_NOTE);
  });

  it('renders ABSENT — not a note, and never a pill — when there is no reading', () => {
    const cell = stageRuntimeCell('uat', undefined);
    expect(cell.kind).toBe('absent');
    expect(cell.note).toBeNull();
  });

  it('refuses to caption THIS stage with ANOTHER stage’s reading', () => {
    const cell = stageRuntimeCell('prod-like', { stage: 'uat', status: 'ready' });
    expect(cell.kind).toBe('note');
    expect(cell.note).toBe(RUNTIME_SCOPE_NOTE);
  });

  it('delegates attribution to runtimeScope rather than re-deriving it', () => {
    const readings: Pick<RuntimeStatus, 'stage' | 'status'>[] = [
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
    const cell = stageRuntimeCell('uat', { stage: 'uat', status: 'not_deployed' });
    expect(cell.kind).toBe('pill');
    expect(cell.status).toBe('not_deployed');
  });
});

describe('agentRuntimeLabel — the agent-level override for a CONFIG build', () => {
  const COMMIT_TAG = 'a'.repeat(40);

  it('overrides not_deployed/unknown to the honest note when the tag is a commit sha', () => {
    expect(agentRuntimeLabel('not_deployed', COMMIT_TAG)).toBe(CONFIG_RUNTIME_NOTE);
    expect(agentRuntimeLabel('unknown', COMMIT_TAG)).toBe(CONFIG_RUNTIME_NOTE);
  });

  it('does not override a container tag, or any other runtime status', () => {
    expect(agentRuntimeLabel('not_deployed', 'agent-7-abc1234')).toBe('Not deployed');
    expect(agentRuntimeLabel('unknown', 'agent-7-abc1234')).toBe('Runtime unknown');
    expect(agentRuntimeLabel('ready', COMMIT_TAG)).toBe('Ready');
    expect(agentRuntimeLabel('failed', COMMIT_TAG)).toBe('Runtime failed');
  });

  it('does not override with no tag at all — nothing has shown this is a config build', () => {
    expect(agentRuntimeLabel('not_deployed', null)).toBe('Not deployed');
    expect(agentRuntimeLabel('unknown', undefined)).toBe('Runtime unknown');
  });
});

describe('representativeImageTag — the ONE tag chain every caller of agentRuntimeLabel reads', () => {
  it('prefers last_dev, then last_promoted, then the prod candidate', () => {
    expect(
      representativeImageTag({
        last_dev_image_tag: 'dev-tag',
        last_promoted_image_tag: 'promoted-tag',
        prod_candidate_image_tag: 'candidate-tag',
      }),
    ).toBe('dev-tag');
    expect(
      representativeImageTag({
        last_dev_image_tag: null,
        last_promoted_image_tag: 'promoted-tag',
        prod_candidate_image_tag: 'candidate-tag',
      }),
    ).toBe('promoted-tag');
    expect(
      representativeImageTag({
        last_dev_image_tag: null,
        last_promoted_image_tag: null,
        prod_candidate_image_tag: 'candidate-tag',
      }),
    ).toBe('candidate-tag');
  });

  it('is what makes the fleet row and the detail page agree on a candidate-only CONFIG repo', () => {
    const commitSha = 'c'.repeat(40);
    const repo = {
      last_dev_image_tag: null,
      last_promoted_image_tag: null,
      prod_candidate_image_tag: commitSha,
    };
    expect(agentRuntimeLabel('unknown', representativeImageTag(repo))).toBe(CONFIG_RUNTIME_NOTE);
  });
});

describe('stageRuntimeDisplay — the per-stage wrapper, only once something deployed here', () => {
  const COMMIT_TAG = 'b'.repeat(40);

  it('replaces a genuinely-not-deployed CONFIG stage pill with the note', () => {
    const display = stageRuntimeDisplay('uat', { stage: 'uat', status: 'not_deployed' }, COMMIT_TAG);
    expect(display).toEqual({ kind: 'note', status: null, note: CONFIG_RUNTIME_NOTE });
  });

  it('a config stage with a commit-shaped deployed tag: delivery reads "Deployed" while runtime has nothing to say', () => {
    const display = stageRuntimeDisplay('uat', { stage: 'uat', status: 'not_deployed' }, COMMIT_TAG);
    expect(display.kind).toBe('note');
    expect(display.note).toBe(CONFIG_RUNTIME_NOTE);
  });

  it('leaves a stage that has genuinely never deployed reading "Not deployed" — that claim is true', () => {
    const display = stageRuntimeDisplay('uat', { stage: 'uat', status: 'not_deployed' }, null);
    expect(display).toEqual(stageRuntimeCell('uat', { stage: 'uat', status: 'not_deployed' }));
  });

  it('does not touch a container stage, or a reading unattributable for the ORIGINAL reason', () => {
    const container = stageRuntimeDisplay(
      'uat', { stage: 'uat', status: 'ready' }, 'agent-7-abc1234'
    );
    expect(container).toEqual(stageRuntimeCell('uat', { stage: 'uat', status: 'ready' }));
    const mismatched = stageRuntimeDisplay(
      'prod-like', { stage: 'uat', status: 'ready' }, 'agent-7-abc1234'
    );
    expect(mismatched).toEqual(stageRuntimeCell('prod-like', { stage: 'uat', status: 'ready' }));
  });

  it('a CONFIG stage never shows the per-agent note beside the header\'s config note: one build kind, one reason', () => {
    const agentScoped = stageRuntimeDisplay('uat', { stage: 'agent', status: 'unknown' }, COMMIT_TAG);
    expect(agentScoped).toEqual({ kind: 'note', status: null, note: CONFIG_RUNTIME_NOTE });
    const otherStage = stageRuntimeDisplay('prod-like', { stage: 'uat', status: 'ready' }, COMMIT_TAG);
    expect(otherStage.note).toBe(CONFIG_RUNTIME_NOTE);
  });

  it('passes absence straight through — nothing to override on an absent reading', () => {
    expect(stageRuntimeDisplay('uat', undefined, COMMIT_TAG)).toEqual(
      stageRuntimeCell('uat', undefined)
    );
  });
});

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
  it('a successful build on a non-production stage is not production: nothing stamped means no promotion was attempted', () => {
    expect(
      serving({
        deployments: [
          deployment({ build_id: 'build-1', outcome: 'succeeded' }),
          deployment({ build_id: 'build-1', outcome: 'started' }),
        ],
      }),
    ).toBe('none');
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
    expect(
      serving({
        lastPromotionBuildId: 'build-9',
        lastPromotedAt: '2026-08-01T10:00:00Z',
        lastPromotedImageTag: 'a-1-tree999',
        deployments: [deployment({ build_id: 'build-9', outcome: 'failed' })],
      }),
    ).toBe('none');
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

  it('a FAILED promotion in front of an older succeeded image on the same stage is not silence', () => {
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
    expect(serving({ lastPromotionBuildId: 'build-9', historyError: true })).toBe('unknown');
    expect(serving({ lastPromotedAt: '2026-08-01T10:00:00Z', historyError: true })).toBe('unknown');
    expect(serving({ lastPromotedImageTag: 'a-1-tree999', historyError: true })).toBe('unknown');
    expect(serving({ historyError: true })).toBe('none');
  });

  it('a stamp with no delivery row that confirms or denies it is UNKNOWN', () => {
    expect(serving({ lastPromotionBuildId: 'build-9' })).toBe('unknown');
    expect(
      serving({
        lastPromotionBuildId: 'build-9',
        deployments: [deployment({ build_id: 'build-other', outcome: 'failed' })],
      }),
    ).toBe('unknown');
    expect(
      serving({
        lastPromotionBuildId: 'build-9',
        deployments: [deployment({ build_id: 'build-9', outcome: 'started' })],
      }),
    ).toBe('unknown');
    expect(
      serving({
        lastPromotedAt: '2026-08-01T10:00:00Z',
        lastPromotedImageTag: 'a-1-tree999',
        deployments: [deployment({ build_id: 'build-9', outcome: 'succeeded' })],
      }),
    ).toBe('unknown');
  });

  it('makes no claim while the history is still loading', () => {
    expect(serving({ lastPromotionBuildId: 'build-9', historyLoading: true })).toBe('none');
    expect(
      serving({ lastPromotionBuildId: 'build-9', historyLoading: true, historyError: true }),
    ).toBe('none');
    expect(
      serving({
        lastPromotionBuildId: 'build-9',
        historyLoading: false,
        deployments: [deployment({ build_id: 'build-9', outcome: 'succeeded' })],
      }),
    ).toBe('serving');
  });

  it('identifies the production row WITHOUT naming a stage', () => {
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
    for (const state of ['unknown', 'none'] as ProdServingState[]) {
      expect(ungovernedInProd(state, 'proposed'), state).toBe(false);
    }
  });

  it('does not flag when the lifecycle is missing', () => {
    expect(ungovernedInProd('serving', undefined)).toBe(false);
    expect(ungovernedInProd('serving', null)).toBe(false);
    expect(ungovernedInProd('serving', '  ')).toBe(false);
  });

  it('takes a PRODUCTION verdict, not a delivery status: the signature is the fix', () => {
    const states: ProdServingState[] = ['serving', 'unknown', 'none'];
    for (const s of states) {
      expect(s, s).not.toMatch(/deployed|provisioning|promoting|failed|ready/);
    }
  });
});

describe('environmentStripState — an unresolved tenant is not a tenant with no stages', () => {
  it('reports stages-unknown when the tenant could not be resolved', () => {
    expect(
      environmentStripState({ stages: null, stagesUnknown: true, deployments: [] }),
    ).toBe('stages-unknown');
    expect(
      environmentStripState({ stages: undefined, stagesUnknown: true, deployments: [] }),
    ).toBe('stages-unknown');
  });

  it('still reports no-stages when the tenant WAS read and carries none', () => {
    expect(environmentStripState({ stages: {}, stagesUnknown: false, deployments: [] })).toBe(
      'no-stages',
    );
  });

  it('defaults to no-stages when the caller omits the flag', () => {
    expect(environmentStripState({ stages: {}, deployments: [] })).toBe('no-stages');
    expect(environmentStripState({ stages: null, deployments: [] })).toBe('no-stages');
  });

  it('reports rows whenever there are rows, whatever the flag says', () => {
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
    const unknown = ENVIRONMENT_EMPTY_COPY['stages-unknown'].toLowerCase();
    expect(unknown).not.toMatch(/are configured/);
    expect(unknown).not.toMatch(/no environments are/);
    expect(unknown).toMatch(/could not be read/);
    expect(unknown).toMatch(/not a report that the tenant has none/);
  });

  it('keeps the genuine zero-stage case reading as a configuration', () => {
    expect(ENVIRONMENT_EMPTY_COPY['no-stages']).toMatch(/configured/i);
  });

  it('the two sentences are DISTINCT', () => {
    expect(ENVIRONMENT_EMPTY_COPY['no-stages']).not.toBe(
      ENVIRONMENT_EMPTY_COPY['stages-unknown'],
    );
  });
});

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
    for (const badge of ['failed', 'ready', 'provisioning'] as const) {
      const s = materializeSummary(badge, 0);
      expect(s.succeeded, badge).toBe(false);
      expect(s.label, badge).not.toMatch(/in progress|complete/i);
    }
    expect(materializeSummary('provisioning', 0).label).toBe('Not recorded');
  });

  it('agrees with `nextBadgeFromSteps` for every real step shape', () => {
    const done = { status: 'done' as const };
    const failed = { status: 'failed' as const };
    const pending = { status: 'pending' as const };
    const running = { status: 'running' as const };
    const summaryOf = (steps: { status: 'done' | 'failed' | 'pending' | 'running' }[]) =>
      materializeSummary(nextBadgeFromSteps(steps as never), steps.length);

    expect(summaryOf([done, failed, pending]).succeeded).toBe(false);
    expect(summaryOf([done, failed, pending]).label).toMatch(/fail/i);
    expect(summaryOf([done, done, done]).label).toBe('Complete');
    expect(summaryOf([done, running, pending]).label).toBe('In progress');
    expect(summaryOf([]).label).toBe('Not recorded');
  });
});

const CANDIDATE_DIGEST = `sha256:abc1234${'d'.repeat(57)}`;

const PINNED = {
  prod_candidate_status: 'pending',
  prod_candidate_image_tag: 'agent-7-3f9a1c2',
  prod_candidate_digest: CANDIDATE_DIGEST,
};

describe('promotionArtifact — a tag-only candidate is VISIBLE, not assumed away', () => {
  it('reports a digest-pinned candidate as `digest`, and puts the abbreviated digest IN the marker', () => {
    const a = promotionArtifact(PINNED);
    expect(a.kind).toBe('digest');
    expect(a.marker).toEqual({ text: 'sha256:abc1234…', note: null, tone: 'pinned' });
    expect(a.imageTag).toBe('agent-7-3f9a1c2');
  });

  it('reports a candidate with NO digest as `tag-only`, carrying the caution as its marker', () => {
    for (const missing of [null, undefined, '', '   ']) {
      const a = promotionArtifact({ ...PINNED, prod_candidate_digest: missing });
      expect(a.kind, String(missing)).toBe('tag-only');
      expect(a.marker, String(missing)).toEqual({
        text: PROMOTION_TAG_ONLY_LABEL,
        note: PROMOTION_TAG_ONLY_NOTE,
        tone: 'caution',
      });
      expect(a.imageTag, String(missing)).toBe('agent-7-3f9a1c2');
    }
    const legacy = promotionArtifact({
      prod_candidate_status: 'pending',
      prod_candidate_image_tag: 'agent-7-3f9a1c2',
    });
    expect(legacy.kind).toBe('tag-only');
    expect(legacy.marker?.tone).toBe('caution');
  });

  it('reports a bare commit-sha candidate as `commit-pinned`, never `tag-only`', () => {
    const sha = '0123456789abcdef0123456789abcdef01234567';
    const a = promotionArtifact({
      prod_candidate_status: 'pending',
      prod_candidate_image_tag: sha,
      prod_candidate_digest: null,
    });
    expect(a.kind).toBe('commit-pinned');
    expect(a.marker).toEqual({ text: 'commit 0123456', note: null, tone: 'pinned' });
    expect(a.imageTag).toBe(sha);
  });

  it('reports `none` with NO MARKER when nothing is pending — no caution between merges', () => {
    for (const status of [null, undefined, '', 'promoted', 'Pending', 'PENDING', 'nonsense']) {
      const a = promotionArtifact({ ...PINNED, prod_candidate_status: status });
      expect(a.kind, String(status)).toBe('none');
      expect(a.marker, String(status)).toBeNull();
    }
    expect(promotionArtifact(null)).toEqual({ kind: 'none', marker: null, imageTag: null });
    expect(promotionArtifact(undefined)).toEqual({ kind: 'none', marker: null, imageTag: null });
  });

  it('keeps the record’s image tag readable even when nothing is approvable', () => {
    const consumed = promotionArtifact({
      prod_candidate_status: 'promoted',
      prod_candidate_image_tag: 'agent-7-3f9a1c2',
      prod_candidate_digest: CANDIDATE_DIGEST,
    });
    expect(consumed.kind).toBe('none');
    expect(consumed.imageTag).toBe('agent-7-3f9a1c2');
    expect(consumed.marker).toBeNull();
    expect(promotionArtifact({ prod_candidate_image_tag: '   ' }).imageTag).toBeNull();
  });

  it('marker is null EXACTLY when kind is `none` — the two can never disagree', () => {
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
    expect(promotionArtifact({ ...PINNED, prod_candidate_status: 'promoted' }).kind).toBe('none');
    expect(promotionArtifact({ ...PINNED, prod_candidate_status: 'pending' }).kind).toBe('digest');
  });

  it('never claims `digest` on a value the backend would have refused', () => {
    const bad = promotionArtifact({ ...PINNED, prod_candidate_digest: `sha256:${'A'.repeat(64)}` });
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
          if (a.marker !== null) {
            expect(a.marker.text.length, `${status}/${digest}`).toBeGreaterThan(0);
            expect(['pinned', 'caution']).toContain(a.marker.tone);
          }
        }
      }
    }
  });
});

describe('the promote surface renders the artifact through ONE branchless value', () => {
  it('reads the resolved marker and holds no branch of its own over it', () => {
    const s = repositoryDetailSrc;

    expect([...s.matchAll(/\{artifact\.marker\.text\}/g)]).toHaveLength(1);

    const start = s.indexOf('artifact.marker !== null');
    expect(start).toBeGreaterThan(-1);
    const block = s.slice(start, s.indexOf('</Field>', start));
    expect(block).toContain('{artifact.marker.text}');
    expect([...block.matchAll(/&&/g)]).toHaveLength(2);
    const ternary = /(?<!\?)\?(?![?.])/g;
    expect([...block.matchAll(ternary)]).toHaveLength(0);

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
    expect([...'title={artifact.marker.note ?? undefined}'.matchAll(ternary)]).toHaveLength(0);

    expect(s).not.toMatch(/artifact\.digest/);
    expect(s).not.toMatch(/artifact\.kind\s*===/);
  });

  it('holds no branch in the CONSENT DIALOG either: the same counts, on PromoteConfirm.tsx', () => {
    const s = promoteConfirmSrc;

    expect([...s.matchAll(/\{artifact\.marker\.text\}/g)]).toHaveLength(1);

    const start = s.indexOf('artifact.marker !== null');
    expect(start).toBeGreaterThan(-1);
    const block = s.slice(start, s.indexOf('</dl>', start));
    expect(block).toContain('{artifact.marker.text}');
    expect([...block.matchAll(/&&/g)]).toHaveLength(1);
    const ternary = /(?<!\?)\?(?![?.])/g;
    expect([...block.matchAll(ternary)]).toHaveLength(0);

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

    expect(s).not.toMatch(/artifact\.digest/);
    expect(s).not.toMatch(/artifact\.kind\s*===/);
  });

  it('takes its TONE from an indexed table, never from a conditional', () => {
    const s = repositoryDetailSrc;
    expect(s).toMatch(/ARTIFACT_TONE_CLS\[artifact\.marker\.tone\]/);
    expect(s).not.toMatch(/ARTIFACT_TONE_CLS:\s*Record/);
    expect(Object.keys(ARTIFACT_TONE_CLS).sort()).toEqual(['caution', 'pinned']);
    for (const cls of Object.values(ARTIFACT_TONE_CLS)) expect(cls.length).toBeGreaterThan(0);
  });

  it('tints the caution amber and the pinned digest as a neutral mono chip', () => {
    const { caution, pinned } = ARTIFACT_TONE_CLS;
    expect(caution).toContain('bg-amber-50');
    expect(caution).toContain('text-amber-800');
    expect(caution).not.toMatch(/rose|text-red-/);
    expect(pinned).toContain('font-mono');
    expect(pinned).not.toMatch(/amber|rose|emerald|text-red-/);
    expect(caution).not.toBe(pinned);
  });

  it('narrows on the marker, so the whole field disappears when nothing is approvable', () => {
    expect(repositoryDetailSrc).toMatch(/artifact\.marker !== null &&/);
  });

  it('shows the caution’s explanation, from the marker rather than a local constant', () => {
    expect(repositoryDetailSrc).toMatch(/artifact\.marker\.note/);
  });

  it('writes neither the copy nor the decision inline', () => {
    const s = repositoryDetailSrc;
    expect(s).not.toMatch(/Tag-only —/);
    expect(s).not.toContain('PROMOTION_TAG_ONLY_LABEL');
    expect(s).not.toContain('PROMOTION_TAG_ONLY_NOTE');
  });

  it('calls the derivation rather than reading the raw field', () => {
    expect(repositoryDetailSrc).toMatch(/promotionArtifact\s*\(/);
    expect(repositoryDetailSrc).not.toMatch(/repo\.prod_candidate_digest/);
  });

  it('does not turn the caution into a BLOCK on promoting', () => {
    expect(headerActions({
      held: 'owner',
      roleLevel: 0,
      ungoverned: false,
      cicdStatus: 'deployed',
      prodCandidateStatus: 'pending',
      steps: [],
    }).promote).toBe(true);
    expect(repositoryDetailSrc).not.toMatch(/headerActions\s*\(\s*\{[^)]*digest/);
  });

  it('the reachability guards cannot be satisfied by prose', () => {
    const prose = [
      '// The marker renders as {artifact.marker.text} with no branch.',
      "{/* Tone comes from ARTIFACT_TONE_CLS[artifact.marker.tone] — amber for caution. */}",
      '// bg-amber-50 text-amber-800 is the caution tint.',
      '<span>{somethingElse}</span>',
    ].join('\n');
    const stripped = stripComments(prose);
    expect(stripped).not.toContain('artifact.marker.text');
    expect(stripped).not.toContain('ARTIFACT_TONE_CLS');
    expect(stripped).not.toContain('bg-amber-50');
  });
});

describe('prodGovernanceState — a failed governance read does not read as approval', () => {
  it('still raises the full warning on an established serving + proposed', () => {
    expect(
      prodGovernanceState({ serving: 'serving', lifecycleState: 'proposed', agentRead: true }),
    ).toBe('ungoverned');
  });

  it('says UNKNOWN when something is in production and the record could not be read', () => {
    expect(
      prodGovernanceState({ serving: 'serving', lifecycleState: undefined, agentRead: false }),
    ).toBe('unknown');
  });

  it('says UNKNOWN when production itself is unestablished and there is a question to ask', () => {
    expect(
      prodGovernanceState({ serving: 'unknown', lifecycleState: 'proposed', agentRead: true }),
    ).toBe('unknown');
    expect(
      prodGovernanceState({ serving: 'unknown', lifecycleState: undefined, agentRead: false }),
    ).toBe('unknown');
    expect(
      prodGovernanceState({ serving: 'unknown', lifecycleState: 'approved', agentRead: true }),
    ).toBe('none');
  });

  it('stays SILENT when the record WAS read and does not say proposed', () => {
    for (const lifecycle of ['approved', 'active', 'deprecated', '', null, undefined]) {
      expect(
        prodGovernanceState({ serving: 'serving', lifecycleState: lifecycle, agentRead: true }),
        String(lifecycle),
      ).toBe('none');
    }
  });

  it('stays SILENT on an unread record when NOTHING is in production', () => {
    for (const lifecycle of ['proposed', undefined, null]) {
      for (const agentRead of [true, false]) {
        expect(
          prodGovernanceState({ serving: 'none', lifecycleState: lifecycle, agentRead }),
          `${String(lifecycle)}/${agentRead}`,
        ).toBe('none');
      }
    }
  });

  it('a repository that only delivered to a non-production stage raises NO banner, end to end', () => {
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
    const states: string[] = ['ungoverned', 'unknown', 'none'];
    for (const s of states) {
      expect(s).not.toMatch(/approve|grant|classify|deprecate/i);
    }
  });
});

describe('recordStatusLabel — never renders a bare wire value, never invents one', () => {
  it('names what the record status is actually about', () => {
    expect(recordStatusLabel('provisioning')).toBe('Materializing');
    expect(recordStatusLabel('ready')).toBe('Materialized');
    expect(recordStatusLabel('failed')).toBe('Materialize failed');
  });

  it('does not render the word the OTHER two machines already use for something else', () => {
    for (const raw of ['provisioning', 'ready', 'failed']) {
      expect(recordStatusLabel(raw), raw).not.toBe(raw);
    }
    expect(Object.values(RECORD_STATUS_LABEL)).not.toContain('Ready');
  });

  it('absence is absence, not a status', () => {
    expect(recordStatusLabel(null)).toBeNull();
    expect(recordStatusLabel(undefined)).toBeNull();
    expect(recordStatusLabel('')).toBeNull();
    expect(recordStatusLabel('   ')).toBeNull();
  });

  it('shows an UNRECOGNIZED value verbatim rather than mapping it to a plausible neighbour', () => {
    expect(recordStatusLabel('archived')).toBe('archived');
    expect(recordStatusLabel('  archived  ')).toBe('archived');
    expect(recordStatusLabel('READY')).toBe('READY');
  });
});

type Pollable = Parameters<typeof shouldPollRepo>[0];
function pollable(over: Partial<Pollable> = {}): Pollable {
  return { cicd_status: 'ready', steps: [], ...over };
}
const RUNNING = stepState({ status: 'running' });
const PENDING = stepState({ status: 'pending' });
const DONE = stepState({ status: 'done' });
const FAILED = stepState({ status: 'failed' });

describe('shouldPollRepo — a settled repository does ZERO polling', () => {
  it('polls while the materialize run is still moving', () => {
    expect(shouldPollRepo(pollable({ steps: [DONE, RUNNING, PENDING] }))).toBe(true);
    expect(shouldPollRepo(pollable({ steps: [PENDING, PENDING] }))).toBe(true);
  });

  it('stops the moment the run reaches a terminal state', () => {
    expect(shouldPollRepo(pollable({ steps: [DONE, DONE] }))).toBe(false);
    expect(shouldPollRepo(pollable({ steps: [DONE, FAILED] }))).toBe(false);
    expect(shouldPollRepo(pollable({ steps: [FAILED, PENDING] }))).toBe(false);
  });

  it('a record with NO steps is not a record with a finished run', () => {
    expect(shouldPollRepo(pollable({ steps: [] }))).toBe(false);
  });

  it('ALSO polls while DELIVERY is in flight, so a promoting repo settles on this page too', () => {
    expect(shouldPollRepo(pollable({ cicd_status: 'promoting', steps: [DONE, DONE] }))).toBe(true);
    expect(shouldPollRepo(pollable({ cicd_status: 'provisioning', steps: [] }))).toBe(true);
  });

  it('does not poll for a delivery status that is RESTING rather than running', () => {
    const resting: CicdStatus[] = ['ready', 'deployed', 'failed', 'unknown'];
    for (const status of resting) {
      expect(shouldPollRepo(pollable({ cicd_status: status, steps: [DONE] })), status).toBe(false);
    }
  });
});

const idle = { inFlight: false };
const busy = { inFlight: true };

describe('deliveryHeaderState — "Building…" when the history says a build is running', () => {
  it('says Building… over a running build, and treats it as in flight', () => {
    const state = deliveryHeaderState({
      cicdStatus: 'ready',
      environmentRows: [idle, busy],
      historyError: false,
    });
    expect(state.label).toBe(DELIVERY_BUILDING_LABEL);
    expect(state.inFlight).toBe(true);
  });

  it('says it over a REDEPLOY too, not only a first build', () => {
    const state = deliveryHeaderState({
      cicdStatus: 'deployed',
      environmentRows: [busy],
      historyError: false,
    });
    expect(state.label).toBe(DELIVERY_BUILDING_LABEL);
    expect(state.inFlight).toBe(true);
  });

  it('leaves every resting record reading EXACTLY what it reads today', () => {
    const members: CicdStatus[] = ['ready', 'deployed', 'provisioning', 'promoting', 'failed', 'unknown'];
    for (const raw of members) {
      const state = deliveryHeaderState({
        cicdStatus: raw,
        environmentRows: [idle, idle],
        historyError: false,
      });
      expect(state.label, raw).toBe(CICD_LABEL[raw]);
    }
    expect(deliveryHeaderState({ cicdStatus: 'provisioning', environmentRows: [] }).inFlight).toBe(
      true,
    );
    expect(deliveryHeaderState({ cicdStatus: 'promoting', environmentRows: [] }).inFlight).toBe(true);
    expect(deliveryHeaderState({ cicdStatus: 'ready', environmentRows: [] }).inFlight).toBe(false);
    expect(deliveryHeaderState({ cicdStatus: 'deployed', environmentRows: [] }).inFlight).toBe(false);
  });

  it('a FAILED history read never produces a Building… claim (fails safe)', () => {
    const state = deliveryHeaderState({
      cicdStatus: 'ready',
      environmentRows: [busy],
      historyError: true,
    });
    expect(state.label).toBe(CICD_LABEL.ready);
    expect(state.inFlight).toBe(false);
    expect(
      deliveryHeaderState({ cicdStatus: 'ready', environmentRows: [busy], historyError: false })
        .label,
    ).toBe(DELIVERY_BUILDING_LABEL);
  });

  it('never overrides a record state that is already RUNNING something else', () => {
    const running: CicdStatus[] = ['provisioning', 'promoting', 'failed'];
    for (const raw of running) {
      const state = deliveryHeaderState({
        cicdStatus: raw,
        environmentRows: [busy],
        historyError: false,
      });
      expect(state.label, raw).toBe(CICD_LABEL[raw]);
    }
  });

  it('says nothing about a build when the status is unknown', () => {
    const state = deliveryHeaderState({
      cicdStatus: 'unknown',
      environmentRows: [busy],
      historyError: false,
    });
    expect(state.label).toBe(CICD_LABEL.unknown);
    expect(state.inFlight).toBe(false);
  });

  it('is its own word, distinct from all three machines already on the page', () => {
    expect(Object.values(CICD_LABEL)).not.toContain(DELIVERY_BUILDING_LABEL);
    expect(Object.values(RECORD_STATUS_LABEL)).not.toContain(DELIVERY_BUILDING_LABEL);
    expect(DELIVERY_BUILDING_LABEL).toBe('Building…');
  });

  it('an absent historyError is the same as a readable history', () => {
    expect(deliveryHeaderState({ cicdStatus: 'ready', environmentRows: [busy] }).label).toBe(
      DELIVERY_BUILDING_LABEL,
    );
  });
});

describe('deliveryHeaderState — a never-deployed repo after Build now', () => {
  const started = deployment({
    outcome: 'started', build_id: 'b:1', image_tag: 'c'.repeat(40),
    started_at: '2026-10-05T03:24:50Z',
  });
  const now = Date.parse('2026-10-05T03:26:00Z');

  it('reads the neutral "Deploying…" while the history is still loading (no rows yet)', () => {
    const rows = environmentRows({ stages: tenant('uat'), deployments: [], now });
    expect(deliveryHeaderState({ cicdStatus: 'promoting', environmentRows: rows }).label).toBe(
      CICD_LABEL.promoting,
    );
  });

  it('names the stage once the history arrives with the Build now launch row', () => {
    const rows = environmentRows({ stages: tenant('uat'), deployments: [started], now });
    const state = deliveryHeaderState({ cicdStatus: 'promoting', environmentRows: rows });
    expect(state.label).toBe('Deploying to uat…');
    expect(state.inFlight).toBe(true);
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

  it('the page source concatenates every sibling, so a negative pin cannot pass vacuously', () => {
    for (const [name, raw] of [
      ['repo-tabs/OverviewTab.tsx', overviewTabRaw],
      ['repo-tabs/AccessTab.tsx', accessTabRaw],
      ['RepoSummaryCard.tsx', repoSummaryCardRaw],
      ['RepoFields.tsx', repoFieldsRaw],
      ['useRepositoryDetail.ts', useRepositoryDetailRaw],
    ] as const) {
      expect(repositoryDetailSrc, `${name} is missing from the page source`).toContain(stripComments(raw));
    }
  });

  it('contains no stage literal in code, quoted, as a property access, or as a key or binding: a tenant\'s stage set is open', () => {
    const stageLiteral =
      /(['"`])(dev|prod)\1|[.[]\s*['"`]?(dev|prod)['"`]?(?![\w$])|[{,;(]\s*(dev|prod)\s*[:,}]/;
    for (const [name, src] of [
      ['repo-tabs/OverviewTab.tsx', stripComments(overviewTabRaw)],
      ['repo-tabs/AccessTab.tsx', stripComments(accessTabRaw)],
      ['RepoSummaryCard.tsx', stripComments(repoSummaryCardRaw)],
      ['RepoFields.tsx', stripComments(repoFieldsRaw)],
      ['useRepositoryDetail.ts', stripComments(useRepositoryDetailRaw)],
      ['RepositoryDetail.tsx', repositoryDetailSrc],
      ['EnvironmentStrip.tsx', environmentStripSrc],
      ['TabStrip.tsx', tabStripSrc],
      ['ProjectDetail.tsx', projectDetailSrc],
      ['client.ts', clientSrc],
    ] as const) {
      expect(stageLiteral.test(src), `${name} contains a stage literal`).toBe(false);
    }
    expect(stageLiteral.test(pureSrc)).toBe(false);
    expect(stageLiteral.test(ownSrc)).toBe(false);
  });

  it('the stage guard can see a property-access stage read, the form that crashed a single-stage tenant', () => {
    const stageLiteral =
      /(['"`])(dev|prod)\1|[.[]\s*['"`]?(dev|prod)['"`]?(?![\w$])|[{,;(]\s*(dev|prod)\s*[:,}]/;
    const stage = 'd' + 'ev';
    const other = 'pro' + 'd';
    expect(stageLiteral.test(`tenantAccount.stages.${stage}.account_id`)).toBe(true);
    expect(stageLiteral.test(`t.stages["${stage}"]`)).toBe(true);
    expect(stageLiteral.test(`{...t.stages.${stage}}`)).toBe(true);
    expect(
      stageLiteral.test(`stages: { ${stage}: TenantStageConfig; ${other}: TenantStageConfig };`),
    ).toBe(true);
    expect(stageLiteral.test(`stages: { ${stage}: cfg, ${other}: cfg }`)).toBe(true);
    expect(stageLiteral.test(`const { ${stage}, ${other} } = t.stages;`)).toBe(true);
    expect(stageLiteral.test(`the ${stage} buildspec writes it`)).toBe(false);
    expect(stageLiteral.test(`a.${stage}elopment.x`)).toBe(false);
    expect(stageLiteral.test(`cond ? ${stage} : x`)).toBe(false);
    expect(stageLiteral.test(`f(${stage})`)).toBe(false);
  });

  it('does not caption a runtime pill by an UNATTRIBUTED stage: the strip reads a status only off stageRuntimeDisplay\'s answer, the per-stage wrapper over stageRuntimeCell', () => {
    expect(environmentStripSrc).toMatch(/stageRuntimeDisplay\(/);
    expect(environmentStripSrc).not.toMatch(/stageRuntimeCell\(/);
    expect(environmentStripSrc).toContain('RUNTIME_LABEL');
    expect(environmentStripSrc).toContain('RUNTIME_BADGE_KEY');
    for (const table of ['RUNTIME_LABEL', 'RUNTIME_BADGE_KEY']) {
      const reads = [...environmentStripSrc.matchAll(new RegExp(`${table}\\[([^\\]]*)\\]`, 'g'))];
      expect(reads.length).toBeGreaterThan(0);
      for (const [, index] of reads) expect(index).toContain('cell.status');
    }
    expect(environmentStripSrc).not.toContain('runtimeScope(');
    expect(repositoryDetailSrc).toContain('agentRuntimeLabel');
  });

  it('wires the back link through the selector, and keeps the fleet fallback on the early returns', () => {
    const s = repositoryDetailSrc;

    expect([...s.matchAll(/repoBackLink\(/g)]).toHaveLength(1);
    expect(s).toMatch(/backTo=\{backLink\.to\}/);
    expect(s).toMatch(/backLabel=\{backLink\.label\}/);

    expect([...s.matchAll(/backTo=\{listPath\}/g)]).toHaveLength(3);
    expect(s).toMatch(/const listPath = objectListPathFor\(routeKind\)/);

    expect(s).not.toMatch(/backLabel="/);
    expect(s).not.toMatch(/←/);
  });

  it('keeps OpsPage’s default back label as ONE literal, in opsLabels.ts, for its 18 other call sites', () => {
    expect([...opsLabelsSrc.matchAll(/'← Operations'/g)]).toHaveLength(1);
    expect([...opsPageSrc.matchAll(/'← Operations'/g)]).toHaveLength(0);
    expect(opsPageSrc).toMatch(/import \{[^}]*\bOPS_BACK_LABEL\b[^}]*\} from '\.\/opsLabels'/);
    expect(opsPageSrc).toMatch(/backLabel\s*=\s*OPS_BACK_LABEL/);
    expect(OPS_BACK_LABEL).toBe('← Operations');
    expect(opsPageSrc).toMatch(/backLabel\?:\s*string/);
    expect(opsPageSrc).toMatch(/\{backLabel\}/);
  });

  it('reads the wire status as the generated union, never through an assertion', () => {
    expect(repositoryDetailSrc).not.toContain('as CicdStatus');
    expect(repositoryDetailSrc).not.toContain('as RuntimeStatusKey');
  });

  it('POLLS while something is running, and asks the predicate rather than deciding itself', () => {
    expect(repositoryDetailSrc).toContain('shouldPollRepo');
    expect(repositoryDetailSrc).toMatch(/setInterval/);
    expect(repositoryDetailSrc).toContain('projectsApi.getRepoStatus');
    expect(repositoryDetailSrc).toMatch(/clearInterval/);
    expect(repositoryDetailSrc).toMatch(/getRepoStatus\([^)]*\)\s*\.catch\(\(\) => null\)/);
  });

  it('reloads the HISTORY once on the settle edge, not only the record and never per tick', () => {
    const tickStart = repositoryDetailSrc.search(/setInterval/);
    const tick = repositoryDetailSrc.slice(
      tickStart,
      repositoryDetailSrc.indexOf('}, 3000);', tickStart),
    );
    expect(tick.length).toBeGreaterThan(200);
    expect(tick.length).toBeLessThan(1500);
    expect(tick).toMatch(/if\s*\(\s*!shouldPollRepo\(fresh\)\s*\)/);
    expect(tick).toMatch(/refetch\(\)/);
    expect(tick.search(/stop\(\)/)).toBeLessThan(tick.search(/refetch\(\)/));
    expect(tick).toMatch(/if\s*\(cancelled/);
  });

  it('renders the DERIVED delivery header, so the pill cannot contradict the strip below it', () => {
    expect(repositoryDetailSrc).toContain('deliveryHeaderState');
    expect(repositoryDetailSrc).toMatch(/delivery\.label/);
    expect(repositoryDetailSrc).toMatch(/delivery\.inFlight/);
    expect(repositoryDetailSrc).not.toMatch(/\{CICD_LABEL\[/);
    expect(repositoryDetailSrc).not.toMatch(/isCicdInFlight\s*\(/);
    expect(repositoryDetailSrc).toMatch(/environmentRows\s*\(/);
  });

  it('uses the shared status LABELS, never its own strings', () => {
    expect(repositoryDetailSrc).toContain('agentRuntimeLabel(');
    expect(repositoryDetailSrc).not.toMatch(/\{RUNTIME_LABEL\[/);
    expect(pureSrc).toContain('RUNTIME_LABEL[');
    expect(pureSrc).toContain('CICD_LABEL[');
    expect(repositoryDetailSrc).not.toContain("'Delivery failed'");
    expect(repositoryDetailSrc).not.toContain("'Runtime failed'");
    expect(repositoryDetailSrc).not.toContain(DELIVERY_BUILDING_LABEL);
    expect(pureSrc).toContain(DELIVERY_BUILDING_LABEL);
  });

  it('offers NO governance verb — the lifecycle pill is read-only', () => {
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
    expect(repositoryDetailSrc).toContain('projectsApi.retryRepo');
    expect(repositoryDetailSrc).toContain("from './DeleteRepositoryModal'");
    expect(repositoryDetailSrc).toContain('<DeleteRepositoryModal');
    expect(repositoryDetailSrc).not.toContain('projectsApi.deleteRepo');
    expect(repositoryDetailSrc).not.toContain('projectsApi.deletePreview');
    expect(repositoryDetailSrc).toContain('headerActions');
    expect(repositoryDetailSrc).not.toContain('meetsRole');
    expect(repositoryDetailSrc).toContain('isNothingToRetry(');
    expect(repositoryDetailSrc).not.toMatch(/nothing to retry\/i/);
  });

  it('renders the step WORDING through the shared mapping, not the raw wire status', () => {
    expect(repositoryDetailSrc).toContain('stepStatusText');
    expect(repositoryDetailSrc).not.toMatch(
      /\{\s*(?:String\(\s*)?s\.status(?:\s*\))?(?:\s+as\s+\w+)?\s*\}/,
    );
  });

  it('does not render the raw project UUID as the project link text', () => {
    expect(repositoryDetailSrc).toContain('headerName(projectName, projectRead, repo.project_id)');
    expect(repositoryDetailSrc).not.toMatch(/>\s*\{repo\.project_id\}/);
  });

  it('labels the framework field FRAMEWORK, never Model', () => {
    expect(repositoryDetailSrc).toContain('label="Framework"');
    expect(repositoryDetailSrc).not.toContain('label="Model"');
  });

  it('honours TabStrip\'s own contract about the whole tabpanel attribute set', () => {
    expect(tabStripSrc).toContain('tabs.length < 2');
    expect(repositoryDetailSrc).toContain('panelProps');
    for (const key of selectableTabKeys()) {
      expect(repositoryDetailSrc, key).toContain(`{...panelProps('${key}')}`);
    }
    expect(repositoryDetailSrc).not.toContain('aria-labelledby={tabId(');
    expect(repositoryDetailSrc).not.toMatch(/role="tabpanel"/);
    expect(repositoryDetailSrc).not.toMatch(/tabIndex=\{0\}/);
  });

  it('clears the success notice when an action starts, so two claims cannot co-exist', () => {
    expect(repositoryDetailSrc).toContain('setPromoteNotice(null)');
    expect(repositoryDetailSrc).not.toMatch(/updates when you reload/i);
  });

  it('reports an unreadable deployment history as unknown, never as "never deployed"', () => {
    expect(repositoryDetailSrc).toContain("const historyError = historyLoad.state.kind === 'error'");
    expect(repositoryDetailSrc).toContain('historyError');
    expect(environmentStripSrc).toContain('historyUnknown');
  });

  it('tells the strip when the TENANT could not be resolved, guarded on the tenant id', () => {
    expect(repositoryDetailSrc).toContain('stagesUnknown');
    expect(repositoryDetailSrc).toMatch(/tenantId !== null && tenantAccount === null/);
    expect(repositoryDetailSrc).toMatch(/source=\{\{[^}]*stagesUnknown/);
  });

  it('renders BOTH empty states from the copy table, not one hardcoded sentence', () => {
    expect(environmentStripSrc).toContain('ENVIRONMENT_EMPTY_COPY');
    expect(environmentStripSrc).toContain('environmentStripState');
    expect(environmentStripSrc).not.toContain('No environments are configured');
    expect(environmentStripSrc).toMatch(/state !== 'rows'/);
  });

  it('asks whether the materialize SUCCEEDED, not whether it stopped', () => {
    expect(repositoryDetailSrc).toContain('materializeSummary');
    expect(repositoryDetailSrc).toContain('nextBadgeFromSteps');
    expect(repositoryDetailSrc).not.toMatch(/isMaterializeTerminal\s*\(/);
    expect(repositoryDetailSrc).not.toContain("'Complete'");
    expect(repositoryDetailSrc).not.toContain("'In progress'");
  });

  it('scopes the materialize card’s heading to the RUN it describes', () => {
    expect(repositoryDetailSrc).toMatch(/Materialization run/);
    const bareHeading = />\s*Materialization\s*</;
    expect(bareHeading.test(repositoryDetailSrc)).toBe(false);
    expect(bareHeading.test('// the Materialization card used to read "Complete"')).toBe(false);
    expect(bareHeading.test('<h3>Materialization</h3>')).toBe(true);
  });

  it('warns when a PRODUCTION deployment’s governance record could not be read', () => {
    expect(repositoryDetailSrc).toContain("const agentError = recordLoad.state.kind === 'error'");
    expect(repositoryDetailSrc).toContain('prodGovernanceState');
    expect(repositoryDetailSrc).toMatch(/prodGovernance === 'unknown'/);
    expect(repositoryDetailSrc).not.toMatch(/ungovernedInProd\s*\(/);
    expect(repositoryDetailSrc).toContain('agent’s governance record');
  });

  it('derives the banner from a PRODUCTION verdict, never from the delivery status', () => {
    const servingCall = /prodServingState\s*\(\s*\{/;
    const governanceTakesServing = /prodGovernanceState\s*\(\s*\{[^)]*\bserving\s*[,:]/;
    expect(servingCall.test(repositoryDetailSrc)).toBe(true);
    expect(governanceTakesServing.test(repositoryDetailSrc)).toBe(true);
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
    expect(repositoryDetailSrc).not.toMatch(/prodGovernanceState\s*\(\s*\{[^)]*cicdStatus/);
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
    const slateBanner = /\{prodGovernance === 'unknown' && \(/;
    expect(slateBanner.test(repositoryDetailSrc)).toBe(true);
    const start = repositoryDetailSrc.search(slateBanner);
    const body = repositoryDetailSrc.slice(start, start + 900);
    expect(body).toContain('role="status"');
    expect(body).toMatch(/could not be confirmed/);
    expect(body).toContain('Deployments tab');
    expect(body).toMatch(/Either[\s\S]{0,240}\bor\b/);
    expect(body).not.toMatch(/and what production is serving/);
    expect(body).not.toMatch(/deployed to production/);
    expect(body).not.toMatch(/serving production/i);
    for (const prose of [
      "// when prodGovernance === 'unknown' the slate banner renders instead of the amber one",
      '* prodGovernance === \'unknown\' && the copy must state a disjunction',
    ]) {
      expect(slateBanner.test(prose), prose).toBe(false);
    }
    expect(slateBanner.test("{prodGovernance === 'unknown' && (")).toBe(true);
  });

  it('renders the record status through the pinned label, not as a raw wire value', () => {
    expect(repositoryDetailSrc).toMatch(/recordStatusLabel\s*\(\s*repo\.status\s*\)/);
    expect(repositoryDetailSrc).not.toMatch(
      /\{\s*(?:String\(\s*)?repo\.status(?:\s*\))?\s*(?:\|\|[^}]*)?\}/,
    );
  });

  it('keeps `orgLabel`’s documented id fallback instead of discarding it', () => {
    expect(repositoryDetailSrc).toContain('orgLabel(connection, connectionId)');
    expect(repositoryDetailSrc).not.toContain('orgLabel(connection, null)');
    expect(repositoryDetailSrc).toContain('projectRead ? orgLabel(connection, connectionId) : null');
    expect(repositoryDetailSrc).toContain('const connectionId = detail?.project.connection_id || null');
  });

  it('gates Promote on the role helper and calls the ONE promote route', () => {
    expect(repositoryDetailSrc).toContain('projectsApi.promoteRepo');
    expect(repositoryDetailSrc).toContain('promotionActionMessage');
    expect(repositoryDetailSrc).not.toContain('canPromote');
    expect(repositoryDetailSrc).toContain('headerActions({');
  });

  it('carries the accessible tablist contract in ONE extracted component', () => {
    expect(tabStripSrc).toContain('role="tablist"');
    expect(tabStripSrc).toContain('role="tab"');
    expect(tabStripSrc).toContain('aria-selected');
    expect(tabStripSrc).toContain('aria-controls');
    expect(tabStripSrc).toContain('nextTabKey');
    expect(tabStripSrc).not.toContain('aria-current');
    expect(repositoryDetailSrc).toContain('TabStrip');
    expect(repositoryDetailSrc).not.toContain('role="tablist"');
    expect(repositoryDetailSrc).toContain('aria-labelledby');
    expect(repositoryDetailSrc).toContain('tabPanelId(');
  });

  it('keeps the tab button class string identical to the ProjectDetail tab strip\'s: three surfaces, one visual system', () => {
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
    for (const owned of ['DeploymentsTab', 'ResourcesTab', 'ObservabilityTab']) {
      expect(repositoryDetailSrc, owned).toContain(`from './repo-tabs/${owned}.tsx'`);
      expect(repositoryDetailSrc, owned).not.toContain(`function ${owned}(`);
    }
    expect(repositoryDetailSrc).toContain("from './repo-tabs/PullRequestsTab.tsx'");
    expect(repositoryDetailSrc).not.toContain('function PullRequestsTab(');
    expect(repositoryDetailSrc).toContain("from './repo-tabs/pullRequestsTabModel.ts'");
  });

  it('reads the pull requests HERE, because that read is also the capability probe that decides whether the tab appears', () => {
    expect(repositoryDetailSrc).toContain('pullRequestsApi.list');
    expect(repositoryDetailSrc).toContain('prTabVisibility');
    expect(repositoryDetailSrc).toMatch(/prVisibility === 'visible'/);
    for (const verb of ['pullRequestsApi.create', 'pullRequestsApi.approve', 'pullRequestsApi.merge']) {
      expect(repositoryDetailSrc, verb).not.toContain(verb);
    }
  });

  it('keeps the Pull requests tab mounted through a refetch: the probe comes from prProbeFrom, loading only before the FIRST read lands', () => {
    expect(repositoryDetailSrc).toContain('const prVisibility = prTabVisibility(prProbeFrom(prsLoad.state));');
    expect(repositoryDetailSrc).toMatch(/repoId \? pullRequestsApi\.list\(repoId\) : Promise\.resolve<PullRequestView\[\] \| null>\(null\)/);
  });

  it('never lets the capability refusal reach the page as an ERROR state', () => {
    const start = repositoryDetailSrc.search(/pullRequestsApi\s*\.\s*list\s*\(/);
    expect(start, 'the PR list read should be findable').toBeGreaterThan(-1);
    const rest = repositoryDetailSrc.slice(start);
    const end = rest.search(/\.catch\(/);
    expect(end, 'the outer .catch should follow the PR read').toBeGreaterThan(-1);
    const prBlock = rest.slice(0, end);
    expect(prBlock).toContain('prProbeFrom(prsLoad.state)');
    expect(prBlock).not.toContain('setError(');
    expect(prBlock).not.toContain('setNotFound(');
  });

  it('renders the freed step timeline from `s.label`, not from re-derived stage names', () => {
    expect(repositoryDetailSrc).toContain('{s.label}');
    expect(repositoryDetailSrc).toContain('repo.steps.map(');
  });

  it('every clickable element it adds is keyboard-reachable: real buttons and links, no onClick on a non-interactive tag', () => {
    const clickableNonInteractive = /<(tr|div|span|td|li)\b[^>]*\sonClick/;
    expect(clickableNonInteractive.test(repositoryDetailSrc)).toBe(false);
    expect(clickableNonInteractive.test(environmentStripSrc)).toBe(false);
  });

  it('renders the timeline from `label` and uses `key` only as a list key', () => {
    expect(repositoryDetailSrc).toContain('{s.label}');
    expect(repositoryDetailSrc).toMatch(/key=\{s\.key\}/);
  });

  it('introduces NO key→label map — an unknown step key must degrade to its stored label', () => {
    const SUPERSEDED_KEYS = [
      'generate_repo',
      'commit_config',
      'create_env_dev',
      'create_env_prod',
      'set_env_vars',
    ];
    const CURRENT_KEYS = ['mint_identity', 'create_repo', 'push_template', 'set_repo_vars'];
    const sources = TREE;
    expect(sources.length).toBeGreaterThan(NON_TEST_SOURCE_FLOOR);
    expect(sources.map(([p]) => p).filter((p) => p.endsWith('api/client.ts'))).toHaveLength(1);
    for (const [path, src] of sources) {
      for (const key of [...SUPERSEDED_KEYS, ...CURRENT_KEYS]) {
        expect(src, `${path} mentions the step key ${key}`).not.toContain(key);
      }
    }
    expect(SUPERSEDED_KEYS).toHaveLength(5);
    expect(CURRENT_KEYS).toHaveLength(4);
    expect(CURRENT_KEYS).toContain('create_repo');
    expect([...SUPERSEDED_KEYS, ...CURRENT_KEYS]).not.toContain('create_branch');
  });

  it('claims no stale step count anywhere, comments included: materialize has five steps, and a count written anywhere is a contract that rots', () => {
    const sources = TREE_RAW;
    expect(sources.map(([p]) => p).filter((p) => p.endsWith('api/client.ts'))).toHaveLength(1);
    const stale = /\b(eight|8)[\s-]+(background\s+|side-effecting\s+|sequential\s+|materialize\s+)?steps?\b/i;
    for (const [path, src] of sources) {
      expect(stale.test(src), `${path} still claims eight steps`).toBe(false);
    }
    expect(stale.test('this runs eight steps in the background')).toBe(true);
    expect(stale.test('The 8 background steps advance')).toBe(true);
    expect(stale.test('the 8 side-effecting steps then advance')).toBe(true);
    expect(stale.test('The 8-step timeline was visible')).toBe(true);
  });

  it('the materialize modal states NO step count at all, and stays closable mid-run', () => {
    const anyCount =
      /\b(one|two|three|four|five|six|seven|eight|nine|ten|\d+)[\s-]+(background\s+|materialize\s+)?steps?\b/i;
    const progress = addRepoModalSrc.slice(addRepoModalSrc.indexOf('if (materializing) {'));
    const copies = [
      materializeDialogCopy('r', { allDone: false, hasFailed: false }),
      materializeDialogCopy('r', { allDone: true, hasFailed: false }),
      materializeDialogCopy('r', { allDone: false, hasFailed: true }),
    ];
    expect(copies[0].description).toMatch(/background/i);
    for (const c of copies) expect(anyCount.test(`${c.title} ${c.description}`)).toBe(false);
    expect(anyCount.test(addRepoModalSrc), 'AddRepoModal.tsx claims a step count').toBe(false);
    expect(anyCount.test('this runs five steps in the background')).toBe(true);
    expect(progress.slice(0, progress.indexOf('footer='))).toContain('actionPending={false}');
  });

  it('leaves the summary counting the record’s OWN steps, never a literal', () => {
    expect(repositoryDetailSrc).toContain('repo.steps.length');
    expect(materializeSummary('ready', 8)).toEqual({ label: 'Complete', succeeded: true });
    expect(materializeSummary('ready', 5)).toEqual({ label: 'Complete', succeeded: true });
    expect(materializeSummary('provisioning', 13).label).toBe('In progress');
  });

  it('contains no AWS account id (a hard project rule), including in the strip', () => {
    const twelveDigits = /(?<!\d)\d{12}(?!\d)/;
    for (const [name, src] of [
      ['repo-tabs/OverviewTab.tsx', stripComments(overviewTabRaw)],
      ['repo-tabs/AccessTab.tsx', stripComments(accessTabRaw)],
      ['RepoSummaryCard.tsx', stripComments(repoSummaryCardRaw)],
      ['RepoFields.tsx', stripComments(repoFieldsRaw)],
      ['useRepositoryDetail.ts', stripComments(useRepositoryDetailRaw)],
      ['RepositoryDetail.tsx', repositoryDetailSrc],
      ['EnvironmentStrip.tsx', environmentStripSrc],
      ['TabStrip.tsx', tabStripSrc],
      ['repositoryDetailTabs.ts', pureSrc],
    ] as const) {
      expect(twelveDigits.test(src), `${name} contains a 12-digit literal`).toBe(false);
    }
  });
});

describe('runtimeStatusTitle', () => {
  it('returns the detail alone when there is no scope note', () => {
    expect(runtimeStatusTitle({ detail: 'runtime is deleting' })).toBe('runtime is deleting');
  });

  it('returns the scope note alone when the reading carries no detail', () => {
    expect(runtimeStatusTitle({ detail: null }, RUNTIME_SCOPE_NOTE)).toBe(RUNTIME_SCOPE_NOTE);
    expect(runtimeStatusTitle(undefined, RUNTIME_SCOPE_NOTE)).toBe(RUNTIME_SCOPE_NOTE);
  });

  it('keeps the two claims SEPARATED when both are present', () => {
    const t = runtimeStatusTitle({ detail: 'runtime is deleting' }, RUNTIME_SCOPE_NOTE);
    expect(t).toContain(RUNTIME_SCOPE_NOTE);
    expect(t).toContain('runtime is deleting');
    expect(t).toBe(`${RUNTIME_SCOPE_NOTE} — runtime is deleting`);
  });

  it('returns null — NOT an empty string — when there is nothing to say', () => {
    expect(runtimeStatusTitle(undefined)).toBeNull();
    expect(runtimeStatusTitle(null)).toBeNull();
    expect(runtimeStatusTitle({ detail: null })).toBeNull();
  });

  it('treats a blank detail as absence, not as data', () => {
    expect(runtimeStatusTitle({ detail: '   ' })).toBeNull();
    expect(runtimeStatusTitle({ detail: '' }, RUNTIME_SCOPE_NOTE)).toBe(RUNTIME_SCOPE_NOTE);
    expect(runtimeStatusTitle({ detail: '\n' })).toBeNull();
  });

  it('surfaces the distinction the status field cannot express: a recognised runtime state and a probe that never ran both read "Runtime unknown"', () => {
    const recognizedState = runtimeStatusTitle({ detail: 'runtime is deleting' });
    const probeNeverRan = runtimeStatusTitle({
      detail: 'runtime status could not be read (no Databricks reader is configured)',
    });
    expect(recognizedState).not.toBe(probeNeverRan);
    expect(recognizedState).not.toBeNull();
    expect(probeNeverRan).not.toBeNull();
  });

});

describe('runtimeStatusTitle reaches every surface that renders a runtime pill', () => {
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
      expect(src, name).toContain('runtimeStatusTitle(');
    }
  });

  it('no site renders the raw detail field as a status label', () => {
    for (const [name, src] of [
      ['RepositoryDetail', repositoryDetailSrc],
      ['EnvironmentStrip', environmentStripSrc],
      ['RepoRow', repoRowTsxSrc],
    ] as const) {
      const stripped = src;
      expect(stripped, name).not.toContain('{runtime?.detail}');
      expect(stripped, name).not.toContain('{runtime.detail}');
    }
  });
});

const CONFIG_REPO: Parameters<typeof canBuildNow>[0] = { build_kind: 'config', cicd_status: 'ready', status: 'ready' };

describe('canBuildNow — config repos only, on the gate Retry uses', () => {
  const NOW = Date.parse('2026-10-05T12:00:00Z');
  const minutesAgo = (m: number) => new Date(NOW - m * 60_000).toISOString();

  it('offers an enabled button on a materialized config repo to a maintainer', () => {
    expect(canBuildNow(CONFIG_REPO, true)).toEqual({
      visible: true,
      disabledReason: null,
      label: 'Build now',
      hint: null,
    });
  });

  it('is absent for a container repo — those build from CI on push', () => {
    expect(canBuildNow({ ...CONFIG_REPO, build_kind: 'container' }, true).visible).toBe(false);
  });

  it('is absent when build_kind is unresolved (null or missing) — never guess a kind', () => {
    expect(canBuildNow({ ...CONFIG_REPO, build_kind: null }, true).visible).toBe(false);
    expect(canBuildNow({ cicd_status: 'ready', status: 'ready' }, true).visible).toBe(false);
  });

  it('is absent for a caller without the maintainer standing (rendered, not disabled)', () => {
    expect(canBuildNow(CONFIG_REPO, false).visible).toBe(false);
    expect(canBuildNow(CONFIG_REPO, false).disabledReason).toBeNull();
  });

  it('is disabled with a reason and "Build running…" while the record says a build is running', () => {
    const r = canBuildNow(
      { ...CONFIG_REPO, cicd_status: 'promoting', last_promoted_at: minutesAgo(5) },
      true,
      false,
      NOW,
    );
    expect(r.visible).toBe(true);
    expect(r.disabledReason).toMatch(/build is already running/i);
    expect(r.label).toBe('Build running…');
  });

  it('is disabled while the history shows a build in flight the record has not named', () => {
    const r = canBuildNow({ ...CONFIG_REPO, cicd_status: 'deployed' }, true, true);
    expect(r.disabledReason).toMatch(/build is already running/i);
    expect(r.label).toBe('Build running…');
  });

  it('re-enables over a STUCK promoting record (past the 60-min bound), with a hint', () => {
    const r = canBuildNow(
      { ...CONFIG_REPO, cicd_status: 'promoting', last_promoted_at: minutesAgo(61) },
      true,
      true,
      NOW,
    );
    expect(r).toEqual({ visible: true, disabledReason: null, label: 'Build now', hint: BUILD_NOW_STUCK_HINT });
  });

  it('stays disabled just inside the bound', () => {
    const r = canBuildNow(
      { ...CONFIG_REPO, cicd_status: 'promoting', last_promoted_at: minutesAgo(59) },
      true,
      false,
      NOW,
    );
    expect(r.disabledReason).not.toBeNull();
  });

  it('reads the Build now clock: disabled inside the window, re-enabled past it', () => {
    const running = canBuildNow(
      { ...CONFIG_REPO, cicd_status: 'promoting', last_build_started_at: minutesAgo(5) },
      true,
      false,
      NOW,
    );
    expect(running.label).toBe('Build running…');
    const stuck = canBuildNow(
      { ...CONFIG_REPO, cicd_status: 'promoting', last_build_started_at: minutesAgo(61) },
      true,
      false,
      NOW,
    );
    expect(stuck.hint).toBe(BUILD_NOW_STUCK_HINT);
  });

  it('either clock inside the window means running (an old prod promote does not mask a fresh build)', () => {
    const r = canBuildNow(
      {
        ...CONFIG_REPO,
        cicd_status: 'promoting',
        last_promoted_at: minutesAgo(3 * 24 * 60),
        last_build_started_at: minutesAgo(5),
      },
      true,
      false,
      NOW,
    );
    expect(r.disabledReason).toMatch(/build is already running/i);
  });

  it('Build now on a never-promoted proposed config repo raises no production banner', () => {
    const repo = {
      last_promotion_build_id: null,
      last_promoted_at: null,
      last_promoted_image_tag: null,
      last_build_started_at: minutesAgo(5),
    };
    const s = prodServingState({
      lastPromotionBuildId: repo.last_promotion_build_id,
      lastPromotedAt: repo.last_promoted_at,
      lastPromotedImageTag: repo.last_promoted_image_tag,
      deployments: [
        deployment({ build_id: 'build-1', outcome: 'started' }),
        deployment({ build_id: 'build-1', outcome: 'succeeded' }),
      ],
    });
    expect(s).toBe('none');
    expect(prodGovernanceState({ serving: s, lifecycleState: 'proposed', agentRead: true })).toBe(
      'none',
    );
    const call = repositoryDetailSrc.slice(repositoryDetailSrc.indexOf('prodServingState({'));
    expect(call.slice(0, call.indexOf('});'))).not.toContain('last_build_started_at');
  });

  it('matches the backend at the exact bound and for a FUTURE stamp', () => {
    for (const stamp of [minutesAgo(60), minutesAgo(-5)]) {
      const r = canBuildNow({ ...CONFIG_REPO, cicd_status: 'promoting', last_promoted_at: stamp }, true, false, NOW);
      expect(r.disabledReason).toBeNull();
      expect(r.hint).toBe(BUILD_NOW_STUCK_HINT);
    }
    expect(BUILD_NOW_STUCK_HINT).not.toMatch(/hour ago/i);
  });

  it('treats a promoting record with no readable clock as stuck — the backend guard fails open too', () => {
    expect(canBuildNow({ ...CONFIG_REPO, cicd_status: 'promoting' }, true, false, NOW).hint).toBe(
      BUILD_NOW_STUCK_HINT,
    );
    expect(
      canBuildNow({ ...CONFIG_REPO, cicd_status: 'promoting', last_promoted_at: 'nope' }, true, false, NOW)
        .disabledReason,
    ).toBeNull();
  });

  it('is disabled while materialize is still running', () => {
    const r = canBuildNow({ ...CONFIG_REPO, cicd_status: 'provisioning', status: 'provisioning' }, true);
    expect(r.disabledReason).toMatch(/setup/i);
  });

  it('is disabled after a failed materialize, pointing at Retry', () => {
    const r = canBuildNow({ ...CONFIG_REPO, cicd_status: 'failed', status: 'failed' }, true);
    expect(r.disabledReason).toMatch(/retry/i);
    expect(r.disabledReason).toMatch(/setup failed/i);
    expect(r.disabledReason).not.toMatch(/materialize/i);
  });

  it('names an unfinished materialize for an unrecognized record status', () => {
    expect(canBuildNow({ ...CONFIG_REPO, status: 'weird' }, true).disabledReason).toMatch(/setup/i);
  });

  it('stays enabled after a failed DELIVERY on a materialized repo — rebuilding is the fix', () => {
    expect(canBuildNow({ ...CONFIG_REPO, cicd_status: 'failed' }, true).disabledReason).toBeNull();
  });
});

function apiFailure({ status, code, detail = '', message = detail }: { status: number; code: ErrorCode | null; detail?: string; message?: string }): ApiFailure {
  return new ApiFailure(status, code, detail, message);
}

const BUILD_REFUSAL: Record<BuildRefusalKind, ApiFailure> = {
  insufficient: apiFailure({ status: 403, code: 'project_role.insufficient', detail: 'insufficient project role' }),
  container_build: apiFailure({ status: 422, code: 'project.container_build', detail: 'container repos build from CI on push' }),
  no_commit: apiFailure({ status: 409, code: 'project.no_commit', detail: 'repo has no commit to build yet' }),
  not_ready: apiFailure({
    status: 409,
    code: 'project.not_ready',
    detail: 'this repo is still being created — try again when materialize finishes',
  }),
  materialize_failed: apiFailure({
    status: 409,
    code: 'project.materialize_failed',
    detail: "this repo's setup failed — retry the setup before building",
  }),
  in_flight: apiFailure({
    status: 409,
    code: 'project.conflict',
    detail: 'a build is already running for this agent/stage, try again shortly',
  }),
  build_failed: apiFailure({ status: 502, code: 'project.build_failed', detail: 'failed to start the build' }),
  repo_not_found: apiFailure({ status: 404, code: 'project.repo_not_found', detail: 'Repository not found' }),
};

describe('BUILD_REFUSAL_CODES — typed on the generated ErrorCode', () => {
  it('satisfies Record<BuildRefusalKind, readonly ErrorCode[]>, so a renamed backend code fails tsc where this table names it', () => {
    expectTypeOf(BUILD_REFUSAL_CODES).toExtend<Record<BuildRefusalKind, readonly ErrorCode[]>>();
  });

  it('names at least one code per kind and no code under two kinds', () => {
    const all = Object.values(BUILD_REFUSAL_CODES).flat();
    for (const codes of Object.values(BUILD_REFUSAL_CODES)) expect(codes.length).toBeGreaterThan(0);
    expect(new Set(all).size).toBe(all.length);
  });

  it('keys the two-code kinds on both codes: setup failed is the materialize refusal and the materialize error; in flight is the promote lock and the build lock', () => {
    expect(classifyBuildFailure(BUILD_REFUSAL.materialize_failed)).toBe('materialize_failed');
    expect(classifyBuildFailure(apiFailure({ status: 502, code: 'project.materialize_error', detail: 'setup failed' }))).toBe('materialize_failed');
    expect(classifyBuildFailure(BUILD_REFUSAL.in_flight)).toBe('in_flight');
    expect(
      classifyBuildFailure(apiFailure({ status: 409, code: 'project.promote_in_flight', detail: 'a promotion is already in flight' })),
    ).toBe('in_flight');
  });

  it('classifies by code alone: the detail under no code is unrecognised, a reworded detail under the code is not', () => {
    expect(classifyBuildFailure(apiFailure({ status: 404, code: null, detail: 'Repository not found' }))).toBeNull();
    expect(classifyBuildFailure(apiFailure({ status: 404, code: 'project.repo_not_found', detail: 'reworded' }))).toBe('repo_not_found');
  });
});

describe('buildNowActionMessage — the route refusals as sentences', () => {
  const cases: [BuildRefusalKind, RegExp][] = [
    ['container_build', /builds from CI on every push/],
    ['no_commit', /^This repository has no commit to build yet\./],
    ['not_ready', /^This repository is still being set up\./],
    ['materialize_failed', /setup failed, so there is nothing to build yet/],
    ['in_flight', /^A build is already running for this repository\./],
    ['build_failed', /couldn’t be started, so nothing was deployed/],
    ['repo_not_found', /no longer available/],
  ];
  for (const [kind, want] of cases) {
    it(`maps ${kind}`, () => {
      const m = buildNowActionMessage(BUILD_REFUSAL[kind], 'x');
      expect(m).toMatch(want);
      expect(m).toMatch(/^[A-Z]/);
      expect(m).not.toMatch(/promotion/i);
    });
  }

  it('says the same thing for the promote lock as for the build lock: both mean a build is running', () => {
    expect(
      buildNowActionMessage(apiFailure({ status: 409, code: 'project.promote_in_flight', detail: 'a promotion is already in flight' }), 'x'),
    ).toBe(buildNowActionMessage(BUILD_REFUSAL.in_flight, 'x'));
  });

  it('names Build now in the 403 sentence, not add/retry', () => {
    const m = buildNowActionMessage(BUILD_REFUSAL.insufficient, 'x');
    expect(m).toMatch(/Maintainer/);
    expect(m).toMatch(/build/i);
    expect(m).not.toMatch(/retry/i);
  });

  it('never surfaces a refusal\'s raw detail', () => {
    for (const failure of Object.values(BUILD_REFUSAL)) {
      expect(buildNowActionMessage(failure, 'x'), failure.message).not.toBe(failure.message);
    }
  });

  it('passes an unknown detail through as sent, and falls back on an empty one', () => {
    expect(buildNowActionMessage(new Error('something new'), 'x')).toBe('something new');
    expect(buildNowActionMessage(apiFailure({ status: 404, code: 'project.not_found', detail: 'Project not found' }), 'x')).toBe('Project not found');
    expect(buildNowActionMessage(apiFailure({ status: 404, code: null, detail: 'Repository not found' }), 'x')).toBe('Repository not found');
    expect(buildNowActionMessage(new Error('  '), 'The build could not be started.')).toBe('The build could not be started.');
    expect(buildNowActionMessage(null, 'The build could not be started.')).toBe('The build could not be started.');
  });

  it('pins the Build now success copy', () => {
    expect(BUILD_NOW_STARTED_NOTICE).toBe('Build started — the harness is usually ready in about 5 minutes');
  });

  it('names the gateway, not the harness, for an MCP server repo', () => {
    expect(buildNowStartedNotice('agent')).toBe(BUILD_NOW_STARTED_NOTICE);
    expect(buildNowStartedNotice('mcp_server')).toBe(
      'Build started — the gateway updates when the build finishes, usually in a few minutes',
    );
  });
});

describe('deliveryHeaderState — a `promoting` record names the stage its history shows running', () => {
  const DEV = 'd' + 'ev';
  const PROD = 'pr' + 'od';
  const run = (stage: string) => ({ stage, inFlight: true });
  const rest = (stage: string) => ({ stage, inFlight: false });

  it('a Build now (dev row running) reads "Deploying to dev…", never a prod claim', () => {
    const state = deliveryHeaderState({
      cicdStatus: 'promoting',
      environmentRows: [run(DEV), rest(PROD)],
      historyError: false,
    });
    expect(state).toEqual({ label: `Deploying to ${DEV}…`, inFlight: true });
    expect(state.label).not.toContain(PROD);
  });

  it('a uat rollback names uat', () => {
    expect(
      deliveryHeaderState({
        cicdStatus: 'promoting',
        environmentRows: [rest(DEV), run('uat'), rest(PROD)],
        historyError: false,
      }).label,
    ).toBe(deliveryDeployingLabel(['uat']));
  });

  it('a prod promote names prod', () => {
    expect(
      deliveryHeaderState({
        cicdStatus: 'promoting',
        environmentRows: [rest(DEV), run(PROD)],
        historyError: false,
      }).label,
    ).toBe(`Deploying to ${PROD}…`);
  });

  it('a failed history read falls back to the stage-neutral record word', () => {
    const state = deliveryHeaderState({
      cicdStatus: 'promoting',
      environmentRows: [run(DEV)],
      historyError: true,
    });
    expect(state).toEqual({ label: CICD_LABEL.promoting, inFlight: true });
    expect(CICD_LABEL.promoting).toBe('Deploying…');
  });

  it('no running row (history not loaded yet) also falls back to "Deploying…"', () => {
    expect(
      deliveryHeaderState({ cicdStatus: 'promoting', environmentRows: [rest(DEV)] }).label,
    ).toBe('Deploying…');
  });
});

describe('RepositoryDetail wires Build now through the pure gate and the buildRepo client call', () => {
  it('calls canBuildNow and projectsApi.buildRepo', () => {
    expect(repositoryDetailSrc).toContain('canBuildNow(');
    expect(repositoryDetailSrc).toContain('projectsApi.buildRepo(');
  });

  it('refetches after a started build, so the new dev history row names the stage', () => {
    const body = repositoryDetailSrc.slice(repositoryDetailSrc.indexOf('const handleBuildNow'));
    const fn = body.slice(0, body.indexOf('}, [repo, building'));
    expect(fn).toContain('refetch();');
  });
});

describe('MCP-server repos — tabs, header link, gateway status, no promote', () => {
  it('a record with no artifact_kind is an agent', () => {
    expect(repoKindOf({})).toBe('agent');
    expect(repoKindOf(null)).toBe('agent');
    expect(repoKindOf({ artifact_kind: 'mcp_server' })).toBe('mcp_server');
  });

  it('agent tabs are unchanged; an MCP repo has no Observability tab', () => {
    expect(selectableTabKeys('agent')).toEqual(selectableTabKeys());
    expect(selectableTabKeys('mcp_server')).toEqual(
      selectableTabKeys().filter((k) => k !== 'observability'),
    );
    expect(isTabSelectable('observability', 'mcp_server')).toBe(false);
    expect(isTabSelectable('observability')).toBe(true);
    expect(tabsForKind('mcp_server').map((t) => t.key)).toContain('deployments');
  });

  it('the header links to the MCP record for an MCP repo and to the agent otherwise', () => {
    expect(repoArtifactLink({ artifact_kind: 'mcp_server', agent_id: null, mcp_server_id: 'mcp-1' })).toEqual({
      label: 'MCP server', to: '/mcp-servers/mcp-1', id: 'mcp-1',
    });
    expect(repoArtifactLink({ artifact_kind: 'agent', agent_id: 'a-1', mcp_server_id: null })).toEqual({
      label: 'Agent', to: '/agents/a-1', id: 'a-1',
    });
  });

  it('gateway status reads the write-back ARN', () => {
    expect(gatewayStatus({ gateway_arn: null })).toEqual({ deployed: false, label: GATEWAY_NOT_BUILT_LABEL });
    expect(gatewayStatus({ gateway_arn: 'arn:aws:bedrock-agentcore:us-east-1:000000000000:gateway/g-1' })).toEqual({
      deployed: true, label: GATEWAY_DEPLOYED_LABEL,
    });
  });

  it('never offers Promote on an MCP repo, even to an owner with a candidate', () => {
    const agentMay = headerActions({ ...OWNER_INPUT, steps: [] });
    expect(agentMay.promote).toBe(true);
    const mcpMay = headerActions({ ...OWNER_INPUT, steps: [], artifactKind: 'mcp_server' });
    expect(mcpMay).toEqual({ ...agentMay, promote: false });
  });

  it('the page wires the kind through', () => {
    const s = repositoryDetailSrc;
    expect(s).toMatch(/tabsForKind\(/);
    expect(s).toMatch(/repoArtifactLink\(/);
    expect(s).toMatch(/projectsApi\.repoDeployments\(/);
    expect(s).toMatch(/artifactKind:/);
  });
});

describe('rowActionForKind / offersRollback / repoKindChip: an MCP-server repo has one gateway and no prod promotion', () => {
  const mcp = { artifact_kind: 'mcp_server', gateway_arn: null } as const;
  const agent = { artifact_kind: 'agent', gateway_arn: null } as const;
  const withGateway = { ...mcp, gateway_arn: 'arn:aws:bedrock-agentcore:us-east-1:000000000000:gateway/g-1' };

  it('an agent row keeps its action verbatim', () => {
    for (const action of ['approval-pending', 'drift', 'never-deployed', 'none', 'retry'] as const) {
      expect(rowActionForKind(agent, action)).toBe(action);
    }
  });

  it('an MCP repo turns the prod-shaped answers into never-deployed until a gateway exists, and nothing after', () => {
    for (const action of ['approval-pending', 'drift', 'never-deployed'] as const) {
      expect(rowActionForKind(mcp, action)).toBe('never-deployed');
      expect(rowActionForKind(withGateway, action)).toBe('none');
    }
    expect(rowActionForKind(mcp, 'retry')).toBe('retry');
  });

  it('offers rollback on an agent repo and never on an MCP repo, whose route answers 422', () => {
    expect(offersRollback(agent)).toBe(true);
    expect(offersRollback({})).toBe(true);
    expect(offersRollback(mcp)).toBe(false);
  });

  it('chips only an MCP-server row, so agent rows render as before', () => {
    expect(repoKindChip(mcp)).toBe(MCP_KIND_CHIP_LABEL);
    expect(repoKindChip(agent)).toBeNull();
    expect(repoKindChip({})).toBeNull();
  });
});

describe('deliveryFailureReason — the header "Delivery failed" chip says why', () => {
  it('is null unless the delivery failed', () => {
    expect(deliveryFailureReason('deployed', [deployment({ outcome: 'failed', error: 'x' })])).toBeNull();
  });
  it('reads the newest failed attempt’s recorded error', () => {
    expect(
      deliveryFailureReason('failed', [
        deployment({ id: 'old', outcome: 'failed', started_at: '2026-10-05T10:00:00Z', error: 'old cause' }),
        deployment({ id: 'new', outcome: 'failed', started_at: '2026-10-06T10:00:00Z', error: 'the gateway build failed' }),
      ]),
    ).toBe('The gateway build failed (uat)');
  });
  it('falls back to a plain sentence when no failed attempt recorded a cause', () => {
    expect(deliveryFailureReason('failed', [])).toBe('The last build or deployment failed');
  });
});
