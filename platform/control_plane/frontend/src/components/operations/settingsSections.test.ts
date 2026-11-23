import { describe, it, expect } from 'vitest';
import type { ReactElement } from 'react';
import {
  GITHUB_CONNECTION_SECTION_ID,
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
import connectionsAdminRaw from './ConnectionsAdmin.tsx?raw';
import gitHubLinkRaw from './GitHubLink.tsx?raw';
import settingsRaw from './Settings.tsx?raw';
import sidebarRaw from '../Sidebar.tsx?raw';
import { stripComments } from '../../rawSource';

const connectionsAdminSrc = stripComments(connectionsAdminRaw);
const gitHubLinkSrc = stripComments(gitHubLinkRaw);
const settingsSrc = stripComments(settingsRaw);
const sidebarSrc = stripComments(sidebarRaw);

const VIEWER = 0;
const OPERATOR = 1;

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
    for (const s of SETTINGS_SECTIONS.filter((x) => x.tab === 'admin')) {
      expect(s.gate, s.id).toBe('admin');
    }
  });
});

describe('SETTINGS_SECTIONS — the pinned first sections', () => {
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

describe('showTabStrip — a one-item tablist is suppressed', () => {
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
    for (const level of [VIEWER, OPERATOR, ROLE_LEVEL_ADMIN]) {
      expect(showTabStrip(level), String(level)).toBe(visibleTabs(level).length > 1);
    }
  });
});

describe('settingsSubtitle — never promises a hidden surface', () => {
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
      expect(/administrator/i.test(settingsSubtitle(level)), String(level)).toBe(seesAdmin);
    }
  });
});

describe('section bodies compose frame-less', () => {
  it('the GitHub section does NOT mount the bare page component', () => {
    const section = SETTINGS_SECTIONS.find((s) => s.id === 'github-connection')!;
    expect(section.Component).not.toBe(GitHubLink);
  });

  it('mounts a wrapper that passes embedded: true, inspectable by calling it without rendering GitHubLink', () => {
    const section = SETTINGS_SECTIONS.find((s) => s.id === 'github-connection')!;
    const el = (section.Component as unknown as () => ReactElement<{ embedded?: boolean }>)();
    expect(el.type).toBe(GitHubLink);
    expect(el.props.embedded).toBe(true);
  });

  it('no section mounts a component that renders its own OpsPage frame', () => {
    for (const s of SETTINGS_SECTIONS) {
      const src = SECTION_BODY_SRC[s.id];
      expect(src, `${s.id} has no source registered in SECTION_BODY_SRC`).toBeDefined();
      if (src.includes('OpsPage')) {
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
    expect(resolveInitialTab(ROLE_LEVEL_ADMIN, '#org-connections')).toBe('admin');
    expect(resolveInitialTab(ROLE_LEVEL_ADMIN, '#github-connection')).toBe('general');
  });

  it('accepts the fragment with or without its leading #', () => {
    expect(resolveInitialTab(ROLE_LEVEL_ADMIN, 'org-connections')).toBe('admin');
  });

  it('ignores a fragment naming a section the caller cannot see', () => {
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

describe('the GitHub-connection section id has one owner', () => {
  it('is exported as a constant that names a registered section on the General tab, so a renamed id cannot leave a deep link pointing at the top of Settings', () => {
    const section = SETTINGS_SECTIONS.find((s) => s.id === GITHUB_CONNECTION_SECTION_ID);
    expect(section?.tab).toBe('general');
  });

  it("Sidebar.tsx builds the profile menu's Settings deep link from the constant and never retypes the fragment", () => {
    expect(sidebarSrc).toContain('GITHUB_CONNECTION_SECTION_ID');
    expect(sidebarRaw).not.toContain('#github-connection');
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
    expect(settingsSrc).not.toContain("'General'");
    expect(settingsSrc).not.toContain("'Admin'");
  });

  it('carries the accessible tablist contract, with the panel role attached only when a tablist exists and no aria-current on a client-side tab', () => {
    expect(settingsSrc).toContain('role="tablist"');
    expect(settingsSrc).toContain('role="tab"');
    expect(settingsSrc).toContain('aria-selected');
    expect(settingsSrc).toContain('aria-controls');
    expect(settingsSrc).toContain('aria-labelledby');
    expect(settingsSrc).toMatch(/role[=:]\s*'?"?tabpanel/);
    expect(settingsSrc).toContain('tabPanelId(');
    expect(settingsSrc).not.toContain('aria-current');
  });

  it('re-uses the roving-tabindex model instead of re-deriving arrow-key arithmetic', () => {
    expect(settingsSrc).toContain('nextTabKey');
  });

  it('takes the strip condition and the subtitle from the registry', () => {
    expect(settingsSrc).toContain('showTabStrip(');
    expect(settingsSrc).toContain('settingsSubtitle(');
    expect(settingsSrc).not.toContain('for administrators');
  });

  it('does not open-code the `role_level >= 2` gate', () => {
    expect(settingsSrc).not.toContain('>= 2');
    expect(settingsSrc).not.toContain('role_level ?? 0) >=');
  });
});
