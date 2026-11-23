# Every table write goes through one store whose primitives carry their condition

## Status

Accepted 2026-10-28, Epic 52A; amended 2026-11-02 (Epic 52C): the store has no `put`, and
`set_owned` is the one unconditional write.

## Context

Each state module held its own copy of the same block: a table built in a try/except, a dictionary
and a lock as the memory fallback, and helpers that wrote whole rows unconditionally. Check-then-act
on a shared row let two actors both pass: two eval starts both ran, two runtime builds both started,
two admins could both approve one publish request, and a monitor toggle could rewrite the cursor the
monitor loop advances. Each fix was one conditional write; nothing held that the next write carries
one, and a raw `put_item` had no condition anyone could see.

## Decision

- `platform/control_plane/backend/src/core/state_store.py` is the one way to reach a DynamoDB
  table. `StateStore(table_name, *, region=None, table=None, clock=time.time)` selects its table
  once: an injected table, the private memory double for an empty name, otherwise
  `core.aws_clients.dynamodb_table` with no try/except. Every table is keyed `(pk, sk)`.
- Each write primitive carries its condition as a `boto3.dynamodb.conditions` builder, never a
  string: `put_new` under `attribute_not_exists(sk)`; `put_if_version` under
  `<version_attr> = :expected` for an int and, for `None`, under
  `attribute_exists(sk) AND attribute_not_exists(<version_attr>)`, raising `VersionMismatch` on
  the conditional failure; `transition` under one `IN` set per expected attribute, `ABSENT`
  admitting a missing one; `acquire_lease` under free, expired or own holder and `release_lease`
  under own holder; `update_existing` and `delete` under `attribute_exists(sk)`. Every other
  `ClientError` propagates unchanged; `STORE_FAULTS` is the pair the services catch. A whole row
  is written by `put_new`, `put_if_version` or `transition`. `set_owned` (a `SET` of attributes
  the caller owns alone, or of a row whose single writer owns every attribute, an upsert by
  design) is the one unconditional write, named in `BY_DESIGN_UNCONDITIONAL`.
- The memory double is private to the module and proven against moto by the parametrised
  conformance suite `platform/control_plane/backend/tests/test_state_store.py`, every primitive
  against both tables, exception codes included. Where moto cannot be the oracle the double follows
  boto3's serializer and the store's own grammar: a float raises `TypeError` and an empty set the
  `ValidationException` DynamoDB answers, numbers read back as `Decimal` on every read path, only
  the store's canonical `SET` shape parses and no keyword the store does not send is accepted, so a
  new idiom fails in memory before it reaches DynamoDB; `Limit` applies after ordering, a
  memory-only pin because moto applies it in storage order; the twenty-thread race for one
  conditional `put_new` is memory-only too, because moto's `PutItem` reads, checks and assigns with no
  lock between the three and so has several winners. The double is not the oracle for boto3's
  condition-expression builder, which lives on the `Table` resource and resets its placeholder
  counters at the start of every call, so one resource shared by threads can name one attribute twice
  in a concurrent condition (a two-attribute `transition` written past a failed check, a legitimate
  `put_if_version(None)` refused): the store builds one `Table` per thread through the factory, and a
  deterministic re-entry test pins it on moto alone, since the double evaluates the builder objects
  itself. One `Table` per thread per store means resources scale with threads × stores (about 42
  threads × 12 stores worst case, about 300 KiB each, roughly 150 MiB of the 1,024 MiB task; a request
  touches a few stores, and idle anyio workers retire after 10 s and rebuild on return); a per-thread
  DynamoDB resource shared by all stores is the lever if that bound is ever approached, and it needs a
  per-thread cache in `aws_clients`, which its contract rules out today.
- `platform/control_plane/backend/tests/test_state_writes_are_conditional.py` reads the source under
  three rules. A raw `put_item`, `update_item` or `delete_item` outside the store is a finding;
  `test_no_raw_table_write_outside_the_store` and `test_no_new_unconditional_delete` hold at zero.
  Inside the store a raw write without `ConditionExpression=` is a finding unless its method is in
  `BY_DESIGN_UNCONDITIONAL`, imported and never re-typed. In `src/services/**` and `src/api/**`
  every `.put(` on a store receiver is a finding, and `test_no_new_store_put` holds at zero.
- A store fault surfaces through the error layer as it did before the migration: the marketplace
  reads keep answering absent on a store fault and log it, and the OAuth manifest state's consume
  raises the service's `secret_error`, a 502, where an unknown, spent or expired state stays
  `bad_request`, a 400, because that one conditional delete is what makes the state single-use.

## Consequences

- A caller cannot write without a condition unless it chooses `set_owned` by name, declaring that
  it owns the attributes it sets; a whole-row write always carries its condition.
- A configured table whose client cannot be built fails at the first call; no production task runs
  on memory unnoticed, and a marketplace write that fails against DynamoDB propagates like every
  other store's instead of landing in the task's memory as a success.
- A new primitive brings its condition and its conformance test on both tables in the same change;
  the behaviour tests that prove each past race on its parent stay in the services' own test files.
