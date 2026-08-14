import { describe, it, expect } from 'vitest';
import type { PrincipalHit } from '../../../api/client';
import {
  PLATFORM_ROLES,
  ROLE_BADGE,
  isPlatformRole,
  roleLabel,
  filterNewPrincipals,
} from './usersAdminForm';

describe('usersAdminForm', () => {
  it('PLATFORM_ROLES is admin/operator/viewer', () => {
    expect(PLATFORM_ROLES).toEqual(['admin', 'operator', 'viewer']);
  });
  it('ROLE_BADGE has a class + label for every role', () => {
    for (const r of PLATFORM_ROLES) {
      expect(ROLE_BADGE[r].cls.length).toBeGreaterThan(0);
      expect(ROLE_BADGE[r].label.length).toBeGreaterThan(0);
    }
  });
  it('isPlatformRole guards the union', () => {
    expect(isPlatformRole('admin')).toBe(true);
    expect(isPlatformRole('superuser')).toBe(false);
  });
  it('roleLabel maps known roles and passes through unknown', () => {
    expect(roleLabel('admin')).toBe('Admin');
    expect(roleLabel('mystery')).toBe('mystery');
  });
  it('filterNewPrincipals drops already-assigned ids', () => {
    const hits: PrincipalHit[] = [
      { id: 'u1', display_name: 'Ana', type: 'user' },
      { id: 'u2', display_name: 'Bo', type: 'user' },
    ];
    const out = filterNewPrincipals(hits, new Set(['u1']));
    expect(out.map((h) => h.id)).toEqual(['u2']);
  });
});
