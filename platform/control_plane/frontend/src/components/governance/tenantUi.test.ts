import { describe, it, expect } from 'vitest';
import type { UserTenant } from '../../api/client';
import {
  tenantSelectOptions,
  resolveTenantName,
  findTenantAccount,
  canEditShared,
  sharedBadge,
  tenantNames,
} from './tenantUi';

const stage = (account_id: string) => ({
  account_id,
  region: 'us-east-1',
  ecr_repo_uri: '',
  push_role_arn: '',
  deploy_role_arn: '',
});
const ut = (id: string, name: string): UserTenant => ({
  id,
  name,
  line_of_business: `${name} LoB`,
  platform: 'aws',
  binding_mode: '',
  readiness: {},
  stages: { dev: stage('111111111111'), prod: stage('222222222222') },
});

const RETAIL = ut('t-retail', 'Retail Claims');
const FRAUD = ut('t-fraud', 'Fraud');
const ANNUITIES = ut('t-annuities', 'Annuities');

describe('tenantSelectOptions', () => {
  it('non-admin is locked to the membership list even when a directory is loaded', () => {
    expect(tenantSelectOptions([RETAIL], [RETAIL, FRAUD, ANNUITIES], false)).toEqual([RETAIL]);
  });
  it('admin picks from the full directory once loaded', () => {
    expect(tenantSelectOptions([RETAIL], [FRAUD, ANNUITIES], true)).toEqual([ANNUITIES, FRAUD]);
  });
  it('admin degrades to memberships while the directory is still loading or failed (null)', () => {
    expect(tenantSelectOptions([RETAIL], null, true)).toEqual([RETAIL]);
  });
  it('sorts by name and does not mutate the input', () => {
    const input = [FRAUD, ANNUITIES, RETAIL];
    const out = tenantSelectOptions(input, null, false);
    expect(out.map((t) => t.name)).toEqual(['Annuities', 'Fraud', 'Retail Claims']);
    expect(input.map((t) => t.name)).toEqual(['Fraud', 'Annuities', 'Retail Claims']);
  });
  it('empty memberships yield an empty option list', () => {
    expect(tenantSelectOptions([], null, false)).toEqual([]);
  });
});

describe('resolveTenantName', () => {
  it('resolves from the first directory that knows the id', () => {
    expect(resolveTenantName('t-retail', [RETAIL], null)).toBe('Retail Claims');
  });
  it('falls through to later directories (admin list) when memberships miss', () => {
    expect(resolveTenantName('t-fraud', [RETAIL], [FRAUD])).toBe('Fraud');
  });
  it('returns null for a null/undefined/empty/unknown id', () => {
    expect(resolveTenantName(null, [RETAIL])).toBeNull();
    expect(resolveTenantName(undefined, [RETAIL])).toBeNull();
    expect(resolveTenantName('', [RETAIL])).toBeNull();
    expect(resolveTenantName('t-nope', [RETAIL], [FRAUD])).toBeNull();
  });
});

describe('findTenantAccount', () => {
  it('matches from the memberships first', () => {
    expect(findTenantAccount('t-retail', [RETAIL], [FRAUD])).toEqual(RETAIL);
  });
  it('reads the admin directory when the memberships miss', () => {
    expect(findTenantAccount('t-fraud', [RETAIL], [FRAUD])).toEqual(FRAUD);
  });
  it('returns null when unknown or when the id is missing', () => {
    expect(findTenantAccount('t-nope', [RETAIL], [FRAUD])).toBeNull();
    expect(findTenantAccount(null, [RETAIL], [FRAUD])).toBeNull();
    expect(findTenantAccount(undefined, [], null)).toBeNull();
  });
});

describe('canEditShared', () => {
  it('is ADMIN-only (role_level >= 2)', () => {
    expect(canEditShared(0)).toBe(false);
    expect(canEditShared(1)).toBe(false);
    expect(canEditShared(2)).toBe(true);
    expect(canEditShared(3)).toBe(true);
  });
});

describe('sharedBadge', () => {
  it('maps shared/unshared to distinct badges with cls + label', () => {
    const on = sharedBadge(true);
    const off = sharedBadge(false);
    expect(on.label).toBe('Shared with all tenants');
    expect(off.label).toBe('Tenant-scoped');
    expect(on.cls).not.toBe(off.cls);
    expect(on.cls.length).toBeGreaterThan(0);
    expect(off.cls.length).toBeGreaterThan(0);
  });
});

describe('tenantNames', () => {
  it('joins names with a comma, preserving order', () => {
    expect(tenantNames([RETAIL, FRAUD])).toBe('Retail Claims, Fraud');
  });
  it('returns the empty string for no memberships (hides the sidebar line)', () => {
    expect(tenantNames([])).toBe('');
  });
});
