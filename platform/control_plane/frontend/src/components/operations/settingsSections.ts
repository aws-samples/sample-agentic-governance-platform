// Decision: docs/adr/frontend-absent-standing-renders-nothing.md
// Rationale: frontend/README.md "Tests are node-environment only, by design"

import { type ComponentType, createElement } from 'react';

import ConnectionsAdmin from './ConnectionsAdmin.tsx';
import GitHubLink from './GitHubLink.tsx';

const EmbeddedGitHubLink: ComponentType = () => createElement(GitHubLink, { embedded: true });

/** ADMIN on the backend's role ladder (`core/rbac.py`: VIEWER=0, OPERATOR=1, ADMIN=2). */
export const ROLE_LEVEL_ADMIN = 2;

/** `everyone` is about the caller's own account; `admin` changes platform-wide state. */
export type SectionGate = 'everyone' | 'admin';

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
  id: string;
  tab: SettingsTabKey;
  gate: SectionGate;
  title: string;
  purpose: string;
  Component: ComponentType;
}

export const GITHUB_CONNECTION_SECTION_ID = 'github-connection';

export const SETTINGS_SECTIONS: readonly SettingsSection[] = [
  {
    id: GITHUB_CONNECTION_SECTION_ID,
    tab: 'general',
    gate: 'everyone',
    title: 'Your GitHub account',
    purpose:
      'Link your personal GitHub account so the deployments you trigger here are recorded on GitHub as you, not as the platform App.',
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

export function isSectionVisible(gate: SectionGate, roleLevel: number): boolean {
  return gate === 'everyone' || roleLevel >= ROLE_LEVEL_ADMIN;
}

export function visibleSections(roleLevel: number): SettingsSection[] {
  return SETTINGS_SECTIONS.filter((s) => isSectionVisible(s.gate, roleLevel));
}

export function visibleTabs(roleLevel: number): SettingsTab[] {
  const keys = [...new Set(visibleSections(roleLevel).map((s) => s.tab))];
  return keys.map((key) => ({ key, label: SETTINGS_TAB_LABEL[key] }));
}

export function resolveInitialTab(roleLevel: number, hash: string): SettingsTabKey {
  const sections = visibleSections(roleLevel);
  const wanted = hash.replace(/^#/, '');
  const hit = wanted ? sections.find((s) => s.id === wanted) : undefined;
  if (hit) return hit.tab;
  const tabs = visibleTabs(roleLevel);
  return tabs.length > 0 ? tabs[0].key : 'general';
}

export function showTabStrip(roleLevel: number): boolean {
  return visibleTabs(roleLevel).length > 1;
}

export function settingsSubtitle(roleLevel: number): string {
  const seesAdmin = visibleTabs(roleLevel).some((t) => t.key === 'admin');
  return seesAdmin
    ? 'Your own account, and — for administrators — the platform-wide connections everyone here depends on.'
    : 'Your own account settings for this platform.';
}

export function tabId(key: string): string {
  return `settings-tab-${key}`;
}
export function tabPanelId(key: string): string {
  return `settings-tabpanel-${key}`;
}

export function sectionAnchorId(id: string): string {
  return id;
}
