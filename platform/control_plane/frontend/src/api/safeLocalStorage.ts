export const safeLocalStorage = {
  getItem(key: string): string | null {
    try {
      return typeof localStorage === 'undefined' ? null : localStorage.getItem(key);
    } catch {
      return null;
    }
  },

  setItem(key: string, value: string): void {
    try {
      if (typeof localStorage !== 'undefined') localStorage.setItem(key, value);
    } catch {
      return;
    }
  },

  removeItem(key: string): void {
    try {
      if (typeof localStorage !== 'undefined') localStorage.removeItem(key);
    } catch {
      return;
    }
  },

  key(index: number): string | null {
    try {
      return typeof localStorage === 'undefined' ? null : localStorage.key(index);
    } catch {
      return null;
    }
  },

  get length(): number {
    try {
      return typeof localStorage === 'undefined' ? 0 : localStorage.length;
    } catch {
      return 0;
    }
  },
};
