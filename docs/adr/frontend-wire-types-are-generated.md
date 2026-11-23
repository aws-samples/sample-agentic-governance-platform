# Frontend wire types are generated from the OpenAPI snapshot

## Status

Accepted 2026-10-30, Epic 52B.

## Context

The frontend declared every wire shape by hand in `platform/control_plane/frontend/src/api/client.ts`
and `src/types/index.ts`: interfaces, string-literal unions and `as const` member arrays copied from the
backend's models. A backend field added without the matching edit left the page reading `undefined`
under a type that said otherwise; a dropped response model weakened the contract with nothing to
notice; a text-level drift test compared the two sides by name behind shrink-only exception lists.
The reviewed snapshot `platform/control_plane/backend/tests/fixtures/openapi.json` already held the
document; the frontend was the one reader that did not consume it.

## Decision

`platform/control_plane/frontend/src/api/openapi.ts` is the output of `openapi-typescript`, pinned
exact in `package.json`, over the snapshot, committed and never hand-edited; the header the tool
prints says so. The command is `npm run generate:api`, whose flags are `--empty-objects-unknown`
(a `Dict[str, Any]` renders `unknown`, not `Record<string, never>`), `--root-types=true` with
`--root-types-no-schema-prefix=true` (one-word aliases, `Agent`, not `components['schemas']['Agent']`)
and `--default-non-nullable=false` (the document's `required` is the truth about defaults; nothing
is inferred from a `default`). A backend change is two commands in order: in the backend,
`AGP_UPDATE_OPENAPI_SNAPSHOT=1 ./venv/bin/python -m pytest tests/test_openapi_contract.py`, the
snapshot diff read in the merge request; then `npm run generate:api` in the frontend.

`platform/control_plane/frontend/src/api/openapi.test.ts` holds the sync test and the zero-target.
The sync test runs the package's Node API with the same options over the snapshot and asserts the
committed file byte for byte, its message the two commands. The zero-target reads `client.ts` as
text and asserts no `export interface` and no literal-union `export type`. `client.ts` is the one
home: `export type * from './openapi'` re-exports every alias, and the types it declares are derived
from the generated ones, not copied from the backend: `TenantReadiness` keys the readiness map by
`Verb`, `AgentListFilters` and `McpServerListFilters` are the two list operations' query parameters,
which the generator gives no root alias, `StageConfig` is the value type of the two stage maps, an
`anyOf` the generator names nowhere, and `ErrorCode` and `ErrorDetail` derive from the generated
`ErrorBody` and `HttpValidationError`.

The document tells the truth about defaults on the backend side. Every class that reaches the wire
inherits `models.base.WireModel`, whose config marks a defaulted property required in serialization
mode and leaves it optional in validation mode, so a response field the backend always fills is
required and a request field a client may omit is optional; a model used on both sides splits into
`XInput` and `XOutput`. `tests/test_openapi_contract.py` holds it with
`test_response_schemas_require_every_property` and
`test_request_schemas_keep_their_defaulted_properties_optional`.

## Consequences

- A backend field change is a snapshot diff a reviewer reads, then one regenerate; the frontend
  never edits a wire type.
- A renamed or removed enum member fails `tsc -b` in every table and classifier that names it.
- A wire shape is read where it stands (`Repository['cicd_status']`, `TenantView['stages'][string]`),
  and a request body is typed `XInput` while the response is `XOutput`; a form that holds the
  complete object the backend delivers is typed on the output.
- A wire `string` the backend never closed stays a `string` on the frontend; narrowing it is one
  type guard at the boundary, and closing it is a backend change.
