# Frontend loading state is derived, never set

## Status

Accepted 2026-10-23 (Epics 49B+50B)

## Context

Pages read from the API inside a `useEffect` that opened with `setLoading(true)` and set the result
inside the promise callbacks. `react-hooks/set-state-in-effect` flags those synchronous writes, and the
shape was repeated with small variations: some pages kept the previous rows while refetching, others
blanked them. Optimistic edits after a write sat beside the fetch state as a second writer of the rows.

## Decision

Loading is derived from whether the settled record matches the current request. The pure model is
`platform/control_plane/frontend/src/api/loadState.ts`, pinned by its `loadState.test.ts`: a `Settled<T>`
record carries the `deps` and `nonce` of the request it answered, its `Outcome<T>` and `lastData`.
`resolveLoadState` answers `loading` when nothing has settled or when the settled record belongs to
other deps or an older nonce (`sameDeps` is `Object.is` per slot), otherwise the outcome. `data`
travels on the `loading` and `error` states, so a page that painted rows while refetching reads
`state.data`, and a page that blanked them keys its body on `state.kind === 'loading'`; the render
decides, not the hook. `patchSettled` rewrites a settled ready record whose deps and nonce still
match, which is how a page keeps an optimistic edit after a write; a newer nonce wins over a patch.

`platform/control_plane/frontend/src/api/useLoad.ts` is the one effect: two `useState`s (the settled
record, the nonce), one `useEffect` keyed on the caller's deps and the nonce, whose body calls
`load()` and sets the record only inside `.then` and `.catch`, with a cancelled flag in the cleanup.
`reload` bumps the nonce; `patch` applies `patchSettled` while ready, its other two branches being
under Consequences. Nothing is set synchronously in the effect body, so the lint stays on as the
guard. The hook forwards a deps array `exhaustive-deps` cannot see from inside, so that one line
carries a reasoned suppression; `platform/control_plane/frontend/eslint.config.js` lists `useLoad`
under `additionalHooks`, so the rule checks each caller's deps against its `load` closure, as an error.

The sites outside the hook by decision, all under `platform/control_plane/frontend/src/components/`:
`operations/GitHubLinkCallback.tsx` and `operations/ConnectionCallback.tsx` each consume a single-use
value from storage, and `main.tsx` renders under `StrictMode`, which runs mount effects twice: a
cancelled first run would already have consumed the value, so a model that re-runs on a nonce cannot
hold it; each keeps its own effect with a run-once ref and sets state only inside the promise callbacks.
The debounced directory search (`governance/admin/AddUserModal.tsx`, `governance/admin/TenantModal.tsx`,
`governance/PrincipalPicker.tsx`, `governance/CedarPoliciesTab.tsx`, `operations/ProjectAccessTab.tsx`)
is a timer effect whose results land in the timeout and promise callbacks. The documentation deep link
(`Documentation.tsx`) is a derivation, `manual ?? section` under the previous-value pattern, not a
fetch. `operations/Playground.tsx`'s streaming phase is timers revealing a fixed text, with one effect
that clears them. Polling intervals re-reading a record until it settles are not fetches of this shape.

## Consequences

- A page fetches with `const { state, reload, patch } = useLoad(() => api.list(args), [args])` and
  renders on `state.kind`; it owns no `loading` or `error` state of its own.
- The error text a page shows is `state.message`; the hook's default is `Request failed`, and a page
  with its own copy passes it as the third argument.
- A new request after an error (Retry, or a filter change) shows the loading view instead of the
  stale error card, because `LoadState` carries `data` and not a previous error; copy, requests and
  timing are unchanged. A `patch` while ready applies the edit; while loading it refetches once after
  the load settles; after an error it refetches, so a write after a failed read converges on the
  server; a poller patches only while ready.
- The sites above are the decided exceptions; any other fetch effect still outside the hook is
  unfinished migration, not a further decision; a new exception is a decision to record here.
