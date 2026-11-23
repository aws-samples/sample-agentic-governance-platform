# The Entra directory is served from memory behind the Graph adapter, under an explicit switch

## Status

Accepted 2026-11-04 (Epic 53C)

## Context

A local session already ran every table and both registries in memory, yet it proved `agent-access`
only to the edge of the directory: a grant is an Entra app-role assignment, and nothing stood in for
Microsoft Graph, so the grant, the read-back and the remove were driven on dev alone. Every Entra and
Graph call the backend makes goes through one adapter, `GraphService` in
`platform/control_plane/backend/src/services/graph_service.py`, which already accepts an injected
`httpx.AsyncClient`; the app registration and platform-role sequences in
`services/graph_app_registration.py` and `services/graph_platform_users.py` reach Graph through that
same client. No offline oracle exists for
Graph, so a double is written from Graph's documented answers, the ones the adapter's own tests pin.

## Decision

- The seam is the adapter's injected client, not a second class behind the singletons: under the
  switch, `GraphService` is built over `services.graph_memory.memory_graph_client(graph_base)`, an
  `httpx.AsyncClient` over `MemoryGraphTransport`, so the adapter, the app registration, the identity
  service, the grant mirror and every route run on a laptop as they run on a stack.
- `GRAPH_IN_MEMORY` (`platform/control_plane/backend/src/core/config.py`, default off) is read in one
  place, `graph_service_from_settings()`, which `api/routes/grants.py:get_graph_service` and
  `api/routes/mcp_server_grants.py:get_mcp_graph_service` call. Under it the adapter's tenant, backend
  app and secret are the directory's; the identity service's discovery URL and `/admin/users` still read
  the `ENTRA_*` settings. `e2e/launch.py` is its only setter, and no deploy path names it.
- The directory is fixed and small: one tenant, the backend application and its principal, four users
  and one group whose id is the one `platform/control_plane/backend/scripts/seed_e2e_fixtures.py`
  writes on the tenant it seeds, all constants of `services/graph_memory.py`. Applications, principals,
  assignments and consent grants exist only as the backend creates them, in one process-wide directory
  under one lock; there is no seeding route, environment value or fixture file.
- The edge is honest: a query form the backend does not issue answers 400 `Request_UnsupportedQuery` and
  any other method and path 501 `Request_NotServedInMemory`, both naming `GRAPH_IN_MEMORY=true` so the
  adapter's `GraphError` says which double refused; a `$search` without the `ConsistencyLevel: eventual`
  header answers the same 400 with Graph's own message. The token endpoint grants client credentials to
  the local backend application alone and refuses the On-Behalf-Of exchange.
- The double answers Graph's real statuses and codes, so it inherits the adapter's replication retry:
  a 400 or 404 on the principal create, the principal update or a consent grant is retried for about
  fifty seconds before it surfaces. A test that needs an immediate refusal refuses the application
  create with 403 `Authorization_RequestDenied`, which the adapter raises at once.
- Rejected: blank Entra values as the selector (a stack with a blank tenant id would serve a fake
  directory silently where it fails loud today); `USE_DEV_AUTH` (a directory choice hidden behind an
  auth flag, and no dev-auth debugging against the real directory); `REGISTRY_IN_MEMORY` doing double
  duty (one flag for two doubles, and the in-memory registry beside the real directory stays
  expressible); a second class behind the singletons (the adapter's own logic would not run locally).

## Consequences

- A laptop and a runner with no AWS credential and no Entra prove the grant path over the code the
  stack runs: `platform/control_plane/backend/tests/test_graph_memory.py` drives the real adapter over
  the double, and `platform/control_plane/backend/tests/test_graph_memory_routes.py` provisions a
  harness agent and runs register, grant, revoke and delete through the routes.
- A Graph path the backend starts issuing is added to the double in the same change, or the local gate
  fails first on the 501 that names the switch.
- `/admin/users` on local lists nobody: no platform application client id is configured and the
  directory's backend application defines no Platform roles, so the route answers 502
  `Failed to read platform users`.
