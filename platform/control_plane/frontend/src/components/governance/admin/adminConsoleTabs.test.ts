import { describe, it, expect } from 'vitest';
import { ADMIN_CONSOLE_TABS } from './adminConsoleTabs';

describe('ADMIN_CONSOLE_TABS', () => {
  it('has the marketplace-approvals tab as the first (current) tool', () => {
    expect(ADMIN_CONSOLE_TABS[0].key).toBe('marketplace-approvals');
    expect(ADMIN_CONSOLE_TABS[0].label).toBe('Marketplace Approvals');
  });
  it('every tab has a non-empty key and label', () => {
    for (const t of ADMIN_CONSOLE_TABS) {
      expect(t.key.length).toBeGreaterThan(0);
      expect(t.label.length).toBeGreaterThan(0);
    }
  });
  it('keys are unique', () => {
    const keys = ADMIN_CONSOLE_TABS.map((t) => t.key);
    expect(new Set(keys).size).toBe(keys.length);
  });
  it('includes the users tab', () => {
    const users = ADMIN_CONSOLE_TABS.find((t) => t.key === 'users');
    expect(users?.label).toBe('Users');
  });
  it('includes the tenants tab', () => {
    const tenants = ADMIN_CONSOLE_TABS.find((t) => t.key === 'tenants');
    expect(tenants?.label).toBe('Tenants');
  });
});
