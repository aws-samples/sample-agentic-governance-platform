// Decision: docs/adr/frontend-refusals-classified-by-code.md
import { type ApiFailure, type ErrorCode, isApiFailure } from '../../api/failure';
import { refusalKind } from '../../api/refusalKind';
import type { ReconcileItemView } from '../../api/client';
import { warnOnce } from './warnOnce';

export interface ReconcileItemLike {
  name: string;
  origin: ReconcileItemView['origin'];
  state: ReconcileItemView['state'];
  /** What `read_repo` found; null when there was nothing to read (an absent repo, or an un-probed org row). */
  default_branch: string | null;
  /** Null for an absent repo, an un-probed org row, or a repo that exists with no commit yet. */
  head_sha: string | null;
}

export const RECONCILE_STATE_LABEL: Record<ReconcileItemView['state'], string> = {
  registered_present: 'In sync',
  registered_missing: 'Repository missing',
  unregistered_present: 'Found in org, not ours',
  seed_absent: 'Not in org yet',
};

export const RECONCILE_STATE_TONE: Record<ReconcileItemView['state'], string> = {
  registered_present: 'bg-emerald-50 text-emerald-700',
  registered_missing: 'bg-amber-50 text-amber-700',
  unregistered_present: 'bg-slate-100 text-slate-500',
  seed_absent: 'bg-amber-50 text-amber-700',
};

export type ReconcileAction = 'create' | 'repush' | 'recreate' | 'adopt' | 'deregister';

export type SingleRepoAction = Extract<ReconcileAction, 'adopt' | 'deregister'>;

export const RECONCILE_ACTION_LABEL: Record<ReconcileAction, string> = {
  create: 'Create',
  repush: 'Re-push seed',
  recreate: 'Re-create from seed',
  adopt: 'Adopt',
  deregister: 'Deregister',
};

export const ADOPT_NAME_RULE_HINT =
  'This repository’s name can’t be a template name: AGP requires lowercase letters, digits and hyphens, starting with a letter. Rename it in the provider to adopt it.';

const ADOPT_NAME_RE = /^[a-z][a-z0-9-]{0,63}$/;

export function isAdoptableName(name: string): boolean {
  return ADOPT_NAME_RE.test(name);
}

const QUEUEABLE: readonly ReconcileAction[] = ['create', 'repush', 'recreate'];

export interface ReconcileRowModel {
  item: ReconcileItemLike;
  label: string;
  tone: string;
  actions: ReconcileAction[];
  queueAction: ReconcileAction | null;
  provenance: string | null;
  actionDisabled: boolean;
  unknown: boolean;
  note: string | null;
}

type ReconcileState = ReconcileItemView['state'];

type ReconcileStateReading = ReconcileState | { kind: 'unknown'; raw: string };

const KNOWN_RECONCILE_STATES = [
  'registered_present',
  'registered_missing',
  'unregistered_present',
  'seed_absent',
] as const satisfies readonly ReconcileState[];

function isKnownReconcileState(state: string): state is (typeof KNOWN_RECONCILE_STATES)[number] {
  return (KNOWN_RECONCILE_STATES as readonly string[]).includes(state);
}

function readReconcileState(state: ReconcileState): ReconcileStateReading {
  if (isKnownReconcileState(state)) return state;
  const raw: string = state satisfies never;
  warnOnce('[reconcileView] reconcile state this frontend does not know; the row offers no action:', raw);
  return { kind: 'unknown', raw };
}

export function reconcileRowModel(item: ReconcileItemLike): ReconcileRowModel {
  const state = readReconcileState(item.state);
  if (typeof state === 'object') {
    return {
      item,
      label: `State "${state.raw}" not recognised`,
      tone: 'bg-slate-100 text-slate-500',
      actions: [],
      queueAction: null,
      provenance: provenanceFor(item),
      actionDisabled: true,
      unknown: true,
      note: `This build reports "${state.raw}", which this frontend does not know yet; reload once the deploy finishes.`,
    };
  }
  const seeded = item.origin === 'seed';
  let actions: ReconcileAction[];
  switch (state) {
    case 'seed_absent':
      actions = ['create'];
      break;
    case 'registered_present':
      actions = seeded ? ['repush'] : [];
      break;
    case 'registered_missing':
      actions = seeded ? ['recreate', 'deregister'] : ['deregister'];
      break;
    case 'unregistered_present':
      actions = ['adopt'];
      break;
  }

  const adoptBlocked = actions[0] === 'adopt' && !isAdoptableName(item.name);
  return {
    item,
    label: RECONCILE_STATE_LABEL[state],
    tone: RECONCILE_STATE_TONE[state],
    actions,
    queueAction: actions.find((a) => QUEUEABLE.includes(a)) ?? null,
    provenance: provenanceFor(item),
    actionDisabled: adoptBlocked,
    unknown: false,
    note: adoptBlocked ? ADOPT_NAME_RULE_HINT : null,
  };
}

function provenanceFor(item: ReconcileItemLike): string | null {
  if (!item.default_branch) return null;
  if (!item.head_sha) return item.default_branch;
  return `${item.default_branch} @ ${item.head_sha.slice(0, 7)}`;
}

export function reconcileRows(items: readonly ReconcileItemLike[]): ReconcileRowModel[] {
  return items.map(reconcileRowModel);
}

export interface ReconcileSummary {
  createCount: number;
  adoptCount: number;
  inSyncCount: number;
  missingCount: number;
}

reconcileRows.summarize = function summarize(rows: readonly ReconcileRowModel[]): ReconcileSummary {
  return {
    createCount: rows.filter((r) => r.item.state === 'seed_absent').length,
    adoptCount: rows.filter((r) => r.item.state === 'unregistered_present').length,
    inSyncCount: rows.filter((r) => r.item.state === 'registered_present').length,
    missingCount: rows.filter((r) => r.item.state === 'registered_missing').length,
  };
};

export interface BatchPlan {
  names: string[];
  overwrite: boolean;
  overwriteInfra: boolean;
  creates: string[];
  repushes: string[];
  recreates: string[];
  dropped: string[];
  empty: boolean;
}

export function wireFlagNote(plan: Pick<BatchPlan, 'overwrite' | 'overwriteInfra'>): string | null {
  const infra = 'AGP’s Terraform module in each runtime-infra repo that already exists';
  if (plan.overwrite && plan.overwriteInfra) {
    return `This request is sent with two separate overwrites enabled: one for the registered templates listed above, and one for ${infra}.`;
  }
  if (plan.overwrite) {
    return 'This request is sent with overwrite enabled for the registered templates listed above only — existing runtime-infra repos are left as they are.';
  }
  if (plan.overwriteInfra) {
    return `This request is sent with overwrite enabled only for ${infra} — never a template repository.`;
  }
  return null;
}

export function batchPlan(
  rows: readonly ReconcileRowModel[],
  queued: ReadonlyMap<string, ReconcileAction>,
  infraIntent: InfraIntent,
): BatchPlan {
  const live = rows.filter(
    (r) => r.queueAction !== null && queued.get(r.item.name) === r.queueAction,
  );
  const carried = new Set(live.map((r) => r.item.name));
  const by = (action: ReconcileAction) =>
    live.filter((r) => r.queueAction === action).map((r) => r.item.name);
  const repushes = by('repush');
  return {
    names: live.map((r) => r.item.name),
    overwrite: repushes.length > 0,
    overwriteInfra: infraIntent === 'repush',
    creates: by('create'),
    repushes,
    recreates: by('recreate'),
    dropped: [...queued.keys()].filter((name) => !carried.has(name)).sort(),
    empty: live.length === 0,
  };
}

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

export function batchConfirmLines(
  rows: readonly ReconcileRowModel[],
  queued: ReadonlyMap<string, ReconcileAction>,
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
  const infraLead =
    'Every roll-out also ensures the runtime-infra repos it forces: each is created and seeded with ' +
    'AGP’s Terraform module if this org does not have it yet, and otherwise left exactly as it is. ';
  lines.push(
    infraLead +
      (plan.overwriteInfra
        ? 'You have also authorised re-pushing the module into every one of them that already ' +
          'exists — not only the row the box sits on: AGP adds one commit per repository with the ' +
          'module’s files, and that push is idempotent, so an unchanged module writes no commit at ' +
          'all.'
        : 'An existing one is not written to — re-pushing the module needs its own box ticked above.'),
  );
  return lines;
}

export function infraNeedsCreate(infra: ReconcileItemLike | null | undefined): boolean {
  return infra?.state === 'seed_absent';
}

export type InfraIntent = 'none' | 'create' | 'repush';

export function infraIntentFor(
  infra: ReconcileItemLike | null | undefined,
  overwriteInfra: boolean,
): InfraIntent {
  if (!infra) return 'none';
  if (infraNeedsCreate(infra)) return 'create';
  return overwriteInfra ? 'repush' : 'none';
}

export function batchConfirmTitle(plan: BatchPlan, infraIntent: InfraIntent, org: string): string {
  const n = plan.names.length;
  if (n > 0) return `Roll out ${n === 1 ? '1 template' : `${n} templates`} — ${org}`;
  if (infraIntent === 'create') return `Create AGP’s infra repos — ${org}`;
  if (infraIntent === 'repush') return `Re-push AGP’s infra modules — ${org}`;
  return `Nothing selected — ${org}`;
}

const ROLLOUT_ACTION_LABELS: ReadonlyMap<string, { label: string; cls: string }> = new Map([
  ['created', { label: 'Created', cls: 'bg-emerald-50 text-emerald-700' }],
  ['recreated', { label: 'Re-created from seed', cls: 'bg-emerald-50 text-emerald-700' }],
  ['overwritten', { label: 'Re-pushed', cls: 'bg-emerald-50 text-emerald-700' }],
  ['unchanged', { label: 'Already up to date', cls: 'bg-slate-100 text-slate-500' }],
  ['adopted', { label: 'Adopted', cls: 'bg-emerald-50 text-emerald-700' }],
  ['skipped', { label: 'Skipped', cls: 'bg-slate-100 text-slate-500' }],
  ['failed', { label: 'Failed', cls: 'bg-rose-50 text-rose-700' }],
] as const);

export interface RolloutActionView {
  label: string;
  cls: string;
  known: boolean;
}

export function rolloutActionLabel(action: string): RolloutActionView {
  const known = ROLLOUT_ACTION_LABELS.get(action);
  if (known) return { ...known, known: true };
  return {
    label: action || 'Unreported',
    cls: 'bg-slate-100 text-slate-500',
    known: false,
  };
}

export function repushConfirm(name: string): string {
  return (
    `Re-push the seed into “${name}”? AGP adds one commit with the starter files on the ` +
    'repository’s default branch. Its history stays — nothing is removed, and a re-push that ' +
    'changes no file writes no commit at all.'
  );
}

export function recreateConfirm(name: string): string {
  return (
    `Re-create “${name}” from AGP’s seed? The registered repository no longer exists in the org, ` +
    'so this creates it again and pushes the starter files. The catalog record is kept, ' +
    'including who registered it.'
  );
}

export const ADOPT_CONFIRM_NO_PROVENANCE =
  'AGP reads the repository when you adopt it, and records the branch and commit it finds then.';

export function adoptConfirm(item: ReconcileItemLike): string {
  const head = `Adopt “${item.name}” as a template? AGP registers it as-is — no files are read, changed or pushed.`;
  const prov = provenanceFor(item);
  return prov ? `${head} Currently at ${prov}.` : `${head} ${ADOPT_CONFIRM_NO_PROVENANCE}`;
}

export function deregisterConfirm(name: string): string {
  return (
    `Deregister “${name}”? This removes AGP’s record so the name can no longer be materialized. ` +
    'AGP never deletes the repository behind a template — and this one is already missing from ' +
    'the org, which is why the record has nothing to point at.'
  );
}

export function SEED_CONSENT_PROMPT(org: string): string {
  return `Seed starter templates into ${org}? Nothing is created until you confirm below.`;
}

export type RolloutErrorKind =
  | 'not_found'
  | 'repo_not_found'
  | 'conflict'
  | 'rollout_error'
  | 'validation';

export const ROLLOUT_RETRY_HINT = 'Try again in a moment.';

export const ROLLOUT_ERROR_CODES = {
  not_found: ['rollout.not_found'],
  repo_not_found: ['rollout.repo_not_found'],
  conflict: ['rollout.conflict'],
  rollout_error: ['rollout.rollout_error', 'github_repo.provider_error'],
  validation: ['rollout.validation'],
} as const satisfies Record<RolloutErrorKind, readonly ErrorCode[]>;

const ROLLOUT_RETRYABLE_KINDS: readonly RolloutErrorKind[] = ['rollout_error'];

const ROLLOUT_MESSAGES: Record<RolloutErrorKind, string> = {
  rollout_error:
    'The rollout failed at the provider or the catalog rather than because of anything you entered. Re-running it is safe — AGP’s writes here are idempotent, so a repeat push changes nothing it has already done.',
  not_found:
    'AGP doesn’t ship a starter template under that name, so there is nothing to push. Refresh this surface — the seed list may have changed.',
  repo_not_found:
    'That repository isn’t in this org any more, so it can’t be adopted. Refresh this surface to see what is actually there.',
  conflict:
    'AGP already accounts for that repository. It is either registered as a template, the platform’s own runtime-infra repo, or a materialized agent’s repository — none of which can be adopted as a template.',
  validation:
    'That name isn’t a legal template name. AGP requires lowercase letters, digits and hyphens, starting with a letter.',
};

export interface ClassifiedErrorView {
  message: string;
  retryable: boolean;
}

export interface RolloutErrorView extends ClassifiedErrorView {
  kind: RolloutErrorKind | null;
}

export function classifyRolloutFailure(failure: ApiFailure): RolloutErrorKind | null {
  return refusalKind(ROLLOUT_ERROR_CODES, failure);
}

export function classifyRolloutError(err: unknown, fallback: string): RolloutErrorView {
  const raw = typeof err === 'string' ? err.trim() : err instanceof Error && err.message ? err.message.trim() : '';
  const kind = isApiFailure(err) ? classifyRolloutFailure(err) : null;
  if (kind === null) return { message: raw || fallback, retryable: false, kind: null };
  const retryable = ROLLOUT_RETRYABLE_KINDS.includes(kind);
  return {
    message: retryable ? `${ROLLOUT_MESSAGES[kind]} ${ROLLOUT_RETRY_HINT}` : ROLLOUT_MESSAGES[kind],
    retryable,
    kind,
  };
}
