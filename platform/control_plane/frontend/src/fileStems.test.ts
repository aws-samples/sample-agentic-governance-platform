import { describe, expect, it } from 'vitest';

const sourceFiles = Object.keys(import.meta.glob('/src/**/*.{ts,tsx}'));

describe('source file stems', () => {
  it('no two source files under src share a stem case-insensitively, so a .ts and a .tsx can never collide on a case-insensitive filesystem', () => {
    const byStem = new Map<string, string[]>();
    for (const file of sourceFiles) {
      const stem = file.replace(/\.tsx?$/, '').toLowerCase();
      byStem.set(stem, [...(byStem.get(stem) ?? []), file]);
    }
    const collisions = [...byStem.values()].filter((files) => files.length > 1);
    expect(collisions, collisions.map((files) => files.join(' and ')).join('\n')).toEqual([]);
  });
});
