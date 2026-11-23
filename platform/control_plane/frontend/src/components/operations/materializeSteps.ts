import type { StepState } from '../../api/client';
import type { OPS_BADGE } from './opsUi';

export function isMaterializeTerminal(steps: StepState[]): boolean {
  if (steps.some((s) => s.status === 'failed')) return true;
  return !steps.some((s) => s.status === 'running' || s.status === 'pending');
}

export function nextBadgeFromSteps(steps: StepState[]): keyof typeof OPS_BADGE {
  if (steps.some((s) => s.status === 'failed')) return 'failed';
  if (steps.length > 0 && steps.every((s) => s.status === 'done')) return 'ready';
  return 'provisioning';
}

export function stepStatusText(status: StepState['status']): string {
  return status === 'done' ? 'done' : status === 'running' ? 'running' : status === 'failed' ? 'failed' : 'waiting';
}
