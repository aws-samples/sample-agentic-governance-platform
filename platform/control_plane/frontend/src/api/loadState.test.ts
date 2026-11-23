import { describe, expect, it } from 'vitest';

import { ApiFailure } from './failure';
import { failureOf, patchAction, patchSettled, resolveLoadState, sameDeps, type Settled } from './loadState';

type Rows = readonly string[];

function ready(deps: readonly unknown[], nonce: number, data: Rows, lastData: Rows | null = data): Settled<Rows> {
  return { deps, nonce, outcome: { kind: 'ready', data }, lastData };
}

function failed(deps: readonly unknown[], nonce: number, message: string, lastData: Rows | null): Settled<Rows> {
  return { deps, nonce, outcome: { kind: 'error', message, failure: null }, lastData };
}

describe('sameDeps', () => {
  it('sameDeps is Object.is per slot and false on a length change', () => {
    const args = { tenant: 't1' };
    expect(sameDeps([args, 'a', 1], [args, 'a', 1])).toBe(true);
    expect(sameDeps([{ tenant: 't1' }], [{ tenant: 't1' }])).toBe(false);
    expect(sameDeps([Number.NaN], [Number.NaN])).toBe(true);
    expect(sameDeps([0], [-0])).toBe(false);
    expect(sameDeps(['a'], ['a', 'b'])).toBe(false);
    expect(sameDeps([], [])).toBe(true);
  });
});

describe('resolveLoadState', () => {
  const deps = ['t1', 7];

  it('resolveLoadState answers loading with null data before anything settled', () => {
    expect(resolveLoadState<Rows>(null, deps, 0)).toEqual({ kind: 'loading', data: null });
  });

  it('resolveLoadState answers loading when the settled record belongs to other deps', () => {
    const settled = ready(['t0', 7], 0, ['old']);
    expect(resolveLoadState(settled, deps, 0)).toEqual({ kind: 'loading', data: ['old'] });
  });

  it('resolveLoadState answers loading when the nonce moved on', () => {
    const settled = ready(deps, 0, ['old']);
    expect(resolveLoadState(settled, deps, 1)).toEqual({ kind: 'loading', data: ['old'] });
  });

  it('resolveLoadState returns the ready outcome for matching deps and nonce', () => {
    const settled = ready(deps, 3, ['a', 'b']);
    expect(resolveLoadState(settled, deps, 3)).toEqual({ kind: 'ready', data: ['a', 'b'] });
  });

  it('resolveLoadState adds lastData to an error outcome', () => {
    const settled = failed(deps, 3, 'boom', ['kept']);
    expect(resolveLoadState(settled, deps, 3)).toEqual({ kind: 'error', message: 'boom', failure: null, data: ['kept'] });
    expect(resolveLoadState(failed(deps, 3, 'boom', null), deps, 3)).toEqual({
      kind: 'error',
      message: 'boom',
      failure: null,
      data: null,
    });
  });
});

describe('patchSettled', () => {
  const deps = ['t1'];
  const drop = (rows: Rows) => rows.filter((row) => row !== 'b');

  it('patchSettled ignores a record for other deps', () => {
    const settled = ready(['t0'], 0, ['a', 'b']);
    expect(patchSettled(settled, deps, 0, drop)).toBe(settled);
    expect(patchSettled(null, deps, 0, drop)).toBeNull();
    const errored = failed(deps, 0, 'boom', ['a', 'b']);
    expect(patchSettled(errored, deps, 0, drop)).toBe(errored);
  });

  it('patchSettled rewrites data and lastData of a matching ready record', () => {
    const settled = ready(deps, 0, ['a', 'b']);
    expect(patchSettled(settled, deps, 0, drop)).toEqual(ready(deps, 0, ['a']));
    expect(settled.outcome).toEqual({ kind: 'ready', data: ['a', 'b'] });
  });

  it('a newer nonce wins over a patched record', () => {
    const settled = ready(deps, 0, ['a', 'b']);
    const patched = patchSettled(settled, deps, 0, drop);
    expect(resolveLoadState(patched, deps, 0)).toEqual({ kind: 'ready', data: ['a'] });
    expect(resolveLoadState(patched, deps, 1)).toEqual({ kind: 'loading', data: ['a'] });
    expect(patchSettled(settled, deps, 1, drop)).toBe(settled);
  });
});

describe('failureOf', () => {
  it('failureOf keeps an ApiFailure beside its message', () => {
    const failure = new ApiFailure(409, 'agent.name_taken', 'An agent named x exists', 'An agent named x exists');
    const result = failureOf(failure, 'Request failed');
    expect(result).toEqual({ message: 'An agent named x exists', failure });
    expect(result.failure).toBe(failure);
  });

  it('failureOf prefers a non-empty Error message and falls back to the default text, with no failure', () => {
    expect(failureOf(new Error('boom'), 'Request failed')).toEqual({ message: 'boom', failure: null });
    expect(failureOf(new Error(''), 'Request failed')).toEqual({ message: 'Request failed', failure: null });
    expect(failureOf('boom', 'Request failed')).toEqual({ message: 'Request failed', failure: null });
    expect(failureOf(undefined, 'Request failed')).toEqual({ message: 'Request failed', failure: null });
  });
});

describe('patchAction', () => {
  it('applies while ready, so an optimistic edit lands on the record it was made against', () => {
    expect(patchAction('ready')).toBe('apply');
  });

  it('defers while loading, so an in-flight request is neither restarted nor its edit dropped: one reload follows the settle', () => {
    expect(patchAction('loading')).toBe('defer');
  });

  it('reloads after an error, so a write after a failed read converges on the server', () => {
    expect(patchAction('error')).toBe('reload');
  });
});
