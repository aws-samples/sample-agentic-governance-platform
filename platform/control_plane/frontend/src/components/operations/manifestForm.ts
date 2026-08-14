// Pure helpers for the GitHub App Manifest submit (Epic 20 / U3).
// The manifest handshake POSTs a hidden form to GitHub with the manifest JSON,
// then GitHub redirects the browser back to `redirect_url` with `?code=&state=`.
// These two functions are the load-bearing, non-JSX bits — the SPA callback path
// (U4) reads the same `/ops/connections/callback` route this builder points at.

// Where GitHub returns the browser after the operator approves the App.
export function buildManifestRedirectUrl(origin: string): string {
  return `${origin}/ops/connections/callback`;
}

// The single form field GitHub's manifest endpoint expects: `manifest=<JSON>`.
export function manifestFormFields(
  manifest: Record<string, unknown>,
): { name: string; value: string }[] {
  return [{ name: 'manifest', value: JSON.stringify(manifest) }];
}

// GitHub org "Installed GitHub Apps" settings page — where an operator installs
// (or confirms the install of) the App on their org before finalizing a pending
// connection. Used by the "Finish setup" recovery affordance.
export function buildInstallSettingsUrl(org: string): string {
  return `https://github.com/organizations/${encodeURIComponent(org.trim())}/settings/installations`;
}
