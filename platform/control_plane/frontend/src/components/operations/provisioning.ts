export interface ProvStage {
  key: string;
  label: string;
  status: 'idle' | 'running' | 'done';
}

export function experimentStages(): ProvStage[] {
  return [
    { key: 'allocate', label: 'Allocate sandbox account', status: 'idle' },
    { key: 'directory', label: 'Sync directory groups', status: 'idle' },
    { key: 'collaborators', label: 'Grant collaborators access', status: 'idle' },
    { key: 'ready', label: 'Sandbox ready', status: 'idle' },
  ];
}

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

export function isComplete(stages: ProvStage[]): boolean {
  return stages.every((s) => s.status === 'done');
}

export function mockAccountId(seed: number): string {
  const n = (Math.abs(Math.trunc(seed)) * 2654435761 + 4471209388561) % 1_000_000_000_000;
  const digits = String(n).padStart(12, '0');
  return `aws://${digits.slice(0, 4)}-${digits.slice(4, 8)}-${digits.slice(8, 12)}`;
}
