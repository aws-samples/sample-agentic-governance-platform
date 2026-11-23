import type { McpServer } from '../../api/client';

export const REPO_LOCKED_FIELDS = ['name', 'kind', 'gateway_arn', 'runtime_arn', 'endpoint_url'] as const;

export const WIZARD_GATEWAY_HINT =
  'To build a gateway from an API, create one in Ops → MCP servers → + New MCP server.';

export interface RepoOwnership {
  repoId: string;
  projectId: string | null;
  href: string;
}

type OwnershipFields = Pick<McpServer, 'repo_id' | 'project_id'>;

export function repoOwnership(mcp: Partial<OwnershipFields>): RepoOwnership | null {
  if (!mcp.repo_id) return null;
  return {
    repoId: mcp.repo_id,
    projectId: mcp.project_id ?? null,
    href: `/ops/mcp-servers/${encodeURIComponent(mcp.repo_id)}`,
  };
}

export function repoOwnershipLabel(repoName: string | null): string {
  return repoName ? `Managed by repository ${repoName}` : 'Managed by a repository';
}

export function lockedReason(field: string, repoOwned: boolean): string | null {
  if (!repoOwned) return null;
  if (!(REPO_LOCKED_FIELDS as readonly string[]).includes(field)) return null;
  return 'Managed by its repository — change it there and rebuild.';
}

export function mcpRepoActions(repoOwned: boolean): { reprovision: boolean; deleteViaRepo: boolean } {
  return { reprovision: !repoOwned, deleteViaRepo: repoOwned };
}

export function showGatewayRows(mcp: Pick<McpServer, 'kind' | 'gateway_arn' | 'gateway_url'>): boolean {
  return mcp.kind === 'gateway' || !!mcp.gateway_arn || !!mcp.gateway_url;
}

export function mcpEndpoint(
  mcp: Pick<McpServer, 'endpoint_url' | 'gateway_url'>,
): { url: string; fromGateway: boolean } | null {
  if (mcp.endpoint_url) return { url: mcp.endpoint_url, fromGateway: false };
  if (mcp.gateway_url) return { url: mcp.gateway_url, fromGateway: true };
  return null;
}
