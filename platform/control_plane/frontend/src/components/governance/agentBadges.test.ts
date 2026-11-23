import { describe, it, expect } from 'vitest';
import type { LifecycleState, Origin, Kind } from '../../api/client';
import { kindBadge, lifecycleBadge, originBadge, tenantBadge } from './agentBadges';

const LIFECYCLE_STATES: LifecycleState[] = ['proposed', 'pending_approval', 'approved', 'rejected', 'deprecated'];
const ORIGINS: Origin[] = ['Deployed', 'Registered'];
const KINDS: Kind[] = ['gateway', 'standard', 'runtime'];

function colourFamilies(classes: string[]): Set<string> {
  return new Set(classes.flatMap((cls) => [...cls.matchAll(/(?:bg|text)-([a-z]+)-/g)].map((m) => m[1])));
}

describe('agentBadges colour families', () => {
  it("kindBadge colour families (violet/indigo/slate) are disjoint from originBadge's (emerald/amber)", () => {
    const kind = colourFamilies(KINDS.map((k) => kindBadge(k).cls));
    const origin = colourFamilies(ORIGINS.map((o) => originBadge(o).cls));
    expect([...kind].sort()).toEqual(['indigo', 'slate', 'violet']);
    expect([...origin].sort()).toEqual(['amber', 'emerald']);
    expect([...kind].filter((f) => origin.has(f))).toEqual([]);
  });

  it('a non-blank tenantBadge is teal, and teal appears in no lifecycle, origin or kind class', () => {
    expect([...colourFamilies([tenantBadge('Acme').cls])]).toEqual(['teal']);
    const others = colourFamilies([
      ...LIFECYCLE_STATES.map((s) => lifecycleBadge(s).cls),
      ...ORIGINS.map((o) => originBadge(o).cls),
      ...KINDS.map((k) => kindBadge(k).cls),
    ]);
    expect(others.has('teal')).toBe(false);
  });
});
