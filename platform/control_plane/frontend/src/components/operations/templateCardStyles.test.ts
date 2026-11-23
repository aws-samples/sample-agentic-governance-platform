import { describe, expect, it } from 'vitest';
import repoRowRaw from './RepoRow.tsx?raw';
import { KIND_CHIP } from './templateCardStyles';
import { stripComments } from '../../rawSource';

describe('templateCardStyles', () => {
  it("the repo list's MCP chip reads KIND_CHIP.mcp_server from this module, so a kind reads the same on both surfaces", () => {
    const src = stripComments(repoRowRaw);
    expect(src).toMatch(/import \{[^}]*\bKIND_CHIP\b[^}]*\} from '\.\/templateCardStyles'/);
    expect(src).toContain('${KIND_CHIP.mcp_server}');
    expect(repoRowRaw).not.toContain(KIND_CHIP.mcp_server);
  });
});
