// Decision: docs/adr/frontend-dev-auth-is-a-build-mode.md
export const DEVAUTH_MODE = 'devauth';

export const DEVAUTH_MARKER = 'devauth';

export const DEVAUTH_USER = { username: 'dev@example.com', name: 'Local developer' } as const;

export const DEVAUTH_HEADERS = { 'x-user-email': 'dev@example.com' } as const;

export const SWAPPED_MODULES = ['AuthContext.tsx', 'tokenBroker.ts', 'authBoot.ts'] as const;

export const ARM_DIR = 'src/auth/devauth/';

export function swapTable(mode: string): Record<string, string> {
  if (mode !== DEVAUTH_MODE) return {};
  return Object.fromEntries(SWAPPED_MODULES.map((name) => [`src/auth/${name}`, `${ARM_DIR}${name}`]));
}

export function isArmModule(relativePath: string): boolean {
  return relativePath.split('?')[0].startsWith(ARM_DIR);
}

export function leaks(code: string): boolean {
  return /\bdevauth\b/.test(code);
}

export function leakMessage(mode: string, chunk: string): string {
  return `the dev-auth arm leaked into a ${mode} build (${chunk}): import src/auth/devauth/** from nowhere but vite.config.ts`;
}
