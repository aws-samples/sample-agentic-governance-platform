import { describe, it, expect } from 'vitest';
import { OPS_CARD, OPS_HEADING, OPS_BADGE, OPS_PRIMARY_BTN, OPS_TABLE_HEAD, formatDate } from './opsUi';

describe('opsUi tokens', () => {
  it('OPS_CARD carries an emerald-tinted identity (not the governance white card)', () => {
    expect(OPS_CARD).toMatch(/emerald/);
    expect(OPS_CARD).toMatch(/rounded-xl/);
  });
  it('OPS_PRIMARY_BTN is an emerald button', () => {
    expect(OPS_PRIMARY_BTN).toMatch(/emerald-600/);
  });
  it('OPS_HEADING is a large semibold heading whose emerald identity comes from cards and badges, not the text colour', () => {
    expect(OPS_HEADING).toMatch(/text-3xl/);
    expect(OPS_HEADING).toMatch(/font-semibold/);
    expect(OPS_HEADING).not.toMatch(/emerald/);
  });
  it('OPS_BADGE has every status key, unknown included', () => {
    for (const k of ['ready', 'provisioning', 'ok', 'warn', 'failed', 'running', 'pending', 'unknown'] as const) {
      expect(typeof OPS_BADGE[k]).toBe('string');
      expect(OPS_BADGE[k].length).toBeGreaterThan(0);
    }
  });
  it('OPS_BADGE values are a background and a text colour only: the pill shape is the caller\'s', () => {
    for (const v of Object.values(OPS_BADGE)) expect(v).toMatch(/^bg-\S+ text-\S+$/);
  });
  it('OPS_BADGE tints: emerald for the healthy keys, amber for the in-flight ones, rose for failed, and slate for unknown alone', () => {
    for (const k of ['ready', 'ok', 'running'] as const) expect(OPS_BADGE[k]).toMatch(/emerald/);
    for (const k of ['provisioning', 'warn', 'pending'] as const) expect(OPS_BADGE[k]).toMatch(/amber/);
    expect(OPS_BADGE.failed).toMatch(/rose/);
    expect(OPS_BADGE.unknown).toMatch(/slate/);
    expect(OPS_BADGE.unknown).not.toMatch(/emerald|amber|rose/);
    expect(Object.entries(OPS_BADGE).filter(([, v]) => /slate/.test(v)).map(([k]) => k)).toEqual(['unknown']);
  });
  it('OPS_TABLE_HEAD styles a header row', () => {
    expect(OPS_TABLE_HEAD).toMatch(/uppercase/);
  });
});

describe('formatDate', () => {
  it('renders a dash for a missing value: null, undefined and the empty string', () => {
    expect(formatDate(null)).toBe('—');
    expect(formatDate(undefined)).toBe('—');
    expect(formatDate('')).toBe('—');
  });
  it('returns an unparsable string as it came', () => {
    expect(formatDate('not-a-date')).toBe('not-a-date');
  });
  it('formats a parsable ISO string into a date that keeps the year and is neither the dash nor the raw string', () => {
    const iso = '2026-03-04T10:15:00Z';
    expect(formatDate(iso)).not.toBe('—');
    expect(formatDate(iso)).not.toBe(iso);
    expect(formatDate(iso)).toContain('2026');
  });
  it('formats the instant, so one moment written in two offsets reads the same', () => {
    expect(formatDate('2026-03-04T11:15:00+01:00')).toBe(formatDate('2026-03-04T10:15:00Z'));
  });
});
