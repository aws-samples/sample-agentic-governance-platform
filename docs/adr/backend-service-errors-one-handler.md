# Backend service errors leave through one handler

## Status

Accepted 2026-10-23, Epics 49A/50A; amended 2026-10-30 (Epic 52B): the body is declared in the document;
amended 2026-11-02 (Epic 52C): every route refusal is a kind and `code` is required.

## Context

Services raised one exception class per domain carrying a `kind` string, and every route caught it
and chose a status and a detail by hand: 148 `except` blocks, thirteen of them returning `str(err)`,
and a kind no route mapped fell through to the generic 500. Two routes could map one meaning two
ways, and 37 service sites raised FastAPI's `HTTPException` directly, so HTTP vocabulary leaked below
the edge. A broad `except Exception` in a route could swallow a failure silently.

## Decision

- `platform/control_plane/backend/src/core/errors.py` defines `AppError`. A subclass sets `domain` and
  `kinds`, a table from kind to `Outcome(status, detail)`; `detail` is a fixed text, `None` (the message
  is the detail) or a dict template in which `MESSAGE` becomes the message and the exception's `extra`
  is merged. A `None` detail or a `MESSAGE` value sends the service's message to the client verbatim,
  so the message is client copy and never carries an upstream body or a secret. A kind outside the
  table is a `TypeError` at construction, so a wrong raise fails in the test that exercises it.
  `body()` is `{"detail": ..., "code": "<domain>.<kind>"}` and nothing else.
- `platform/control_plane/backend/src/main.py` registers one `app_error_handler` for `AppError`: a 5xx
  is logged through `logger.exception`, a 4xx at warning with the code only (the message carries
  caller-typed text and reaches the body, never the log), and the body goes back with CORS headers
  re-attached. The generic `Exception` handler stays and masks everything else.
- `platform/control_plane/backend/src/models/errors.py` declares that body once for the document as
  `ErrorBody` on every operation for 400, 403, 404, 409, 502, 503 and 504, `code` every table's
  `domain.kind` as one closed enum, and required: every route refusal carries a kind. FastAPI's 422,
  the generic 500 and the auth dependencies' 401 are the bodies without a `code`, and none of them
  sits on a declared status of a matched operation.
- Routes never translate a service error: no `except` block raises `HTTPException` or an `AppError`
  kind for one. A route raises an `AppError` kind for a refusal it decides; only the auth dependencies
  in `core/` raise `HTTPException`, for 401.
- Services never raise or subclass `HTTPException`; an upstream error (Graph, boto, httpx) becomes a
  kind inside the service, where its meaning is known. The FastAPI dependencies in `core/` keep theirs.
- A broad `except Exception` is legitimate in three shapes only: a boundary that logs through
  `logger.exception` or `logger.error(..., exc_info=True)` and continues, a handler that re-raises,
  and the generic handler. Every other broad catch narrows to the exception the called code documents.
- A kind is a meaning, not a message: when one kind served two client texts the kind splits; when two
  routes mapped one meaning two ways, the domain's own route wins.
- An upstream carrier class (`GraphError`, `DatabricksError`) stays a plain exception, not an
  `AppError`: its codes are the provider's open set, read by callers as a diagnostic, and the service
  that called the provider translates it into its own kind where the meaning is known. An internal
  exception no route lets escape stays plain too.
- A class raised at many sites without a kind (`ProvisioningError`) may carry a one-row table naming
  the only wire it reaches; the row splits only when a second wire appears, and the raise sites that
  mean it then name their kind.

`platform/control_plane/backend/tests/test_error_layer.py` holds the rules over every module under
`src`. It walks their AST, and checks that no module but `core/security_entra.py`,
`core/security_github_oidc.py` and `core/rbac.py` imports, raises, subclasses or catches
`HTTPException`, that no route `except` translates, that every `AppError` subclass declares `domain`
and `kinds`, that every `kind=` literal exists in the table of the class its raise site names (one
table per class, by module and qualified name), and that every table row is raised somewhere.

## Consequences

- The client sees the same status and detail as before, plus `code`; a known kind can never 500.
- "What does the client see for kind X" is answered by one table per class, in one place.
- Adding a kind means adding a table row and a raise; forgetting either fails the suite.
- The wire is pinned by the route tests, so a change to a table row changes a test on purpose.
- A new route file or service file is judged by the same rules from its first commit.
