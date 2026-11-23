import { afterEach, describe, expect, it, vi } from 'vitest';
import client, { agentOpsApi } from '../../../api/client';
import { ApiFailure } from '../../../api/failure';
import { stripComments } from '../../../rawSource';
import tabRaw from './EvaluationsTab.tsx?raw';
import pageRaw from '../AgentDetail.tsx?raw';

const tabSrc = stripComments(tabRaw);
const pageSrc = stripComments(pageRaw);

afterEach(() => vi.restoreAllMocks());

describe('the agent-scoped eval reads: a reader below Operator sees /agents/{id}/repository and /agents/{id}/evals/runs, never the Operator-only /repositories list', () => {
  it('finds the repo through the agent, and reads null when the agent has none', async () => {
    const get = vi.spyOn(client, 'get').mockResolvedValueOnce({ data: { project_id: 'p', repo_id: 'r', name: 'n' } });
    expect(await agentOpsApi.repository('a-1')).toEqual({ project_id: 'p', repo_id: 'r', name: 'n' });
    expect(get).toHaveBeenCalledWith('/api/v1/agents/a-1/repository');
    get.mockRejectedValueOnce(
      new ApiFailure(404, 'project.no_repository', 'No repository for this agent', 'No repository for this agent'),
    );
    expect(await agentOpsApi.repository('a-1')).toBeNull();
    get.mockRejectedValueOnce(new Error('Agent not found'));
    await expect(agentOpsApi.repository('a-1')).rejects.toThrow('Agent not found');
  });

  it('reads the runs agent-scoped', async () => {
    const get = vi.spyOn(client, 'get').mockResolvedValueOnce({ data: { runs: [] } });
    expect(await agentOpsApi.evalRuns('a-1')).toEqual({ runs: [] });
    expect(get).toHaveBeenCalledWith('/api/v1/agents/a-1/evals/runs');
  });

  it('the tab no longer goes through the Operator-only lists; the repository read is the page\'s, one read for the header chip and this tab', () => {
    expect(tabRaw).not.toMatch(/repositoriesApi|evalsApi/);
    expect(pageSrc).toMatch(/agentOpsApi\s*\.repository\(/);
    expect(tabSrc).toMatch(/agentOpsApi\s*\.evalRuns\(/);
  });
});

describe('the no-repository empty state', () => {
  it('says plainly why there is nothing to show', () => {
    expect(tabSrc).toContain("This agent isn't built from an AGP repository, so it has no evaluations.");
  });
});
