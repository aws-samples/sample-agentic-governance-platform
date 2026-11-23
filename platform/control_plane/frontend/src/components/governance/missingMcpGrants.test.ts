import { describe, it, expect } from 'vitest';
import { missingMcpGrantsHint } from './missingMcpGrants';

describe('missingMcpGrantsHint: a harness that lists an MCP gateway in its tools with no grant behind it fails every invoke, even a prompt that uses no tool', () => {
  it('is null when every declared tool is granted', () => {
    expect(missingMcpGrantsHint([])).toBeNull();
  });

  it('names the one MCP the agent uses without a grant', () => {
    expect(missingMcpGrantsHint([{ mcp_id: 'm1', mcp_name: 'claims-api' }])).toBe(
      "This agent's harness lists claims-api but has no grant for it, so every invoke fails until granted.",
    );
  });

  it('lists several MCPs in one sentence', () => {
    expect(
      missingMcpGrantsHint([
        { mcp_id: 'm1', mcp_name: 'claims-api' },
        { mcp_id: 'm2', mcp_name: 'policy-api' },
      ]),
    ).toBe(
      "This agent's harness lists claims-api, policy-api but has no grant for them, so every invoke fails until granted.",
    );
  });
});
