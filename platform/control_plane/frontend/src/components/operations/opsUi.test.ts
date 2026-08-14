import { describe, it, expect } from 'vitest';
import { OPS_CARD, OPS_HEADING, OPS_BADGE, OPS_PRIMARY_BTN, OPS_TABLE_HEAD } from './opsUi';

describe('opsUi tokens', () => {
  it('OPS_CARD carries an emerald-tinted identity (not the governance white card)', () => {
    expect(OPS_CARD).toMatch(/emerald/);
    expect(OPS_CARD).toMatch(/rounded-xl/);
  });
  it('OPS_PRIMARY_BTN is an emerald button', () => {
    expect(OPS_PRIMARY_BTN).toMatch(/emerald-600/);
  });
  it('OPS_HEADING is a large semibold heading', () => {
    expect(OPS_HEADING).toMatch(/text-3xl/);
    expect(OPS_HEADING).toMatch(/font-semibold/);
  });
  it('OPS_BADGE has every status used in the epic', () => {
    for (const k of ['ready', 'provisioning', 'ok', 'warn', 'failed', 'running', 'pending'] as const) {
      expect(typeof OPS_BADGE[k]).toBe('string');
      expect(OPS_BADGE[k].length).toBeGreaterThan(0);
    }
  });
  it('OPS_TABLE_HEAD styles a header row', () => {
    expect(OPS_TABLE_HEAD).toMatch(/uppercase/);
  });
});
