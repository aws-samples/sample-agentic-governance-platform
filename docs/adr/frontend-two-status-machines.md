# Frontend status pills come from two closed unions, narrowed once at the wire

## Status

Accepted 2026-10-23 (Epics 49B+50B); amended 2026-10-29 (Epic 52B): the narrowing point moved from
the frontend's boundary to the backend model.

## Context

`cicd_status` reached the wire as a bare string with several writers (the project service's
provisioning, promote, build and rollback paths, its failed and ready transitions, and a best-effort
shell helper in the CodeBuild buildspec) and no enum behind it. The status-to-tint table was once
forked across two pages; when `promoting` and `deployed` were added only one copy was extended, and on
the other page both fell through to the amber in-flight tint, so a repository live in production
rendered the same as one still provisioning. The remedy was a comment asking the next author not to
re-fork the table, which holds only until a second surface legitimately needs a different treatment.
The runtime's health is a different fact from a different producer, a describe against the AgentCore
control plane read through `GET /agents/{id}/runtime`; one merged pill would read green for a
delivered agent nobody can reach.

## Decision

The one narrowing point for either status is the backend model. `CicdStatus` in
`platform/control_plane/backend/src/models/repository.py` is a `Literal` over exactly the values a
writer produces, with a `before` validator that folds a stored value outside the set to `unknown` and
logs it; `RuntimeStatusValue` in `platform/control_plane/backend/src/models/agent.py` is the same
shape for the runtime probe, whose producers write members only. A member no writer produces is
refused there (no `building`, `pending` or `not_probed`), and a value outside the set narrows to
`unknown`, never to a plausible neighbour and never to `ready`. The OpenAPI snapshot carries both
unions as closed enums, and the generated `platform/control_plane/frontend/src/api/openapi.ts` renders
them as string-literal unions, so the frontend receives a closed type and holds no copy of either
member list and no fold of its own: there is no value outside the union for it to narrow.

`platform/control_plane/frontend/src/components/operations/opsStatus.ts` declares `CicdStatus` as
`Repository['cicd_status']` and `RuntimeStatusKey` as `RuntimeStatus['status']`, both over the
generated types, and nothing else about the members. Every lookup (`CICD_BADGE_KEY`, `CICD_LABEL`,
`RUNTIME_BADGE_KEY`, `RUNTIME_LABEL`) is a `Record<CicdStatus, X>` or `Record<RuntimeStatusKey, X>`
with no default branch, so a member the backend adds fails `tsc -b` in every table that lacks it and
a member the backend drops fails it in every table that still carries it; the tables answer a
semantic `OPS_BADGE` key, never a class string, so a surface that needs another treatment changes its
styling, not the table. `OPS_BADGE` itself is the palette in
`platform/control_plane/frontend/src/components/operations/opsUi.ts`, and `promotionStatusLabel`,
`cicdBadgeKey` and `isPromotionInFlight` in `projectRoles.ts` take the generated union, delegate to
these tables and narrow nothing of their own. The two machines stay separate: both carry `unknown`,
tinted apart from `failed`, because an unreachable control plane is not a broken runtime.

`platform/control_plane/frontend/src/components/operations/opsStatus.test.ts` holds what the types
cannot, under these titles: `a repo live in production never renders amber`,
`unknown is visually distinct from failed, on both machines`,
`every cicd status has a badge key and a label, and no table carries an extra key` and its runtime
twin, `the tables are keyed by the literal unions, not by string`, and the label rules (sentence
case, no raw wire value, no label shared across the machines, `unknown` describes our read). The
members themselves are the backend's to pin: `tests/test_cicd_status_members.py` holds the writers'
set against the project service and the buildspec. `RuntimeStatus`, the wire shape, is generated once
into `openapi.ts` and re-exported through `platform/control_plane/frontend/src/api/client.ts`;
`opsStatus.ts` holds no copy of it, so there is no second declaration to drift.

## Consequences

- A new status is a change to the backend `Literal`, to a backend writer, to the reviewed snapshot
  and to every frontend table; `npm run generate:api` carries the member across, and `tsc -b` names
  the tables that lack the entry.
- A `.tsx` renders `OPS_BADGE[CICD_BADGE_KEY[repo.cicd_status]]` and `CICD_LABEL[...]` straight off
  the wire type; it holds no status table, no default branch and no type assertion of its own, and
  applies no `capitalize` to the labels, which arrive sentence-cased. The one absence the frontend
  decides is a runtime reading that has not arrived: a component reads
  `runtime?.status ?? 'unknown'`; no wire value carries that case.
- `deployed` says a delivery succeeded for whatever stage the buildspec ran; whether production is
  serving is a join over the deployment history, answered by `prodServingState` in
  `operations/repositoryDetailTabs.ts`, never by the status alone.
- `promoting` names no stage: a prod promote, a rollback to any stage and a dev Build now all write
  it, and the detail page derives the stage from its history.
