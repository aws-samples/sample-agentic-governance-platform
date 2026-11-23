// Decision: docs/adr/frontend-auth-config-guard.md

export const AUTH_ENV_KEYS = {
  tenantId: 'VITE_ENTRA_TENANT_ID',
  clientId: 'VITE_ENTRA_SPA_CLIENT_ID',
} as const;

const AUTHORITY_HOST = 'https://login.microsoftonline.com';

export function isUnusableAuthValue(raw: string | undefined): boolean {
  const value = (raw ?? '').trim();
  if (value === '') return true;
  if (value.includes('<') || value.includes('>')) return true;
  return value.startsWith('00000000-0000-0000-0000-');
}

export function resolveAuthConfig(
  raw: Record<string, string | undefined>,
): { clientId: string; authority: string } {
  const tenantId = (raw[AUTH_ENV_KEYS.tenantId] ?? '').trim();
  const clientId = (raw[AUTH_ENV_KEYS.clientId] ?? '').trim();

  const unusable = [
    isUnusableAuthValue(tenantId) ? AUTH_ENV_KEYS.tenantId : null,
    isUnusableAuthValue(clientId) ? AUTH_ENV_KEYS.clientId : null,
  ].filter((key) => key !== null);

  if (unusable.length > 0) {
    throw new Error(
      `Entra sign-in is not configured: ${unusable.join(', ')} ` +
        `${unusable.length === 1 ? 'is' : 'are'} missing or still a placeholder. ` +
        'Set it in platform/control_plane/frontend/.env (see .env.local.example / ' +
        '.env.production.example) and rebuild. Where each value comes from: docs/entra-setup.md.',
    );
  }

  return { clientId, authority: `${AUTHORITY_HOST}/${tenantId}` };
}
