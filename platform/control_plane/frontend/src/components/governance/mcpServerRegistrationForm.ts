import type { Kind } from '../../api/client';
import type { ApiFailure, ErrorCode } from '../../api/failure';
import { refusalKind } from '../../api/refusalKind';

export function requiredArnPresent(draft: {
  kind: Kind;
  gateway_arn: string;
  runtime_arn: string;
}): boolean {
  if (draft.kind === 'gateway') return draft.gateway_arn.trim().length > 0;
  if (draft.kind === 'runtime') return draft.runtime_arn.trim().length > 0;
  return true;
}

export type McpCreateRefusalKind = 'name_taken';

export const MCP_CREATE_REFUSAL_CODES = {
  name_taken: ['mcp_server.name_taken'],
} as const satisfies Record<McpCreateRefusalKind, readonly ErrorCode[]>;

export function classifyMcpCreateRefusal(failure: ApiFailure): McpCreateRefusalKind | null {
  return refusalKind(MCP_CREATE_REFUSAL_CODES, failure);
}
