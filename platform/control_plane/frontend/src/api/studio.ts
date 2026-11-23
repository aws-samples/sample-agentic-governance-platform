import client, {
  type DraftRequest,
  type DraftResponse,
  type ProposalRequest,
  type ProposalResponse,
} from './client';
import {
  ApiFailure,
  formatApiErrorDetail,
  isApiFailure,
  type ErrorDetail,
} from './failure';

export const DRAFT_TIMEOUT_MESSAGE = 'The draft took too long. Compose manually or try again.';
export const DRAFT_FAILED_MESSAGE = 'The draft failed. Compose manually or try again.';
export const PROPOSAL_TIMEOUT_MESSAGE = 'The request timed out. Retry this step.';
export const PROPOSAL_FAILED_MESSAGE = 'Opening the pull request failed. Retry this step.';

/** The message for a 5xx. 503 (API Gateway timeout, no body) reads like 504. */
export function serverErrorMessage(route: 'draft' | 'proposal', status: number, detail: ErrorDetail): string {
  const timeout = status === 503 || status === 504;
  if (route === 'draft') {
    if (timeout) return DRAFT_TIMEOUT_MESSAGE;
    return formatApiErrorDetail(detail, DRAFT_FAILED_MESSAGE);
  }
  if (status === 503) return PROPOSAL_TIMEOUT_MESSAGE;
  return formatApiErrorDetail(detail, timeout ? PROPOSAL_TIMEOUT_MESSAGE : PROPOSAL_FAILED_MESSAGE);
}

async function post<T>(route: 'draft' | 'proposal', url: string, body: unknown): Promise<T> {
  try {
    const res = await client.post<T>(url, body);
    return res.data;
  } catch (e) {
    if (isApiFailure(e) && e.status >= 500) {
      throw new ApiFailure(e.status, e.code, e.detail, serverErrorMessage(route, e.status, e.detail));
    }
    throw e;
  }
}

export const draftComposition = (req: DraftRequest): Promise<DraftResponse> =>
  post<DraftResponse>('draft', '/api/v1/studio/draft', req);

/** Opens a branch, a commit and a pull request on one fresh repo; a retry may open a second PR, so keep the returned number. */
export const submitProposal = (projectId: string, repoId: string, req: ProposalRequest): Promise<ProposalResponse> =>
  post<ProposalResponse>('proposal', `/api/v1/projects/${projectId}/repos/${repoId}/studio-proposal`, req);
