import { describe, it, expect } from 'vitest';
import type { ReactElement } from 'react';
import {
  ROLE_LEVEL_ADMIN,
  SETTINGS_SECTIONS,
  SETTINGS_TAB_LABEL,
  isSectionVisible,
  resolveInitialTab,
  sectionAnchorId,
  settingsSubtitle,
  showTabStrip,
  tabId,
  tabPanelId,
  visibleSections,
  visibleTabs,
} from './settingsSections';
import ConnectionsAdmin from './ConnectionsAdmin.tsx';
import GitHubLink from './GitHubLink.tsx';
import connectionsAdminSrc from './ConnectionsAdmin.tsx?raw';
import gitHubLinkSrc from './GitHubLink.tsx?raw';
// Vite's `?raw` rather than `node:fs`: `tsconfig.app.json` sets `types: ["vite/client"]`
// (no `node`), so `node:fs` has no type declarations here — while `vite/client` declares
// `*?raw`. Same idiom as operationsNav.test.ts / githubLinkApi.test.ts. The explicit `.tsx`
// is load-bearing: on a case-insensitive filesystem a name that differs from a sibling
// module only in casing resolves to the sibling (`./GitHubLink` → `githubLink.ts`). `tsc`
// DOES catch that — TS1149 + TS1192, non-zero exit — so the extension is belt to the
// typecheck gate's braces, not a substitute for it.
import settingsSrc from './Settings.tsx?raw';

// Role levels as the backend mirrors them (core/rbac.py ENTRA_ROLE_VIEWER/OPERATOR/ADMIN).
const VIEWER = 0;
const OPERATOR = 1;

// The underlying body component + its source, per section id. Keyed by id so adding a section
// without registering it here FAILS rather than silently skipping the frame-less check below.
const SECTION_BODY_COMPONENT: Record<string, unknown> = {
  'github-connection': GitHubLink,
  'org-connections': ConnectionsAdmin,
};
const SECTION_BODY_SRC: Record<string, string> = {
  'github-connection': gitHubLinkSrc,
  'org-connections': connectionsAdminSrc,
};

describe('SETTINGS_SECTIONS — registry shape', () => {
  it('is non-empty (guards every assertion below against a vacuous pass)', () => {
    expect(SETTINGS_SECTIONS.length).toBeGreaterThan(0);
  });

  it('every section has a stable, unique, slug-shaped id', () => {
    const ids = SETTINGS_SECTIONS.map((s) => s.id);
    for (const id of ids) {
      // Slug-shaped because the id is also the URL fragment a deep link uses.
      expect(id, id).toMatch(/^[a-z][a-z0-9-]*$/);
    }
    expect(new Set(ids).size).toBe(ids.length);
  });

  it('every section has a component to render', () => {
    for (const s of SETTINGS_SECTIONS) {
      expect(typeof s.Component, s.id).toBe('function');
    }
  });

  it('every section has a title and a one-line purpose', () => {
    for (const s of SETTINGS_SECTIONS) {
      expect(s.title.length, s.id).toBeGreaterThan(0);
      expect(s.purpose.length, s.id).toBeGreaterThan(0);
    }
  });

  it('every section declares a known gate and a known tab', () => {
    for (const s of SETTINGS_SECTIONS) {
      expect(['everyone', 'admin'], s.id).toContain(s.gate);
      expect(Object.keys(SETTINGS_TAB_LABEL), s.id).toContain(s.tab);
    }
  });

  it("everything on the 'admin' tab is admin-gated", () => {
    // The tab is LABELLED Admin, so a non-gated section landing there would both mislead
    // and — because tab visibility is derived from section visibility — expose the tab to
    // a viewer. Cheaper to forbid than to explain.
    for (const s of SETTINGS_SECTIONS.filter((x) => x.tab === 'admin')) {
      expect(s.gate, s.id).toBe('admin');
    }
  });
});

describe('SETTINGS_SECTIONS — the pinned first sections (T8 brief)', () => {
  it("General's first section is the personal GitHub connection", () => {
    const general = SETTINGS_SECTIONS.filter((s) => s.tab === 'general');
    expect(general.length).toBeGreaterThan(0);
    expect(general[0].id).toBe('github-connection');
    expect(general[0].gate).toBe('everyone');
  });

  it("Admin's first section is org connections, and it is admin-gated", () => {
    const admin = SETTINGS_SECTIONS.filter((s) => s.tab === 'admin');
    expect(admin.length).toBeGreaterThan(0);
    expect(admin[0].id).toBe('org-connections');
    expect(admin[0].gate).toBe('admin');
  });
});

describe('isSectionVisible', () => {
  it("an 'everyone' section is visible at every role level, including no user at all", () => {
    for (const level of [VIEWER, OPERATOR, ROLE_LEVEL_ADMIN]) {
      expect(isSectionVisible('everyone', level)).toBe(true);
    }
  });

  it("an 'admin' section needs role_level >= ADMIN — an OPERATOR is not enough", () => {
    expect(isSectionVisible('admin', VIEWER)).toBe(false);
    expect(isSectionVisible('admin', OPERATOR)).toBe(false);
    expect(isSectionVisible('admin', ROLE_LEVEL_ADMIN)).toBe(true);
  });

  it('mirrors the backend ladder: ADMIN is 2', () => {
    // core/rbac.py maps ENTRA_ROLE_VIEWER/OPERATOR/ADMIN to 0/1/2 and every other
    // frontend surface open-codes `>= 2`. Drifting this constant would silently widen
    // or narrow every gate at once.
    expect(ROLE_LEVEL_ADMIN).toBe(2);
  });
});

describe('visibleSections', () => {
  it('a viewer gets only the ungated sections — no admin section leaks through', () => {
    const forViewer = visibleSections(VIEWER);
    expect(forViewer.length).toBeGreaterThan(0);
    expect(forViewer.every((s) => s.gate === 'everyone')).toBe(true);
    expect(forViewer.map((s) => s.id)).not.toContain('org-connections');
    expect(forViewer.map((s) => s.id)).toContain('github-connection');
  });

  it('an admin gets every section, in registry order', () => {
    expect(visibleSections(ROLE_LEVEL_ADMIN).map((s) => s.id)).toEqual(
      SETTINGS_SECTIONS.map((s) => s.id),
    );
  });

  it('an OPERATOR sees exactly what a viewer sees (no middle tier on this page)', () => {
    expect(visibleSections(OPERATOR).map((s) => s.id)).toEqual(
      visibleSections(VIEWER).map((s) => s.id),
    );
  });

  it('filters, never reorders or rewrites', () => {
    const forViewer = visibleSections(VIEWER);
    // Same object identities, in the same relative order as the registry.
    for (const s of forViewer) expect(SETTINGS_SECTIONS).toContain(s);
    const order = forViewer.map((s) => SETTINGS_SECTIONS.indexOf(s));
    expect(order).toEqual([...order].sort((a, b) => a - b));
  });
});

describe('visibleTabs', () => {
  it('a viewer sees General and NOT Admin', () => {
    const keys = visibleTabs(VIEWER).map((t) => t.key);
    expect(keys).toContain('general');
    expect(keys).not.toContain('admin');
  });

  it('an OPERATOR still does not see Admin', () => {
    expect(visibleTabs(OPERATOR).map((t) => t.key)).not.toContain('admin');
  });

  it('an admin sees both General and Admin, General first', () => {
    expect(visibleTabs(ROLE_LEVEL_ADMIN).map((t) => t.key)).toEqual(['general', 'admin']);
  });

  it('is DERIVED from the registry, not a hardcoded list', () => {
    // Expected value computed from SETTINGS_SECTIONS: the distinct `tab` values of the
    // sections this role can see, in first-appearance order. Appending a section with a
    // new tab must grow this without touching visibleTabs.
    for (const level of [VIEWER, OPERATOR, ROLE_LEVEL_ADMIN]) {
      const expected = [...new Set(visibleSections(level).map((s) => s.tab))];
      expect(visibleTabs(level).map((t) => t.key)).toEqual(expected);
    }
  });

  it('a tab with no visible section does not render at all', () => {
    for (const level of [VIEWER, OPERATOR, ROLE_LEVEL_ADMIN]) {
      for (const tab of visibleTabs(level)) {
        expect(
          visibleSections(level).some((s) => s.tab === tab.key),
          `${tab.key} @ ${level}`,
        ).toBe(true);
      }
    }
  });

  it('takes every label from SETTINGS_TAB_LABEL', () => {
    for (const t of visibleTabs(ROLE_LEVEL_ADMIN)) {
      expect(t.label).toBe(SETTINGS_TAB_LABEL[t.key]);
      expect(t.label.length).toBeGreaterThan(0);
    }
  });
});

describe('showTabStrip — a one-item tablist is suppressed (FIX-1)', () => {
  it('a viewer gets NO strip: one tab is a choice that does not exist', () => {
    expect(visibleTabs(VIEWER)).toHaveLength(1);
    expect(showTabStrip(VIEWER)).toBe(false);
    expect(showTabStrip(OPERATOR)).toBe(false);
  });

  it('an admin gets the strip, because there is a real choice to make', () => {
    expect(visibleTabs(ROLE_LEVEL_ADMIN).length).toBeGreaterThan(1);
    expect(showTabStrip(ROLE_LEVEL_ADMIN)).toBe(true);
  });

  it('is derived from the tab COUNT, not from the role', () => {
    // So the day a second General-visible tab exists, a viewer gets a strip automatically
    // rather than this needing to be revisited.
    for (const level of [VIEWER, OPERATOR, ROLE_LEVEL_ADMIN]) {
      expect(showTabStrip(level), String(level)).toBe(visibleTabs(level).length > 1);
    }
  });
});

describe('settingsSubtitle — never promises a hidden surface (FIX-1)', () => {
  it("a viewer's subtitle does not mention administrators or platform-wide settings", () => {
    const sub = settingsSubtitle(VIEWER);
    expect(sub.length).toBeGreaterThan(0);
    expect(sub).not.toMatch(/administrator/i);
    expect(sub).not.toMatch(/platform-wide/i);
    expect(sub).not.toMatch(/everyone/i);
    expect(settingsSubtitle(OPERATOR)).toBe(sub);
  });

  it("an admin's subtitle DOES describe the admin half they can reach", () => {
    expect(settingsSubtitle(ROLE_LEVEL_ADMIN)).toMatch(/administrator/i);
  });

  it('differs by what is visible, keyed on the Admin tab rather than the role', () => {
    expect(settingsSubtitle(VIEWER)).not.toBe(settingsSubtitle(ROLE_LEVEL_ADMIN));
    for (const level of [VIEWER, OPERATOR, ROLE_LEVEL_ADMIN]) {
      const seesAdmin = visibleTabs(level).some((t) => t.key === 'admin');
      // The admin clause may appear if and only if the Admin tab is actually reachable.
      expect(/administrator/i.test(settingsSubtitle(level)), String(level)).toBe(seesAdmin);
    }
  });
});

describe('section bodies compose frame-less (FIX-2)', () => {
  // The bug this pins: `GitHubLink` is a full PAGE (its own OpsPage → its own <h1> and its own
  // "← Operations" back link). Mounted bare inside Settings it produced a nested max-w-7xl, a
  // SECOND <h1> duplicating the band heading, and a second back link. It takes `embedded` to
  // suppress that — and nothing detected that the prop was never passed.
  it('the GitHub section does NOT mount the bare page component', () => {
    const section = SETTINGS_SECTIONS.find((s) => s.id === 'github-connection')!;
    expect(section.Component).not.toBe(GitHubLink);
  });

  it('it mounts a wrapper that passes embedded: true', () => {
    const section = SETTINGS_SECTIONS.find((s) => s.id === 'github-connection')!;
    // Calling the wrapper builds the element WITHOUT rendering GitHubLink itself (no hooks
    // run, so this needs no DOM) — which makes the props it binds directly inspectable.
    // Cast via `unknown`: `ComponentType`'s call signature takes props, so TS rightly refuses
    // the direct conversion to a zero-arg function.
    const el = (section.Component as unknown as () => ReactElement<{ embedded?: boolean }>)();
    expect(el.type).toBe(GitHubLink);
    expect(el.props.embedded).toBe(true);
  });

  it('no section mounts a component that renders its own OpsPage frame', () => {
    // Generalized so section 3..11 cannot reintroduce the same double-frame bug. A section
    // body must be frame-less: either natively (ConnectionsAdmin) or via a wrapper (above).
    for (const s of SETTINGS_SECTIONS) {
      const src = SECTION_BODY_SRC[s.id];
      expect(src, `${s.id} has no source registered in SECTION_BODY_SRC`).toBeDefined();
      if (src.includes('OpsPage')) {
        // It IS a page component, so the registry must not mount it directly.
        expect(s.Component, s.id).not.toBe(SECTION_BODY_COMPONENT[s.id]);
      }
    }
  });
});

describe('resolveInitialTab', () => {
  it('lands on the first visible tab with no fragment', () => {
    expect(resolveInitialTab(VIEWER, '')).toBe('general');
    expect(resolveInitialTab(ROLE_LEVEL_ADMIN, '')).toBe('general');
  });

  it('opens the tab that owns the deep-linked section', () => {
    // The profile menu deep-links to #github-connection; a bookmark may name an admin one.
    expect(resolveInitialTab(ROLE_LEVEL_ADMIN, '#org-connections')).toBe('admin');
    expect(resolveInitialTab(ROLE_LEVEL_ADMIN, '#github-connection')).toBe('general');
  });

  it('accepts the fragment with or without its leading #', () => {
    expect(resolveInitialTab(ROLE_LEVEL_ADMIN, 'org-connections')).toBe('admin');
  });

  it('ignores a fragment naming a section the caller cannot see', () => {
    // A viewer following an admin's bookmark gets General, not an empty Admin tab.
    expect(resolveInitialTab(VIEWER, '#org-connections')).toBe('general');
  });

  it('ignores an unknown fragment rather than stranding the page', () => {
    expect(resolveInitialTab(ROLE_LEVEL_ADMIN, '#no-such-section')).toBe('general');
  });
});

describe('DOM id derivations', () => {
  it('are one derivation each, so a tab and its panel can never disagree', () => {
    expect(tabId('general')).toBe('settings-tab-general');
    expect(tabPanelId('general')).toBe('settings-tabpanel-general');
    expect(sectionAnchorId('github-connection')).toBe('github-connection');
  });

  it('the section anchor IS the id, because it is also the URL fragment', () => {
    for (const s of SETTINGS_SECTIONS) expect(sectionAnchorId(s.id)).toBe(s.id);
  });

  it('a tab id can never collide with a panel id', () => {
    const ids = Object.keys(SETTINGS_TAB_LABEL).flatMap((k) => [tabId(k), tabPanelId(k)]);
    expect(new Set(ids).size).toBe(ids.length);
  });
});

describe('Settings.tsx reads the registry rather than restating it', () => {
  it('parses the source (guards the assertions below against an empty read)', () => {
    expect(settingsSrc.length).toBeGreaterThan(500);
    expect(settingsSrc).toContain('OpsPage');
  });

  it('derives its tab strip from visibleTabs', () => {
    expect(settingsSrc).toContain('visibleTabs(');
    expect(settingsSrc).toContain('visibleSections(');
  });

  it('never hardcodes a tab label — labels live only in SETTINGS_TAB_LABEL', () => {
    // The whole point of the registry is that adding a section (or a third tab) is a
    // config entry. A label literal here is the first step back to a layout change.
    expect(settingsSrc).not.toContain("'General'");
    expect(settingsSrc).not.toContain("'Admin'");
  });

  it('carries the accessible tablist contract, not a tenth bare-button pill bar', () => {
    // ProjectDetail.tsx is the app's only accessible tab strip; this is the second.
    expect(settingsSrc).toContain('role="tablist"');
    expect(settingsSrc).toContain('role="tab"');
    expect(settingsSrc).toContain('aria-selected');
    expect(settingsSrc).toContain('aria-controls');
    expect(settingsSrc).toContain('aria-labelledby');
    // The panel's tab semantics are applied via a conditional prop spread (they are attached
    // only when a tablist actually exists to own them — FIX-1), so match either spelling
    // rather than the JSX-attribute form alone.
    expect(settingsSrc).toMatch(/role[=:]\s*'?"?tabpanel/);
    expect(settingsSrc).toContain('tabPanelId(');
    // `aria-current="page"` is for navigation between PAGES — on a client-side tab it
    // announces a "current page" the URL contradicts (WCAG 4.1.2).
    expect(settingsSrc).not.toContain('aria-current');
  });

  it('re-uses the roving-tabindex model instead of re-deriving arrow-key arithmetic', () => {
    expect(settingsSrc).toContain('nextTabKey');
  });

  it('takes the strip condition and the subtitle from the registry (FIX-1)', () => {
    expect(settingsSrc).toContain('showTabStrip(');
    expect(settingsSrc).toContain('settingsSubtitle(');
    // The subtitle must not be a literal in the layout — that is how it drifted from what the
    // page actually renders in the first place.
    expect(settingsSrc).not.toContain('for administrators');
  });

  it('does not open-code the `role_level >= 2` gate', () => {
    // Twelve surfaces open-code this. The gate on THIS page is the registry's, so a
    // comparison literal here means a second, drift-prone authority.
    expect(settingsSrc).not.toContain('>= 2');
    expect(settingsSrc).not.toContain('role_level ?? 0) >=');
  });
});
