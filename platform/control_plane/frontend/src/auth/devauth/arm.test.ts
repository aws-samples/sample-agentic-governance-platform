import { describe, expect, it } from 'vitest';

import {
  ARM_DIR,
  DEVAUTH_HEADERS,
  DEVAUTH_MARKER,
  DEVAUTH_MODE,
  DEVAUTH_USER,
  SWAPPED_MODULES,
  isArmModule,
  leakMessage,
  leaks,
  swapTable,
} from './arm';

describe('swapTable', () => {
  it('swapTable is empty for production, development, test and any other mode', () => {
    for (const mode of ['production', 'development', 'test', 'staging', '', 'DEVAUTH', 'devauth ']) {
      expect(swapTable(mode), mode).toEqual({});
    }
  });

  it('swapTable names exactly the three twins for devauth', () => {
    expect(SWAPPED_MODULES).toEqual(['AuthContext.tsx', 'tokenBroker.ts', 'authBoot.ts']);
    expect(swapTable(DEVAUTH_MODE)).toEqual({
      'src/auth/AuthContext.tsx': 'src/auth/devauth/AuthContext.tsx',
      'src/auth/tokenBroker.ts': 'src/auth/devauth/tokenBroker.ts',
      'src/auth/authBoot.ts': 'src/auth/devauth/authBoot.ts',
    });
  });
});

describe('the bundle scan', () => {
  it('leaks flags the whole word devauth and passes data-auth-arm', () => {
    expect(leaks('const arm="devauth"')).toBe(true);
    expect(leaks('e("div",{"data-auth-arm":a})')).toBe(false);
    expect(leaks('const x = "devauthx"')).toBe(false);
  });

  it('leakMessage names the mode and the chunk and the fix', () => {
    const message = leakMessage('production', 'assets/index-abc123.js');
    expect(message).toContain('production');
    expect(message).toContain('assets/index-abc123.js');
    expect(message).toContain('vite.config.ts');
    expect(message).toBe(
      'the dev-auth arm leaked into a production build (assets/index-abc123.js): import src/auth/devauth/** from nowhere but vite.config.ts',
    );
  });

  it('the marker is the mode name', () => {
    expect(DEVAUTH_MARKER).toBe(DEVAUTH_MODE);
    expect(DEVAUTH_MODE).toBe('devauth');
    expect(leaks(DEVAUTH_MARKER)).toBe(true);
  });

  it('isArmModule flags every path under src/auth/devauth and passes the Entra arm', () => {
    expect(ARM_DIR).toBe('src/auth/devauth/');
    expect(isArmModule('src/auth/devauth/authBoot.ts')).toBe(true);
    expect(isArmModule('src/auth/devauth/arm.ts?raw')).toBe(true);
    expect(isArmModule('src/auth/authBoot.ts')).toBe(false);
    expect(isArmModule('src/auth/devauthx/arm.ts')).toBe(false);
    expect(isArmModule('../src/auth/devauth/arm.ts')).toBe(false);
  });
});

describe('the developer identity', () => {
  it('DEVAUTH_HEADERS carries the address DEVAUTH_USER names and nothing else', () => {
    expect(DEVAUTH_HEADERS).toEqual({ 'x-user-email': 'dev@example.com' });
    expect(Object.keys(DEVAUTH_HEADERS)).toHaveLength(1);
    expect(DEVAUTH_HEADERS['x-user-email']).toBe(DEVAUTH_USER.username);
    expect(DEVAUTH_USER).toEqual({ username: 'dev@example.com', name: 'Local developer' });
  });
});
