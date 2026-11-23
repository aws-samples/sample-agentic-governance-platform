export function buildManifestRedirectUrl(origin: string): string {
  return `${origin}/ops/connections/callback`;
}

// GitHub's manifest endpoint takes exactly one form field, `manifest=<JSON>`.
export function manifestFormFields(
  manifest: Record<string, unknown>,
): { name: string; value: string }[] {
  return [{ name: 'manifest', value: JSON.stringify(manifest) }];
}

export function buildInstallSettingsUrl(org: string): string {
  return `https://github.com/organizations/${encodeURIComponent(org.trim())}/settings/installations`;
}
