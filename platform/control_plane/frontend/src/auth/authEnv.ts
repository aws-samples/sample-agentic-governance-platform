const DEFAULT_REDIRECT_URI = 'http://localhost:5173/auth/callback';
const DEFAULT_API_SCOPE = 'api://agp/Access.Default';

export function resolveRedirectUri(env: Record<string, string | undefined>): string {
  const value = (env.VITE_ENTRA_SPA_REDIRECT_URI ?? '').trim();
  return value === '' ? DEFAULT_REDIRECT_URI : value;
}

export function resolveApiScope(env: Record<string, string | undefined>): string {
  const value = (env.VITE_ENTRA_SPA_SCOPE ?? '').trim();
  return value === '' ? DEFAULT_API_SCOPE : value;
}
