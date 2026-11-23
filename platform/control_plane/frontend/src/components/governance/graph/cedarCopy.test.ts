import { describe, expect, it } from 'vitest';
import { CEDAR_ENFORCEMENT_COPY, CEDAR_CAVEAT, CEDAR_LEGEND } from './cedarCopy';

describe('a Cedar policy is (user, tool)-scoped and enforced by the gateway; no copy may say a policy targets an agent', () => {
  const COPY = { CEDAR_ENFORCEMENT_COPY, CEDAR_CAVEAT, CEDAR_LEGEND };

  it.each(Object.entries(COPY))('%s names the gateway as the enforcing party', (_name, text) => {
    expect(text).toMatch(/gateway/i);
  });

  it.each(Object.entries(COPY))('%s never says the policy targets this or the agent', (_name, text) => {
    expect(text).not.toMatch(/this agent|the agent/i);
  });
});
