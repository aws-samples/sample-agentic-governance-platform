# The registries are served from memory behind the one provider, under an explicit switch

## Status

Accepted 2026-10-29 (Epic 53B)

## Context

A local session already ran every DynamoDB-backed domain in memory, dev auth on loopback and a headless
Chrome of its own, yet its first `/agents` call resolved `agp-agents` against AWS: the two registry
services and the resolver obtain their client from `registry_control_client` in
`platform/control_plane/backend/src/core/aws_clients.py`, and nothing stood in for that client. The
write suite's `register-agent` test skipped on local, so a laptop proved less than the pipeline and a
runner without credentials could not run it at all. moto ships no backend for `agent-registry-control`,
so no offline oracle exists for a double, and the one stateful fake in the tree modelled one record.

## Decision

- `REGISTRY_IN_MEMORY` (`platform/control_plane/backend/src/core/config.py`, default off) selects the
  in-memory registry, and it is read in one place: `registry_control_client` answers
  `core.registry_memory.memory_registry_client(region)` when it is set and the boto3 client otherwise.
  The two services, the resolver and the registry writer do not change.
- The seam is the provider, not the owner, because the registry client is one connection two
  resources share: every table names its own resource, so the store selects memory by an empty table
  name, but blanking a registry name would turn today's loud `RegistryNotConfiguredError` into a silent
  empty registry in production, and the names keep their meaning in memory, since the resolver still
  resolves them through `list_registries`.
- `platform/control_plane/backend/src/core/registry_memory.py` holds `MemoryRegistryClient`: one
  locked instance per process over the distinct configured names, answering the eight members the
  backend calls and raising `AttributeError` on anything else. A write answers its transient status and
  settles on the next read of the record with a later `updatedAt`, the asynchronous settle the
  registry writer models; an update resets any status to `DRAFT`; `APPROVED` is granted
  from `PENDING_APPROVAL` alone; `ConflictException` is never raised, because no mutation is refused
  for timing. Every request is validated by botocore's own `ParamValidator` against the
  `agent-registry-control` model in the venv, raising `ParamValidationError` as boto3 does; an unknown
  registry or record raises the real `ClientError` `ResourceNotFoundException`.
- Without an oracle, `platform/control_plane/backend/tests/test_registry_memory.py` proves each
  member's sequence, the register path of `AgentRegistryService` with every boto3 constructor raising,
  and that no deploy path names the switch; `platform/control_plane/backend/tests/test_registry_writer.py`
  drives the registry writer over the double. The double is strict where the service's rule is known and
  lenient where it is not, so a drift shows as a local pass beside a dev failure of the same recipe,
  never the reverse.
- Rejected: blank registry names as the selector (a silent empty registry in production); riding on
  `USE_DEV_AUTH` (a persistence choice hidden behind an auth flag, and no dev-auth debugging against
  the real registry); a selection inside the services (three copies of one decision in modules pinned
  unchanged).

## Consequences

- A laptop and a runner with no AWS credential run the write suite over the code path the stack runs,
  one boolean apart; `e2e/launch.py` is the switch's only setter and no deploy path carries it.
- A script that needs `create_registry` or a waiter fails under the switch on the unknown attribute,
  with a message naming the eight members and the switch.
- A member the services start calling is added to the double, validated by the model, in the same
  change, or the local gate fails first.
