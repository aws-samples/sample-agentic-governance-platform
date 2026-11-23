// Rationale: frontend/README.md "Tests are node-environment only, by design"

import type { ArtifactKind, RepoDeletePreview } from '../../../api/client';
import type { ApiFailure } from '../../../api/failure';
import type { OPS_BADGE } from '../opsUi';
import { classifyRoleFailure } from '../projectRoles';

type BadgeKey = keyof typeof OPS_BADGE;

export const RESOURCE_ITEMS = ['record', 'github', 'image', 'runtime', 'identity'] as const;
export type ResourceKey = typeof RESOURCE_ITEMS[number];

export const RESOURCE_LABEL: Record<ResourceKey, string> = {
  record: 'Platform record + governed agent',
  github: 'Source repository',
  image: 'Container image(s)',
  runtime: 'AgentCore runtime + Terraform state',
  identity: 'Entra identity',
};

export const RESOURCE_HINT: Record<ResourceKey, string> = {
  record: 'This repository’s tracking row and its entry in the agent registry.',
  github: 'The repository on the connected provider, created from the template.',
  image: 'The images built for this agent in the tenant’s container registry.',
  runtime: 'The deployed runtime and the Terraform state that describes it.',
  identity: 'The app registration this agent authenticates as.',
};

export type ResourceKind = ArtifactKind;

export function resourceItemsForKind(kind: ResourceKind = 'agent'): readonly ResourceKey[] {
  return kind === 'mcp_server' ? RESOURCE_ITEMS.filter((k) => k !== 'image') : RESOURCE_ITEMS;
}

export const MCP_RESOURCE_LABEL: Record<ResourceKey, string> = {
  record: 'Platform record + MCP registry entry',
  github: RESOURCE_LABEL.github,
  image: RESOURCE_LABEL.image,
  runtime: 'AgentCore gateway (Terraform-managed)',
  identity: 'Entra MCP app',
};

export const MCP_RESOURCE_HINT: Record<ResourceKey, string> = {
  record: 'This repository’s tracking row and its entry in the MCP server registry.',
  github: RESOURCE_HINT.github,
  image: 'MCP servers build no container image.',
  runtime: 'The gateway the build created and the Terraform state that describes it.',
  identity: 'The app registration agents get tokens for to call this MCP server.',
};

export function resourceLabelFor(key: ResourceKey, kind: ResourceKind = 'agent'): string {
  return (kind === 'mcp_server' ? MCP_RESOURCE_LABEL : RESOURCE_LABEL)[key];
}

export function resourceHintFor(key: ResourceKey, kind: ResourceKind = 'agent'): string {
  return (kind === 'mcp_server' ? MCP_RESOURCE_HINT : RESOURCE_HINT)[key];
}

export const RESOURCE_STATES = ['present', 'gone', 'unknown'] as const;
export type ResourceState = typeof RESOURCE_STATES[number];

export function toResourceState(raw: string | null | undefined): ResourceState {
  const s = (raw ?? '').trim().toLowerCase();
  return (RESOURCE_STATES as readonly string[]).includes(s) ? (s as ResourceState) : 'unknown';
}

export const RESOURCE_STATE_LABEL: Record<ResourceState, string> = {
  present: 'Provisioned',
  gone: 'Not found',
  unknown: 'Not established',
};

export const RESOURCE_STATE_BADGE_KEY: Record<ResourceState, BadgeKey> = {
  present: 'ready',
  gone: 'pending',
  unknown: 'unknown',
};

export interface ResourceRow {
  key: ResourceKey;
  label: string;
  hint: string;
  state: ResourceState;
}

export function resourceInventory(
  preview: RepoDeletePreview | null | undefined,
  kind: ResourceKind = 'agent',
): ResourceRow[] {
  const byItem = new Map<string, string>();
  for (const item of preview?.items ?? []) byItem.set(item.item, item.state);
  return resourceItemsForKind(kind).map((key) => ({
    key,
    label: resourceLabelFor(key, kind),
    hint: resourceHintFor(key, kind),
    state: toResourceState(byItem.get(key)),
  }));
}

export type InventoryState = 'loading' | 'forbidden' | 'unread' | 'data';

// Decision: docs/adr/frontend-refusals-classified-by-code.md
export function inventoryState(source: {
  loading: boolean;
  failure: ApiFailure | null;
  preview: RepoDeletePreview | null;
}): InventoryState {
  if (source.loading) return 'loading';
  if (source.failure !== null) {
    return classifyRoleFailure(source.failure) === 'insufficient' ? 'forbidden' : 'unread';
  }
  return source.preview === null ? 'unread' : 'data';
}

export const INVENTORY_STATE_COPY: Record<
  InventoryState,
  { headline: string; detail: string } | null
> = {
  loading: {
    headline: 'Checking what exists…',
    detail: 'Probing each artifact this repository owns. Nothing is changed by this read.',
  },
  forbidden: {
    headline: 'You can’t see this repository’s artifacts',
    detail:
      'Enumerating them needs the Owner role on this project — the same standing the teardown itself needs. Ask an owner if you need this inventory.',
  },
  unread: {
    headline: 'Couldn’t check what exists',
    detail:
      'The reachability probe failed, so which artifacts exist is unknown — not that they are missing. Retry in a moment.',
  },
  data: null,
};
