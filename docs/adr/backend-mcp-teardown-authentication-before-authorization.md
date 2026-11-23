# MCP teardown removes authentication before authorization

## Status

Accepted 2026-10-24, Epics 49A/50A. The practice dates from the teardown cascade work; this record
writes it down.

## Context

Deleting an MCP record must remove the platform-managed identity (the Entra app and its consents)
and the Cedar policy engine attached to a gateway the platform did not create and never deletes. A
half-finished teardown leaves a live gateway.

## Decision

- Authentication is torn down before authorization, so a sequence that dies in the middle leaves a
  gateway that cannot mint a token rather than one serving every call with Cedar stripped. A failed
  identity leg skips the engine leg and reports it `skipped`.
- Only an engine id stored on the record is ever detached or deleted, because `gateway_id` descends
  from the caller-settable `gateway_arn` and the engine a live gateway reports is not provably the
  record's; an engine the record does not own is reported, never touched.
- The record is deleted even when a leg fails; the `[teardown]` log line per item is the audit
  trail.

## Consequences

- `platform/control_plane/backend/src/services/mcp_teardown.py` (`teardown_mcp_identity`),
  `platform/control_plane/backend/src/services/mcp_cedar_service.py` (`delete_policy_engine`,
  `_delete_policy_engine_sync`), `platform/control_plane/backend/src/api/routes/mcp_servers.py`
  (`delete_mcp_server`) and `platform/control_plane/backend/src/services/project_teardown.py` (the
  repository cascade shares the function) follow this order.
- Pinned by the teardown section of
  `platform/control_plane/backend/tests/test_mcp_servers_routes_rbac.py` and the
  `delete_policy_engine` tests in `platform/control_plane/backend/tests/test_mcp_cedar_service.py`.
- Any new identity-bearing resource joins the cascade on the authentication side first.
