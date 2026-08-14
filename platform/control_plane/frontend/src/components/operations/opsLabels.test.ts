// opsLabels.test.ts — the pure display-string derivations the Ops surface had FORKED
// (E28/T10).
//
// Each helper here replaces a live divergence that was found by reading the code, not by
// a failing test — which is the point: a derivation with no test is a derivation that
// drifts, and every one of the forks below carried a "mirrors X" comment naming the copy
// it had already stopped mirroring. That comment is the smell.
//
// The three fixed here, with their evidence:
//
//   • orgLabel — `Projects.tsx:43` rendered `github · acme` (the RAW wire enum), while
//     `TemplatesAdmin.tsx:57` rendered `GitHub · acme` (the brand casing). Same fact, same
//     surface, two spellings. `ProjectDetail.tsx:59` was a third copy of the lowercase one.
//   • providerLabel — the `github → GitHub` table itself was forked THREE times
//     (TemplatesAdmin, ConnectionsAdmin, RolloutTemplatesModal), which is why the two
//     lowercase `orgLabel` copies existed at all: neither had the table in scope.
//   • initialsFor — `PrincipalPicker.tsx:36` splits on WHITESPACE ONLY, so `jane.doe`
//     yields `JA`; `agentUi.tsx:105` splits on whitespace/dot/underscore/hyphen and
//     yields the correct `JD`. Both live under `components/governance/`, which this epic
//     may not touch, so the correct derivation is (re)established here for the Ops rows
//     that need it and the governance forks are reported to their owner.
//
// Vitest only collects `src/**/*.test.ts`, so these must be pure `.ts` to be pinnable at
// all — the same reason `projectRoles.ts` exists.

import { describe, expect, it } from 'vitest';

import { initialsFor, orgLabel, providerLabel } from './opsLabels';

// ---------------------------------------------------------------------------
// providerLabel — the wire provider enum → its BRAND casing.
// ---------------------------------------------------------------------------
describe('providerLabel', () => {
  it('uses the brand casing for the known providers', () => {
    // The casing is the whole point: `github` is an identifier, `GitHub` is a product
    // name, and a UI that shows the identifier is showing its own database.
    expect(providerLabel('github')).toBe('GitHub');
    expect(providerLabel('gitlab')).toBe('GitLab');
  });

  it('is case- and whitespace-tolerant about the incoming value', () => {
    expect(providerLabel('GITHUB')).toBe('GitHub');
    expect(providerLabel(' gitlab ')).toBe('GitLab');
  });

  it('passes an UNKNOWN provider through unchanged rather than guessing', () => {
    // A provider this build does not know about is a fact, not an error. Echoing the raw
    // value is honest and debuggable; mapping it to "GitHub" would be a lie, and blanking
    // it would hide a real connection. Matches how the repo already treats an unknown
    // provider elsewhere (falling back to the host).
    expect(providerLabel('bitbucket')).toBe('bitbucket');
    expect(providerLabel('forgejo')).toBe('forgejo');
  });

  it('answers a blank value with an em dash, never an empty pill', () => {
    expect(providerLabel('')).toBe('—');
    expect(providerLabel('   ')).toBe('—');
    expect(providerLabel(null)).toBe('—');
    expect(providerLabel(undefined)).toBe('—');
  });
});

// ---------------------------------------------------------------------------
// orgLabel — a connection → the `Provider · org` line the Ops tables show.
// ---------------------------------------------------------------------------
describe('orgLabel', () => {
  it('renders `Provider · org` with the BRAND casing', () => {
    // THE divergence. Two Ops pages rendered this same fact as `github · acme`; a third
    // rendered `GitHub · acme`. The brand casing is correct, so the lowercase copies move.
    expect(orgLabel({ provider: 'github', org: 'acme' })).toBe('GitHub · acme');
    expect(orgLabel({ provider: 'gitlab', org: 'acme' })).toBe('GitLab · acme');
  });

  it('never emits the raw lowercase provider enum for a KNOWN provider', () => {
    for (const provider of ['github', 'gitlab']) {
      expect(orgLabel({ provider, org: 'acme' })).not.toContain(provider);
    }
  });

  it('uses the MIDDLE DOT separator the Ops tables already use', () => {
    // Pinned because the separator is shared with the promotion-audit line and the
    // template modal's title; a hyphen here would read as a different kind of fact.
    expect(orgLabel({ provider: 'github', org: 'acme' })).toContain(' · ');
  });

  it('falls back to the connection id when the connection is not resolvable', () => {
    // A connection can be missing because it was deleted, or because it is not visible to
    // this caller (the Ops tables resolve it best-effort — a 403 must not blank the row).
    // The raw id is the honest thing to show: it is what the repo record actually holds.
    expect(orgLabel(undefined, 'conn-123')).toBe('conn-123');
    expect(orgLabel(null, 'conn-123')).toBe('conn-123');
  });

  it('falls back to the id when the connection resolved but its org is blank', () => {
    // A half-populated connection would otherwise render `GitHub · ` — a trailing
    // separator over nothing, which reads as a rendering bug rather than as missing data.
    expect(orgLabel({ provider: 'github', org: '' }, 'conn-123')).toBe('conn-123');
    expect(orgLabel({ provider: 'github', org: '   ' }, 'conn-123')).toBe('conn-123');
  });

  it('answers an em dash when there is no id to fall back to either', () => {
    expect(orgLabel(undefined)).toBe('—');
    expect(orgLabel(undefined, '')).toBe('—');
  });

  it('still names the org when the provider is unknown', () => {
    // The org is the load-bearing half of the line; an unrecognized provider must not
    // cost the operator the org name.
    expect(orgLabel({ provider: 'bitbucket', org: 'acme' })).toBe('bitbucket · acme');
  });
});

// ---------------------------------------------------------------------------
// initialsFor — a display name / login → up to two letters for an avatar.
// ---------------------------------------------------------------------------
describe('initialsFor', () => {
  it('splits on DOTS, not just whitespace — `jane.doe` is JD', () => {
    // THE divergence, and the reason it matters here specifically: the names this Ops row
    // renders come from `created_by`, which is frequently a dotted login or a UPN rather
    // than a spaced display name. The whitespace-only fork answered `JA` — the first two
    // letters of the FIRST name — which for `jane.doe` and `jane.smith` is the same
    // avatar, i.e. it silently merges two different people.
    expect(initialsFor('jane.doe')).toBe('JD');
    expect(initialsFor('jane.smith')).toBe('JS');
    expect(initialsFor('jane.doe')).not.toBe('JA');
  });

  it('two dotted logins that share a first name get DIFFERENT initials', () => {
    // Stated as its own case because "distinguishes two people" is the actual
    // requirement; the exact letters are just how it is met.
    expect(initialsFor('jane.doe')).not.toBe(initialsFor('jane.smith'));
  });

  it('splits on spaces, underscores and hyphens too', () => {
    expect(initialsFor('Jane Doe')).toBe('JD');
    expect(initialsFor('jane_doe')).toBe('JD');
    expect(initialsFor('claims-triage')).toBe('CT');
  });

  it('takes the FIRST TWO segments, not the first and last', () => {
    // A deliberate choice, and the one place the Ops mock page's copy differs (it took
    // first+last). First-two matches the established `agentUi.initialsFor`, and for the
    // kebab agent names this surface renders (`claims-triage-de`) first+last would give
    // `CD` — two letters that appear in no readable prefix of the name.
    expect(initialsFor('claims-triage-de')).toBe('CT');
    expect(initialsFor('Jane Mary Doe')).toBe('JM');
  });

  it('handles a single segment by taking its first two characters', () => {
    expect(initialsFor('jane')).toBe('JA');
    expect(initialsFor('j')).toBe('J');
  });

  it('is always UPPERCASE and never longer than two characters', () => {
    for (const name of ['jane.doe', 'Jane Doe', 'jane', 'j', 'claims-triage-de', 'ünter.k']) {
      const out = initialsFor(name);
      expect(out).toBe(out.toUpperCase());
      expect(out.length).toBeLessThanOrEqual(2);
      expect(out.length).toBeGreaterThan(0);
    }
  });

  it('answers `?` for an empty or unusable name rather than an empty avatar', () => {
    expect(initialsFor('')).toBe('?');
    expect(initialsFor('   ')).toBe('?');
    expect(initialsFor(null)).toBe('?');
    expect(initialsFor(undefined)).toBe('?');
    expect(initialsFor('...')).toBe('?');
  });

  it('tolerates an email or UPN', () => {
    // `created_by` is an Entra oid or a UPN depending on the writer, so both shapes reach
    // this. The domain must not become the second initial.
    expect(initialsFor('jane.doe@example.com')).toBe('JD');
  });

  it('tolerates repeated and leading separators', () => {
    expect(initialsFor('  jane..doe  ')).toBe('JD');
    expect(initialsFor('-jane-doe')).toBe('JD');
  });
});
