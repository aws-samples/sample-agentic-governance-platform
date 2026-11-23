import type { McpServer } from '../../api/client';

export const ENFORCEMENT_COPY: Record<McpServer['cedar_enforcement_mode'], { heading: string; body: string }> = {
  none: {
    heading: 'No policy engine attached',
    body: 'Every assigned user can call any tool on this gateway. Add a policy to switch to default-deny.',
  },
  log_only: {
    heading: 'Logging only',
    body: 'Policies are evaluated and logged to CloudWatch, but not enforced — every assigned user can still call any tool.',
  },
  enforce: {
    heading: 'Enforcing — default-deny',
    body: 'Only users named in a policy can call tools. Assigned users not covered by a policy are blocked.',
  },
};

export const ENFORCE_FLIP_WARNING =
  'Applying a policy engine switches this gateway to default-deny — only users named in ' +
  'policies will be able to call its tools. Agents and users not covered by a policy ' +
  'will be blocked.';

// Rationale: docs/cedar-tool-policies.md "Enforcement, and the first policy", "What a deny looks like" (default-deny consequence)
export const ENFORCE_POSTURE_WARNING =
  'Every tool call not covered by a policy below is blocked — for every agent connected ' +
  'to this gateway. A blocked call is not recorded here: it reaches the agent as a ' +
  'tool error ("Tool Execution Denied") naming the policy id. Removing the last policy does not lift ' +
  'enforcement — only changing the mode does.';

const POSTURE_WARNING: Record<McpServer['cedar_enforcement_mode'], string | null> = {
  none: null,
  log_only: null,
  enforce: ENFORCE_POSTURE_WARNING,
};

const POSTURE_WARNING_BY_MODE: ReadonlyMap<string, string | null> = new Map(
  Object.entries(POSTURE_WARNING),
);

export function postureWarning(engineMode: string | undefined): string | null {
  return POSTURE_WARNING_BY_MODE.get((engineMode ?? '').trim().toLowerCase()) ?? null;
}
