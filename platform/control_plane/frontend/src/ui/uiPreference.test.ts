import { afterEach, describe, expect, it, vi } from 'vitest';
import { getUiFlavor, setUiFlavor, UI_FLAVOR_STORAGE_KEY } from './uiPreference';

// node env: no localStorage global unless a test installs one.
function installStorage(initial: Record<string, string> = {}) {
  const store = new Map(Object.entries(initial));
  const storage = {
    getItem: (k: string) => (store.has(k) ? store.get(k)! : null),
    setItem: (k: string, v: string) => void store.set(k, v),
    removeItem: (k: string) => void store.delete(k),
  };
  vi.stubGlobal('localStorage', storage);
  return store;
}

afterEach(() => vi.unstubAllGlobals());

describe('getUiFlavor', () => {
  it('defaults to classic when storage has no key', () => {
    installStorage();
    expect(getUiFlavor()).toBe('classic');
  });
  it('defaults to classic when no localStorage exists at all (node env)', () => {
    expect(getUiFlavor()).toBe('classic');
  });
  it('returns cloudscape when stored', () => {
    installStorage({ [UI_FLAVOR_STORAGE_KEY]: 'cloudscape' });
    expect(getUiFlavor()).toBe('cloudscape');
  });
  it('falls back to classic on an unrecognized stored value', () => {
    installStorage({ [UI_FLAVOR_STORAGE_KEY]: 'neon' });
    expect(getUiFlavor()).toBe('classic');
  });
  it('falls back to classic when storage.getItem throws', () => {
    vi.stubGlobal('localStorage', { getItem: () => { throw new Error('denied'); } });
    expect(getUiFlavor()).toBe('classic');
  });
  it('returns classic when stored explicitly as classic', () => {
    installStorage({ [UI_FLAVOR_STORAGE_KEY]: 'classic' });
    expect(getUiFlavor()).toBe('classic');
  });
  it('falls back to classic on an empty stored value', () => {
    installStorage({ [UI_FLAVOR_STORAGE_KEY]: '' });
    expect(getUiFlavor()).toBe('classic');
  });
  it('is case-sensitive — a differently-cased value is not recognized', () => {
    installStorage({ [UI_FLAVOR_STORAGE_KEY]: 'Cloudscape' });
    expect(getUiFlavor()).toBe('classic');
  });
  it('ignores values under other keys', () => {
    installStorage({ uiFlavor: 'cloudscape' });
    expect(getUiFlavor()).toBe('classic');
  });
});

describe('setUiFlavor', () => {
  it('persists under the pinned key and round-trips', () => {
    const store = installStorage();
    setUiFlavor('cloudscape');
    expect(store.get(UI_FLAVOR_STORAGE_KEY)).toBe('cloudscape');
    expect(getUiFlavor()).toBe('cloudscape');
  });
  it('never throws when storage is absent or throwing', () => {
    expect(() => setUiFlavor('classic')).not.toThrow();
    vi.stubGlobal('localStorage', { setItem: () => { throw new Error('quota'); } });
    expect(() => setUiFlavor('cloudscape')).not.toThrow();
  });
  it('overwrites a previously stored flavor', () => {
    const store = installStorage({ [UI_FLAVOR_STORAGE_KEY]: 'cloudscape' });
    setUiFlavor('classic');
    expect(store.get(UI_FLAVOR_STORAGE_KEY)).toBe('classic');
    expect(getUiFlavor()).toBe('classic');
  });
});

describe('UI_FLAVOR_STORAGE_KEY', () => {
  it('is the pinned namespaced key', () => {
    expect(UI_FLAVOR_STORAGE_KEY).toBe('agp.uiFlavor');
  });
});
