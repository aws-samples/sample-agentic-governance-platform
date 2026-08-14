import { test, expect } from 'vitest';

import { isMaterializeTerminal, nextBadgeFromSteps } from './ProjectDetail';

// The live materialize timeline (E25C/T4) polls until it reaches a terminal state:
// every step done, or one failed. These pin the pure polling-stop predicate the
// AddRepoModal interval reads each tick — no DOM required.

test('polling stops when all steps done', () => {
  const steps = Array(8).fill({ status: 'done' });
  expect(isMaterializeTerminal(steps as any)).toBe(true);
});

test('polling continues while a step runs', () => {
  const steps = [{ status: 'done' }, { status: 'running' }, { status: 'pending' }];
  expect(isMaterializeTerminal(steps as any)).toBe(false);
});

test('polling stops on a failed step', () => {
  const steps = [{ status: 'done' }, { status: 'failed' }, { status: 'pending' }];
  expect(isMaterializeTerminal(steps as any)).toBe(true);
});

// nextBadgeFromSteps maps the timeline onto the repos-table cicd badge palette so the
// modal header pill and the table pill read the same state.
test('badge is failed when any step failed', () => {
  const steps = [{ status: 'done' }, { status: 'failed' }, { status: 'pending' }];
  expect(nextBadgeFromSteps(steps as any)).toBe('failed');
});

test('badge is ready when all steps done', () => {
  const steps = Array(8).fill({ status: 'done' });
  expect(nextBadgeFromSteps(steps as any)).toBe('ready');
});

// In flight the pill maps to 'provisioning' (amber) — the SAME key/color the repos table
// uses for a provisioning repo — so the two never disagree mid-flight. Emerald is reserved
// for the terminal 'ready' state.
test('badge is provisioning while in flight', () => {
  const steps = [{ status: 'done' }, { status: 'running' }, { status: 'pending' }];
  expect(nextBadgeFromSteps(steps as any)).toBe('provisioning');
});
