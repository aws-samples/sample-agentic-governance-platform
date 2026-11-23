export function showPageSkeleton(loading: boolean, hasDetail: boolean): boolean {
  return loading && !hasDetail;
}

export interface ProjectDetailTab {
  key: string;
  label: string;
}

export const PROJECT_DETAIL_TABS: readonly ProjectDetailTab[] = [
  { key: 'repositories', label: 'Repositories' },
  { key: 'access', label: 'Access' },
] as const;

export function tabId(key: string): string {
  return `project-tab-${key}`;
}
export function tabPanelId(key: string): string {
  return `project-tabpanel-${key}`;
}

export function nextTabKey(
  keys: readonly string[],
  current: string,
  pressed: string,
): string | null {
  if (keys.length === 0) return null;
  const at = keys.indexOf(current);
  const from = at === -1 ? 0 : at;
  switch (pressed) {
    case 'ArrowRight':
      return keys[(from + 1) % keys.length];
    case 'ArrowLeft':
      return keys[(from - 1 + keys.length) % keys.length];
    case 'Home':
      return keys[0];
    case 'End':
      return keys[keys.length - 1];
    default:
      return null;
  }
}
