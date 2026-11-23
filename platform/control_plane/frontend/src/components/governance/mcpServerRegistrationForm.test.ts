import { describe, expect, expectTypeOf, it } from 'vitest';
import { ApiFailure, type ErrorCode } from '../../api/failure';
import { stripComments } from '../../rawSource';
import { MCP_CREATE_REFUSAL_CODES, classifyMcpCreateRefusal, requiredArnPresent } from './mcpServerRegistrationForm';
import wizardRaw from './McpServerRegistrationWizard.tsx?raw';

const wizardSrc = stripComments(wizardRaw);

describe('requiredArnPresent — a gateway needs a gateway ARN and a runtime a runtime ARN: the platform derives the identity from it, and a missing one registers a server stuck at identity none', () => {
  it('a gateway without a gateway ARN is not registrable, whatever the runtime ARN says', () => {
    expect(requiredArnPresent({ kind: 'gateway', gateway_arn: '', runtime_arn: 'arn:aws:bedrock-agentcore:eu-west-1:_:runtime/r' })).toBe(false);
    expect(requiredArnPresent({ kind: 'gateway', gateway_arn: '   ', runtime_arn: '' })).toBe(false);
    expect(requiredArnPresent({ kind: 'gateway', gateway_arn: 'arn:aws:bedrock-agentcore:eu-west-1:_:gateway/g', runtime_arn: '' })).toBe(true);
  });

  it('a runtime without a runtime ARN is not registrable, whatever the gateway ARN says', () => {
    expect(requiredArnPresent({ kind: 'runtime', gateway_arn: 'arn:aws:bedrock-agentcore:eu-west-1:_:gateway/g', runtime_arn: '' })).toBe(false);
    expect(requiredArnPresent({ kind: 'runtime', gateway_arn: '', runtime_arn: '\n' })).toBe(false);
    expect(requiredArnPresent({ kind: 'runtime', gateway_arn: '', runtime_arn: 'arn:aws:bedrock-agentcore:eu-west-1:_:runtime/r' })).toBe(true);
  });

  it('a standard server carries no AgentCore identity, so no ARN is required', () => {
    expect(requiredArnPresent({ kind: 'standard', gateway_arn: '', runtime_arn: '' })).toBe(true);
  });
});

describe('classifyMcpCreateRefusal: the wizard returns to the name step for the registry\'s name_taken code alone', () => {
  function failure(status: number, code: ErrorCode | null = null, detail?: string): ApiFailure {
    return new ApiFailure(status, code, detail, detail ?? `Request failed with status code ${status}`);
  }

  it('a 409 mcp_server.name_taken is name_taken', () => {
    expect(classifyMcpCreateRefusal(failure(409, 'mcp_server.name_taken', 'An MCP server named x already exists'))).toBe('name_taken');
  });

  it('a 409 with another code is not: the agent registry\'s name_taken, a tenant\'s, or a route-decided 409 with no code', () => {
    expect(classifyMcpCreateRefusal(failure(409, 'agent.name_taken', 'An agent named x already exists'))).toBeNull();
    expect(classifyMcpCreateRefusal(failure(409, 'tenant.name_taken', 'tenant name already exists'))).toBeNull();
    expect(classifyMcpCreateRefusal(failure(409))).toBeNull();
  });

  it('a registry 503 whose message says "exists", and a network failure, are not', () => {
    expect(classifyMcpCreateRefusal(failure(503, 'registry.not_configured', 'No registry exists for this tenant'))).toBeNull();
    expect(classifyMcpCreateRefusal(failure(0))).toBeNull();
  });

  it('the sentence alone classifies nothing: a 500 reading "already taken" is a plain error', () => {
    expect(classifyMcpCreateRefusal(failure(500, null, 'That name is already taken'))).toBeNull();
  });

  it('the table is typed on the generated codes, so a renamed kind fails tsc where the table names it', () => {
    expectTypeOf(MCP_CREATE_REFUSAL_CODES).toExtend<Record<'name_taken', readonly ErrorCode[]>>();
  });
});

describe('the wizard\'s create handler', () => {
  it('returns to the name step on the code and shows every other refusal where it stands', () => {
    expect(wizardSrc).toMatch(/isApiFailure\(err\) && classifyMcpCreateRefusal\(err\) === 'name_taken'/);
    expect(wizardSrc).not.toMatch(/already\|exist\|taken/);
  });
});
