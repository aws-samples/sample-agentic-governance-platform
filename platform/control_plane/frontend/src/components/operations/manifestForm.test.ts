import { describe, it, expect } from 'vitest';
import {
  buildManifestRedirectUrl,
  manifestFormFields,
  buildInstallSettingsUrl,
} from './manifestForm';

describe('buildManifestRedirectUrl', () => {
  it('appends the SPA callback path to the origin', () => {
    expect(buildManifestRedirectUrl('https://app.example.com')).toBe(
      'https://app.example.com/ops/connections/callback',
    );
  });
  it('works for a localhost dev origin', () => {
    expect(buildManifestRedirectUrl('http://localhost:5173')).toBe(
      'http://localhost:5173/ops/connections/callback',
    );
  });
});

describe('manifestFormFields', () => {
  it('returns a single manifest field carrying the JSON-serialized manifest', () => {
    const manifest = { name: 'agp-acme-provisioning', public: false };
    const fields = manifestFormFields(manifest);
    expect(fields).toEqual([{ name: 'manifest', value: JSON.stringify(manifest) }]);
  });
  it('serializes an empty manifest to an empty object', () => {
    expect(manifestFormFields({})).toEqual([{ name: 'manifest', value: '{}' }]);
  });
});

describe('buildInstallSettingsUrl', () => {
  it('builds the org installations settings URL', () => {
    expect(buildInstallSettingsUrl('acme')).toBe(
      'https://github.com/organizations/acme/settings/installations',
    );
  });
  it('trims and URL-encodes the org', () => {
    expect(buildInstallSettingsUrl('  my org  ')).toBe(
      'https://github.com/organizations/my%20org/settings/installations',
    );
  });
});
