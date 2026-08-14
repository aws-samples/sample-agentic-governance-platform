// Settings' section registry (E28/T8) — the pure companion to `Settings.tsx`, holding
// everything on that page that is a DECISION rather than markup: which sections exist,
// who may see each one, which tab each lands on, and which tab a deep link opens.
//
// WHY a registry, and why two levels of it. `/ops/settings` replaces the old Operations
// Admin console, which put templates and org connections behind ONE admin gate — but only
// org connections genuinely needs admin rights, so the gate was doing the wrong job at the
// wrong altitude. The fix splits the page by AUDIENCE (General = anyone, Admin = ADMIN),
// and the audience split is exactly the kind of thing that rots if it lives in JSX: the
// brainstorm named 2 sections and a backlog sweep found 11 candidates. So `operationsAdminTabs.ts`'s
// flat `{key,label}[]` idiom is extended ONE level down — sections are the registry, and
// the TAB STRIP IS DERIVED FROM IT (`visibleTabs`). Adding a section is a config entry
// here; adding a whole tab is a `SETTINGS_TAB_LABEL` entry plus sections that name it.
// Neither is a layout change, and neither can produce an empty tab.
//
// This lives in a `.ts` because only `src/**/*.test.ts` is collected by vitest — logic
// placed in the `.tsx` could never be pinned. Precedent: `projectDetailTabs.ts`.
//
// The gate here is a DISPLAY concern and nothing more. It hides a section a caller cannot
// use, so the page doesn't advertise a dead end; the real boundary is the backend's ADMIN
// dependency on the admin routes (`core/rbac.py`), which is unchanged and unreachable from
// here. `ConnectionsAdmin` additionally self-gates to `null` — belt and braces, deliberately.
//
// The explicit `.tsx` on every component import is load-bearing, not noise: on a
// case-insensitive filesystem (macOS, Windows) `./GitHubLink` resolves to the sibling
// pure-logic module `githubLink.ts`, so the import lands on the wrong file. This is a
// COMPILE error, not a silent one — `tsc -b` reports TS1192 ("has no default export") plus
// TS1149 ("differs from already included file name only in casing") and exits non-zero.
// Worth stating precisely, because the failure mode it is easy to assume — a component that
// imports as `undefined` and blanks a section at runtime — is what you get from a
// transform-only tool (`vitest`, `vite build`) that never typechecks. The typecheck gate is
// what catches this; the explicit extension is what avoids needing it to.
// Same reasoning as `githubLinkApi.test.ts:18-20` and `App.tsx`'s import block.

import { type ComponentType, createElement } from 'react';

import ConnectionsAdmin from './ConnectionsAdmin.tsx';
import GitHubLink from './GitHubLink.tsx';

/**
 * `GitHubLink` serves two hosts: its own `/ops/github-link` route, where it owns the page
 * frame, and this registry, where Settings owns it. `embedded` drops the frame — without it
 * the section renders a nested `max-w-7xl`, a SECOND `<h1>` duplicating the band heading,
 * and a second "← Operations" back link inside the tab.
 *
 * Bound HERE, not in `Settings.tsx`, because which props a body needs to compose is
 * registry knowledge — `Settings.tsx` renders `<section.Component />` uniformly and must not
 * grow a per-section special case. A section whose body is a full page is the normal case
 * for this registry (most of the 11 candidates are existing pages), so this is the seam they
 * all use. `ConnectionsAdmin` needs no equivalent: it is already frame-less.
 */
const EmbeddedGitHubLink: ComponentType = () => createElement(GitHubLink, { embedded: true });

/**
 * ADMIN on the backend's role ladder (`core/rbac.py`: VIEWER=0, OPERATOR=1, ADMIN=2).
 * Named once here because ~12 frontend surfaces open-code `(user?.role_level ?? 0) >= 2`
 * and this page must not become the thirteenth place the number can drift.
 */
export const ROLE_LEVEL_ADMIN = 2;

/**
 * Who a section is for.
 *  - `everyone`  — about the caller's OWN account or a read anyone may have. No gate.
 *  - `admin`     — changes platform-wide state on behalf of the whole org.
 *
 * Deliberately NOT a role level. `admin` is a statement about the section's altitude; the
 * mapping from altitude to a number is `isSectionVisible`'s single job, so widening the
 * ladder later (an OPERATOR tier, a per-tenant gate) touches one function.
 */
export type SectionGate = 'everyone' | 'admin';

/** Tab keys, and the ONLY place a tab label is written. */
export const SETTINGS_TAB_LABEL = {
  general: 'General',
  admin: 'Admin',
} as const;

export type SettingsTabKey = keyof typeof SETTINGS_TAB_LABEL;

export interface SettingsTab {
  key: SettingsTabKey;
  label: string;
}

export interface SettingsSection {
  /** Stable slug — also the URL fragment, so it is part of the page's public surface. */
  id: string;
  /** Which tab this section renders under. */
  tab: SettingsTabKey;
  /** Who may see it. */
  gate: SectionGate;
  /** Band heading. */
  title: string;
  /**
   * One line saying what this section is FOR, above the controls. With 11 sections on the
   * horizon, a band whose purpose is only inferable from its widgets is a band that gets
   * scrolled past — so the copy is required by the type, not left to each body.
   */
  purpose: string;
  /** The body. Mounted, never re-implemented — these are the existing, live components. */
  Component: ComponentType;
}

/**
 * The registry. ORDER IS THE PAGE ORDER within a tab, and the first section of a tab is
 * that tab's landing content — so put the thing most callers came for first.
 *
 * To add a section: append an entry. Nothing else. To add a tab: add a
 * `SETTINGS_TAB_LABEL` key, then give a section that `tab` — the strip picks it up.
 */
export const SETTINGS_SECTIONS: readonly SettingsSection[] = [
  {
    id: 'github-connection',
    tab: 'general',
    gate: 'everyone',
    title: 'Your GitHub account',
    // Attribution is the entire point and is not self-evident from the word "link" — the
    // same reason GitHubLink leads with its own explainer card.
    purpose:
      'Link your personal GitHub account so the deployments you trigger here are recorded on GitHub as you, not as the platform App.',
    // The frame-less form — see EmbeddedGitHubLink. Mounting `GitHubLink` bare here is the
    // exact bug this wrapper exists to prevent, and a test pins the distinction.
    Component: EmbeddedGitHubLink,
  },
  {
    id: 'org-connections',
    tab: 'admin',
    gate: 'admin',
    title: 'Organization connections',
    purpose:
      'The GitHub organizations and GitLab groups AGP can reach on behalf of everyone, and the Secrets-Manager-backed credential behind each one.',
    Component: ConnectionsAdmin,
  },
];

/** Does a caller at `roleLevel` get to see a section with this gate? */
export function isSectionVisible(gate: SectionGate, roleLevel: number): boolean {
  return gate === 'everyone' || roleLevel >= ROLE_LEVEL_ADMIN;
}

/**
 * The sections this caller may see, in registry order. A FILTER — it never reorders or
 * rewrites an entry, so the rendered page and the registry always read the same way.
 */
export function visibleSections(roleLevel: number): SettingsSection[] {
  return SETTINGS_SECTIONS.filter((s) => isSectionVisible(s.gate, roleLevel));
}

/**
 * The tab strip, DERIVED: the distinct tabs of the sections this caller can see, in
 * first-appearance order. Two consequences worth stating, because both are the point:
 *  • a viewer never sees an Admin tab — not a disabled one, not an empty one. An
 *    unavailable capability is ABSENT (the E27 rule that also governs GitHubLink's
 *    `action === null ⇒ no button`); a greyed tab would advertise the dead end instead.
 *  • a tab with no visible sections cannot render, so no future gate combination can
 *    produce a tab that opens onto nothing.
 */
export function visibleTabs(roleLevel: number): SettingsTab[] {
  const keys = [...new Set(visibleSections(roleLevel).map((s) => s.tab))];
  return keys.map((key) => ({ key, label: SETTINGS_TAB_LABEL[key] }));
}

/**
 * Which tab to open on mount, honouring a `#section-id` deep link (the profile menu links
 * straight to `#github-connection`).
 *
 * A fragment naming a section the caller cannot see is IGNORED rather than obeyed: a viewer
 * following an admin's bookmark should land on something, and switching them to a tab that
 * would render nothing is worse than dropping the hint. Same for an unknown fragment.
 */
export function resolveInitialTab(roleLevel: number, hash: string): SettingsTabKey {
  const sections = visibleSections(roleLevel);
  const wanted = hash.replace(/^#/, '');
  const hit = wanted ? sections.find((s) => s.id === wanted) : undefined;
  if (hit) return hit.tab;
  const tabs = visibleTabs(roleLevel);
  return tabs.length > 0 ? tabs[0].key : 'general';
}

/**
 * Should the tab strip render at all?
 *
 * A tablist offering ONE tab announces a choice that does not exist — for a viewer (who sees
 * General only) it is a control that cannot do anything, and screen readers dutifully read it
 * out as "tab, selected, 1 of 1". The sections below it are unchanged either way, so the
 * strip is pure noise at one tab. Same principle as the Admin tab's absence: a capability
 * that isn't there is absent, not decoratively present.
 *
 * Derived from the tab COUNT rather than from the role, so it stays correct as sections are
 * added — the day a second General-visible tab exists, a viewer gets a strip automatically.
 */
export function showTabStrip(roleLevel: number): boolean {
  return visibleTabs(roleLevel).length > 1;
}

/**
 * The page subtitle, which must not promise a surface the reader cannot reach.
 *
 * An admin gets the full sentence; anyone else would otherwise read "and — for
 * administrators — the platform-wide connections everyone here depends on" under a page with
 * no such tab, which reads as either a broken page or a withheld one. Neither is true, and
 * both are worse than simply describing what IS there. Keyed on the Admin tab's visibility
 * rather than on the role directly, so it cannot drift from what the page renders.
 */
export function settingsSubtitle(roleLevel: number): string {
  const seesAdmin = visibleTabs(roleLevel).some((t) => t.key === 'admin');
  return seesAdmin
    ? 'Your own account, and — for administrators — the platform-wide connections everyone here depends on.'
    : 'Your own account settings for this platform.';
}

/** DOM ids tying a tab to its panel — one derivation so the two can never disagree. */
export function tabId(key: string): string {
  return `settings-tab-${key}`;
}
export function tabPanelId(key: string): string {
  return `settings-tabpanel-${key}`;
}

/**
 * The band's DOM id. It IS the section id, unprefixed, because it doubles as the URL
 * fragment `resolveInitialTab` reads — a prefix here would mean two spellings of the same
 * public anchor, and links would rot against whichever one changed.
 */
export function sectionAnchorId(id: string): string {
  return id;
}
