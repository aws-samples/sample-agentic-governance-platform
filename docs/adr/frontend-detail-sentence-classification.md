# Frontend classifies a refusal by the backend's detail sentence

## Status

Accepted 2026-10-23 (Epics 49B+50B)

Superseded 2026-10-30 by `frontend-refusals-classified-by-code.md`: a refusal is classified by its `code` and `status`, never by its sentence.

## Context

The response interceptor in `platform/control_plane/frontend/src/api/client.ts` rejects every
non-401 failure as a bare `Error` whose message is `formatApiErrorDetail(detail, error.message)`:
the body's `detail` when the backend sent one, else axios's own message. The HTTP status, the headers
and the raw body are gone by the time a page holds the rejection. A page that must tell "you need
Owner" from "nothing to promote" from "the provider is down" has one input, the sentence the route
wrote into `detail`.

## Decision

A page classifies a refusal by matching the message against the route's fixed detail sentence, in a
pure module its test pins wherever the page has one, and renders its own copy for each recognised
kind; an unrecognised message is shown as the backend sent it, or as the page's fallback when it is
empty. The classifiers, under `platform/control_plane/frontend/src/components/`:

- `operations/githubLinkModel.ts`: `classifyLinkError`, a `Map` from the link route's eight literals
  to a `LinkErrorKind`; `operations/reconcileView.ts`: `classifyRolloutError`, a `Map` from the
  rollout route's five literals to a `RolloutErrorKind`.
- `operations/projectRoles.ts`: `roleActionMessage`, `destructiveActionMessage`,
  `maintainerActionMessage` and `promotionActionMessage`: `/insufficient project role/i`,
  `/needs a passing evaluation/i` and `/promotion is already in flight/i`, case-insensitive regexes.
- `operations/repositoryDetailTabs.ts`: `buildNowActionMessage`, the same shape for the build route;
  `operations/repo-tabs/deploymentsTabModel.ts`: `rollbackActionMessage`, the same for the rollback
  route, with `/no such succeeded deployment to roll back to/i`.
- `operations/repo-tabs/pullRequestsTabModel.ts`: `pullRequestActionMessage` for the pull-request
  routes, and `isCapabilityRefusal` for an org that never granted the app pull-request access.
- `operations/repo-tabs/resourcesTabModel.ts`: `inventoryState` reads the role refusal as `forbidden`
  and every other failed read as `unread`.
- `operations/promoteGate.ts`: `isEvalGateRefusal`, the eval gate's 409 sentence.
- `operations/ProjectAccessTab.tsx`: the roster read rewrites the role refusal into the one sentence a
  reader without a role sees, before the hook holds the error.
- `governance/AgentDetail.tsx`, `governance/McpServerDetail.tsx`, `operations/RepositoryDetail.tsx`:
  a load error matching `/not found/i` or `/404/` renders the not-found page, never the error card.
- `governance/InvokePanel.tsx`: `/not assigned to this agent/i` recognises the Entra 403 on invoke;
  the `status === 403` arm beside it never fires for an error that passed the interceptor.

A `Map` keyed on the exact sentence is used where the route pins its literals (a prototype key such
as `constructor` must classify nothing); a regex is used where the sentence carries a variable part.
The `Map` tables pin the link and rollout routes' literals once each; the regex classifiers share the
project routes' sentences, so one route's literal may be matched by every page that renders its
refusal, and a reworded sentence degrades each of them at once.

## Consequences

- A backend route that rewords a detail sentence degrades the page silently to its fallback copy;
  nothing in the frontend suite can hold the backend's half, so the test that pins each literal is
  the backend's own.
- The typed error shape, a rejection carrying the status and the detail so a page can switch on a
  code, is the generated API client's, planned after this epic; when it lands, these tables become
  per-code copy and the sentence matching goes.
- A new refusal a page must tell apart is one more row in the table and one more test, never a
  second parse of the message inside a `.tsx`.
