import type { Provider } from '../../api/client';

export const PROVIDER_LABEL: Record<Provider, string> = {
  github: 'GitHub',
  gitlab: 'GitLab',
};

export const NO_VALUE = '—';

export const OPS_BACK_LABEL = '← Operations';

export function providerLabel(raw: string | null | undefined): string {
  const trimmed = (raw ?? '').trim();
  if (!trimmed) return NO_VALUE;
  const known = PROVIDER_LABEL[trimmed.toLowerCase() as Provider];
  return known ?? trimmed;
}

export interface OrgLabelSource {
  provider: string;
  org: string;
}

export function orgLabel(
  connection: OrgLabelSource | null | undefined,
  connectionId?: string | null,
): string {
  const org = (connection?.org ?? '').trim();
  if (connection && org) return `${providerLabel(connection.provider)} · ${org}`;
  const id = (connectionId ?? '').trim();
  return id || NO_VALUE;
}
