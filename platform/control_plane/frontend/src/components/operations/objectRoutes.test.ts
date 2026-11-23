import { describe, it, expect } from 'vitest';

import type { Repository } from '../../api/client';
import { repository } from '../../test/wire';
import {
  defaultProjectId,
  OBJECT_NOUN,
  OBJECT_NOUN_INLINE,
  OBJECT_NOUN_PLURAL,
  objectKindFromPath,
  objectListPathFor,
  objectNoun,
  objectPath,
  objectsOfKind,
} from './objectRoutes';
import { PAGE_PATHS } from '../../routes';

describe('objectPath', () => {
  it('routes an agent repo to /ops/agents and an MCP repo to /ops/mcp-servers, by repo id', () => {
    expect(objectPath(repository({ id: 'r1' }))).toBe('/ops/agents/r1');
    expect(objectPath(repository({ id: 'r2', artifact_kind: 'mcp_server' }))).toBe('/ops/mcp-servers/r2');
  });
  it('treats a pre-E40 record with no kind as an agent', () => {
    expect(objectPath({ id: 'r3' } as Repository)).toBe('/ops/agents/r3');
  });
  it('encodes the id', () => {
    expect(objectPath(repository({ id: 'a/b' }))).toBe('/ops/agents/a%2Fb');
  });
});

describe('objectKindFromPath', () => {
  it.each([
    ['/ops/agents', 'agent'],
    ['/ops/agents/r1', 'agent'],
    ['/ops/mcp-servers', 'mcp_server'],
    ['/ops/mcp-servers/r1', 'mcp_server'],
    ['/ops/repositories/r1', null],
    ['/ops/agentsx', null],
    ['/agents/r1', null],
  ])('%s → %s', (p, k) => {
    expect(objectKindFromPath(p)).toBe(k);
  });
});

describe('objectNoun', () => {
  it('the inline noun is the mid-sentence form: agent lowercases, MCP server keeps its casing, and the plural is the list heading', () => {
    expect(OBJECT_NOUN_INLINE).toEqual({ agent: 'agent', mcp_server: 'MCP server' });
    expect(OBJECT_NOUN).toEqual({ agent: 'Agent', mcp_server: 'MCP server' });
    expect(OBJECT_NOUN_PLURAL).toEqual({ agent: 'Agents', mcp_server: 'MCP servers' });
  });
  it('the loaded repo kind wins over the route: a stale link under the wrong prefix still says what the object is', () => {
    expect(objectNoun(repository({ artifact_kind: 'mcp_server' }), 'agent')).toBe('MCP server');
  });
  it('falls back to the route kind, then to "Repository" on the old route', () => {
    expect(objectNoun(null, 'agent')).toBe('Agent');
    expect(objectNoun(null, 'mcp_server')).toBe('MCP server');
    expect(objectNoun(null, null)).toBe('Repository');
  });
  it('list path follows the kind; the old route keeps the old list', () => {
    expect(objectListPathFor('agent')).toBe('/ops/agents');
    expect(objectListPathFor(null)).toBe('/ops/repositories');
  });
});

describe('objectsOfKind', () => {
  const rows = [
    repository({ id: '1', name: 'zeta' }),
    repository({ id: '2', name: 'claims-mcp', artifact_kind: 'mcp_server', agent_id: null }),
    repository({ id: '3', name: 'alpha' }),
  ];
  it('keeps only the kind asked for, sorted', () => {
    expect(objectsOfKind(rows, 'agent').map((r) => r.id)).toEqual(['3', '1']);
    expect(objectsOfKind(rows, 'mcp_server').map((r) => r.id)).toEqual(['2']);
  });
});

describe('defaultProjectId', () => {
  it('preselects the only project', () => {
    expect(defaultProjectId([{ id: 'p1' }], null)).toBe('p1');
  });
  it('preselects the last used one if still visible, else nothing', () => {
    expect(defaultProjectId([{ id: 'p1' }, { id: 'p2' }], 'p2')).toBe('p2');
    expect(defaultProjectId([{ id: 'p1' }, { id: 'p2' }], 'gone')).toBe('');
    expect(defaultProjectId([], 'p1')).toBe('');
  });
});

describe('the object routes are in the route table', () => {
  it('the object lists and object pages exist, and the old repo routes keep working for old links', () => {
    for (const r of ['/ops/agents', '/ops/agents/:id', '/ops/mcp-servers', '/ops/mcp-servers/:id',
      '/ops/repositories', '/ops/repositories/:id']) {
      expect(PAGE_PATHS).toContain(r);
    }
  });
});
