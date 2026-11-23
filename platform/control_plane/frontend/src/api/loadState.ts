import { type ApiFailure, isApiFailure } from './failure';

export type Outcome<T> = { kind: 'ready'; data: T } | { kind: 'error'; message: string; failure: ApiFailure | null };

export type LoadState<T> =
  | { kind: 'loading'; data: T | null }
  | { kind: 'ready'; data: T }
  | { kind: 'error'; message: string; failure: ApiFailure | null; data: T | null };

export interface Settled<T> {
  deps: readonly unknown[];
  nonce: number;
  outcome: Outcome<T>;
  lastData: T | null;
}

export function sameDeps(a: readonly unknown[], b: readonly unknown[]): boolean {
  return a.length === b.length && a.every((value, i) => Object.is(value, b[i]));
}

export function resolveLoadState<T>(
  settled: Settled<T> | null,
  deps: readonly unknown[],
  nonce: number,
): LoadState<T> {
  if (settled === null) return { kind: 'loading', data: null };
  if (settled.nonce !== nonce || !sameDeps(settled.deps, deps)) {
    return { kind: 'loading', data: settled.lastData };
  }
  if (settled.outcome.kind === 'error') {
    return { kind: 'error', message: settled.outcome.message, failure: settled.outcome.failure, data: settled.lastData };
  }
  return settled.outcome;
}

export function patchSettled<T>(
  settled: Settled<T> | null,
  deps: readonly unknown[],
  nonce: number,
  update: (data: T) => T,
): Settled<T> | null {
  if (settled === null || settled.nonce !== nonce || !sameDeps(settled.deps, deps)) return settled;
  if (settled.outcome.kind !== 'ready') return settled;
  const data = update(settled.outcome.data);
  return { ...settled, outcome: { kind: 'ready', data }, lastData: data };
}

export function patchAction(kind: LoadState<unknown>['kind']): 'apply' | 'defer' | 'reload' {
  if (kind === 'ready') return 'apply';
  if (kind === 'loading') return 'defer';
  return 'reload';
}

export function failureOf(err: unknown, defaultText: string): { message: string; failure: ApiFailure | null } {
  return {
    message: err instanceof Error && err.message ? err.message : defaultText,
    failure: isApiFailure(err) ? err : null,
  };
}
