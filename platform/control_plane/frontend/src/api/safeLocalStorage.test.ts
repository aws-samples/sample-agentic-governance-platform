import { afterEach, describe, expect, it, vi } from 'vitest';
import { safeLocalStorage } from './safeLocalStorage';

afterEach(() => {
  vi.unstubAllGlobals();
  Reflect.deleteProperty(globalThis, 'localStorage');
});

function installWorkingStorage(initial: Record<string, string> = {}) {
  const store = new Map(Object.entries(initial));
  vi.stubGlobal('localStorage', {
    getItem: (k: string) => (store.has(k) ? store.get(k)! : null),
    setItem: (k: string, v: string) => void store.set(k, v),
    removeItem: (k: string) => void store.delete(k),
    key: (i: number) => [...store.keys()][i] ?? null,
    get length() {
      return store.size;
    },
  });
  return store;
}

function installThrowingStorage() {
  const refuse = () => {
    throw new Error('SecurityError');
  };
  vi.stubGlobal('localStorage', {
    getItem: refuse,
    setItem: refuse,
    removeItem: refuse,
    key: refuse,
    get length(): number {
      return refuse();
    },
  });
}

describe('safeLocalStorage — no storage condition reaches the caller', () => {
  it('round-trips through a working storage, so the wrapper is not a stub', () => {
    const store = installWorkingStorage();
    safeLocalStorage.setItem('agp.k', 'v');
    expect(store.get('agp.k')).toBe('v');
    expect(safeLocalStorage.getItem('agp.k')).toBe('v');
    expect(safeLocalStorage.length).toBe(1);
    expect(safeLocalStorage.key(0)).toBe('agp.k');
    safeLocalStorage.removeItem('agp.k');
    expect(store.has('agp.k')).toBe(false);
    expect(safeLocalStorage.getItem('agp.k')).toBeNull();
  });

  it('never throws when every storage call throws: reads give null and an empty walk', () => {
    installThrowingStorage();
    expect(safeLocalStorage.getItem('agp.k')).toBeNull();
    expect(() => safeLocalStorage.setItem('agp.k', 'v')).not.toThrow();
    expect(() => safeLocalStorage.removeItem('agp.k')).not.toThrow();
    expect(safeLocalStorage.key(0)).toBeNull();
    expect(safeLocalStorage.length).toBe(0);
  });

  it('never throws when reading the localStorage global itself throws, as a sandboxed frame does', () => {
    Object.defineProperty(globalThis, 'localStorage', {
      configurable: true,
      get() {
        throw new Error('SecurityError');
      },
    });
    expect(safeLocalStorage.getItem('agp.k')).toBeNull();
    expect(() => safeLocalStorage.setItem('agp.k', 'v')).not.toThrow();
    expect(() => safeLocalStorage.removeItem('agp.k')).not.toThrow();
    expect(safeLocalStorage.key(0)).toBeNull();
    expect(safeLocalStorage.length).toBe(0);
  });

  it('never throws when there is no localStorage global at all, the default state of the node lane', () => {
    expect(typeof localStorage).toBe('undefined');
    expect(safeLocalStorage.getItem('agp.k')).toBeNull();
    expect(() => safeLocalStorage.setItem('agp.k', 'v')).not.toThrow();
    expect(() => safeLocalStorage.removeItem('agp.k')).not.toThrow();
    expect(safeLocalStorage.key(0)).toBeNull();
    expect(safeLocalStorage.length).toBe(0);
  });
});
