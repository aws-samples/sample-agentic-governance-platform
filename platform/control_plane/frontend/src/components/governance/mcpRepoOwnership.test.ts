import { describe, it, expect } from 'vitest';
import {
  REPO_LOCKED_FIELDS,
  WIZARD_GATEWAY_HINT,
  lockedReason,
  mcpRepoActions,
  repoOwnership,
  repoOwnershipLabel,
  mcpEndpoint,
  showGatewayRows,
} from './mcpRepoOwnership';

const base = { kind: 'standard' as const, gateway_arn: null, gateway_url: null };

describe('repoOwnership', () => {
  it('is null for a registered (non-repo) record', () => {
    expect(repoOwnership({ ...base, repo_id: null, project_id: null })).toBeNull();
    expect(repoOwnership({})).toBeNull();
  });

  it('links to the repository detail route for a repo-owned record', () => {
    const own = repoOwnership({ ...base, repo_id: 'r-1', project_id: 'p-1' });
    expect(own).toEqual({ repoId: 'r-1', projectId: 'p-1', href: '/ops/mcp-servers/r-1' });
  });

  it('keeps the link when the project id is missing', () => {
    expect(repoOwnership({ ...base, repo_id: 'r-1' })?.href).toBe('/ops/mcp-servers/r-1');
  });
});

describe('repoOwnershipLabel', () => {
  it('names the repository when known', () => {
    expect(repoOwnershipLabel('claims-api')).toBe('Managed by repository claims-api');
  });
  it('says a repository without naming it while the page has not fetched the name', () => {
    expect(repoOwnershipLabel(null)).toBe('Managed by a repository');
  });
});

describe('locked fields', () => {
  it('locks exactly the five fields PUT /mcp-servers/{id} refuses with 409 on a repo-owned record', () => {
    expect([...REPO_LOCKED_FIELDS].sort()).toEqual(
      ['endpoint_url', 'gateway_arn', 'kind', 'name', 'runtime_arn'],
    );
  });

  it('gives a reason for a locked field on a repo-owned record only', () => {
    expect(lockedReason('gateway_arn', true)).toMatch(/managed by its repository/i);
    expect(lockedReason('gateway_arn', false)).toBeNull();
  });

  it('never locks an unlisted field', () => {
    expect(lockedReason('description', true)).toBeNull();
  });
});

describe('mcpRepoActions', () => {
  it('a registered record keeps reprovision and deletes here', () => {
    expect(mcpRepoActions(false)).toEqual({ reprovision: true, deleteViaRepo: false });
  });
  it('a repo-owned record hides reprovision and deletes via the repository: its identity is minted by the repo\'s materialize and its gateway by the build', () => {
    expect(mcpRepoActions(true)).toEqual({ reprovision: false, deleteViaRepo: true });
  });
});

describe('showGatewayRows', () => {
  it('shows for a gateway even before it is built', () => {
    expect(showGatewayRows({ ...base, kind: 'gateway' })).toBe(true);
  });
  it('hides for a standard record with no gateway values', () => {
    expect(showGatewayRows(base)).toBe(false);
  });
  it('shows when any gateway value is set', () => {
    expect(showGatewayRows({ ...base, gateway_url: 'https://gw.example.com/mcp' })).toBe(true);
  });
});

describe('WIZARD_GATEWAY_HINT', () => {
  it('points at Ops → MCP servers', () => {
    expect(WIZARD_GATEWAY_HINT).toBe(
      'To build a gateway from an API, create one in Ops → MCP servers → + New MCP server.',
    );
  });
});

describe('mcpEndpoint', () => {
  it('prefers the declared endpoint URL', () => {
    expect(
      mcpEndpoint({ endpoint_url: 'https://mcp.example.com', gateway_url: 'https://gw.example.com/mcp' }),
    ).toEqual({ url: 'https://mcp.example.com', fromGateway: false });
  });
  it('uses the gateway URL for a gateway-built server, whose gateway URL is the endpoint, so the header never reads "No endpoint set" beside a live gateway', () => {
    expect(mcpEndpoint({ endpoint_url: null, gateway_url: 'https://gw.example.com/mcp' })).toEqual({
      url: 'https://gw.example.com/mcp',
      fromGateway: true,
    });
  });
  it('is null when neither is set', () => {
    expect(mcpEndpoint({ endpoint_url: '', gateway_url: null })).toBeNull();
  });
});
