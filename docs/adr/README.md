# Architecture decision records

A record is written only when two or more modules depend on a decision and no type or test can carry
it; a tracked document that already owns the topic wins over a new record.

## Convention

- File: `docs/adr/<area>-<slug>.md`, `<area>` one of `frontend`, `backend`, `infra`, `platform`; no
  numbers.
- Four headings, in this order: `## Status`, `## Context`, `## Decision`, `## Consequences`.
- Under sixty lines, in plain English; the files, functions and tests the decision governs are named
  by path.
- No epic codes in the body; the date and the epic go under `## Status`.
- A module that depends on the decision points at it with one line, `Decision: docs/adr/<area>-<slug>.md`,
  in its docstring or as a comment.

## Index

| File | Decision |
|---|---|
| [backend-aws-clients-one-factory.md](backend-aws-clients-one-factory.md) | Every boto3 client, resource and session is constructed in `core/aws_clients.py`, under one lock with a call-time import; ruff bans the constructors everywhere else, the IAM derivation binds every call through the factory, and the registry client comes from one provider. |
| [backend-build-kind-resolved-at-the-route-and-in-the-service.md](backend-build-kind-resolved-at-the-route-and-in-the-service.md) | The build kind is resolved by the route's gate and again by the build service through one resolver; every unresolvable chain folds to the container path in both. |
| [backend-deployment-history-paging-ceiling.md](backend-deployment-history-paging-ceiling.md) | The cross-stage deployment history read stops at twenty pages and warns; a time-major index, not a bigger ceiling, is the fix if the warning is ever seen. |
| [backend-marketplace-publication-is-cross-tenant.md](backend-marketplace-publication-is-cross-tenant.md) | A published marketplace card is visible to every tenant; only publish requests are tenant-scoped. |
| [backend-mcp-grant-entra-first-then-runtime-env.md](backend-mcp-grant-entra-first-then-runtime-env.md) | An MCP grant or revoke writes Entra first, then the registry set, then the runtime env; a failed grant rebuild fails loud, a failed revoke rebuild logs. |
| [backend-mcp-teardown-authentication-before-authorization.md](backend-mcp-teardown-authentication-before-authorization.md) | MCP teardown removes the identity before the Cedar engine and touches only an engine the record owns. |
| [backend-registry-in-memory-behind-the-provider.md](backend-registry-in-memory-behind-the-provider.md) | `REGISTRY_IN_MEMORY`, read inside `registry_control_client` alone, selects one locked in-memory registry per process that answers the eight members the backend calls, validates every request against botocore's own service model, settles a write on the next read and never raises a conflict; the services and the resolver are unchanged. |
| [backend-service-errors-one-handler.md](backend-service-errors-one-handler.md) | Services raise typed `AppError` kinds, one handler in `main.py` renders the wire, routes never translate, and a broad `except` logs or re-raises. |
| [backend-state-store-conditional-writes.md](backend-state-store-conditional-writes.md) | Every DynamoDB table is reached through `core/state_store.py`, whose write primitives carry their condition as builder objects; the store has no `put`, and `set_owned` is the one unconditional write, the memory double is proven against moto, and a raw table write outside the store is a finding. |
| [backend-tenant-account-credentials.md](backend-tenant-account-credentials.md) | Every call into a tenant's account goes through `tenant_credentials.stage_client`, which assumes the deploy role afresh and never falls back to the ambient client. |
| [frontend-absent-standing-renders-nothing.md](frontend-absent-standing-renders-nothing.md) | A capability the caller does not hold is absent, not disabled or hinted at; `disabled` is reserved for the request in flight, and one sentence states the page's standing where an instruction would otherwise stand. |
| [frontend-auth-config-guard.md](frontend-auth-config-guard.md) | `resolveAuthConfig` refuses an empty, stub or example-GUID Entra tenant or client id at module load regardless of `VITE_AUTH_PROVIDER`, with the same predicate as the deploy preflight, so a misconfigured bundle dies before the first render. |
| [frontend-detail-sentence-classification.md](frontend-detail-sentence-classification.md) | Superseded by `frontend-refusals-classified-by-code.md`: a page classified a refusal by matching the message against the route's fixed detail sentence in a pure, tested module. |
| [frontend-dev-auth-is-a-build-mode.md](frontend-dev-auth-is-a-build-mode.md) | Dev auth is the Vite mode `devauth`, which swaps three auth modules for twins by resolved path; every other build fails if its output carries the arm, the deploy script refuses such a bundle, and the Entra configuration guard is never gated. |
| [frontend-loading-state.md](frontend-loading-state.md) | Loading is derived from whether the settled record matches the current request, through `useLoad`, the one fetch effect; a page owns no `loading` or `error` state and nothing is set synchronously in an effect body. |
| [frontend-refusals-classified-by-code.md](frontend-refusals-classified-by-code.md) | A page classifies a refusal by the `ApiFailure`'s `code` and `status` through a table typed on the generated `ErrorCode`, never by matching the sentence; a renamed code fails `tsc -b` where a table names it, and an unrecognised refusal is shown as the backend sent it. |
| [frontend-silent-sign-in-through-the-redirect-bridge.md](frontend-silent-sign-in-through-the-redirect-bridge.md) | A new tab signs in silently through `ssoSilent` with the remembered username as the hint, over MSAL v5's redirect bridge served at the existing callback path by `main.tsx` before React mounts; the button is the fallback for no session or blocked third-party cookies. |
| [frontend-two-status-machines.md](frontend-two-status-machines.md) | CI/CD status and runtime health are two closed unions narrowed once, on the backend model, a stored value outside the set folding to `unknown` and never to `ready`; the frontend holds no copy of either member list, and every lookup is a `Record` over the generated union with no default branch. |
| [frontend-wire-types-are-generated.md](frontend-wire-types-are-generated.md) | The frontend's wire types are `openapi-typescript`'s output over the reviewed OpenAPI snapshot, committed as `src/api/openapi.ts` and never hand-edited; `client.ts` declares no interface and no literal union, a sync test holds the file to the generator, and `WireModel` makes the document say what the wire does about defaults. |
| [backend-registry-writer-owner.md](backend-registry-writer-owner.md) | `services/registry_writer.py` alone calls the AWS Agent Registry and obtains its client; the platform write re-asserts APPROVED within one deadline, the user's edit stays DRAFT, and the two registry services build envelopes and hold no client. |
| [backend-feature-modules-over-the-service.md](backend-feature-modules-over-the-service.md) | A backend hub splits into a store collaborator, workflow functions over the service, one-line delegates on the service and an error-class leaf; the IAM derivation binds a client through a parameter annotated with the service class. |
| [backend-graph-in-memory-behind-the-adapter.md](backend-graph-in-memory-behind-the-adapter.md) | Under `GRAPH_IN_MEMORY`, read in `graph_service_from_settings` alone and set by the launcher alone, `GraphService` is built over an in-memory directory at its injected-client seam, so the adapter, the identity service and the grant routes run unchanged on a laptop; an unserved Graph path answers a Graph-shaped error naming the double. |
