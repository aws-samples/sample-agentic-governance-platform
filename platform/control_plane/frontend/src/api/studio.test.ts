import { afterEach, describe, expect, it, vi } from 'vitest';
import client, { type DraftRequest, type DraftResponse, type ProposalRequest } from './client';
import { ApiFailure } from './failure';
import {
  DRAFT_FAILED_MESSAGE,
  DRAFT_TIMEOUT_MESSAGE,
  PROPOSAL_FAILED_MESSAGE,
  PROPOSAL_TIMEOUT_MESSAGE,
  draftComposition,
  submitProposal,
} from './studio';

afterEach(() => vi.restoreAllMocks());

const DRAFT: DraftRequest = { project_id: 'p-1', description: 'a claims intake agent' };
const PROPOSAL: ProposalRequest = { kind: 'mcp_server', auth: 'none', exposed_operations: ['getPolicy'], openapi_text: 'openapi: 3.0.0' };

function transportFailure(status: number): ApiFailure {
  return new ApiFailure(status, null, undefined, `Request failed with status code ${status}`);
}

describe('draftComposition', () => {
  it('posts the request and returns the body', async () => {
    const body: DraftResponse = { blocks: [], unused_files: [] };
    const post = vi.spyOn(client, 'post').mockResolvedValueOnce({ data: body });
    expect(await draftComposition(DRAFT)).toBe(body);
    expect(post).toHaveBeenCalledWith('/api/v1/studio/draft', DRAFT);
  });

  it('re-wraps a 5xx with the draft copy, keeping the status, the code and the detail', async () => {
    vi.spyOn(client, 'post').mockRejectedValueOnce(transportFailure(504));
    await expect(draftComposition(DRAFT)).rejects.toMatchObject({ status: 504, code: null, detail: undefined, message: DRAFT_TIMEOUT_MESSAGE });

    vi.spyOn(client, 'post').mockRejectedValueOnce(new ApiFailure(502, 'studio.draft_failed', 'The model returned nothing', 'The model returned nothing'));
    await expect(draftComposition(DRAFT)).rejects.toMatchObject({ status: 502, code: 'studio.draft_failed', detail: 'The model returned nothing', message: 'The model returned nothing' });

    vi.spyOn(client, 'post').mockRejectedValueOnce(transportFailure(500));
    await expect(draftComposition(DRAFT)).rejects.toMatchObject({ status: 500, message: DRAFT_FAILED_MESSAGE });
  });

  it('rethrows a 4xx as the interceptor built it', async () => {
    const failure = new ApiFailure(422, null, 'description: field required', 'description: field required');
    vi.spyOn(client, 'post').mockRejectedValueOnce(failure);
    await expect(draftComposition(DRAFT)).rejects.toBe(failure);
  });

  it('rethrows anything that is not an ApiFailure untouched', async () => {
    const halt = new Error('Signed out. Reload the page.');
    vi.spyOn(client, 'post').mockRejectedValueOnce(halt);
    await expect(draftComposition(DRAFT)).rejects.toBe(halt);
  });
});

describe('submitProposal', () => {
  it('posts to the repo-scoped route and returns the body', async () => {
    const body = { branch: 'studio/x', files: ['a.py'], pr_number: 7, pr_url: 'https://example.test/pr/7' };
    const post = vi.spyOn(client, 'post').mockResolvedValueOnce({ data: body });
    expect(await submitProposal('p-1', 'r-1', PROPOSAL)).toBe(body);
    expect(post).toHaveBeenCalledWith('/api/v1/projects/p-1/repos/r-1/studio-proposal', PROPOSAL);
  });

  it('re-wraps a 5xx with the proposal copy: 503 times out, any other reads the detail or the failed copy', async () => {
    vi.spyOn(client, 'post').mockRejectedValueOnce(transportFailure(503));
    await expect(submitProposal('p-1', 'r-1', PROPOSAL)).rejects.toMatchObject({ status: 503, message: PROPOSAL_TIMEOUT_MESSAGE });

    vi.spyOn(client, 'post').mockRejectedValueOnce(new ApiFailure(502, null, 'Storing the API key failed', 'Storing the API key failed'));
    await expect(submitProposal('p-1', 'r-1', PROPOSAL)).rejects.toMatchObject({ status: 502, message: 'Storing the API key failed' });

    vi.spyOn(client, 'post').mockRejectedValueOnce(transportFailure(500));
    await expect(submitProposal('p-1', 'r-1', PROPOSAL)).rejects.toMatchObject({ status: 500, message: PROPOSAL_FAILED_MESSAGE });
  });

  it('re-wraps a proposal 504 with the detail when one is sent and the timeout copy when none', async () => {
    vi.spyOn(client, 'post').mockRejectedValueOnce(transportFailure(504));
    await expect(submitProposal('p-1', 'r-1', PROPOSAL)).rejects.toMatchObject({ status: 504, code: null, detail: undefined, message: PROPOSAL_TIMEOUT_MESSAGE });

    vi.spyOn(client, 'post').mockRejectedValueOnce(new ApiFailure(504, null, 'The build timed out', 'The build timed out'));
    await expect(submitProposal('p-1', 'r-1', PROPOSAL)).rejects.toMatchObject({ status: 504, detail: 'The build timed out', message: 'The build timed out' });
  });

  it('rethrows a 4xx as the interceptor built it', async () => {
    const failure = new ApiFailure(409, 'studio.conflict', 'A proposal is already open', 'A proposal is already open');
    vi.spyOn(client, 'post').mockRejectedValueOnce(failure);
    await expect(submitProposal('p-1', 'r-1', PROPOSAL)).rejects.toBe(failure);
  });
});
