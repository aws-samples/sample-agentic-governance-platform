// Decision: docs/adr/frontend-refusals-classified-by-code.md
import type {
  ArtifactKind,
  Deployment,
  Repository,
  RuntimeStatus,
  StageConfig,
  StepState,
} from '../../api/client';
import {
  type ApiFailure,
  type ErrorCode,
  isApiFailure,
} from '../../api/failure';
import type { LoadState } from '../../api/loadState';
import { refusalKind } from '../../api/refusalKind';
import {
  CICD_LABEL,
  isCicdInFlight,
  RUNTIME_LABEL,
  type CicdStatus,
  type RuntimeStatusKey,
} from './opsStatus';
import { isMaterializeTerminal } from './materializeSteps';
import type { OPS_BADGE } from './opsUi';
import type { DevEvalState } from './repo-tabs/evaluationsLogic';
import {
  canDestroy,
  canPromote,
  mayMaintainProject,
  type ProjectRoleName,
} from './projectRoles';
import {
  isCommitShaTag,
  PROMOTION_TAG_ONLY_LABEL,
  PROMOTION_TAG_ONLY_NOTE,
  shortCommitSha,
  shortDigest,
} from './repoRowModel';

export interface RepositoryDetailTab {
  key: string;
  label: string;
  /** False: registered but not selectable, so a tab whose body does not exist yet never opens an empty panel. */
  ready: boolean;
}

export const REPOSITORY_DETAIL_TABS: readonly RepositoryDetailTab[] = [
  { key: 'overview', label: 'Overview', ready: true },
  { key: 'deployments', label: 'Deployments', ready: true },
  { key: 'pull-requests', label: 'Pull requests', ready: true },
  { key: 'access', label: 'Access', ready: true },
  { key: 'observability', label: 'Observability', ready: true },
  { key: 'evaluations', label: 'Evaluations', ready: true },
  { key: 'resources', label: 'Resources', ready: true },
] as const;

export type RepoKind = ArtifactKind;

export function repoKindOf(repo: { artifact_kind?: string | null } | null | undefined): RepoKind {
  return repo?.artifact_kind === 'mcp_server' ? 'mcp_server' : 'agent';
}

const TABS_HIDDEN_FOR_MCP: ReadonlySet<string> = new Set(['observability']);

export function tabsForKind(kind: RepoKind = 'agent'): RepositoryDetailTab[] {
  return REPOSITORY_DETAIL_TABS.filter(
    (t) => t.ready && !(kind === 'mcp_server' && TABS_HIDDEN_FOR_MCP.has(t.key)),
  );
}

export function selectableTabKeys(kind: RepoKind = 'agent'): string[] {
  return tabsForKind(kind).map((t) => t.key);
}

export function isTabSelectable(key: string, kind: RepoKind = 'agent'): boolean {
  return tabsForKind(kind).some((t) => t.key === key);
}

export const OPS_OBSERVABILITY_TAB = 'observability';

export function initialTabKey(search: string, kind: RepoKind = 'agent'): string {
  const requested = new URLSearchParams(search).get('tab');
  return requested && isTabSelectable(requested, kind) ? requested : selectableTabKeys(kind)[0];
}

export interface RepoArtifactLink {
  label: 'Agent' | 'MCP server';
  to: string;
  id: string;
}

export function repoArtifactLink(
  repo: Pick<Repository, 'artifact_kind' | 'agent_id' | 'mcp_server_id'>,
): RepoArtifactLink {
  if (repoKindOf(repo) === 'mcp_server') {
    const id = repo.mcp_server_id ?? '';
    return { label: 'MCP server', to: `/mcp-servers/${id}`, id };
  }
  const id = repo.agent_id ?? '';
  return { label: 'Agent', to: `/agents/${id}`, id };
}

export const MCP_KIND_CHIP_LABEL = 'MCP server';
export function repoKindChip(repo: { artifact_kind?: string | null }): string | null {
  return repoKindOf(repo) === 'mcp_server' ? MCP_KIND_CHIP_LABEL : null;
}

export function rowActionForKind<A extends string>(
  repo: Pick<Repository, 'artifact_kind' | 'gateway_arn'>,
  action: A,
): A | 'never-deployed' | 'none' {
  if (repoKindOf(repo) !== 'mcp_server') return action;
  if (action === 'approval-pending' || action === 'drift' || action === 'never-deployed') {
    return gatewayStatus(repo).deployed ? 'none' : 'never-deployed';
  }
  return action;
}

export function offersRollback(repo: { artifact_kind?: string | null }): boolean {
  return repoKindOf(repo) !== 'mcp_server';
}

export const GATEWAY_DEPLOYED_LABEL = 'Gateway: deployed';
export const GATEWAY_NOT_BUILT_LABEL = 'Gateway: not built';

export function gatewayStatus(repo: Pick<Repository, 'gateway_arn'>): { deployed: boolean; label: string } {
  const deployed = Boolean(repo.gateway_arn);
  return { deployed, label: deployed ? GATEWAY_DEPLOYED_LABEL : GATEWAY_NOT_BUILT_LABEL };
}

export function tabId(key: string): string {
  return `repo-tab-${key}`;
}
export function tabPanelId(key: string): string {
  return `repo-tabpanel-${key}`;
}

export interface EnvironmentRowSource {
  stages: Record<string, StageConfig> | null | undefined;
  /** The tenant could not be resolved, so which stages exist is unknown; set when a tenant id is known and no record was found, never for a tenant that carries no stage. */
  stagesUnknown?: boolean;
  deployments: readonly Deployment[];
  /** The history read failed, so nothing is derived from the (empty) rows; distinct from a history that is genuinely empty. */
  historyError?: boolean;
  /** The clock the in-flight window is read against; omitted means now. */
  now?: number;
}

export interface DeploymentActor {
  kind: 'github' | 'entra' | 'unknown';
  display: string;
}

export const ACTOR_KIND_TITLE: Record<DeploymentActor['kind'], string> = {
  github: 'GitHub login',
  entra: 'Entra object id',
  unknown: 'Actor recorded without an identity provider — not known to be a GitHub login',
};

export interface EnvironmentRow {
  stage: string;
  imageTag: string | null;
  shortSha: string | null;
  deployedAt: string | null;
  actor: DeploymentActor | null;
  inFlight: boolean;
  drift: { behindTag: string } | null;
  region: string;
  historyUnknown: boolean;
}

function text(value: string | null | undefined): string | null {
  const trimmed = (value ?? '').trim();
  return trimmed.length > 0 ? trimmed : null;
}

export function sortedStageNames(
  stages: Record<string, StageConfig> | null | undefined,
): string[] {
  return Object.keys(stages ?? {}).sort((a, b) => a.localeCompare(b));
}

function newestFirst(rows: readonly Deployment[]): Deployment[] {
  return [...rows].sort((a, b) =>
    a.started_at === b.started_at
      ? b.seq_key.localeCompare(a.seq_key)
      : b.started_at.localeCompare(a.started_at),
  );
}

export interface DeploymentAttempt {
  row: Deployment;
  outcome: Deployment['outcome'];
  inFlight: boolean;
  actor: DeploymentActor | null;
  shortSha: string | null;
}

function withinBuildWindow(startedAt: string | null | undefined, now: number | undefined): boolean {
  if (now === undefined) return true;
  const started = startedAt ? Date.parse(startedAt) : Number.NaN;
  if (Number.isNaN(started)) return true;
  const delta = now - started;
  return delta >= 0 && delta < BUILD_NOW_STUCK_MINUTES * 60_000;
}

export function collapseByBuild(rows: readonly Deployment[], now?: number): DeploymentAttempt[] {
  const closed = new Set<string>();
  for (const r of rows) {
    const build = text(r.build_id);
    if (build !== null && r.outcome !== 'started') closed.add(build);
  }
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
      if (build !== null && closed.has(build)) continue;
      attempts.push({
        row,
        outcome: 'started',
        inFlight: withinBuildWindow(row.started_at, now),
        actor: actorOf(row),
        shortSha: shortShaOf(row),
      });
      continue;
    }
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

function currentFor(attempts: readonly DeploymentAttempt[], stage: string): DeploymentAttempt | null {
  return attempts.find((a) => a.row.stage === stage && a.outcome === 'succeeded') ?? null;
}

function shortShaOf(row: Deployment): string | null {
  return text(row.source_sha)?.slice(0, 7) ?? null;
}

function actorOf(row: Deployment): DeploymentActor | null {
  const actor = text(row.actor);
  if (actor === null) return null;
  const kind = text(row.actor_kind);
  if (kind === 'github') return { kind: 'github', display: `@${actor.replace(/^@/, '')}` };
  if (kind === 'entra') return { kind: 'entra', display: actor };
  return { kind: 'unknown', display: actor };
}

export interface ActorViewer {
  oid: string | null;
  name: string | null;
  email?: string | null;
}

export const UNKNOWN_ENTRA_USER = 'Another user';

const GUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

export function entraActorName(actor: string, viewer: ActorViewer | null): string {
  if (!GUID.test(actor)) return actor;
  if (viewer?.oid && viewer.oid.toLowerCase() === actor.toLowerCase()) {
    return viewer.name || viewer.email || 'You';
  }
  return UNKNOWN_ENTRA_USER;
}

export function actorText(actor: DeploymentActor, viewer: ActorViewer | null): string {
  return actor.kind === 'entra' ? entraActorName(actor.display, viewer) : actor.display;
}

export function actorTitle(actor: DeploymentActor): string {
  const label = ACTOR_KIND_TITLE[actor.kind];
  return actor.kind === 'entra' ? `${label}: ${actor.display}` : label;
}

export function environmentRows(source: EnvironmentRowSource): EnvironmentRow[] {
  const stages = source.stages ?? {};
  const names = sortedStageNames(stages);
  const unknown = source.historyError === true;
  const attempts = unknown ? [] : collapseByBuild(source.deployments, source.now ?? Date.now());
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
      inFlight: attempts.some((a) => a.row.stage === stage && a.inFlight),
      drift: behind,
      region: stages[stage]?.region ?? '',
      historyUnknown: unknown,
    };
  });
}

export type EnvironmentStripState = 'rows' | 'no-stages' | 'stages-unknown';

export const ENVIRONMENT_EMPTY_COPY: Record<Exclude<EnvironmentStripState, 'rows'>, string> = {
  'no-stages': 'No environments are configured for this tenant.',
  'stages-unknown':
    'Environments could not be read — this repository’s tenant did not resolve, so which stages exist is unknown. This is not a report that the tenant has none.',
};

export function environmentStripState(source: EnvironmentRowSource): EnvironmentStripState {
  if (environmentRows(source).length > 0) return 'rows';
  return source.stagesUnknown === true ? 'stages-unknown' : 'no-stages';
}

export type StepBadgeKey = keyof typeof OPS_BADGE;

export interface MaterializeSummary {
  label: string;
  succeeded: boolean;
}

export function materializeSummary(
  badge: StepBadgeKey,
  stepCount: number,
): MaterializeSummary {
  if (stepCount === 0) return { label: 'Not recorded', succeeded: false };
  if (badge === 'failed') return { label: 'Failed', succeeded: false };
  if (badge === 'ready') return { label: 'Complete', succeeded: true };
  return { label: 'In progress', succeeded: false };
}

export const RUNTIME_SCOPE_NOTE =
  'Not attributable per stage — the platform reports one runtime per agent';

export interface RuntimeScope {
  kind: 'agent' | 'stage';
  stage: string | null;
  note: string | null;
}

type RuntimeReading = Pick<RuntimeStatus, 'stage' | 'status'>;

export function runtimeScope(
  runtime: RuntimeReading | null | undefined,
): RuntimeScope {
  const stage = text(runtime?.stage);
  const status = text(runtime?.status);
  const attributable = stage !== null && stage !== 'unknown' && status !== null && status !== 'unknown';
  return attributable
    ? { kind: 'stage', stage, note: null }
    : { kind: 'agent', stage: null, note: RUNTIME_SCOPE_NOTE };
}

const BACK_ARROW = '←';

export interface RepoBackLink {
  to: string;
  label: string;
}

export const BACK_LABEL_PENDING = 'Back to project';

export function repoBackLink(input: {
  projectId: string;
  projectName?: string | null;
  settled?: boolean;
}): RepoBackLink {
  const name = text(input.projectName);
  const fallback = input.settled === false ? BACK_LABEL_PENDING : input.projectId;
  return {
    to: `/ops/projects/${input.projectId}`,
    label: `${BACK_ARROW} ${name ?? fallback}`,
  };
}

export function headerName(name: string | null | undefined, settled: boolean, id: string): string | null {
  const n = text(name);
  if (n !== null) return n;
  return settled ? id : null;
}

export function settledFrom(state: LoadState<unknown>): boolean {
  return state.kind !== 'loading' || state.data !== null;
}

export async function probeStages(
  stages: readonly string[],
  probe: (stage: string) => Promise<RuntimeStatus>,
): Promise<Record<string, RuntimeStatus | undefined>> {
  const entries = await Promise.all(
    stages.map(async (stage): Promise<[string, RuntimeStatus | undefined]> => {
      try {
        return [stage, await probe(stage)];
      } catch {
        return [stage, undefined];
      }
    }),
  );
  return Object.fromEntries(entries);
}

export interface StageRuntimeCell {
  kind: 'pill' | 'note' | 'absent';
  status: RuntimeStatusKey | null;
  note: string | null;
}

export function stageRuntimeCell(
  stage: string,
  reading: RuntimeReading | null | undefined,
): StageRuntimeCell {
  if (reading === null || reading === undefined) {
    return { kind: 'absent', status: null, note: null };
  }
  const status = reading.status;
  const scope = runtimeScope(reading);
  if (scope.kind === 'stage' && scope.stage === stage) {
    return { kind: 'pill', status, note: null };
  }
  return { kind: 'note', status: null, note: RUNTIME_SCOPE_NOTE };
}

export const CONFIG_RUNTIME_NOTE = 'Status not tracked for config agents';

export function representativeImageTag(
  repo: Pick<Repository, 'last_dev_image_tag' | 'last_promoted_image_tag' | 'prod_candidate_image_tag'>,
): string | null | undefined {
  return repo.last_dev_image_tag ?? repo.last_promoted_image_tag ?? repo.prod_candidate_image_tag;
}

export function agentRuntimeLabel(
  runtimeKey: RuntimeStatusKey,
  representativeTag: string | null | undefined,
): string {
  if (
    (runtimeKey === 'not_deployed' || runtimeKey === 'unknown') &&
    isCommitShaTag(representativeTag)
  ) {
    return CONFIG_RUNTIME_NOTE;
  }
  return RUNTIME_LABEL[runtimeKey];
}

export function stageRuntimeDisplay(
  stage: string,
  reading: Pick<RuntimeStatus, 'stage' | 'status'> | null | undefined,
  imageTag: string | null | undefined,
): StageRuntimeCell {
  const cell = stageRuntimeCell(stage, reading);
  if (!isCommitShaTag(imageTag)) return cell;
  if (
    cell.kind === 'note' ||
    (cell.kind === 'pill' && (cell.status === 'not_deployed' || cell.status === 'unknown'))
  ) {
    return { kind: 'note', status: null, note: CONFIG_RUNTIME_NOTE };
  }
  return cell;
}

export function runtimeStatusTitle(
  reading: Pick<RuntimeStatus, 'detail'> | null | undefined,
  scopeNote?: string | null,
): string | null {
  const detail = text(reading?.detail);
  const scope = text(scopeNote);
  if (detail === null) return scope;
  if (scope === null) return detail;
  return `${scope} — ${detail}`;
}

// Decision: docs/adr/frontend-two-status-machines.md
export type ProdServingState = 'serving' | 'unknown' | 'none';

export function prodServingState(input: {
  lastPromotionBuildId: string | null | undefined;
  lastPromotedAt: string | null | undefined;
  lastPromotedImageTag: string | null | undefined;
  deployments: readonly Deployment[];
  historyError?: boolean;
  historyLoading?: boolean;
}): ProdServingState {
  const buildId = text(input.lastPromotionBuildId);
  const attempted =
    buildId !== null ||
    text(input.lastPromotedAt) !== null ||
    text(input.lastPromotedImageTag) !== null;
  if (!attempted) return 'none';
  if (input.historyLoading === true) return 'none';
  if (input.historyError === true) return 'unknown';
  if (buildId === null) {
    return 'unknown';
  }
  const rows = input.deployments.filter((d) => text(d.build_id) === buildId);
  if (rows.some((d) => d.outcome === 'succeeded')) return 'serving';
  const failed = rows.find((d) => d.outcome === 'failed') ?? null;
  if (failed !== null) {
    const stage = text(failed.stage);
    const stillServing =
      stage !== null &&
      input.deployments.some((d) => text(d.stage) === stage && d.outcome === 'succeeded');
    return stillServing ? 'serving' : 'none';
  }
  return 'unknown';
}

export function ungovernedInProd(
  serving: ProdServingState,
  lifecycleState: string | null | undefined,
): boolean {
  return serving === 'serving' && text(lifecycleState) === 'proposed';
}

export type ProdGovernanceState = 'ungoverned' | 'unknown' | 'none';

export function prodGovernanceState(input: {
  serving: ProdServingState;
  lifecycleState: string | null | undefined;
  agentRead: boolean;
}): ProdGovernanceState {
  if (ungovernedInProd(input.serving, input.lifecycleState)) return 'ungoverned';
  const question = text(input.lifecycleState) === 'proposed' || !input.agentRead;
  if (!question) return 'none';
  if (input.serving === 'serving') return 'unknown';
  if (input.serving === 'unknown') return 'unknown';
  return 'none';
}

export function shouldPollRepo(repo: {
  cicd_status: CicdStatus;
  steps: readonly StepState[];
}): boolean {
  const materializeMoving = repo.steps.length > 0 && !isMaterializeTerminal([...repo.steps]);
  return materializeMoving || isCicdInFlight(repo.cicd_status);
}

export const DELIVERY_BUILDING_LABEL = 'Building…';

export interface DeliveryHeaderState {
  label: string;
  inFlight: boolean;
}

export function deliveryDeployingLabel(stages: readonly string[]): string {
  return `Deploying to ${stages.join(', ')}…`;
}

export function deliveryHeaderState(input: {
  cicdStatus: CicdStatus;
  environmentRows: readonly (Pick<EnvironmentRow, 'inFlight'> & { stage?: string })[];
  historyError?: boolean;
}): DeliveryHeaderState {
  const cicd = input.cicdStatus;
  const recordSaysInFlight = isCicdInFlight(cicd);
  const overridable = cicd === 'ready' || cicd === 'deployed';
  const historyKnown = input.historyError !== true;
  const buildRunning = historyKnown && input.environmentRows.some((r) => r.inFlight);
  if (overridable && buildRunning) {
    return { label: DELIVERY_BUILDING_LABEL, inFlight: true };
  }
  if (cicd === 'promoting' && historyKnown) {
    const running = input.environmentRows.filter((r) => r.inFlight);
    if (running.length > 0 && running.every((r) => typeof r.stage === 'string' && r.stage)) {
      return { label: deliveryDeployingLabel(running.map((r) => r.stage as string)), inFlight: true };
    }
  }
  return { label: CICD_LABEL[cicd], inFlight: recordSaysInFlight };
}

export function deliveryFailureReason(
  cicdStatus: CicdStatus,
  deployments: readonly Deployment[],
  now?: number,
): string | null {
  if (cicdStatus !== 'failed') return null;
  const failed = collapseByBuild(deployments, now).find((a) => a.outcome === 'failed');
  const cause = failed?.row.error?.trim();
  if (!cause) return 'The last build or deployment failed';
  const sentence = cause.charAt(0).toUpperCase() + cause.slice(1);
  return failed?.row.stage ? `${sentence} (${failed.row.stage})` : sentence;
}

/** Open on purpose: the record's status enum is not mirrored in TS, and a miss renders the wire value verbatim. */
export const RECORD_STATUS_LABEL: Record<string, string> = {
  provisioning: 'Materializing',
  ready: 'Materialized',
  failed: 'Materialize failed',
};

export function recordStatusLabel(raw: string | null | undefined): string | null {
  const value = text(raw);
  if (value === null) return null;
  return RECORD_STATUS_LABEL[value] ?? value;
}

export type PromotionArtifactKind = 'digest' | 'commit-pinned' | 'tag-only' | 'none';

export interface ArtifactMarker {
  text: string;
  note: string | null;
  tone: 'pinned' | 'caution';
}

export interface PromotionArtifact {
  kind: PromotionArtifactKind;
  marker: ArtifactMarker | null;
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
  if (repo?.prod_candidate_status !== 'pending') {
    return { kind: 'none', marker: null, imageTag };
  }
  const digest = shortDigest(repo.prod_candidate_digest);
  if (digest === null) {
    if (imageTag !== null && isCommitShaTag(imageTag)) {
      return {
        kind: 'commit-pinned',
        marker: { text: shortCommitSha(imageTag), note: null, tone: 'pinned' },
        imageTag,
      };
    }
    return {
      kind: 'tag-only',
      marker: { text: PROMOTION_TAG_ONLY_LABEL, note: PROMOTION_TAG_ONLY_NOTE, tone: 'caution' },
      imageTag,
    };
  }
  return { kind: 'digest', marker: { text: digest, note: null, tone: 'pinned' }, imageTag };
}

export interface HeaderActions {
  promote: boolean;
  retry: boolean;
  destroy: boolean;
  runEvals: boolean;
  evalsLead: boolean;
}

export function headerActions(input: {
  held: ProjectRoleName | null;
  roleLevel: number;
  ungoverned: boolean | null | undefined;
  cicdStatus: CicdStatus;
  prodCandidateStatus: string | null | undefined;
  devOutcomeForCandidate?: string | null;
  steps: readonly Pick<StepState, 'status'>[];
  artifactKind?: RepoKind;
  devEval?: DevEvalState | null;
  hasAgent?: boolean;
}): HeaderActions {
  const cicd = input.cicdStatus;
  const mayRunEvals =
    input.artifactKind !== 'mcp_server' &&
    Boolean(input.hasAgent) &&
    mayMaintainProject(input.held, input.roleLevel, input.ungoverned);
  const unevaluated = input.devEval === 'none' || input.devEval === 'inconclusive';
  return {
    promote:
      input.artifactKind !== 'mcp_server' &&
      canPromote(
        input.held,
        input.roleLevel,
        input.prodCandidateStatus ?? null,
        input.devOutcomeForCandidate ?? null
      ) && !isCicdInFlight(cicd),
    retry:
      mayMaintainProject(input.held, input.roleLevel, input.ungoverned) &&
      input.steps.some((s) => s.status === 'failed'),
    destroy: canDestroy(input.held, input.roleLevel),
    runEvals: mayRunEvals && (unevaluated || input.devEval === 'failed'),
    evalsLead: mayRunEvals && unevaluated,
  };
}

export const BUILD_NOW_STARTED_NOTICE =
  'Build started — the harness is usually ready in about 5 minutes';

export function buildNowStartedNotice(kind: RepoKind): string {
  return kind === 'mcp_server'
    ? 'Build started — the gateway updates when the build finishes, usually in a few minutes'
    : BUILD_NOW_STARTED_NOTICE;
}

export const BUILD_NOW_RUNNING_REASON = 'A build is already running for this repository.';

export const BUILD_NOW_STARTING_LABEL = 'Starting build…';

export const BUILD_NOW_STUCK_MINUTES = 60;

export const BUILD_NOW_STUCK_HINT =
  'The last build never reported back — it has likely stalled. You can build again.';

export interface BuildNowGate {
  visible: boolean;
  disabledReason: string | null;
  label: string;
  hint: string | null;
}

function promotingIsStuck(clocks: Array<string | null | undefined>, now: number): boolean {
  return !clocks.some((c) => !!c && !Number.isNaN(Date.parse(c)) && withinBuildWindow(c, now));
}

export function canBuildNow(
  repo: Pick<Repository, 'cicd_status' | 'status'> & {
    build_kind?: Repository['build_kind'];
    last_promoted_at?: string | null;
    last_build_started_at?: string | null;
  },
  mayMaintain: boolean,
  buildRunning = false,
  now: number = Date.now(),
): BuildNowGate {
  const gate = (disabledReason: string | null, label = 'Build now', hint: string | null = null) => ({
    visible: true,
    disabledReason,
    label,
    hint,
  });
  if (repo.build_kind !== 'config' || !mayMaintain) {
    return { visible: false, disabledReason: null, label: 'Build now', hint: null };
  }
  const cicd = repo.cicd_status;
  if (cicd === 'provisioning' || repo.status === 'provisioning') {
    return gate('Setup is still running — build once it finishes.');
  }
  const stuck =
    cicd === 'promoting' &&
    promotingIsStuck([repo.last_promoted_at, repo.last_build_started_at], now);
  if (!stuck && (cicd === 'promoting' || buildRunning)) {
    return gate(BUILD_NOW_RUNNING_REASON, 'Build running…');
  }
  if (repo.status === 'failed') {
    return gate('This repository’s setup failed — retry the setup before building.');
  }
  if (repo.status !== 'ready') {
    return gate("Setup hasn't finished for this repository.");
  }
  return gate(null, 'Build now', stuck ? BUILD_NOW_STUCK_HINT : null);
}

export type BuildRefusalKind =
  | 'insufficient'
  | 'container_build'
  | 'no_commit'
  | 'not_ready'
  | 'materialize_failed'
  | 'in_flight'
  | 'build_failed'
  | 'repo_not_found';

export const BUILD_REFUSAL_CODES = {
  insufficient: ['project_role.insufficient'],
  container_build: ['project.container_build'],
  no_commit: ['project.no_commit'],
  not_ready: ['project.not_ready'],
  materialize_failed: ['project.materialize_failed', 'project.materialize_error'],
  in_flight: ['project.promote_in_flight', 'project.conflict'],
  build_failed: ['project.build_failed'],
  repo_not_found: ['project.repo_not_found'],
} as const satisfies Record<BuildRefusalKind, readonly ErrorCode[]>;

export function classifyBuildFailure(failure: ApiFailure): BuildRefusalKind | null {
  return refusalKind(BUILD_REFUSAL_CODES, failure);
}

export function buildNowActionMessage(err: unknown, fallback: string): string {
  const kind = isApiFailure(err) ? classifyBuildFailure(err) : null;
  if (kind === 'insufficient') {
    return 'You need the Maintainer role on this project to build this repository. Ask an owner to grant you one.';
  }
  if (kind === 'container_build') {
    return 'This repository builds from CI on every push, so there is nothing to start here. Push to its dev branch instead.';
  }
  if (kind === 'no_commit') {
    return 'This repository has no commit to build yet. Push to its default branch first.';
  }
  if (kind === 'not_ready') {
    return 'This repository is still being set up. Try again when it finishes.';
  }
  if (kind === 'materialize_failed') {
    return 'This repository’s setup failed, so there is nothing to build yet. Retry the setup first.';
  }
  if (kind === 'in_flight') {
    return 'A build is already running for this repository. Wait for it to finish — the status here updates on its own.';
  }
  if (kind === 'build_failed') {
    return 'The build couldn’t be started, so nothing was deployed. Try again in a moment.';
  }
  if (kind === 'repo_not_found') {
    return 'This repository is no longer available — it may have been deleted. Reload the project.';
  }
  const message = err instanceof Error ? err.message.trim() : '';
  return message || fallback;
}
