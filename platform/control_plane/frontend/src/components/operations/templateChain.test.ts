import { describe, expect, it } from 'vitest';

import {
  ARTIFACT_KIND_OPTIONS,
  BUILD_KIND_CHIP,
  DEFAULT_INFRA_TEMPLATE_ID,
  DEFAULT_PLATFORM,
  INFRA_CHIP,
  INFRA_TAB_EMPTY,
  INFRA_TAB_SUBTITLE,
  MCP_DESCRIPTION_MAX,
  PLATFORM_CHIP,
  PREVIEW_CHIP,
  buildKindLabel,
  ensuredInfraIds,
  materializeDialogCopy,
  queuedTemplateInfra,
  templateKindLabel,
  withInfraSeeds,
  frameworkForTemplate,
  infraBodyState,
  infraRolloutRows,
  kindEmptyReason,
  mcpDescriptionError,
  mcpNameError,
  mcpNameHint,
  overwriteInfraConsent,
  overwriteInfraTargets,
  pickTemplateName,
  pickerEmptyReason,
  selectedTemplate,
  templateOptionLabel,
  templatesForKind,
  templatesForPlatform,
  templateKindOf,
  tenantPlatformOf,
  type TemplateLike,
} from './templateChain';
import addRepoModalSrc from './AddRepoModal.tsx?raw';
import templateCardGridSrc from './TemplateCardGrid.tsx?raw';
import rolloutModalSrc from './RolloutTemplatesModal.tsx?raw';
import templateChainSrc from './templateChain.ts?raw';
import { stripComments } from '../../rawSource';

const tpl = (over: Partial<TemplateLike> = {}): TemplateLike => ({
  name: 'strands-agentcore',
  framework: 'strands',
  platform: 'aws',
  build_kind: 'container',
  preview: false,
  infra_template_id: 'agentcore-runtime',
  ...over,
});

const AWS_STRANDS = tpl();
const AWS_LANGGRAPH = tpl({ name: 'langgraph-agentcore', framework: 'langgraph' });
const DBX_HARNESS = tpl({
  name: 'databricks-harness',
  framework: 'harness',
  platform: 'databricks',
  build_kind: 'config',
  preview: true,
  infra_template_id: 'databricks-agent',
});

describe('tenantPlatformOf — absent and blank are the SAME answer', () => {
  it('folds a missing field, null, undefined and blank all to aws', () => {
    expect(tenantPlatformOf(null)).toBe(DEFAULT_PLATFORM);
    expect(tenantPlatformOf(undefined)).toBe(DEFAULT_PLATFORM);
    expect(tenantPlatformOf({})).toBe(DEFAULT_PLATFORM);
    expect(tenantPlatformOf({ platform: undefined })).toBe(DEFAULT_PLATFORM);
    expect(tenantPlatformOf({ platform: '' })).toBe(DEFAULT_PLATFORM);
  });

  it('mirrors the backend EXACTLY — whitespace passes through untrimmed, so both halves of the seam agree', () => {
    expect(tenantPlatformOf({ platform: ' databricks ' })).toBe(' databricks ');
    expect(tenantPlatformOf({ platform: '   ' })).toBe('   ');
  });

  it('never overrides a REAL platform, including one this build has not been taught', () => {
    expect(tenantPlatformOf({ platform: 'databricks' })).toBe('databricks');
    expect(tenantPlatformOf({ platform: 'gcp' })).toBe('gcp');
  });

  it('agrees with the value the backend defaults to', () => {
    expect(DEFAULT_PLATFORM).toBe('aws');
  });
});

describe('templatesForPlatform — an exact match, and nothing widens it', () => {
  const CATALOG = [AWS_STRANDS, DBX_HARNESS, AWS_LANGGRAPH];

  it('offers only the templates whose infra targets the tenant’s platform', () => {
    expect(templatesForPlatform(CATALOG, 'aws').map((t) => t.name)).toEqual([
      'strands-agentcore',
      'langgraph-agentcore',
    ]);
    expect(templatesForPlatform(CATALOG, 'databricks').map((t) => t.name)).toEqual([
      'databricks-harness',
    ]);
  });

  it('does NOT treat aws as a wildcard', () => {
    expect(templatesForPlatform([DBX_HARNESS], 'aws')).toEqual([]);
    expect(templatesForPlatform([AWS_STRANDS], 'databricks')).toEqual([]);
  });

  it('answers empty for a platform nothing targets, rather than falling back to all', () => {
    expect(templatesForPlatform(CATALOG, 'gcp')).toEqual([]);
    expect(templatesForPlatform(CATALOG, 'gcp').length).not.toBe(CATALOG.length);
  });

  it('preserves the API’s own order', () => {
    const reordered = templatesForPlatform([AWS_LANGGRAPH, AWS_STRANDS], 'aws');
    expect(reordered.map((t) => t.name)).toEqual(['langgraph-agentcore', 'strands-agentcore']);
  });

  it('is empty-safe', () => {
    expect(templatesForPlatform([], 'aws')).toEqual([]);
  });
});

describe('pickTemplateName — sticky, but it DROPS a selection the filter removed', () => {
  const OFFERED = [AWS_STRANDS, AWS_LANGGRAPH];

  it('keeps a selection that is still offered', () => {
    expect(pickTemplateName(OFFERED, 'langgraph-agentcore')).toBe('langgraph-agentcore');
  });

  it('drops a selection the platform filter no longer offers', () => {
    expect(pickTemplateName(OFFERED, 'databricks-harness')).toBe('strands-agentcore');
  });

  it('falls back to the first offered template, then to empty', () => {
    expect(pickTemplateName(OFFERED, '')).toBe('strands-agentcore');
    expect(pickTemplateName([], 'anything')).toBe('');
    expect(pickTemplateName([], '')).toBe('');
  });
});

describe('frameworkForTemplate — the SELECTED TEMPLATE’s framework, never a constant', () => {
  const OFFERED = [AWS_STRANDS, AWS_LANGGRAPH, DBX_HARNESS];

  it('answers each template’s own framework', () => {
    expect(frameworkForTemplate(OFFERED, 'strands-agentcore')).toBe('strands');
    expect(frameworkForTemplate(OFFERED, 'langgraph-agentcore')).toBe('langgraph');
    expect(frameworkForTemplate(OFFERED, 'databricks-harness')).toBe('harness');
  });

  it('is not a constant — the answers actually differ', () => {
    const answers = new Set(OFFERED.map((t) => frameworkForTemplate(OFFERED, t.name)));
    expect(answers.size).toBe(OFFERED.length);
    expect(answers.has('strands')).toBe(true);
  });

  it('answers UNKNOWN (empty) for an unresolvable selection, not a default', () => {
    expect(frameworkForTemplate(OFFERED, 'nope')).toBe('');
    expect(frameworkForTemplate(OFFERED, '')).toBe('');
    expect(frameworkForTemplate([], 'strands-agentcore')).toBe('');
  });

  it('carries a template whose catalog framework is blank through as blank', () => {
    const legacy = tpl({ name: 'legacy', framework: '' });
    expect(frameworkForTemplate([legacy], 'legacy')).toBe('');
  });
});

describe('selectedTemplate', () => {
  it('resolves the record the chip row renders, or null', () => {
    expect(selectedTemplate([AWS_STRANDS, DBX_HARNESS], 'databricks-harness')).toBe(DBX_HARNESS);
    expect(selectedTemplate([AWS_STRANDS], 'missing')).toBeNull();
    expect(selectedTemplate([], '')).toBeNull();
  });
});

describe('buildKindLabel — the two kinds, and an unknown one shown rather than hidden', () => {
  it('names container and config', () => {
    expect(buildKindLabel('container')).toBe('Container');
    expect(buildKindLabel('config')).toBe('Config');
  });

  it('shows an unknown kind raw instead of an em-dash', () => {
    expect(buildKindLabel('wheelbarrow')).toBe('wheelbarrow');
    expect(buildKindLabel('')).toBe('');
  });

  it('cannot be fooled by a prototype member arriving as the build kind', () => {
    for (const key of ['toString', 'constructor', 'hasOwnProperty', '__proto__']) {
      expect(typeof buildKindLabel(key), key).toBe('string');
      expect(buildKindLabel(key), key).toBe(key);
    }
  });
});

describe('templateOptionLabel — the badges, as option TEXT', () => {
  it('carries the name and the build kind', () => {
    expect(templateOptionLabel(AWS_STRANDS)).toBe('strands-agentcore · Container');
  });

  it('appends Preview only for a preview template', () => {
    expect(templateOptionLabel(DBX_HARNESS)).toBe('databricks-harness · Config · Preview');
    expect(templateOptionLabel(tpl({ preview: false }))).not.toMatch(/Preview/);
  });

  it('always begins with the name', () => {
    for (const t of [AWS_STRANDS, AWS_LANGGRAPH, DBX_HARNESS]) {
      expect(templateOptionLabel(t).startsWith(t.name), t.name).toBe(true);
    }
  });

  it('keeps the four chip tokens distinct where the distinction is the point', () => {
    expect(PREVIEW_CHIP).not.toBe(BUILD_KIND_CHIP);
    expect(PREVIEW_CHIP).not.toBe(PLATFORM_CHIP);
    expect(PREVIEW_CHIP).not.toBe(INFRA_CHIP);
    expect(PREVIEW_CHIP).toMatch(/amber/);
    expect(BUILD_KIND_CHIP).toBe(PLATFORM_CHIP);
  });
});

describe('pickerEmptyReason — two empty pickers, two different fixes', () => {
  it('says nothing at all when there IS something to offer', () => {
    expect(pickerEmptyReason(3, 2, 'aws')).toBeNull();
  });

  it('tells an empty CATALOG to roll templates out', () => {
    const reason = pickerEmptyReason(0, 0, 'aws');
    expect(reason).toMatch(/No templates in this org/i);
    expect(reason).toMatch(/roll out/i);
  });

  it('tells a PLATFORM MISMATCH something else entirely, and names the platform', () => {
    const reason = pickerEmptyReason(4, 0, 'databricks');
    expect(reason).toMatch(/4 templates/);
    expect(reason).toMatch(/databricks/);
    expect(reason).not.toMatch(/No templates in this org/i);
    expect(reason).toMatch(/infra template/i);
  });

  it('says "1 template" rather than "1 templates"', () => {
    expect(pickerEmptyReason(1, 0, 'aws')).toMatch(/1 template,/);
  });

  it('gives the two cases genuinely different sentences', () => {
    expect(pickerEmptyReason(0, 0, 'aws')).not.toBe(pickerEmptyReason(2, 0, 'aws'));
  });
});

describe('infraBodyState — loading outranks error, error outranks empty', () => {
  it('reports loading even when a previous failure is still held', () => {
    expect(infraBodyState({ loading: true, error: 'boom', count: 0 })).toBe('loading');
    expect(infraBodyState({ loading: true, error: null, count: 3 })).toBe('loading');
  });

  it('reports the error rather than an empty catalog', () => {
    expect(infraBodyState({ loading: false, error: 'boom', count: 0 })).toBe('error');
  });

  it('reports empty only for a SUCCESSFUL read of zero rows', () => {
    expect(infraBodyState({ loading: false, error: null, count: 0 })).toBe('empty');
  });

  it('reports the list when there is one', () => {
    expect(infraBodyState({ loading: false, error: null, count: 2 })).toBe('list');
  });

  it('is total — every input maps to exactly one state', () => {
    for (const loading of [true, false]) {
      for (const error of ['boom', null]) {
        for (const count of [0, 1]) {
          const state = infraBodyState({ loading, error, count });
          expect(['loading', 'error', 'empty', 'list'], `${loading}/${error}/${count}`).toContain(
            state,
          );
        }
      }
    }
  });

  it('states that the infra catalog is READ-ONLY, and does not instruct an upload', () => {
    expect(INFRA_TAB_SUBTITLE).toMatch(/read-only/i);
    expect(INFRA_TAB_EMPTY).toMatch(/roll out/i);
    expect(INFRA_TAB_EMPTY).not.toMatch(/upload/i);
    expect(INFRA_TAB_SUBTITLE).not.toMatch(/upload/i);
  });
});

describe('ensuredInfraIds — the backend’s reference rule, mirrored', () => {
  it('always includes the default mechanism, even with no registered templates', () => {
    expect([...ensuredInfraIds([])]).toEqual([DEFAULT_INFRA_TEMPLATE_ID]);
    expect(DEFAULT_INFRA_TEMPLATE_ID).toBe('agentcore-runtime');
  });

  it('adds every id a registered agent template deploys through', () => {
    const ids = ensuredInfraIds([
      { infra_template_id: 'databricks-agent' },
      { infra_template_id: 'agentcore-harness' },
      { infra_template_id: 'databricks-agent' },
    ]);
    expect([...ids].sort()).toEqual(['agentcore-harness', 'agentcore-runtime', 'databricks-agent']);
  });

  it('folds a blank or missing reference to the default, not to nothing', () => {
    expect([...ensuredInfraIds([{ infra_template_id: '' }])]).toEqual([DEFAULT_INFRA_TEMPLATE_ID]);
    expect([...ensuredInfraIds([{}])]).toEqual([DEFAULT_INFRA_TEMPLATE_ID]);
  });

  it('does NOT include a mechanism merely because the catalog lists it', () => {
    const ids = ensuredInfraIds([{ infra_template_id: 'agentcore-runtime' }]);
    expect(ids.has('agentcore-harness')).toBe(false);
    expect(ids.has('databricks-agent')).toBe(false);
  });
});

describe('infraRolloutRows — the CATALOG enumerates, the reconcile read annotates', () => {
  const CATALOG = [
    {
      id: 'agentcore-runtime',
      name: 'AgentCore runtime',
      platform: 'aws',
      iac_type: 'terraform',
      build_kind: 'container',
      repo_name: 'agp-runtime-infra',
    },
    {
      id: 'databricks-agent',
      name: 'Databricks agent',
      platform: 'databricks',
      iac_type: 'terraform',
      build_kind: 'config',
      repo_name: 'agp-databricks-infra',
    },
  ];
  const probe = {
    name: 'agp-runtime-infra',
    state: 'unregistered_present',
    default_branch: 'main',
    head_sha: 'abc1234',
  };

  it('flags ONLY the mechanisms a roll-out will actually ensure', () => {
    const registeredOnlyRuntime = [{ infra_template_id: 'agentcore-runtime' }];
    const rows = infraRolloutRows(
      CATALOG,
      probe,
      ensuredInfraIds(registeredOnlyRuntime),
    );
    expect(rows.find((r) => r.id === 'agentcore-runtime')?.ensured).toBe(true);
    expect(rows.find((r) => r.id === 'databricks-agent')?.ensured).toBe(false);
  });

  it('flags a mechanism as ensured as soon as a registered template deploys through it', () => {
    const rows = infraRolloutRows(
      CATALOG,
      probe,
      ensuredInfraIds([{ infra_template_id: 'databricks-agent' }]),
    );
    expect(rows.every((r) => r.ensured)).toBe(true);
  });

  it('UNDER-claims when the agent catalog is unavailable, never over-claims', () => {
    const rows = infraRolloutRows(CATALOG, probe);
    expect(rows.find((r) => r.id === 'agentcore-runtime')?.ensured).toBe(true);
    expect(rows.find((r) => r.id === 'databricks-agent')?.ensured).toBe(false);
  });

  it('does not claim an orphan repo will be ensured', () => {
    const orphan = { name: 'agp-mystery-infra', state: 'unregistered_present' };
    const rows = infraRolloutRows(CATALOG, orphan, ensuredInfraIds([]));
    expect(rows.find((r) => r.repoName === 'agp-mystery-infra')?.ensured).toBe(false);
  });

  it('the DEGRADED infra-catalog read still reports the default repo as ensured', () => {
    const probeAbsent = { name: 'agp-runtime-infra', state: 'seed_absent' };
    const degraded = infraRolloutRows([], probeAbsent, ensuredInfraIds([]));
    expect(degraded).toHaveLength(1);
    expect(degraded[0].ensured).toBe(true);
    expect(infraRolloutRows([], probeAbsent)[0].ensured).toBe(true);
    expect(
      infraRolloutRows([], probeAbsent, ensuredInfraIds([{ infra_template_id: 'x' }]))[0].ensured,
    ).toBe(true);
  });

  it('discriminates the two orphan causes on catalog EMPTINESS, not on the repo name', () => {
    const same = { name: 'agp-runtime-infra', state: 'unregistered_present' };
    expect(infraRolloutRows([], same, ensuredInfraIds([]))[0].ensured).toBe(true);
    const readCatalog = [
      {
        id: 'databricks-agent',
        name: 'Databricks agent',
        platform: 'databricks',
        iac_type: 'terraform',
        build_kind: 'config',
        repo_name: 'agp-databricks-infra',
      },
    ];
    const genuine = infraRolloutRows(readCatalog, same, ensuredInfraIds([]));
    expect(genuine.find((r) => r.repoName === 'agp-runtime-infra')?.ensured).toBe(false);
  });

  it('yields one row per REGISTERED infra template, not one hardcoded repo', () => {
    const rows = infraRolloutRows(CATALOG, probe);
    expect(rows.map((r) => r.repoName)).toEqual(['agp-runtime-infra', 'agp-databricks-infra']);
    expect(rows.map((r) => r.id)).toEqual(['agentcore-runtime', 'databricks-agent']);
  });

  it('attaches the reconcile evidence to the matching row and to NO other', () => {
    const rows = infraRolloutRows(CATALOG, probe);
    expect(rows[0].reconcile).toBe(probe);
    expect(rows[1].reconcile).toBeNull();
  });

  it('carries each row’s mechanism, so a multi-row section is readable', () => {
    const rows = infraRolloutRows(CATALOG, probe);
    expect(rows[1].platform).toBe('databricks');
    expect(rows[1].buildKind).toBe('config');
    expect(rows[1].iacType).toBe('terraform');
  });

  it('still lists an infra repo the WIRE reports and the catalog does not name', () => {
    const orphan = { name: 'agp-mystery-infra', state: 'seed_absent' };
    const rows = infraRolloutRows(CATALOG, orphan);
    expect(rows.map((r) => r.repoName)).toContain('agp-mystery-infra');
    const row = rows.find((r) => r.repoName === 'agp-mystery-infra');
    expect(row?.reconcile).toBe(orphan);
    expect(row?.platform).toBe('');
    expect(row?.buildKind).toBe('');
    expect(row?.id).toBe('agp-mystery-infra');
  });

  it('does not duplicate a repo that IS in the catalog', () => {
    const rows = infraRolloutRows(CATALOG, probe);
    expect(rows.filter((r) => r.repoName === 'agp-runtime-infra')).toHaveLength(1);
  });

  it('degrades to the wire’s single row when the catalog read failed (empty)', () => {
    const rows = infraRolloutRows([], probe);
    expect(rows).toHaveLength(1);
    expect(rows[0].repoName).toBe('agp-runtime-infra');
    expect(rows[0].reconcile).toBe(probe);
  });

  it('yields nothing at all when there is neither a catalog nor a probe', () => {
    expect(infraRolloutRows([], null)).toEqual([]);
    expect(infraRolloutRows([], undefined)).toEqual([]);
  });

  it('lists the catalog even with no reconcile evidence at all', () => {
    const rows = infraRolloutRows(CATALOG, null);
    expect(rows).toHaveLength(2);
    expect(rows.every((r) => r.reconcile === null)).toBe(true);
  });
});

describe('overwriteInfraTargets — every repo the ONE tick writes to', () => {
  const CATALOG = [
    {
      id: 'agentcore-runtime',
      name: 'AgentCore runtime',
      platform: 'aws',
      iac_type: 'terraform',
      build_kind: 'container',
      repo_name: 'agp-runtime-infra',
    },
    {
      id: 'agentcore-harness',
      name: 'AgentCore harness',
      platform: 'aws',
      iac_type: 'agentcore_harness',
      build_kind: 'config',
      repo_name: 'agp-harness-infra',
    },
  ];
  const probe = {
    name: 'agp-runtime-infra',
    state: 'registered_present',
    default_branch: 'main',
    head_sha: 'abc1234',
  };

  it('names BOTH repos when a registered harness template makes both ensured', () => {
    const registered = [
      { infra_template_id: 'agentcore-runtime' },
      { infra_template_id: 'agentcore-harness' },
    ];
    const rows = infraRolloutRows(CATALOG, probe, ensuredInfraIds(registered));
    expect(overwriteInfraTargets(rows)).toEqual(['agp-runtime-infra', 'agp-harness-infra']);
  });

  it('excludes a catalogued mechanism no roll-out will ensure', () => {
    const rows = infraRolloutRows(CATALOG, probe, ensuredInfraIds([{ infra_template_id: 'agentcore-runtime' }]));
    expect(overwriteInfraTargets(rows)).toEqual(['agp-runtime-infra']);
  });

  it('under-claims to the default alone when the agent catalog could not be read', () => {
    expect(overwriteInfraTargets(infraRolloutRows(CATALOG, probe))).toEqual(['agp-runtime-infra']);
  });

  it('answers NOTHING when the INFRA catalog was not read, rather than one name', () => {
    const degraded = infraRolloutRows([], probe, ensuredInfraIds([]));
    expect(degraded).toHaveLength(1);
    expect(degraded[0].ensured).toBe(true);
    expect(degraded[0].catalogued).toBe(false);
    expect(overwriteInfraTargets(degraded)).toEqual([]);
    expect(overwriteInfraTargets(infraRolloutRows([], probe))).toEqual([]);
    expect(
      overwriteInfraTargets(
        infraRolloutRows([], probe, ensuredInfraIds([{ infra_template_id: 'agentcore-harness' }])),
      ),
    ).toEqual([]);
  });

  it('still names the read catalog’s targets when the orphan row is a GENUINE orphan', () => {
    const orphan = { name: 'agp-mystery-infra', state: 'unregistered_present' };
    const rows = infraRolloutRows(CATALOG, orphan, ensuredInfraIds([]));
    expect(rows.find((r) => r.repoName === 'agp-mystery-infra')?.catalogued).toBe(false);
    expect(overwriteInfraTargets(rows)).toEqual(['agp-runtime-infra']);
  });

  it('marks every row the CATALOG enumerated as catalogued', () => {
    const rows = infraRolloutRows(CATALOG, probe, ensuredInfraIds([]));
    expect(rows.every((r) => r.catalogued)).toBe(true);
  });
});

describe('overwriteInfraConsent — the checkbox label states its real blast radius', () => {
  it('names the count AND the repositories when there is more than one', () => {
    const copy = overwriteInfraConsent(['agp-runtime-infra', 'agp-harness-infra']);
    expect(copy).toContain('agp-runtime-infra');
    expect(copy).toContain('agp-harness-infra');
    expect(copy).toMatch(/all 2 infra repositories/);
    expect(copy).toMatch(/not just the row this box sits on/i);
    expect(copy).not.toMatch(/this existing repository/i);
  });

  it('stays singular when only one repository is in scope, without dropping the rule', () => {
    const copy = overwriteInfraConsent(['agp-runtime-infra']);
    expect(copy).toContain('agp-runtime-infra');
    expect(copy).not.toMatch(/all 1/);
    expect(copy).not.toMatch(/this existing repository/i);
    expect(copy).toMatch(/already has it/i);
    expect(copy).not.toMatch(/not just the row/i);
  });

  it('falls back to the RULE, not to the singular sentence, when no target is known', () => {
    const copy = overwriteInfraConsent([]);
    expect(copy).toMatch(/every infra repository this roll-out ensures/i);
    expect(copy).not.toMatch(/this existing repository/i);
    expect(copy).toMatch(/that already exists/i);
  });

  it('reaches that rule branch from the DEGRADED read’s real rows, not just from a bare []', () => {
    const present = {
      name: 'agp-runtime-infra',
      state: 'unregistered_present',
      default_branch: 'main',
      head_sha: 'abc1234',
    };
    const copy = overwriteInfraConsent(
      overwriteInfraTargets(infraRolloutRows([], present, ensuredInfraIds([]))),
    );
    expect(copy).toMatch(/every infra repository this roll-out ensures/i);
    expect(copy).not.toContain('agp-runtime-infra');
    expect(copy).not.toMatch(/if this org already has it/i);
    expect(copy).toMatch(/that already exists/i);
    expect(copy).toMatch(/one commit per repository/i);
    expect(copy).not.toBe(overwriteInfraConsent(['agp-runtime-infra']));
  });

  it('keeps the two calming facts the old copy earned — per-repo commit, and idempotence', () => {
    const copy = overwriteInfraConsent(['agp-runtime-infra', 'agp-harness-infra']);
    expect(copy).toMatch(/one commit per repository/i);
    expect(copy).toMatch(/writes no commit at all/i);
    expect(copy).toMatch(/history stays/i);
  });

  it('promises a re-push only for a repo that ALREADY EXISTS', () => {
    expect(overwriteInfraConsent(['a', 'b'])).toMatch(/each one that already exists/i);
    expect(overwriteInfraConsent(['a'])).toMatch(/already/i);
    expect(overwriteInfraConsent([])).toMatch(/already/i);
  });
});

describe('AddRepoModal submits the SELECTED TEMPLATE’s framework, never a literal', () => {
  const src = stripComments(addRepoModalSrc);

  it('names no framework literal anywhere in the modal; the needle is concatenated so this file does not carry it', () => {
    expect(src.length).toBeGreaterThan(2000);
    const literal = new RegExp(`['"\`]${'stra' + 'nds'}['"\`]`);
    expect(literal.test(src)).toBe(false);
    expect(literal.test(`framework: '${'stra' + 'nds'}',`)).toBe(true);
    expect(literal.test(`value="${'stra' + 'nds'}"`)).toBe(true);
  });

  it('binds the payload’s framework to the derived value', () => {
    expect(src).toMatch(/frameworkForTemplate\(/);
    const payload = src.slice(src.indexOf('agent_config: {'), src.indexOf('business_unit:'));
    expect(payload.length).toBeGreaterThan(40);
    expect(payload).toMatch(/\bframework,/);
  });

  it('derives the framework from the PLATFORM-FILTERED list, not the whole catalog', () => {
    expect(src).toMatch(/frameworkForTemplate\(offered,/);
    expect(src).toMatch(/templatesForPlatform\(/);
  });

  it('keeps the framework field READ-ONLY while making it derived', () => {
    const field = src.slice(src.indexOf('add-repo-framework'), src.indexOf('add-repo-model'));
    expect(field.length).toBeGreaterThan(80);
    expect(field).toMatch(/readOnly/);
    expect(field).toMatch(/value=\{framework/);
  });

  it('filters the picker options and resolves the selection against the same subset', () => {
    expect(src).toMatch(/\{offered\.map\(/);
    expect(src).toMatch(/pickTemplateName\(offered, templateName\)/);
    expect(src).toMatch(/templatesForPlatform\(templates, tenantPlatform\)/);
    expect(src).toMatch(/templatesForKind\(platformOffered, kind\)/);
  });

  it('gates the submit on a RESOLVED template, not merely a non-empty name', () => {
    const gate = src.slice(src.indexOf('const canSubmit'), src.indexOf('serializeOverrides'));
    expect(gate.length).toBeGreaterThan(40);
    expect(gate).toMatch(/chosen !== null/);
    expect(gate).not.toMatch(/templateName\.length/);
  });

  it('renders the build-kind and preview badges from the wire fields', () => {
    expect(src).toMatch(/templateOptionLabel\(/);
    expect(src).toMatch(/buildKindLabel\(/);
    expect(src).toMatch(/chosen\.preview/);
    expect(src).toMatch(/chosen\.infra_template_id/);
  });
});

describe('TemplateCardGrid chips the chain', () => {
  const src = stripComments(templateCardGridSrc);

  it('renders platform, build kind and the infra template id on every card', () => {
    expect(src.length).toBeGreaterThan(1000);
    expect(src).toMatch(/t\.platform/);
    expect(src).toMatch(/buildKindLabel\(t\.build_kind\)/);
    expect(src).toMatch(/t\.infra_template_id/);
  });

  it('chips PREVIEW rather than hiding a preview template', () => {
    expect(src).toMatch(/t\.preview/);
    expect(src).toMatch(/Preview/);
    expect(src).not.toMatch(/filter\(.*preview/i);
  });

  it('uses the SHARED chip tokens rather than re-declaring the tints', () => {
    expect(src).toMatch(/from '\.\/templateChain\.ts'/);
    expect(src).toMatch(/\bPLATFORM_CHIP\b/);
    expect(src).toMatch(/\bBUILD_KIND_CHIP\b/);
  });
});

describe('templatesForKind — the Agent | MCP server picker', () => {
  const agent = tpl({ name: 'strands-agentcore', artifact_kind: 'agent' });
  const legacy = tpl({ name: 'legacy' });
  const mcp = tpl({ name: 'gateway-agentcore', artifact_kind: 'mcp_server', build_kind: 'config' });

  it('a template with no artifact_kind is an agent', () => {
    expect(templateKindOf(legacy)).toBe('agent');
    expect(templatesForKind([legacy], 'agent')).toEqual([legacy]);
    expect(templatesForKind([legacy], 'mcp_server')).toEqual([]);
  });

  it('splits the catalog by kind, preserving order', () => {
    const all = [mcp, agent, legacy];
    expect(templatesForKind(all, 'agent').map((t) => t.name)).toEqual(['strands-agentcore', 'legacy']);
    expect(templatesForKind(all, 'mcp_server').map((t) => t.name)).toEqual(['gateway-agentcore']);
  });

  it('composes with the platform filter', () => {
    const dbx = tpl({ name: 'dbx-mcp', artifact_kind: 'mcp_server', platform: 'databricks' });
    expect(templatesForKind(templatesForPlatform([mcp, dbx, agent], 'aws'), 'mcp_server')).toEqual([mcp]);
  });

  it('names the kind in the empty-picker sentence', () => {
    expect(kindEmptyReason('mcp_server')).toMatch(/No MCP server templates/);
    expect(kindEmptyReason('agent')).toMatch(/No agent templates/);
  });

  it('lists Agent first, the kind the picker opens on', () => {
    expect(ARTIFACT_KIND_OPTIONS[0].kind).toBe('agent');
  });
});

describe('mcpNameError — the backend MCP name rule, mirrored', () => {
  it.each(['ab', 'claims-api', 'a1', 'a' + 'b'.repeat(30)])('accepts %s', (n) => {
    expect(mcpNameError(n)).toBeNull();
  });
  it.each(['', 'a', 'Claims', '1abc', 'claims_api', 'claims api', 'a' + 'b'.repeat(31)])('refuses %s', (n) => {
    expect(mcpNameError(n)).not.toBeNull();
  });
  it('caps the description at the registry limit', () => {
    expect(MCP_DESCRIPTION_MAX).toBe(100);
  });
});

describe('AddRepoModal offers the kind picker', () => {
  const src = stripComments(addRepoModalSrc);

  it('filters the offered templates by kind as well as platform', () => {
    expect(src).toMatch(/templatesForKind\(/);
    expect(src).toMatch(/ARTIFACT_KIND_OPTIONS/);
    expect(src).toMatch(/initialKind = 'agent'/);
    expect(src).toMatch(/useState<ArtifactKind>\(initialKind\)/);
  });

  it('sends mcp_name for an MCP server and keeps the agent payload for an agent', () => {
    expect(src).toMatch(/mcp_name:/);
    expect(src).toMatch(/mcpNameError\(/);
    expect(src).toMatch(/maxLength=\{MCP_DESCRIPTION_MAX\}/);
  });
});

describe('MCP create-form messages', () => {
  it('a blank name with a valid repo name is a hint, not an error', () => {
    expect(mcpNameHint('', 'claims-api')).toEqual({ tone: 'hint', text: 'Will use the repository name “claims-api”.' });
    expect(mcpNameHint('  ', '').tone).toBe('hint');
  });
  it('a blank name with a repo name that breaks the rule says the REPO name is the problem', () => {
    const h = mcpNameHint('', 'Claims_API');
    expect(h.tone).toBe('error');
    expect(h.text).toMatch(/repository name “Claims_API” can’t be used/);
  });
  it('a typed name is judged on its own, whatever the repo name', () => {
    expect(mcpNameHint('claims-api', 'Bad_Repo').tone).toBe('hint');
    expect(mcpNameHint('Bad', 'claims-api')).toEqual({ tone: 'error', text: mcpNameError('Bad') });
  });
  it('an over-limit description blocks with a reason; at the limit it passes', () => {
    expect(mcpDescriptionError('x'.repeat(100))).toBeNull();
    expect(mcpDescriptionError('x'.repeat(150))).toBe('Description is 150 characters — shorten it to 100.');
  });
  it('the modal gates submit on the description and never truncates it', () => {
    const src = stripComments(addRepoModalSrc);
    expect(src).toMatch(/mcpDescriptionProblem === null/);
    expect(src).toMatch(/mcpNameHint\(mcpName, name\)/);
    expect(src).not.toMatch(/\.slice\(0, MCP_DESCRIPTION_MAX\)/);
  });
});

describe('templateKindLabel — the card\'s kind chip', () => {
  it('names what the template builds, off its declared artifact kind', () => {
    expect(templateKindLabel({ artifact_kind: 'mcp_server' })).toBe('MCP server');
    expect(templateKindLabel({ artifact_kind: 'agent' })).toBe('Agent');
  });

  it('labels a template that declares nothing as an agent', () => {
    expect(templateKindLabel({})).toBe('Agent');
  });

  it('uses the picker segments’ own words, so the catalog and Add repository name a kind one way', () => {
    for (const option of ARTIFACT_KIND_OPTIONS) {
      expect(templateKindLabel({ artifact_kind: option.kind })).toBe(option.label);
    }
  });

  it('the grid renders the chip and no longer single-line truncates the name', () => {
    const src = stripComments(templateCardGridSrc);
    expect(src).toMatch(/templateKindLabel\(t\)/);
    expect(src).not.toMatch(/<h2[^>]*\btruncate\b/);
  });
});

describe('the roll-out compare view names every infra repo it will ensure', () => {
  const SEEDS = [
    { id: 'agentcore-runtime', name: 'AgentCore Runtime', platform: 'aws', iac_type: 'agentcore_runtime', build_kind: 'container', repo_name: 'agp-runtime-infra' },
    { id: 'agentcore-harness', name: 'AgentCore Harness', platform: 'aws', iac_type: 'agentcore_harness', build_kind: 'config', repo_name: 'agp-harness-infra' },
    { id: 'agentcore-gateway', name: 'AgentCore Gateway', platform: 'aws', iac_type: 'agentcore_gateway', build_kind: 'config', repo_name: 'agp-gateway-infra' },
  ];
  const probe = { name: 'agp-runtime-infra', state: 'unregistered_present', default_branch: 'main', head_sha: 'abc1234' };

  it('withInfraSeeds appends the seeds the catalog lacks — rollout() upserts them before it reads', () => {
    const merged = withInfraSeeds(SEEDS.slice(0, 2), SEEDS);
    expect(merged.map((e) => e.repo_name)).toEqual(['agp-runtime-infra', 'agp-harness-infra', 'agp-gateway-infra']);
    expect(merged.map((e) => e.registered)).toEqual([true, true, false]);
  });

  it('queuedTemplateInfra reads the infra id off each queued seed row, and nothing else', () => {
    const items = [
      { name: 'gateway-agentcore', infra_template_id: 'agentcore-gateway' },
      { name: 'strands-agentcore', infra_template_id: 'agentcore-runtime' },
      { name: 'broken-seed', infra_template_id: null },
    ];
    expect(queuedTemplateInfra(items, ['gateway-agentcore', 'broken-seed'])).toEqual([
      { infra_template_id: 'agentcore-gateway' },
    ]);
  });

  it('a queued gateway seed makes agp-gateway-infra a third target, and the copy says 3', () => {
    const registered = [{ infra_template_id: 'agentcore-harness' }, { infra_template_id: 'agentcore-runtime' }];
    const queued = queuedTemplateInfra([{ name: 'gateway-agentcore', infra_template_id: 'agentcore-gateway' }], ['gateway-agentcore']);
    const rows = infraRolloutRows(withInfraSeeds(SEEDS.slice(0, 2), SEEDS), probe, ensuredInfraIds(registered, queued));
    const targets = overwriteInfraTargets(rows);
    expect(targets.sort()).toEqual(['agp-gateway-infra', 'agp-harness-infra', 'agp-runtime-infra']);
    expect(overwriteInfraConsent(targets)).toMatch(/all 3 infra repositories/);
  });

  it('an unticked gateway seed is listed but NOT ensured — the roll-out will not create it', () => {
    const rows = infraRolloutRows(withInfraSeeds(SEEDS.slice(0, 2), SEEDS), probe, ensuredInfraIds([]));
    const gateway = rows.find((r) => r.id === 'agentcore-gateway');
    expect(gateway?.ensured).toBe(false);
    expect(gateway?.registered).toBe(false);
  });

  it('the modal derives the scope from the seeds AND the plan it sends', () => {
    const src = stripComments(rolloutModalSrc);
    expect(src).toMatch(/withInfraSeeds\(infraCatalog, view\?\.infra_seeds/);
    expect(src).toMatch(/queuedTemplateInfra\(view\?\.templates \?\? \[\], plan\.names\)/);
  });
});

describe('materializeDialogCopy — the title follows the run', () => {
  it('in flight: Materializing <name>', () => {
    expect(materializeDialogCopy('claims-mcp', { allDone: false, hasFailed: false }).title).toBe('Materializing claims-mcp');
  });

  it('every step done: the title says ready, not "Materializing…"', () => {
    const copy = materializeDialogCopy('claims-mcp', { allDone: true, hasFailed: false });
    expect(copy.title).toBe('claims-mcp is ready');
    expect(copy.title).not.toMatch(/Materializing/);
  });

  it('a failed step wins over the rest', () => {
    expect(materializeDialogCopy('claims-mcp', { allDone: false, hasFailed: true }).title).toBe('Materializing claims-mcp failed');
  });

  it('no name yet falls back to "repository"', () => {
    expect(materializeDialogCopy(undefined, { allDone: true, hasFailed: false }).title).toBe('Repository ready');
    expect(materializeDialogCopy('', { allDone: false, hasFailed: false }).title).toBe('Materializing repository');
  });

  it('the dialog reads its title from it', () => {
    const src = stripComments(addRepoModalSrc);
    expect(src).toMatch(/materializeDialogCopy\(/);
    expect(src).not.toMatch(/title=\{repo\?\.name \? `Materializing/);
  });
});

describe('templateChain.ts stays a plain unit', () => {
  it('imports no value from api/, so no test of a materialize decision pulls axios in', () => {
    const src = stripComments(templateChainSrc).replace(/^(?:import|export) type (?:\{[^}]*\}|\*) from '[^'\n]*';?$/gm, '');
    expect(src).toContain('export function');
    expect(src).not.toMatch(/['"]\.\.\/\.\.\/api\//);
  });
});
