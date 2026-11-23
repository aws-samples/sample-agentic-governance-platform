# The cross-stage deployment history read stops at a page ceiling

## Status

Accepted 2026-10-24, Epics 49A/50A. The ceiling itself predates this record; the record names the
trade it makes.

## Context

The deployment partition's sort key is `{repo_id}#{stage}#{started_at}#{suffix}`, so within one
stage DynamoDB order is time order, but across stages it is stage-major. A cross-stage read with a
server-side `Limit` would truncate to the alphabetically first stage and could drop the newest
deployment. `ProjectStore.list_deployments(stage=None)` in
`platform/control_plane/backend/src/services/project_store.py` therefore pages the `repo_id`
prefix itself, keeps at most `limit` rows per stage, merges by `started_at` and applies `limit`
after the merge (`all_stage_deployments`, `_bound_per_stage`). The sort-key format is a contract
with the buildspec that writes the rows, so it is not changed.

## Decision

- The cross-stage read is bounded at `_MAX_DEPLOYMENT_ROWS` rows (sixty thousand, the magnitude of
  twenty one-megabyte pages at the row size) and logs a warning when the ceiling is hit.
- The bound trades correctness for liveness: paging is stage-major, so one early-sorting stage can
  exhaust the budget and the result may omit the true newest deployment. That warning is the only
  signal that the answer is wrong rather than short.
- The ceiling is kept because the alternative is a request that never returns, and at roughly one
  megabyte per page it takes tens of thousands of rows to reach.

## Consequences

- `platform/control_plane/backend/tests/test_deployment_store.py::test_the_cross_stage_read_stops_at_the_row_cap_instead_of_paging_forever`
  pins the stop. `_list_runtime_state_keys` in the same service carries the sibling
  `_MAX_STATE_LIST_PAGES` bound on the delete path for the same reason.
- If the warning is ever seen in practice, the fix is a time-major secondary index or a time-major
  sort key, not a bigger ceiling.
- A frontend reader of the history (`platform/control_plane/frontend/src/api/client.ts`) should
  treat any "current" artifact derived from a ceiling-hit read as unverified.
