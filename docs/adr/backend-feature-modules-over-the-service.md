# Backend hubs split into feature modules over the service

## Status

Accepted 2026-11-03, Epic 52C.

## Context

Five service files had grown past a thousand lines and `api/routes/projects.py` had 859 lines; the
project service was one class holding persistence, wiring, queries and four workflows. Three shapes were
measured for the split. Mixins fail pyright basic on every cross-file `self.` attribute and teach the
next agent to reach into undeclared state. Collaborator classes with ten dependencies each break the
tests that reassign `svc._registry` or `svc._stage_client` after construction, and cannot take the late
`attach_runtime_build_service`. Module functions over the service keep both, with a precedent in
`platform_role_holders(graph: GraphService, ...)`. An error class left in the hub becomes an import
cycle once the workflow modules raise it, and the IAM derivation bound a client only inside the class
that assigned it, so a call through `svc._s3` in another module lost its binding.

## Decision

- State and persistence live in a store collaborator. `ProjectStore` in
  `platform/control_plane/backend/src/services/project_store.py` holds the partition keys, the row
  codecs and the deployment history over the service's `StateStore`, with no reference back to the
  service; `services/marketplace_store.py` and `services/databricks_agent_secrets.py` take the shape.
- A workflow is a module function over the service, `def delete_repo(svc: ProjectService, *, ...)` in
  `services/project_teardown.py`, beside `project_materialize.py`, `project_delivery.py`,
  `project_runtime_teardown.py`, `graph_app_registration.py`, `graph_platform_users.py`,
  `databricks_runtime_status.py` and `marketplace_grants.py`. The service is imported under
  `TYPE_CHECKING` only and its collaborators are read at call time, so a test's instance patch holds.
- The service class wires, queries and delegates. A method that was public on the service stays on it
  as a one-line delegate, `return project_delivery.build_repo(self, ...)`, so routes, other services
  and tests call the names they called before.
- The error class is a leaf. `services/project_errors.py`, `graph_errors.py`, `github_repo_errors.py`
  and `marketplace_errors.py` import nothing of their family; the hub, its modules and every other
  importer take the class from the leaf.
- Routes split the same way: `api/routes/project_repos.py` and `api/routes/repositories.py` resolve
  `get_project_service()` through `api/routes/projects.py` at call time; the OpenAPI snapshot is unmoved.
- The IAM derivation follows the parameter. `derive_actions` in
  `platform/control_plane/backend/tests/test_iam_task_role_covers_boto3_calls.py` resolves
  `<param>.<attr>` through the `self.<attr>` bindings of the class a parameter is annotated with (a
  class under `src`, by bare name, under `TYPE_CHECKING` or as a string), in any module. The idiom row
  `annotated_service_parameter` pins the shape and `test_the_derived_action_set_is_the_anchors` holds
  the derived set across a split.
- A split re-points its readers in the same change: a test that reads a hub's source by path globs the
  feature modules, and a patch names the module that now defines the target, so a stale one fails.

`scripts/lint/file-length.sh` keeps a module from regrowing past the line, and ruff `C901` keeps a
function from regrowing its branches.

## Consequences

- A hub that crosses the line is cut into these four shapes: a store, functions over the service,
  delegates and an error leaf.
- The service class stays wide but thin; a workflow's body is one hop away, where its delegate points.
- A feature module reads the service's private attributes; the coupling is to one class, typed by the
  annotation and checked by pyright.
- An import of an error class through its hub, which binds the name for its own raises, fails ruff
  `TID251` in `scripts/lint/ruff.sh`: the backend's `pyproject.toml` bans each old path, naming the leaf.
- The two registry services remain twins; a shared base is a design call, not a size fix.
