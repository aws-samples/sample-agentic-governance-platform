import { describe, it, expect } from 'vitest';
import { sectionFromPath, SECTION_THEME } from './operationsTheme';

describe('sectionFromPath', () => {
  it('maps /ops and its sub-paths to operations', () => {
    for (const p of ['/ops', '/ops/repositories', '/ops/deployments', '/ops/admin']) {
      expect(sectionFromPath(p)).toBe('operations');
    }
  });
  it('maps governance paths to governance', () => {
    for (const p of ['/', '/agents', '/admin', '/marketplace/agents', '/govern/finops']) {
      expect(sectionFromPath(p)).toBe('governance');
    }
  });
  it('treats /ops as an exact segment boundary (no false prefix match)', () => {
    expect(sectionFromPath('/opsomething')).toBe('governance');
    expect(sectionFromPath('/opst')).toBe('governance');
  });
  it('maps a trailing-slash /ops/ to operations and empty string to governance default', () => {
    expect(sectionFromPath('/ops/')).toBe('operations');
    expect(sectionFromPath('')).toBe('governance');
  });
});

describe('SECTION_THEME', () => {
  it('has an entry for both sections', () => {
    expect(Object.keys(SECTION_THEME).sort()).toEqual(['governance', 'operations']);
  });
  it('switch link points each section at the other', () => {
    expect(SECTION_THEME.governance.switchTo).toBe('/ops');
    expect(SECTION_THEME.operations.switchTo).toBe('/');
    expect(SECTION_THEME.governance.switchLabel.length).toBeGreaterThan(0);
    expect(SECTION_THEME.operations.switchLabel.length).toBeGreaterThan(0);
  });
  it('operations user-box text is dark-theme appropriate (light text, not a light-theme leftover)', () => {
    expect(SECTION_THEME.operations.userName).toBe('text-white');
    expect(SECTION_THEME.operations.userName).not.toBe(SECTION_THEME.governance.userName);
    expect(SECTION_THEME.operations.userSub).toBe('text-slate-300');
    expect(SECTION_THEME.operations.userSub).not.toBe(SECTION_THEME.governance.userSub);
  });
});
