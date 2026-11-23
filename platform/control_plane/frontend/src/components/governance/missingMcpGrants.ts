import type { MissingMcpGrant } from '../../api/client';

export function missingMcpGrantsHint(missing: MissingMcpGrant[]): string | null {
  if (missing.length === 0) return null;
  const names = missing.map((m) => m.mcp_name).join(', ');
  const them = missing.length > 1 ? 'them' : 'it';
  return `This agent's harness lists ${names} but has no grant for ${them}, so every invoke fails until granted.`;
}
