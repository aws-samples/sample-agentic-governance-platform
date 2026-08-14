// ProjectDetail's pure companion (E27/T11): the tab registry, the roving-tabindex
// keyboard model, and the page-frame load predicate — everything on that page that is a
// DECISION rather than markup, so it can be pinned by a test.
//
// Extracted from ProjectDetail.tsx the moment the tab bar earned real WAI-ARIA tab
// semantics: the roving-tabindex arrow-key model is index arithmetic with wraparound,
// i.e. exactly the "logic" the epic's FE constraint says must live in a pure `.ts`
// (only `src/**/*.test.ts` is collected by vitest, so nothing in a `.tsx` can be
// pinned). The `operationsAdminTabs.ts` / `agentDetailTabs.ts` pair is the precedent.
//
// WHY the tab bar carries real semantics here. The pill bar's VISUALS are the
// OperationsAdmin.tsx:54-69 idiom verbatim (same class strings, active tint +
// shadow-sm, transition-colors only) — only the a11y contract changed:
//   • `role="tablist"` / `role="tab"` / `aria-selected` / `aria-controls`, with
//     `role="tabpanel"` + `aria-labelledby` on the bodies, so the two controls are
//     announced as "tab, selected, 1 of 2" and their relationship to the panel is
//     expressed rather than implied by proximity.
//   • `aria-current="page"` is GONE. It is defined for navigation within a set of
//     PAGES; on a client-side tab it announces "current page" for something the URL
//     flatly contradicts (WCAG 4.1.2 name/role/value).
//   • ONE tab stop for the whole bar (roving tabindex): Tab reaches the selected tab
//     and then moves on into the panel; Left/Right move between tabs. Two plain
//     buttons put every tab in the tab sequence, which is not the expected model.
//
// The repo had ZERO `role="tab"` occurrences before this, so this file is the house
// pattern being set. Back-port `OperationsAdmin.tsx` to it separately — that surface
// is explicitly out of scope for this task, and its class strings are the ones being
// preserved byte-for-byte here.

// ---------------------------------------------------------------------------
// showPageSkeleton — should the page frame be REPLACED by "Loading project…"?
//
// Only on the FIRST load. The page's load effect sets `loading` true on every
// `reloadNonce` bump, and a role mutation now bumps it (the caller's own standing lives
// on that read). Replacing the frame on a REVALIDATION unmounts both tab bodies, which
// destroys the Access tab's `rosterLoaded` — replaying the Grant pop-in on every grant /
// change / revoke — and makes it re-read its roster a second time per mutation.
//
// So: blank only when there is nothing to keep showing. `detail` present ⇒ render the
// last-good page while the refresh lands, which is the conventional shape for a
// nonce-driven refetch. Extracted here because it is the whole of a wiring defect that
// no `.tsx` test can reach (only `src/**/*.test.ts` is collected).
// ---------------------------------------------------------------------------
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

/** DOM ids tying a tab to its panel — one derivation so the two can never disagree. */
export function tabId(key: string): string {
  return `project-tab-${key}`;
}
export function tabPanelId(key: string): string {
  return `project-tabpanel-${key}`;
}

/**
 * The tab the given key press should move focus+selection to, or `null` when the
 * press is not part of the tablist model (let it through untouched).
 *
 * ArrowRight/ArrowLeft step with WRAPAROUND — the APG's horizontal-tablist model —
 * and Home/End jump to the ends. An unknown `current` key resolves from index 0 so a
 * stale selection can never strand the keyboard user.
 */
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
