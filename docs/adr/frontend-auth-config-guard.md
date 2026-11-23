# Frontend Entra configuration is refused at module load, unconditionally

## Status

Accepted 2026-10-23 (Epics 49B+50B)

## Context

The sign-in configuration guard used to throw only inside
`if (import.meta.env.VITE_AUTH_PROVIDER === 'entra')`, at the top of
`platform/control_plane/frontend/src/auth/msalConfig.ts`. Three facts made that gate a hole.
`VITE_AUTH_PROVIDER` is typed optional in `platform/control_plane/frontend/src/vite-env.d.ts` and
Entra is the sole provider, so the variable selects nothing. Exactly one build path writes it:
`platform/control_plane/infrastructure/scripts/ci/deploy-frontend.sh` derives it from a Terraform
output and refuses an empty or placeholder value, while a bare `npm run build`, the dev server and any
other path take whatever `.env` holds. And with the key absent the throw was skipped and the module
fell back to `clientId: ''` and the `/common` multi-tenant authority, a working authority pointing at
the wrong directory: sign-in proceeds and every API call answers 401 on issuer, with no diagnostic in
the bundle. The three `.env*.example` files ship all-zero GUIDs, so `cp .env.example .env` and forget
is the common way sign-in dies, with values that look filled in.

`msalConfig.ts` constructs a `PublicClientApplication` and reads `window.location.origin` at module
scope, so the node-only vitest lane cannot import it: a decision that lives there is untestable.

## Decision

`platform/control_plane/frontend/src/auth/authConfigGuard.ts` owns the decision, pure and
framework-free. `resolveAuthConfig` is handed the whole env record and reads exactly the two keys of
`AUTH_ENV_KEYS` (`VITE_ENTRA_TENANT_ID`, `VITE_ENTRA_SPA_CLIENT_ID`), never `VITE_AUTH_PROVIDER`; it
throws when either is unusable, collecting both so an operator filling a fresh `.env` makes one round
trip, and the message names the keys, the `.env` file and `docs/entra-setup.md`, never the provider
variable. `isUnusableAuthValue` is the twin of `unusable()` in `deploy-frontend.sh` plus a trim,
because `.env` is hand-edited: empty once trimmed (the shell twin refuses the empty string only), an
angle bracket anywhere (an unfilled `<api-id>` stub), or the `00000000-0000-0000-0000-` prefix of the
example GUIDs, so a value the deploy preflight refuses is a value the bundle refuses too. The top level of
`msalConfig.ts` is the only caller, and `platform/control_plane/frontend/src/main.tsx` reaches it
through `App`'s provider tree, so a misconfigured bundle dies before the first render instead of after
a sign-in that cannot work. Both fallbacks went with the gate. `VITE_ENTRA_SPA_REDIRECT_URI` and
`VITE_ENTRA_SPA_SCOPE` keep their working defaults: neither can authenticate against the wrong directory.

Two non-goals, recorded as decisions. No GUID shape validation: the deploy twin does none, and a
well-formed GUID naming the wrong directory fails loudly at Entra. A tenant id of `common`,
`organizations` or `consumers` is not refused: no example file carries one, no script writes one, and
the backend's issuer check refuses a foreign directory's token regardless.

`platform/control_plane/frontend/src/auth/authConfigGuard.test.ts` holds the contract under
`isUnusableAuthValue — the twin of unusable() in scripts/ci/deploy-frontend.sh`, `resolveAuthConfig —
throws regardless of VITE_AUTH_PROVIDER` (every unusable value against every provider state: absent,
empty, `entra`, anything else), `resolveAuthConfig — the message names what to fix` and
`resolveAuthConfig — the deleted fallbacks are unreachable`.

## Consequences

- `npm run dev` with no `.env` dies at module load with the message instead of rendering a blank page
  after a sign-in that cannot complete; a configured tree is untouched (Vite loads `.env` in dev too).
- The predicate changes in both places or in neither: narrowing it here alone (refusing a GUID that
  merely opens with a zero group) would refuse a deployment the preflight accepted.
- A new required Entra key is a change to `AUTH_ENV_KEYS`, to the test's `GOOD` fixture and to the
  deploy script's `OUTPUT_TO_VITE` table in the same commit.
