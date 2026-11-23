export type DriftState = 'connected' | 'not_granted' | 'unused_grant';

export function computeDrift(
  declared: string[] | undefined,
  granted: string[],
): { id: string; state: DriftState }[] {
  const known = declared && declared.length > 0 ? new Set(declared) : null;
  const grantedSet = new Set(granted);
  const rows = [...grantedSet].map((id) => ({
    id,
    state: (!known || known.has(id) ? 'connected' : 'unused_grant') as DriftState,
  }));
  for (const id of known ?? []) {
    if (!grantedSet.has(id)) rows.push({ id, state: 'not_granted' });
  }
  return rows;
}

export type DeclaredServerStatus = 'grantable' | 'unprovisioned' | 'missing' | 'unresolved';

export function declaredServerStatus(
  id: string,
  registry: { id: string; name: string; identity_status: string; entra_sp_id?: string | null }[] | null,
): { name: string; status: DeclaredServerStatus } {
  if (!registry) return { name: id, status: 'unresolved' };
  const mcp = registry.find((m) => m.id === id);
  if (!mcp) return { name: id, status: 'missing' };
  const grantable = mcp.identity_status === 'provisioned' && !!mcp.entra_sp_id;
  return { name: mcp.name, status: grantable ? 'grantable' : 'unprovisioned' };
}
