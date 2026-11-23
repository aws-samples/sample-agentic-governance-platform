import { describe, expect, expectTypeOf, it } from 'vitest';

import type { Connection } from '../../api/client';
import { orgLabel, providerLabel, type OrgLabelSource } from './opsLabels';

describe('OrgLabelSource is the wire row', () => {
  it("the client's Connection assigns to it, so a row passes straight in", () => {
    expectTypeOf<Connection>().toExtend<OrgLabelSource>();
  });
});

describe('providerLabel', () => {
  it('uses the brand casing for the known providers: github is an identifier, GitHub is a product name', () => {
    expect(providerLabel('github')).toBe('GitHub');
    expect(providerLabel('gitlab')).toBe('GitLab');
  });

  it('is case- and whitespace-tolerant about the incoming value', () => {
    expect(providerLabel('GITHUB')).toBe('GitHub');
    expect(providerLabel(' gitlab ')).toBe('GitLab');
  });

  it('passes an UNKNOWN provider through unchanged, original casing included, rather than guessing or blanking', () => {
    expect(providerLabel('bitbucket')).toBe('bitbucket');
    expect(providerLabel('forgejo')).toBe('forgejo');
    expect(providerLabel('BitBucket')).toBe('BitBucket');
  });

  it('answers a blank value with an em dash, never an empty pill', () => {
    expect(providerLabel('')).toBe('—');
    expect(providerLabel('   ')).toBe('—');
    expect(providerLabel(null)).toBe('—');
    expect(providerLabel(undefined)).toBe('—');
  });
});

describe('orgLabel', () => {
  it('renders `Provider · org` with the BRAND casing, the one spelling for a fact two pages once spelled two ways', () => {
    expect(orgLabel({ provider: 'github', org: 'acme' })).toBe('GitHub · acme');
    expect(orgLabel({ provider: 'gitlab', org: 'acme' })).toBe('GitLab · acme');
  });

  it('never emits the raw lowercase provider enum for a KNOWN provider', () => {
    for (const provider of ['github', 'gitlab']) {
      expect(orgLabel({ provider, org: 'acme' })).not.toContain(provider);
    }
  });

  it('uses the MIDDLE DOT separator the promotion-audit line and the template modal title share', () => {
    expect(orgLabel({ provider: 'github', org: 'acme' })).toContain(' · ');
  });

  it('answers the connection id when the connection is not resolvable: a deleted connection or a 403 on the list must not blank the row', () => {
    expect(orgLabel(undefined, 'conn-123')).toBe('conn-123');
    expect(orgLabel(null, 'conn-123')).toBe('conn-123');
  });

  it('answers the id when the connection resolved but its org is blank: "GitHub · " would read as a rendering bug', () => {
    expect(orgLabel({ provider: 'github', org: '' }, 'conn-123')).toBe('conn-123');
    expect(orgLabel({ provider: 'github', org: '   ' }, 'conn-123')).toBe('conn-123');
  });

  it('answers an em dash when there is no id to fall back to either', () => {
    expect(orgLabel(undefined)).toBe('—');
    expect(orgLabel(undefined, '')).toBe('—');
  });

  it('still names the org when the provider is unknown: the org is the half the operator needs', () => {
    expect(orgLabel({ provider: 'bitbucket', org: 'acme' })).toBe('bitbucket · acme');
  });
});
