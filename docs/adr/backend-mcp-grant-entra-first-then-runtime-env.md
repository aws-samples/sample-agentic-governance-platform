# An MCP grant writes Entra first, then the registry set, then the runtime env

## Status

Accepted 2026-10-24, Epics 49A/50A. The practice dates from the first MCP grant work; this record
writes it down.

## Context

An agent reaches an MCP server through an Entra app-role assignment on the MCP's service principal
plus a delegated consent grant; the agent's runtime learns which MCPs to call from an injected
`MCP_SERVERS` env built from the agent record's `mcp_server_ids` and the MCP registry. Two stores
describe one grant.

## Decision

- Entra is the enforcement boundary and the only one; the registry set and the runtime env are
  desired state. Every grant and revoke writes Entra first, then the set, then rebuilds the env.
- A grant whose env rebuild fails fails loud (`GrantRevokeError.wiring_failed`), because a
  successful grant must mean a wired agent and a retry is idempotent. A revoke whose env rebuild
  fails only logs, because access is already gone and a stale entry fails closed: the on-behalf-of
  mint is refused.
- The admission gate is the consent grant, not the app-role assignment: the MCP app carries
  `appRoleAssignmentRequired=false`, because an assignment-required service principal refuses the
  delegated user outright (Entra's `AADSTS50105`, confirmed live), so a revoke tears the consent
  down when no sibling assignment remains.
- Reconcile-on-read re-applies the env only from a read that reached the runtime and found
  `MCP_SERVERS` missing.

## Consequences

- `platform/control_plane/backend/src/services/agent_mcp_grant.py` (`apply_agent_mcp_grant`,
  `revoke_agent_mcp_grant`), `platform/control_plane/backend/src/services/agent_mcp_env.py`
  (`rebuild_runtime_mcp_env`, `reconcile_runtime_mcp_env`),
  `platform/control_plane/backend/src/services/marketplace_service.py` (approve and revoke call the
  same two functions) and `platform/control_plane/backend/src/api/routes/mcp_server_grants.py`
  follow this order.
- Pinned by `platform/control_plane/backend/tests/test_agent_mcp_grant.py`,
  `platform/control_plane/backend/tests/test_agent_mcp_env.py` and
  `platform/control_plane/backend/tests/test_mcp_server_grants_routes.py`.
- A future store for grants must keep Entra as the write that happens first.
