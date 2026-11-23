// Rationale: frontend/README.md "The UI is Classic, and that is deliberate" (parked Cloudscape arm)
import { safeLocalStorage } from '../api/safeLocalStorage';

export type UiFlavor = 'classic' | 'cloudscape';

export const UI_FLAVOR_STORAGE_KEY = 'agp.uiFlavor';

const DEFAULT_UI_FLAVOR: UiFlavor = 'classic';

function isUiFlavor(value: unknown): value is UiFlavor {
  return value === 'classic' || value === 'cloudscape';
}

export function getUiFlavor(): UiFlavor {
  const stored = safeLocalStorage.getItem(UI_FLAVOR_STORAGE_KEY);
  return isUiFlavor(stored) ? stored : DEFAULT_UI_FLAVOR;
}

export function setUiFlavor(flavor: UiFlavor): void {
  safeLocalStorage.setItem(UI_FLAVOR_STORAGE_KEY, flavor);
}
