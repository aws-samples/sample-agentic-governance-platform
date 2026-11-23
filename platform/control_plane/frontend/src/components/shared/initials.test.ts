import { describe, expect, it } from 'vitest';

import { initialsFor } from './initials';

describe('initialsFor', () => {
  it('splits on DOTS, not just whitespace — `jane.doe` is JD: created_by is often a dotted login or a UPN, and a whitespace-only split merges jane.doe with jane.smith', () => {
    expect(initialsFor('jane.doe')).toBe('JD');
    expect(initialsFor('jane.smith')).toBe('JS');
    expect(initialsFor('jane.doe')).not.toBe('JA');
  });

  it('two dotted logins that share a first name get DIFFERENT initials', () => {
    expect(initialsFor('jane.doe')).not.toBe(initialsFor('jane.smith'));
  });

  it('splits on spaces, underscores and hyphens too', () => {
    expect(initialsFor('Jane Doe')).toBe('JD');
    expect(initialsFor('jane_doe')).toBe('JD');
    expect(initialsFor('claims-triage')).toBe('CT');
  });

  it('takes the FIRST TWO segments, not the first and last: claims-triage-de reads CT, where first and last would give CD', () => {
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

  it('tolerates an email or UPN: the domain never becomes the second initial', () => {
    expect(initialsFor('jane.doe@example.com')).toBe('JD');
  });

  it('tolerates repeated and leading separators', () => {
    expect(initialsFor('  jane..doe  ')).toBe('JD');
    expect(initialsFor('-jane-doe')).toBe('JD');
  });
});
