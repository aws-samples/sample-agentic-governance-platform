# Frontend dev auth is a build mode, never a runtime switch

## Status

Accepted 2026-10-27 (Epic 53A)

## Context

The backend's dev-auth arm (`USE_DEV_AUTH=true`, `platform/control_plane/backend/src/core/rbac.py`) maps the
`x-user-email` header to a role on loopback, and the live-test harness sends that header on every API call. The SPA
had no counterpart: its one sign-in path was Entra through MSAL, so a local session could prove the API but met the
sign-in screen in the browser. What the frontend carried instead was a dead hook: `platform/control_plane/frontend/
src/api/client.ts` read `dev_user_email` from `localStorage` into the same header and `UserContext.switchUser` wrote
it, dev-auth-flavoured code shipped in every production bundle with no caller. A runtime switch (an environment
variable or a storage key read by the Entra arm) is the shape that leaks: both arms live in one bundle and a flag
decides which runs, so a deployed stack is one setting away from the bypass and the Entra configuration guard
(`platform/control_plane/frontend/src/auth/authConfigGuard.ts`) would have to be gated behind the same flag.

## Decision

Dev auth is the Vite mode `devauth`: `vite --mode devauth`, the dev server `agp up` runs (`npm run dev:devauth` for a
human). Three modules under `platform/control_plane/frontend/src/auth/`, `AuthContext.tsx`, `tokenBroker.ts` and
`authBoot.ts`, have a twin of the same file name under `platform/control_plane/frontend/src/auth/devauth/` exporting
the same names with the same types. The plugin `agp-auth-arm` in `platform/control_plane/frontend/vite.config.ts`
swaps them by resolved path: in the `devauth` mode its `resolveId` resolves every import and, when the resolved file
is a key of `swapTable` in `platform/control_plane/frontend/src/auth/devauth/arm.ts`, answers with the twin, so the
static imports in `main.tsx` and `App.tsx` and the dynamic import in `client.ts` all land on one table. The twins sign
the SPA in as `dev@example.com` with `token: null` and `arm: 'devauth'`; the devauth `bootAuth` installs one request
interceptor adding the `x-user-email` header before React mounts, so the first request already carries it and
`/users/me`, the sidebar and `created_by` agree on one identity. Everything else is shared and unchanged, and nothing
in the Entra arm imports `arm.ts`.

Three locks hold "never ships". Under `vite build` in every mode but `devauth` the same plugin refuses, in its `load`
hook, any module under `platform/control_plane/frontend/src/auth/devauth/` that enters the graph, naming the module
and the fix; it then scans each emitted chunk in `generateBundle` for the whole word `devauth`, the marker, which
`arm.ts` and the twins carry and the Entra arm never does (the shell's attribute value comes from `AuthState.arm` at
run time), and fails the build naming the chunk and the same fix, the second net for arm code that reached a chunk by
a path the first did not see. Both checks run only under `vite build`, so the test runner and the dev server load the
arm's files freely; `npm run build` is unchanged, so the quality job and the deploy script run the lock without
knowing it exists. `platform/control_plane/infrastructure/scripts/ci/deploy-frontend.sh` refuses a `dist` carrying
the marker before its first `aws` call (`platform/control_plane/backend/tests/test_ci_scripts.py`). And
`platform/control_plane/frontend/src/auth/devauth/arm.test.ts` pins the table, the path predicate and the scan under
`swapTable is empty for production, development, test and any other mode`, `swapTable names exactly the three twins
for devauth`, `isArmModule flags every path under src/auth/devauth and passes the Entra arm`, `leaks flags the whole
word devauth and passes data-auth-arm` and `the marker is the mode name`. The dead hook was deleted first, so the
production bundle carries no dev-auth code and the locks' negative proof is honest.

Rejected: the mode name `local`, because Vite would read the operator's git-ignored `.env.local` as that mode's file;
a `.env.devauth` file, because the arm needs nothing beyond `VITE_API_URL` and `VITE_BUILD_SHA`, which the launcher
already exports; a second HTML entry for the dev arm, which duplicates `App.tsx` and still needs mode gating; a mocked
MSAL, which keeps the guard in the graph and feeds it a real-looking placeholder; and any runtime switch, as above.

## Consequences

- `vite --mode devauth` is the only way to get the dev arm; no environment variable, storage key or URL flips it,
  and the Entra configuration guard stays ungated.
- A new import path to one of the three modules needs nothing: the swap is by resolved file, not by import string.
  A fourth module to swap is one entry in `SWAPPED_MODULES` and a twin of the same name.
- The whole word `devauth` is reserved to `arm.ts` and the twins; Entra-arm code that needs the arm reads
  `AuthState.arm`. A build that fails with `the dev-auth arm leaked into a <mode> build` names the module that
  entered the graph (the path lock) or the chunk that carries the marker (the scan); the fix is the import, never
  the lock.
- `GitHubLinkCallback.tsx`, the one other reader of `AuthState.token`, settles on its error branch under `devauth`;
  GitHub linking is not a local feature.
