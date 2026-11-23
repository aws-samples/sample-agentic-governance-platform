import { describe, expect, it } from 'vitest';

import { stripComments } from '../../rawSource';
import comingSoonRaw from './comingSoon.tsx?raw';

const src = stripComments(comingSoonRaw);

function component(name: string): string {
  const start = src.indexOf(`function ${name}(`);
  expect(start, name).toBeGreaterThan(-1);
  const rest = src.slice(start + 1);
  const next = rest.search(/\n(export )?function /);
  return next === -1 ? src.slice(start) : src.slice(start, start + 1 + next);
}

function table(name: string): string {
  const match = src.match(new RegExp(`const ${name}: Record<HonestyTone, string> = \\{[^}]*\\}`));
  expect(match, name).not.toBeNull();
  return match?.[0] ?? '';
}

describe('the honesty disclosures keep their visual grammar', () => {
  it('SoonTag is a rounded rectangle, never the pill: shape keeps "this row is not built" apart from "this widget is illustrative"', () => {
    const soonTag = component('SoonTag');
    expect(soonTag).toContain('rounded-[5px]');
    expect(soonTag).not.toContain('rounded-full');
  });

  it('SampleBadge and ComingSoonBadge are the pill', () => {
    expect(component('SampleBadge')).toContain('rounded-full');
    expect(component('ComingSoonBadge')).toContain('rounded-full');
  });

  it('amber, not rose: a not-yet-live feature is a caveat, not a failure, so no table names rose or red', () => {
    expect(src).not.toMatch(/\b(rose|red)-\d/);
    for (const name of ['BANNER_SHELL', 'BANNER_ICON', 'BANNER_TITLE', 'BANNER_BODY', 'BADGE', 'BADGE_DOT']) {
      expect(table(name), name).toContain('amber');
    }
  });

  it('the nav chip is slate, never amber: a nav row is scanned in a dense list', () => {
    const tag = table('TAG');
    expect(tag).toContain('slate');
    expect(tag).not.toContain('amber');
  });

  it('the banner is non-dismissible and polite: role="status", never role="alert", no click or dismiss handler', () => {
    const banner = component('HonestyBanner');
    expect(banner).toContain('role="status"');
    expect(src).not.toContain('role="alert"');
    expect(banner).not.toMatch(/onClick|onDismiss|dismiss/i);
  });
});
