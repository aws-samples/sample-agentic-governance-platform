import { test, expect } from 'vitest';

import type { StepState } from '../../api/client';
import { isMaterializeTerminal, nextBadgeFromSteps } from './materializeSteps';

function stepsWith(...statuses: StepState['status'][]): StepState[] {
  return statuses.map((status, i) => ({ key: `step-${i}`, label: `Step ${i}`, status, error: null, started_at: null, completed_at: null }));
}

test('polling stops when all steps done', () => {
  const steps = stepsWith(...Array<StepState['status']>(8).fill('done'));
  expect(isMaterializeTerminal(steps)).toBe(true);
});

test('polling continues while a step runs', () => {
  const steps = stepsWith('done', 'running', 'pending');
  expect(isMaterializeTerminal(steps)).toBe(false);
});

test('polling stops on a failed step', () => {
  const steps = stepsWith('done', 'failed', 'pending');
  expect(isMaterializeTerminal(steps)).toBe(true);
});

test('badge is failed when any step failed: nextBadgeFromSteps maps the timeline onto the repos-table badge palette, so the modal pill and the table pill read the same state', () => {
  const steps = stepsWith('done', 'failed', 'pending');
  expect(nextBadgeFromSteps(steps)).toBe('failed');
});

test('badge is ready when all steps done', () => {
  const steps = stepsWith(...Array<StepState['status']>(8).fill('done'));
  expect(nextBadgeFromSteps(steps)).toBe('ready');
});

test('badge is provisioning while in flight, the same amber key the repos table uses for a provisioning repo, so the two never disagree mid-flight; emerald is reserved for ready', () => {
  const steps = stepsWith('done', 'running', 'pending');
  expect(nextBadgeFromSteps(steps)).toBe('provisioning');
});
