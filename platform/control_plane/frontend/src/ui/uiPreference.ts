/**
 * UI flavor = which frontend look-and-feel the user wants (Epic 31E). Persisted in
 * localStorage under a single namespaced key so the choice survives reloads per
 * browser/user. Pure logic — no react import — because main.tsx consults it before
 * createRoot. Every storage access is guarded (`typeof localStorage !== 'undefined'`
 * for the node/SSR case) and wrapped in try/catch (private mode, quota, blocked
 * cookies), so the module is loadable anywhere and never throws: unset,
 * unrecognized, or unavailable all resolve to 'classic'.
 *
 * Today 'cloudscape' is parked (the Cloudscape tree lives at tags e31c-complete /
 * e31d-parked); the seam exists so a future epic only has to add the render arm.
 */
export type UiFlavor = 'classic' | 'cloudscape';

/** localStorage key holding the persisted UiFlavor. */
export const UI_FLAVOR_STORAGE_KEY = 'agp.uiFlavor';

/** Fallback used when nothing valid is stored or storage is unusable. */
const DEFAULT_UI_FLAVOR: UiFlavor = 'classic';

function isUiFlavor(value: unknown): value is UiFlavor {
  return value === 'classic' || value === 'cloudscape';
}

/** Persisted flavor; 'classic' when unset, unrecognized, or storage is unavailable/throws. */
export function getUiFlavor(): UiFlavor {
  if (typeof localStorage === 'undefined') return DEFAULT_UI_FLAVOR;
  try {
    const stored = localStorage.getItem(UI_FLAVOR_STORAGE_KEY);
    return isUiFlavor(stored) ? stored : DEFAULT_UI_FLAVOR;
  } catch {
    return DEFAULT_UI_FLAVOR;
  }
}

/** Persists the choice; swallows storage errors (private mode etc.) — never throws. */
export function setUiFlavor(flavor: UiFlavor): void {
  if (typeof localStorage === 'undefined') return;
  try {
    localStorage.setItem(UI_FLAVOR_STORAGE_KEY, flavor);
  } catch {
    // Storage unavailable (private mode, quota, blocked) — the preference just
    // won't persist; the caller's in-memory state stays authoritative for this load.
  }
}
