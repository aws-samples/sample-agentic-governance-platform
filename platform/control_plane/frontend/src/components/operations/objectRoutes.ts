import type { Project, Repository } from '../../api/client';
import { repoKindOf } from './repositoryDetailTabs';
import { sortRepoRows } from './repoRowModel';
import type { ArtifactKind } from './templateChain';

export const OBJECT_LIST_PATH: Record<ArtifactKind, string> = {
  agent: '/ops/agents',
  mcp_server: '/ops/mcp-servers',
};

export const OBJECT_NOUN: Record<ArtifactKind, string> = {
  agent: 'Agent',
  mcp_server: 'MCP server',
};

export const OBJECT_NOUN_INLINE: Record<ArtifactKind, string> = {
  agent: 'agent',
  mcp_server: 'MCP server',
};

export const OBJECT_NOUN_PLURAL: Record<ArtifactKind, string> = {
  agent: 'Agents',
  mcp_server: 'MCP servers',
};

export function objectPath(repo: Pick<Repository, 'id' | 'artifact_kind'>): string {
  return `${OBJECT_LIST_PATH[repoKindOf(repo)]}/${encodeURIComponent(repo.id)}`;
}

export function objectKindFromPath(pathname: string): ArtifactKind | null {
  if (pathname === OBJECT_LIST_PATH.agent || pathname.startsWith(`${OBJECT_LIST_PATH.agent}/`)) return 'agent';
  if (pathname === OBJECT_LIST_PATH.mcp_server || pathname.startsWith(`${OBJECT_LIST_PATH.mcp_server}/`)) {
    return 'mcp_server';
  }
  return null;
}

export function objectNoun(
  repo: { artifact_kind?: string | null } | null | undefined,
  routeKind: ArtifactKind | null,
): string {
  if (repo) return OBJECT_NOUN[repoKindOf(repo)];
  return routeKind ? OBJECT_NOUN[routeKind] : 'Repository';
}

export function objectListPathFor(kind: ArtifactKind | null): string {
  return kind ? OBJECT_LIST_PATH[kind] : '/ops/repositories';
}

export function objectsOfKind<T extends Repository>(repos: readonly T[], kind: ArtifactKind): T[] {
  return sortRepoRows(repos.filter((r) => repoKindOf(r) === kind));
}

export const LAST_PROJECT_KEY = 'ops.newObject.lastProjectId';

export function defaultProjectId(projects: readonly Pick<Project, 'id'>[], lastId: string | null): string {
  if (projects.length === 1) return projects[0].id;
  if (lastId && projects.some((p) => p.id === lastId)) return lastId;
  return '';
}
