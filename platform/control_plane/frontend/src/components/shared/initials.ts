export function initialsFor(name: string | null | undefined): string {
  const trimmed = (name ?? '').trim();
  if (!trimmed) return '?';
  const segments = trimmed.split(/[\s._@-]+/).filter(Boolean);
  if (segments.length === 0) return '?';
  if (segments.length >= 2) return (segments[0][0] + segments[1][0]).toUpperCase();
  return segments[0].slice(0, 2).toUpperCase();
}
