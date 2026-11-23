# Tenant-account resources are reached through one credential seam

## Status

Accepted 2026-10-24, Epics 49A/50A.

## Context

A tenant stage that carries `deploy_role_arn` deploys its runtime into the tenant's account. The
control plane's ambient credentials answer "not found" from the wrong account, which every
idempotent teardown reads as already done, so a runtime kept billing and an account-global role kept
blocking re-materialisation. Four modules address such resources:
`platform/control_plane/backend/src/services/project_runtime_teardown.py`,
`platform/control_plane/backend/src/services/langfuse_provisioning.py`,
`platform/control_plane/backend/src/services/containment_native.py` and
`platform/control_plane/backend/src/services/eval_runner.py`. Two more report the credential
failures as teardown line items: `platform/control_plane/backend/src/services/mcp_teardown.py` and
`platform/control_plane/backend/src/api/routes/agents.py`.

## Decision

- `platform/control_plane/backend/src/services/tenant_credentials.py` is the one place that
  decides which account a call lands in.
- `stage_client` assumes `deploy_role_arn` afresh on every call. There is no credential cache: a
  cached session has a floor as low as fifteen minutes and nothing would refresh it. A failed
  assume raises `TenantCredentialsError` rather than falling back to the ambient client; a stage
  whose account cannot be determined raises `StageUnresolvedError`, which leaf services subclass so
  one `except` arm renders both.
- Callers report the failure as a line item (`assume_role_failed:`, `stage_unresolved:`), never as
  success. Messages carry the role name (parsed by `_safe_role_label`, never sliced) and the
  exception type; no message, log line or detail carries an account id, an ARN or a provider body.
- `probe_stage_reach` is the read-only counterpart and never raises.

## Consequences

- A tenant-side trust policy that does not yet admit the backend fails visibly instead of
  reporting a successful teardown.
- Every teardown step costs one STS call.
- A new leaf service that touches a tenant account imports `stage_client` and subclasses
  `StageUnresolvedError` for its own lookup failures.
- Pinned by `platform/control_plane/backend/tests/test_tenant_credentials.py` and, in
  `platform/control_plane/backend/tests/test_tenant_service.py`,
  `test_the_two_copies_of_the_role_arn_pattern_are_byte_identical` and
  `test_accepted_deploy_role_arns_are_exactly_what_the_assume_seam_can_parse`.
