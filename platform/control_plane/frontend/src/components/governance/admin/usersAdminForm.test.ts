import { describe, it, expect } from 'vitest';
import type { PrincipalHit } from '../../../api/client';
import { PLATFORM_ROLES, ROLE_BADGE, roleLabel, filterNewPrincipals } from './usersAdminForm';

describe('usersAdminForm', () => {
  it('PLATFORM_ROLES is admin/operator/viewer', () => {
    expect(PLATFORM_ROLES).toEqual(['admin', 'operator', 'viewer']);
  });
  it('ROLE_BADGE has a class + label for every assignable role', () => {
    for (const r of PLATFORM_ROLES) {
      expect(ROLE_BADGE[r].cls.length).toBeGreaterThan(0);
      expect(ROLE_BADGE[r].label.length).toBeGreaterThan(0);
    }
  });
  it('roleLabel names the assignable roles and reads blank for the unmapped role the backend writes as ""', () => {
    expect(roleLabel('admin')).toBe('Admin');
    expect(roleLabel('')).toBe('');
  });
  it('filterNewPrincipals hides principals who already hold a platform role from the Add-user search', () => {
    const hits: PrincipalHit[] = [
      { id: 'u1', display_name: 'Ana', type: 'user', mail: null },
      { id: 'u2', display_name: 'Bo', type: 'user', mail: null },
    ];
    const out = filterNewPrincipals(hits, new Set(['u1']));
    expect(out.map((h) => h.id)).toEqual(['u2']);
  });
});
