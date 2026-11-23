# Build kind is resolved at the route and again in the service

## Status

Accepted 2026-10-24, Epics 49A/50A. The practice dates from the harness deploy path; this record
writes it down.

## Context

The deploy mechanism of a repository (`iac_type`, `build_kind`) comes from the infra chain, agent
to repository to template to infra template, never from the request body. `POST /builds/runtime`
in `platform/control_plane/backend/src/api/routes/builds.py` needs the `build_kind` before it
validates the image tag, because a config build posts a bare commit sha that the container prefix
rule refuses. `RuntimeBuildService.start_runtime_build` in
`platform/control_plane/backend/src/services/runtime_build_service.py` needs the same answer to
choose the env contract. `promote_repo` and `build_repo` in
`platform/control_plane/backend/src/services/project_delivery.py` call the service directly,
without the route; `ProjectService` holds one-line delegates to them.

## Decision

- The route and the service each call the one public resolver
  `RuntimeBuildService.resolve_infra_template`; the route adds only a fault fold and the
  `"container"` default (`_resolve_build_kind` in `routes/builds.py`). The chain is walked twice per
  OIDC-triggered build.
- Every "cannot resolve" case (chain services unwired, no owning repository, absent template or
  infra record, store fault) folds to the container path in both places; the route's tag gate folds
  to the stricter container rule; an MCP build never folds and refuses with `build_kind_mismatch`.
- Threading the resolved record from the route into the service was rejected: the service's other
  callers would have to resolve and pass it too, and forgetting it there would silently deploy a
  config agent through the image mechanism on the one legitimate prod path.

## Consequences

- Three `get_item` reads per request on a table the request already reads. If the cost ever
  matters, the remedy is a request-scoped cache, not removing one resolution.
- The two resolutions cannot diverge into an unsafe pair: both folds go the same direction, so the
  only reachable disagreement is "the gate resolved config, the service container" or the reverse,
  which yields a refused or container-shaped build, never a config build admitted through the weaker
  gate.
- Pinned by `platform/control_plane/backend/tests/test_runtime_build_service.py`
  (`test_an_unresolvable_chain_folds_to_the_container_path`,
  `test_an_infra_registry_fault_folds_to_the_container_path`) and
  `platform/control_plane/backend/tests/test_builds_routes.py`
  (`test_a_resolver_fault_does_not_fail_the_build_and_keeps_the_container_rule`).
