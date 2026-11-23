# Every boto3 client, resource and session comes from one factory

## Status

Accepted 2026-10-28, Epic 52A; amended 2026-11-03 (Epic 52C): the registry client has one owner.

## Context

Thirty-two modules under `src`, nine scripts and eight test files each constructed their own boto3
client, with their own region default and, at eleven sites, a try/except that fell back to an
in-memory dictionary when the client could not be built, so a production task could run on memory
and report success. The registry services and the resolver each built their own
`agent-registry-control` client. The IAM derivation bound a factory only inside its own module, so a
shared factory would have turned twenty tier-one registry sites ambiguous.

## Decision

- `platform/control_plane/backend/src/core/aws_clients.py` is the only module that constructs a
  boto3 client, resource or session: `client(service, *, region=None, config=None,
  credentials=None)`, `dynamodb_table(name, *, region=None)` through the private `_dynamodb`
  resource, `registry_control_client(region)` and `ambient_region()`; `AssumedCredentials` carries
  an assumed role's three values into `client`. Every construction runs under one module lock.
  Nothing is cached: each holder keeps its client as an attribute.
- boto3 is imported inside each function, never at module scope, for two reasons: importing the
  factory constructs nothing and leaves `boto3` unimported, which
  `platform/control_plane/backend/tests/test_aws_clients.py` pins, and a test that patches the
  global `boto3.client` intercepts every construction through the factory.
- ruff `TID251` bans `boto3.client`, `boto3.resource`, `boto3.Session` and `boto3.session.Session`
  over `src`, `tests` and `scripts` in `platform/control_plane/backend/pyproject.toml`; the message
  names the factory. The per-file allowlist holds two permanent lines, the factory itself and the
  IAM idiom corpus under `platform/control_plane/backend/tests/fixtures/iam/`; every migration line
  it carried has gone.
- The IAM derivation in
  `platform/control_plane/backend/tests/test_iam_task_role_covers_boto3_calls.py` reads the factory
  first and binds a call made through `aws_clients.<fn>`, `core.aws_clients.<fn>` or a bare name
  imported from the module. A factory call in argument position, such as
  `aws_clients.client("codebuild", ...)` passed to a constructor, is a construction too, so the
  service it names resolves the parameter-bound receivers that take it.
  `test_every_factory_function_is_in_the_index` holds that every factory function binds, so a
  helper the reader cannot follow fails the suite instead of losing its callers' sites, and
  `test_the_derived_action_set_is_the_anchors` pins the resolved `(service, operation)` set, so a
  call site added, removed or lost by the reader is a reviewed change.
- `registry_control_client` is the one provider of the registry control client, and
  `platform/control_plane/backend/src/services/registry_writer.py` alone obtains it. In
  `platform/control_plane/backend/tests/test_registry_write_paths.py`,
  `test_the_registry_literal_appears_in_no_client_call` is a zero-target over any
  `.client("agent-registry-control")` under `src`,
  `test_the_registry_client_is_obtained_only_by_the_owner` holds the provider's one caller, and
  `test_the_services_hold_no_client` holds that the two registry services keep no client of their own.
- The region is `settings.AWS_REGION` unless the caller passes one; the one script that read the
  profile's region calls `ambient_region()`, the only reader of the default session.

## Consequences

- A new AWS call is one import and one factory call; the wrong constructor fails ruff before review.
- A configured table whose client cannot be built fails at the first call instead of falling back
  to memory in a production task.
- The task role's grants stay derived from the code, every call being visible through the factory;
  the owner binds the registry calls and `test_every_boto3_call_site_is_resolved` holds every site.
