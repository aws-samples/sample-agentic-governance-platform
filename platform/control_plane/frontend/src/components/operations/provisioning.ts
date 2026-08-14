// Pure provisioning stage machine for the Operations Build & Run demo (Epic 18).
// Models the staged "spin up an account / ship a build" choreography as plain
// data: an ordered list of ProvStage, advanced one beat at a time by the pure
// `advance` reducer. Both the Experiments page (Task 6, account provisioning) and
// the Deployments page (Task 7, live CI/CD provisioning) drive their animated
// checklists off these exact shapes — keep the keys, labels, and signatures
// stable. No UI, no timers, no randomness here. House style mirrors demoData.ts:
// TS interfaces + pure functions (map/spread, no mutation).

// ─────────────────────────── Stages ───────────────────────────
export interface ProvStage {
  key: string;
  label: string;
  status: 'idle' | 'running' | 'done';
}

/** Experiment (sandbox account) provisioning beats: allocate → sync → grant → ready. */
export function experimentStages(): ProvStage[] {
  return [
    { key: 'allocate', label: 'Allocate sandbox account', status: 'idle' },
    { key: 'directory', label: 'Sync directory groups', status: 'idle' },
    { key: 'collaborators', label: 'Grant collaborators access', status: 'idle' },
    { key: 'ready', label: 'Sandbox ready', status: 'idle' },
  ];
}

/**
 * Deployment (live CI/CD) provisioning beats — consumed by Task 7. The CI/CD trio
 * (build / scan / deploy) is required; the scaffolding beats (pick template /
 * create repository / register Entra app) lead into it.
 */
export function deploymentStages(): ProvStage[] {
  return [
    { key: 'template', label: 'Pick template', status: 'idle' },
    { key: 'repository', label: 'Create repository', status: 'idle' },
    { key: 'entra', label: 'Register Entra app', status: 'idle' },
    { key: 'build', label: 'Build container image', status: 'idle' },
    { key: 'scan', label: 'Scan for vulnerabilities', status: 'idle' },
    { key: 'deploy', label: 'Deploy to account', status: 'idle' },
  ];
}

/**
 * Pure single-beat advance: the first `running` stage flips to `done`; otherwise
 * the first `idle` stage flips to `running`. Returns a fresh array with fresh
 * stage objects — the input is never mutated. When every stage is `done` the
 * input is returned unchanged (idempotent at completion), so a loop converges.
 */
export function advance(stages: ProvStage[]): ProvStage[] {
  const runningIdx = stages.findIndex((s) => s.status === 'running');
  if (runningIdx !== -1) {
    return stages.map((s, i) => (i === runningIdx ? { ...s, status: 'done' as const } : { ...s }));
  }
  const idleIdx = stages.findIndex((s) => s.status === 'idle');
  if (idleIdx !== -1) {
    return stages.map((s, i) => (i === idleIdx ? { ...s, status: 'running' as const } : { ...s }));
  }
  return stages.map((s) => ({ ...s }));
}

/** True only when every stage is `done`. */
export function isComplete(stages: ProvStage[]): boolean {
  return stages.every((s) => s.status === 'done');
}

/**
 * Deterministic mock AWS account id of shape `aws://NNNN-NNNN-NNNN` — 12 digits
 * derived from `seed` arithmetic (no Math.random, no Date), so the same seed
 * always yields the same id. A large odd multiplier + offset spreads small seeds
 * into a full 12-digit space; the result is taken modulo 1e12 and zero-padded.
 */
export function mockAccountId(seed: number): string {
  const n = (Math.abs(Math.trunc(seed)) * 2654435761 + 4471209388561) % 1_000_000_000_000;
  const digits = String(n).padStart(12, '0');
  return `aws://${digits.slice(0, 4)}-${digits.slice(4, 8)}-${digits.slice(8, 12)}`;
}
