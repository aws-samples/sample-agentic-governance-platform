// authConfigGuard.test.ts — the UNCONDITIONAL Entra fail-fast (E36/T19, research item 9).
//
// WHY THIS FILE IS THE CONTRACT. The defect was not that the guard was missing — it was that
// the guard was GATED on `VITE_AUTH_PROVIDER`, a variable typed OPTIONAL
// (`vite-env.d.ts:5`) and written by exactly one build path. Every other path
// (`deploy-frontend.sh`, a bare `npm run build`, CI) could produce a bundle whose
// `.env.production` lacks the key, which made the throw inert and shipped `clientId: ''`
// against the `/common` multi-tenant authority — a sign-in that cannot work, with no
// diagnostic. So the two things worth pinning are exactly the two the old code got wrong:
//
//   • the throw does NOT depend on `VITE_AUTH_PROVIDER` in ANY of its states (absent,
//     empty, 'entra', or something else). It is asserted key-by-key against a matrix of
//     provider values, because "the guard is unconditional" is a claim about a variable that
//     is no longer read at all;
//   • there is NO fallback. `clientId: ''` and the `/common` authority are the values the
//     old module produced when the throw was skipped, so both are asserted UNREACHABLE
//     rather than merely absent from the source.
//
// The unusable-value predicate is pinned against its TWIN in
// `infrastructure/scripts/deploy-full.sh:86-91` (`frontend_var_unusable`). Two spellings of
// one rule is the drift this epic exists to remove: a value the deploy preflight refuses must
// be a value the bundle refuses, and vice versa.

import { describe, expect, it } from 'vitest';

import {
  AUTH_ENV_KEYS,
  isUnusableAuthValue,
  resolveAuthConfig,
} from './authConfigGuard';

/** A tenant/client pair that is genuinely usable — real GUIDs, shaped like `frontend/.env`. */
const GOOD = {
  [AUTH_ENV_KEYS.tenantId]: 'c244fa39-b8de-4f97-9f36-d053103d65c4',
  [AUTH_ENV_KEYS.clientId]: '1e335d0e-0af8-4699-acd4-d78d04acb585',
} as const;

// Every state `VITE_AUTH_PROVIDER` can be in. The var is typed `?: 'entra'`, so `undefined`
// and `'entra'` are the declared ones; the other two are what a hand-written or
// pre-E34/T11-generated `.env.production` actually contains.
const PROVIDER_STATES: (string | undefined)[] = [undefined, '', 'entra', 'something-else'];

// The unusable forms, one per clause of `frontend_var_unusable`. Kept as (label, value) pairs
// so a failure names WHICH clause regressed.
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

describe('isUnusableAuthValue — the twin of deploy-full.sh:86-91', () => {
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

  it('does NOT reject a GUID that merely starts with a zero group', () => {
    // The `deploy-full.sh` clause is the full four-group all-zero prefix, not "starts with
    // 0". A tenant whose real GUID opens with `00000000-` must not be refused; narrowing
    // this predicate to be "safer" would refuse a valid deployment.
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

  it('ignores every other key it is handed', () => {
    // It is handed the whole of `import.meta.env`. Reading only its two keys is what makes
    // the "regardless of VITE_AUTH_PROVIDER" property structural rather than asserted.
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

describe('resolveAuthConfig — throws REGARDLESS of VITE_AUTH_PROVIDER', () => {
  // The regression net for the actual defect. Each unusable value is asserted against every
  // provider state: the old module threw only in the `'entra'` column and returned a broken
  // config in the other three.
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
    // `cp .env.example .env` and forget is the common way sign-in dies, and the value LOOKS
    // filled in — so the message must say "or still a placeholder", exactly as the deploy
    // preflight does, or the operator stares at a populated file.
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

  it('does NOT tell the reader to set VITE_AUTH_PROVIDER', () => {
    // The old message was "VITE_AUTH_PROVIDER=entra requires …", which pointed the reader at
    // the variable that no longer participates. Setting it would now fix nothing.
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
    // `clientId: clientId || ''` (old `msalConfig.ts:22`). An empty client id is the value
    // MSAL happily builds a request URL from, which is how this shipped unnoticed.
    for (const value of UNUSABLE_VALUES) {
      expect(() =>
        resolveAuthConfig({ ...GOOD, [AUTH_ENV_KEYS.clientId]: value[1] }),
      ).toThrow();
    }
  });

  it('never returns the /common multi-tenant authority', () => {
    // Old `msalConfig.ts:23-25`. This is the harmful half: `/common` is a WORKING authority
    // pointing at the wrong directory, so sign-in proceeds and every API call 401s on issuer.
    for (const value of UNUSABLE_VALUES) {
      expect(() =>
        resolveAuthConfig({ ...GOOD, [AUTH_ENV_KEYS.tenantId]: value[1] }),
      ).toThrow();
    }
    // And no usable tenant can produce it either, short of literally being named `common`
    // (a deliberate act, refused by the backend's issuer check — see the module comment).
    expect(resolveAuthConfig({ ...GOOD }).authority).not.toContain('/common');
  });
});
