# Backend registry calls go through one writer

## Status

Accepted 2026-11-03, Epic 52C.

## Context

The AWS Agent Registry resets an APPROVED record to DRAFT on every `UpdateRegistryRecord`, and the
marketplace lists APPROVED records only, so a platform write (an identity id, a runtime ARN, a grant, the
publish flag, the marketplace block) sent as a plain update delists the card. Each registry service held
its own client, resolved the registry id, wrapped the PATCH envelope and retried conflicts on its own
budget, and a separate reapproval module put the record back for some paths. Shrink-only lists named the
modules that still called the registry or obtained its client, and four call sites took the client as a
parameter, which the IAM derivation could not bind.

## Decision

- `platform/control_plane/backend/src/services/registry_writer.py` is the one module under `src` that
  calls an `agent-registry-control` operation and the one that obtains the client: `RegistryWriter` sets
  `self._ctl = control_client or registry_control_client(region)`, and the `or` keeps every call site
  bound for `tests/test_iam_task_role_covers_boto3_calls.py`.
- `RegistryWriter.registry_id` resolves the name once, through `_resolve_registry`, which pages
  `ListRegistries` into `find_registry_by_name` (`src/core/registry_resolver.py`); the bootstrap script
  `backend/scripts/ensure_registry.py` pages its own client into the same function.
- `RecordContent` is a record's governed content and owns both wire shapes, `create_params` and the
  `{"optionalValue": ...}` envelope of `update_params`. `WriteOutcome` says what a write did: `status`,
  `written`, `reset_and_recovered`, `superseded_by_other_writer`.
- `write` is the platform write. It resolves the registry id before any call, reads the settled record,
  makes no call when the content already equals it, updates, and puts a record that was APPROVED back
  through `SubmitRegistryRecordForApproval` and `UpdateRegistryRecordStatus(APPROVED)` with the
  approver's reason kept. Before each re-assert step it re-reads and stops when the content is no longer
  what it wrote. One deadline, `REAPPROVAL_BUDGET_S` from the start of the write, bounds the conflict
  retry, the pre-read and the re-assert. Once the content landed it never raises: a failed re-assert is
  logged and the true status returned.
- `write_content` is a user's governed edit: retried inside the same deadline, never re-asserted, `None`
  when the record is gone; only `AgentRegistryService.update` and `McpServerRegistryService.update` call it.
- The services build envelopes. `services/agent_registry_service.py` and `services/mcp_server_service.py`
  keep their public methods, their `AppError` kinds and the translation from `ClientError`; they map a
  model to `RecordContent`, read the lifecycle from `WriteOutcome.status` and hold one writer, no client.
  Their three platform entry points, `persist_identity`, `set_published` and `persist_marketplace`, reach
  the registry through `write`; `control_client=` is forwarded, so a test's mock reaches the writer.

`platform/control_plane/backend/tests/test_registry_write_paths.py` holds the rule at zero through
`test_every_registry_operation_call_sits_in_the_owner` (the seven operations and the `list_registries`
paginator, direct or through a literal `getattr`), `test_the_registry_client_is_obtained_only_by_the_owner`,
`test_the_services_hold_no_client` and `test_write_content_is_called_only_by_the_two_user_put_routes`;
`tests/test_registry_writer.py` drives the writer over the in-memory registry and a conflicting mock.

## Consequences

- A platform write cannot delist a card by forgetting the re-assert; the lifecycle rule is read in one place.
- Every write retries inside one budget: agent `update()` gains a conflict retry, and an MCP write gives up
  at the deadline where it waited twelve rounds of two seconds. The budget is one constant.
- A new registry operation is added to the writer and its IAM action derives; a call anywhere else fails
  the zero-target, which names the writer.
- A write that lands between the writer's last re-read and its next call is not caught.
- The build still writes the agent record through the CLI; the buildspec checks in the same test file hold
  that shell writer, `_reg_status` before and `_reapprove_after` after each write.
