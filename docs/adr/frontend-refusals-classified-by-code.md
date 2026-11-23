# Frontend classifies a refusal by its code

## Status

Accepted 2026-10-30, Epic 52B; supersedes `frontend-detail-sentence-classification.md`; amended
2026-11-03 (Epic 52C): no classifier keys on `status` alone, and `api/failure.ts` is the failure's home.

## Context

The response interceptor in `platform/control_plane/frontend/src/api/client.ts` rejected every non-401
failure as a bare `Error` carrying the formatted detail, so a page that had to tell "you need Owner"
from "nothing to promote" matched the sentence the route wrote: three exact-match maps, thirteen regex
functions, two equality checks and ten inline checks in pages. A reworded detail degraded a page
silently to its fallback copy and nothing on the frontend could hold the backend's half; the two
registration wizards over-matched `already|exist|taken|409` and sent a user back to the name step on an
unrelated 409 or a registry 503; the invoke panel's `status === 403` arm could never fire. Every service
error now leaves the backend through one handler as `{"detail", "code": "<domain>.<kind>"}`
(`backend-service-errors-one-handler.md`), and the generated
`platform/control_plane/frontend/src/api/openapi.ts` carries the closed set of codes as `ErrorBody['code']`.

## Decision

`platform/control_plane/frontend/src/api/failure.ts` is the failure's home: `ErrorCode`
(`NonNullable<ErrorBody['code']>`), `ErrorDetail`, `ApiFailure extends Error` with `status`, `code`
(`ErrorCode | null`), `detail` and the formatted `message`, `isApiFailure`, `formatApiErrorDetail` and
`errorBodyOf`, which reads an unknown body without trusting its shape. The interceptor rejects every
failure as an `ApiFailure`, the 401 arm one with the reload or halt copy and no code; `failureOf` and
`useLoad` carry it beside the message.

A refusal kind is classified in a pure `.ts` module beside its page and its test. The module exports one
table, `<KIND>_CODES = { <kind>: ['domain.kind', …] } as const satisfies Record<Kind, readonly ErrorCode[]>`,
and one `classify<X>(failure: ApiFailure): Kind | null` that hands it to `refusalKind` in
`api/refusalKind.ts`, which answers `null` for a null code; a kind keys several codes where one meaning
has several producers. Because the table is typed on the generated union, a code the backend renames or
removes fails `tsc -b` on the line that names it, where a sentence or a `Map<string, …>` would compile
and degrade silently. No classifier keys on `status` alone; the 401 arm and `api/studio.ts`'s 5xx copy
do. The message is copy: a page shows `failure.message` for an unrecognised refusal and never parses it.

The classifiers, under `platform/control_plane/frontend/src/components/`:

- `operations/githubLinkModel.ts`, `operations/reconcileView.ts`, `operations/templatesView.ts`,
  `operations/projectRoles.ts`, `operations/repositoryDetailTabs.ts`, `operations/promoteGate.ts`,
  the `operations/repo-tabs/*Model.ts`, `operations/repo-tabs/evaluationsLogic.ts` and
  `operations/repo-tabs/tracesToDataset.ts`.
- `governance/agent-tabs/containmentLogic.ts`: `classifyInvokeRefusal`, `invoke.contained` as
  contained, `obo.not_assigned` or `eval.not_assigned` as unassigned; a codeless 403 is neither.
- `governance/agentRegistrationWizardModel.ts` and `governance/mcpServerRegistrationForm.ts`:
  `agent.name_taken` and `mcp_server.name_taken`, the one refusal that returns a wizard to its name step.
- `governance/publishForm.ts`: `isMissingPublishRequest`, `marketplace.not_found` is "no request".
- `governance/AgentDetail.tsx` and `governance/McpServerDetail.tsx` render the not-found page when the
  load's `failure?.code` is `agent.not_found` or `mcp_server.not_found`, an inline check with no table.

## Consequences

- A backend rewording changes what a user reads and a backend route test, never a page's branch.
- A renamed or removed kind is a snapshot diff, a regenerated `openapi.ts` and a `tsc -b` failure that
  names the table; a new kind is shown as the backend sent it until a table names it.
- Each route refusal carries `code`; a codeless 422 or 500 shows as sent; a 401 shows the interceptor's copy.
- A new refusal a page must tell apart is one more table row and one more test, never a second parse.
