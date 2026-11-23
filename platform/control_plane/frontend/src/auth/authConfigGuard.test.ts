import { describe, expect, it } from 'vitest';

import {
  AUTH_ENV_KEYS,
  isUnusableAuthValue,
  resolveAuthConfig,
} from './authConfigGuard';

const GOOD = {
  [AUTH_ENV_KEYS.tenantId]: '11111111-2222-3333-4444-555555555555',
  [AUTH_ENV_KEYS.clientId]: '66666666-7777-8888-9999-aaaaaaaaaaaa',
} as const;

const PROVIDER_STATES: (string | undefined)[] = [undefined, '', 'entra', 'something-else'];

const UNUSABLE_VALUES: [string, string | undefined][] = [
  ['absent', undefined],
  ['empty', ''],
  ['whitespace only', '   '],
  ['angle-bracketed stub', '<tenant-id>'],
  ['half-open stub', '<not-filled-in'],
  ['half-closed stub', 'not-filled-in>'],
  ['the example all-zero tenant GUID', '00000000-0000-0000-0000-000000000001'],
  ['the example all-zero client GUID', '00000000-0000-0000-0000-000000000002'],
  ['a fully zero GUID', '00000000-0000-0000-0000-000000000000'],
  ['an all-zero GUID with padding', '  00000000-0000-0000-0000-000000000009  '],
];

describe('isUnusableAuthValue — the twin of unusable() in scripts/ci/deploy-frontend.sh', () => {
  it.each(UNUSABLE_VALUES)('rejects %s', (_label, value) => {
    expect(isUnusableAuthValue(value)).toBe(true);
  });

  it('accepts a real GUID', () => {
    expect(isUnusableAuthValue(GOOD[AUTH_ENV_KEYS.tenantId])).toBe(false);
    expect(isUnusableAuthValue(GOOD[AUTH_ENV_KEYS.clientId])).toBe(false);
  });

  it('accepts a real GUID with surrounding whitespace (a .env file is hand-edited)', () => {
    expect(isUnusableAuthValue(`  ${GOOD[AUTH_ENV_KEYS.clientId]}\n`)).toBe(false);
  });

  it('does not reject a GUID that merely opens with a zero group: the clause is the full four-group prefix', () => {
    expect(isUnusableAuthValue('00000000-1111-2222-3333-444444444444')).toBe(false);
  });
});

describe('resolveAuthConfig — the happy path', () => {
  it('returns the client id and a tenant-specific authority', () => {
    expect(resolveAuthConfig({ ...GOOD })).toEqual({
      clientId: GOOD[AUTH_ENV_KEYS.clientId],
      authority: `https://login.microsoftonline.com/${GOOD[AUTH_ENV_KEYS.tenantId]}`,
    });
  });

  it('trims both values, so a hand-edited .env cannot smuggle whitespace into a URL', () => {
    const config = resolveAuthConfig({
      [AUTH_ENV_KEYS.tenantId]: `  ${GOOD[AUTH_ENV_KEYS.tenantId]}  `,
      [AUTH_ENV_KEYS.clientId]: `\t${GOOD[AUTH_ENV_KEYS.clientId]}\n`,
    });
    expect(config.clientId).toBe(GOOD[AUTH_ENV_KEYS.clientId]);
    expect(config.authority).toBe(
      `https://login.microsoftonline.com/${GOOD[AUTH_ENV_KEYS.tenantId]}`,
    );
  });

  it('ignores every other key it is handed, VITE_AUTH_PROVIDER included', () => {
    expect(
      resolveAuthConfig({
        ...GOOD,
        VITE_AUTH_PROVIDER: 'something-else',
        VITE_API_URL: 'https://example.invalid',
      }),
    ).toEqual({
      clientId: GOOD[AUTH_ENV_KEYS.clientId],
      authority: `https://login.microsoftonline.com/${GOOD[AUTH_ENV_KEYS.tenantId]}`,
    });
  });
});

describe('resolveAuthConfig — throws regardless of VITE_AUTH_PROVIDER', () => {
  for (const provider of PROVIDER_STATES) {
    describe(`with VITE_AUTH_PROVIDER=${JSON.stringify(provider)}`, () => {
      it.each(UNUSABLE_VALUES)('throws on an %s tenant id', (_label, value) => {
        expect(() =>
          resolveAuthConfig({
            ...GOOD,
            [AUTH_ENV_KEYS.tenantId]: value,
            VITE_AUTH_PROVIDER: provider,
          }),
        ).toThrow();
      });

      it.each(UNUSABLE_VALUES)('throws on an %s client id', (_label, value) => {
        expect(() =>
          resolveAuthConfig({
            ...GOOD,
            [AUTH_ENV_KEYS.clientId]: value,
            VITE_AUTH_PROVIDER: provider,
          }),
        ).toThrow();
      });
    });
  }

  it('throws on a completely empty env', () => {
    expect(() => resolveAuthConfig({})).toThrow();
  });
});

describe('resolveAuthConfig — the message names what to fix', () => {
  it('names the one unusable key and not the usable one', () => {
    let message = '';
    try {
      resolveAuthConfig({ ...GOOD, [AUTH_ENV_KEYS.clientId]: undefined });
    } catch (err) {
      message = (err as Error).message;
    }
    expect(message).toContain(AUTH_ENV_KEYS.clientId);
    expect(message).not.toContain(AUTH_ENV_KEYS.tenantId);
  });

  it('names both keys when both are unusable', () => {
    let message = '';
    try {
      resolveAuthConfig({});
    } catch (err) {
      message = (err as Error).message;
    }
    expect(message).toContain(AUTH_ENV_KEYS.tenantId);
    expect(message).toContain(AUTH_ENV_KEYS.clientId);
  });

  it('says a placeholder is as broken as an absent value, and points at a file', () => {
    let message = '';
    try {
      resolveAuthConfig({ ...GOOD, [AUTH_ENV_KEYS.tenantId]: '00000000-0000-0000-0000-000000000001' });
    } catch (err) {
      message = (err as Error).message;
    }
    expect(message).toContain('placeholder');
    expect(message).toContain('.env');
    expect(message).toContain('docs/entra-setup.md');
  });

  it('does not tell the reader to set VITE_AUTH_PROVIDER, which no longer participates', () => {
    let message = '';
    try {
      resolveAuthConfig({});
    } catch (err) {
      message = (err as Error).message;
    }
    expect(message).not.toContain('VITE_AUTH_PROVIDER');
  });
});

describe('resolveAuthConfig — the deleted fallbacks are unreachable', () => {
  it('never returns an empty client id', () => {
    for (const value of UNUSABLE_VALUES) {
      expect(() =>
        resolveAuthConfig({ ...GOOD, [AUTH_ENV_KEYS.clientId]: value[1] }),
      ).toThrow();
    }
  });

  it('never returns the /common multi-tenant authority, the old silent default', () => {
    for (const value of UNUSABLE_VALUES) {
      expect(() =>
        resolveAuthConfig({ ...GOOD, [AUTH_ENV_KEYS.tenantId]: value[1] }),
      ).toThrow();
    }
    expect(resolveAuthConfig({ ...GOOD }).authority).not.toContain('/common');
  });
});
