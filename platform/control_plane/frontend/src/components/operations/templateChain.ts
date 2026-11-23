// Rationale: frontend/README.md "Tests are node-environment only, by design"

import type { ArtifactKind } from '../../api/client';

export type { ArtifactKind };

/** The chain fields of one agent template (a structural subset of `TemplateView`). */
export interface TemplateLike {
  name: string;
  framework: string;
  /** "aws" | "databricks" — the INFRA record's platform, joined onto the view server-side. */
  platform: string;
  /** "container" | "config" — the pipeline shape. */
  build_kind: string;
  preview: boolean;
  infra_template_id: string;
  /** What the template builds; absent on older backends means `'agent'`. */
  artifact_kind?: ArtifactKind;
}

/** The one field a tenant contributes. */
export interface TenantLike {
  platform?: string;
}

export const DEFAULT_PLATFORM = 'aws';

export function tenantPlatformOf(tenant: TenantLike | null | undefined): string {
  return tenant?.platform || DEFAULT_PLATFORM;
}

export function templatesForPlatform<T extends TemplateLike>(
  templates: readonly T[],
  platform: string,
): T[] {
  return templates.filter((t) => t.platform === platform);
}

export const ARTIFACT_KIND_OPTIONS: readonly { kind: ArtifactKind; label: string }[] = [
  { kind: 'agent', label: 'Agent' },
  { kind: 'mcp_server', label: 'MCP server' },
];

export function templateKindOf(template: { artifact_kind?: string | null }): ArtifactKind {
  return template.artifact_kind === 'mcp_server' ? 'mcp_server' : 'agent';
}

export function templateKindLabel(template: { artifact_kind?: string | null }): string {
  const kind = templateKindOf(template);
  return ARTIFACT_KIND_OPTIONS.find((o) => o.kind === kind)?.label ?? 'Agent';
}

export function templatesForKind<T extends TemplateLike>(templates: readonly T[], kind: ArtifactKind): T[] {
  return templates.filter((t) => templateKindOf(t) === kind);
}

export function kindEmptyReason(kind: ArtifactKind): string {
  return kind === 'mcp_server'
    ? 'No MCP server templates in this org yet — roll out the base templates under Admin › Org Connections › Manage templates.'
    : 'No agent templates in this org yet — roll out the base templates under Admin › Org Connections › Manage templates.';
}

/** The backend's MCP record name rule, mirrored; the gateway name derives from it and refuses underscores. */
export const MCP_NAME_RE = /^[a-z][a-z0-9-]{1,30}$/;

/** The MCP description cap (the registry's 100-char limit). */
export const MCP_DESCRIPTION_MAX = 100;

export function mcpNameError(name: string): string | null {
  if (MCP_NAME_RE.test(name)) return null;
  return 'Lowercase letter first, then lowercase letters, digits or hyphens (2–31 characters).';
}

export function mcpNameHint(
  typed: string,
  repoName: string,
): { tone: 'error' | 'hint'; text: string } {
  const own = typed.trim();
  if (own) {
    const err = mcpNameError(own);
    return err ? { tone: 'error', text: err } : { tone: 'hint', text: 'Valid MCP server name.' };
  }
  const repo = repoName.trim();
  if (!repo) return { tone: 'hint', text: 'Leave blank to use the repository name.' };
  if (mcpNameError(repo) === null) return { tone: 'hint', text: `Will use the repository name “${repo}”.` };
  return {
    tone: 'error',
    text: `The repository name “${repo}” can’t be used as an MCP server name — type one here (lowercase letters, digits, hyphens; 2–31 characters, letter first).`,
  };
}

export function mcpDescriptionError(description: string): string | null {
  const n = description.trim().length;
  return n > MCP_DESCRIPTION_MAX
    ? `Description is ${n} characters — shorten it to ${MCP_DESCRIPTION_MAX}.`
    : null;
}

export function pickTemplateName(templates: readonly TemplateLike[], current: string): string {
  if (current && templates.some((t) => t.name === current)) return current;
  return templates[0]?.name ?? '';
}

export function frameworkForTemplate(
  templates: readonly TemplateLike[],
  selectedName: string,
): string {
  return templates.find((t) => t.name === selectedName)?.framework ?? '';
}

export function selectedTemplate<T extends TemplateLike>(
  templates: readonly T[],
  selectedName: string,
): T | null {
  return templates.find((t) => t.name === selectedName) ?? null;
}

const BUILD_KIND_LABELS: ReadonlyMap<string, string> = new Map([
  ['container', 'Container'],
  ['config', 'Config'],
]);

export function buildKindLabel(buildKind: string): string {
  return BUILD_KIND_LABELS.get(buildKind) ?? buildKind;
}

export const PREVIEW_CHIP = 'bg-amber-50 text-amber-700';
export const BUILD_KIND_CHIP = 'bg-slate-100 text-slate-500';
export const PLATFORM_CHIP = 'bg-slate-100 text-slate-500';
export const INFRA_CHIP = 'bg-emerald-50/70 text-emerald-700';

/** The badges travel as text because an `<option>` may not contain elements. */
export function templateOptionLabel(template: TemplateLike): string {
  const parts = [template.name, buildKindLabel(template.build_kind)];
  if (template.preview) parts.push('Preview');
  return parts.join(' · ');
}

export function pickerEmptyReason(
  totalCount: number,
  offeredCount: number,
  platform: string,
): string | null {
  if (offeredCount > 0) return null;
  if (totalCount === 0) {
    return 'No templates in this org — roll out the base templates under Admin › Org Connections › Manage templates.';
  }
  return `This org has ${totalCount} template${totalCount === 1 ? '' : 's'}, but none targets this project’s tenant platform (${platform}). Register an infra template for ${platform}, or materialize into a tenant whose platform matches.`;
}

export const INFRA_TAB_SUBTITLE =
  'The deploy mechanisms registered for this org — which cloud, which IaC, and whether the pipeline builds a container image or pushes a config. Seeded and rolled out by AGP; read-only here.';

export const INFRA_TAB_EMPTY =
  'No infra templates registered for this org yet. Roll out templates for this org and AGP seeds its infra catalog.';

export type InfraBodyState = 'loading' | 'error' | 'empty' | 'list';

export function infraBodyState(input: {
  loading: boolean;
  error: string | null;
  count: number;
}): InfraBodyState {
  if (input.loading) return 'loading';
  if (input.error !== null) return 'error';
  if (input.count === 0) return 'empty';
  return 'list';
}

/** The reconcile evidence for one repo name. */
export interface InfraReconcileLike {
  name: string;
  state: string;
  default_branch?: string | null;
  head_sha?: string | null;
}

/** Mirrors `template_rollout_service._referenced_infra_records`: the default, plus every id a registered agent template deploys through. */
export function ensuredInfraIds(
  agentTemplates: readonly { infra_template_id?: string }[],
  queuedSeeds: readonly { infra_template_id: string }[] = [],
): Set<string> {
  const ids = new Set<string>([DEFAULT_INFRA_TEMPLATE_ID]);
  for (const t of agentTemplates) ids.add(t.infra_template_id || DEFAULT_INFRA_TEMPLATE_ID);
  for (const t of queuedSeeds) ids.add(t.infra_template_id);
  return ids;
}

export function queuedTemplateInfra(
  items: readonly { name: string; infra_template_id?: string | null }[],
  names: readonly string[],
): { infra_template_id: string }[] {
  const queued = new Set(names);
  return items.flatMap((i) =>
    queued.has(i.name) && i.infra_template_id ? [{ infra_template_id: i.infra_template_id }] : [],
  );
}

export interface InfraCatalogEntry {
  id: string;
  name: string;
  platform: string;
  iac_type: string;
  build_kind: string;
  repo_name: string;
  /** In the org's catalog today; `false` is a seed the roll-out will add; absent means `true`. */
  registered?: boolean;
}

export function withInfraSeeds(
  catalog: readonly Omit<InfraCatalogEntry, 'registered'>[],
  seeds: readonly Omit<InfraCatalogEntry, 'registered'>[],
): InfraCatalogEntry[] {
  const known = new Set(catalog.map((e) => e.id));
  return [
    ...catalog.map((e) => ({ ...e, registered: true })),
    ...seeds.filter((s) => !known.has(s.id)).map((s) => ({ ...s, registered: false })),
  ];
}

/** `DEFAULT_INFRA_TEMPLATE_ID` in `template_rollout_service.py`, and `TemplateRecord.infra_template_id`'s default. */
export const DEFAULT_INFRA_TEMPLATE_ID = 'agentcore-runtime';

export interface InfraRolloutRow {
  id: string;
  name: string;
  platform: string;
  iacType: string;
  buildKind: string;
  repoName: string;
  registered: boolean;
  ensured: boolean;
  catalogued: boolean;
  reconcile: InfraReconcileLike | null;
}

export function infraRolloutRows(
  catalog: readonly InfraCatalogEntry[],
  infraRepo: InfraReconcileLike | null | undefined,
  ensuredIds: ReadonlySet<string> = new Set([DEFAULT_INFRA_TEMPLATE_ID]),
): InfraRolloutRow[] {
  const rows: InfraRolloutRow[] = catalog.map((entry) => ({
    id: entry.id,
    name: entry.name,
    platform: entry.platform,
    iacType: entry.iac_type,
    buildKind: entry.build_kind,
    repoName: entry.repo_name,
    registered: entry.registered ?? true,
    ensured: ensuredIds.has(entry.id),
    catalogued: true,
    reconcile: infraRepo && infraRepo.name === entry.repo_name ? infraRepo : null,
  }));
  if (infraRepo && !catalog.some((entry) => entry.repo_name === infraRepo.name)) {
    rows.push({
      id: infraRepo.name,
      name: infraRepo.name,
      platform: '',
      iacType: '',
      buildKind: '',
      repoName: infraRepo.name,
      registered: false,
      ensured: catalog.length === 0,
      catalogued: false,
      reconcile: infraRepo,
    });
  }
  return rows;
}

export function overwriteInfraTargets(rows: readonly InfraRolloutRow[]): string[] {
  if (rows.some((r) => !r.catalogued && r.ensured)) return [];
  return rows.filter((r) => r.ensured).map((r) => r.repoName);
}

export function overwriteInfraConsent(targets: readonly string[]): string {
  const lead = 'Also re-push AGP’s Terraform module into ';
  const tail =
    ' One commit per repository on its default branch; history stays, and an unchanged module ' +
    'writes no commit at all.';
  if (targets.length === 0) {
    return `${lead}every infra repository this roll-out ensures that already exists.${tail}`;
  }
  if (targets.length === 1) {
    return `${lead}${targets[0]}, if this org already has it.${tail}`;
  }
  return (
    `${lead}all ${targets.length} infra repositories this roll-out ensures ` +
    `(${targets.join(', ')}) — each one that already exists, not just the row this box sits on.${tail}`
  );
}

export function materializeDialogCopy(
  name: string | null | undefined,
  run: { allDone: boolean; hasFailed: boolean },
): { title: string; description: string } {
  if (run.hasFailed) {
    return {
      title: name ? `Materializing ${name} failed` : 'Materialization failed',
      description: 'A step failed — the timeline below shows which one.',
    };
  }
  if (run.allDone) {
    return {
      title: name ? `${name} is ready` : 'Repository ready',
      description: 'Every step finished.',
    };
  }
  return {
    title: name ? `Materializing ${name}` : 'Materializing repository',
    description: 'Provisioning the repository — the steps run in the background.',
  };
}
